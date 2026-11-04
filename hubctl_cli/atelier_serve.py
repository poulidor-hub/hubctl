# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""`hubctl atelier serve` — mini-serveur HTTP hôte pour piloter les ateliers.

Brique hôte du feature « ateliers pilotables par l'agent » : le Hub proxy
les routes /atelier/list, /atelier/close-all et /vault/* (shapes JSON =
contrat).

Contraintes (spec §4) :
- stdlib uniquement : ThreadingHTTPServer, aucune dépendance nouvelle ;
- bind 127.0.0.1 par défaut (--host pour l'ouvrir au strict nécessaire :
  p. ex. 172.17.0.1, passerelle docker0, pour la joignabilité des
  conteneurs via host.docker.internal — l'auth token reste le garde-fou) ;
- routes fixes sans paramètre libre (5) ;
- auth par header ``X-Atelier-Token`` comparé en constant-time
  (hmac.compare_digest) contre le contenu du fichier --token-file
  (défaut ~/.config/hubctl/atelier_token).

Routes :
- GET  /health            → 200 {"status": "ok"} — sans auth (liveness, ne divulgue rien)
- GET  /atelier/list      → 200 {"ateliers": [{app, ws_slug, api_port,
                            frontend_port, containers_state}]}
- POST /atelier/close-all → 200 (rc 0) / 207 (échec partiel) — voir _close_all
- GET  /vault/status      → 200 {"status": "ok"|"locked", "locked", "last_backup",
                            "vault_size", "entries" {env, files, apps, db_dump},
                            "divergences", "pra"} — voir _vault_state
- POST /vault/backup      → 200 {"status": "ok"|"locked", …} / 409 (déjà en cours)
                            / 500 (> 110 s) — voir _run_backup

Le mapping {closed, failed} du contrat est reconstruit par diff des
.workshop.env avant/après action_atelier_close_all (qui ne renvoie qu'un
rc + console riche, ruling R1) ; la réponse embarque aussi `rc` et
`output` (console capturée) pour traçabilité côté Hub.
"""
from __future__ import annotations

import hmac
import io
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from rich.console import Console

from hubctl_cli import actions, hub_state

DEFAULT_TOKEN_FILE = Path.home() / ".config" / "hubctl" / "atelier_token"

_ROUTES_DOC = ("GET /health · GET /atelier/list · POST /atelier/close-all · "
               "GET /vault/status · POST /vault/backup")

# close-all = `docker compose down -v` successifs : deux exécutions en
# parallèle sur le même atelier seraient imprévisibles (retry du Hub pendant
# qu'une fermeture tourne). ThreadingHTTPServer = 1 thread/requête → sérialiser.
_close_all_lock = threading.Lock()

# Backup vault = lecture disque + dump DB + réplication PRA : potentiellement
# long (dizaines de secondes). Un seul à la fois (409 sinon) ; le job tourne
# dans un worker unique et l'appelant attend au plus _VAULT_BACKUP_TIMEOUT —
# au-delà : 500, mais le verrou reste tenu par le worker jusqu'au bout.
_vault_backup_lock = threading.Lock()
_vault_backup_pool = ThreadPoolExecutor(max_workers=1)
_VAULT_BACKUP_TIMEOUT = 110.0


# ── Collecte des ateliers ──

def _int_or_none(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_docker_ps(out: str) -> dict[str, str]:
    """Parse la sortie de `docker ps --format '{{.Names}}\\t{{.Status}}'`."""
    states: dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        name, sep, status = line.partition("\t")
        if not sep:
            continue
        states[name.strip()] = status.strip()
    return states


def _ws_containers_state() -> dict[str, str] | None:
    """Conteneurs docker des ateliers : {nom: statut}, None si docker injoignable.

    Un seul appel `docker ps --filter name=-ws` pour toute la liste
    (les projets atelier se nomment {slug}-ws, les conteneurs en héritent).
    """
    try:
        r = subprocess.run(
            ["docker", "ps", "--filter", "name=-ws",
             "--format", "{{.Names}}\t{{.Status}}"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return _parse_docker_ps(r.stdout)


def collect_ateliers() -> list[dict]:
    """Liste les ateliers actifs (présence de APPS/X/.workshop.env).

    Même source de vérité que actions._ws_env_path (hub_state.APPS_DIR).
    Chaque entrée : {app, ws_slug, api_port, frontend_port, containers_state}
    — api_port/frontend_port en int (None si absent/non numérique),
    containers_state = {nom conteneur: statut} pour les conteneurs du
    projet -ws, {} si docker est indisponible.
    """
    all_states = _ws_containers_state() or {}
    ateliers: list[dict] = []
    for env_path in sorted(hub_state.APPS_DIR.glob("*/.workshop.env")):
        app = env_path.parent.name
        if app.startswith("."):
            continue
        ws_slug = actions._ws_project(app)
        ws = actions._ws_read_env(app)
        ateliers.append({
            "app": app,
            "ws_slug": ws_slug,
            "api_port": _int_or_none(ws.get("API_PORT")),
            "frontend_port": _int_or_none(ws.get("FRONTEND_PORT")),
            # Préfixe strict (pas de sous-chaîne) : sinon le slug « exemple »
            # s'attribuerait les conteneurs de « app-exemple-ws-api-1 ».
            "containers_state": {n: s for n, s in all_states.items()
                                 if n == ws_slug or n.startswith(ws_slug + "-")},
        })
    return ateliers


# ── Coffre (vault) ──

def _pra_info() -> dict:
    """{"configured": bool, "location": str|None} — jamais fatal (try/except large)."""
    from hubctl_cli import pra_config  # lazy : voir actions._get_vault
    try:
        cfg = pra_config.load_config()
    except Exception:
        return {"configured": False, "location": None}
    if not cfg:
        return {"configured": False, "location": None}
    # location = champ "path" de pra_location.json (dict) ; getattr si objet.
    loc = cfg.get("path") if isinstance(cfg, dict) else getattr(cfg, "path", None)
    return {"configured": True, "location": str(loc) if loc is not None else None}


def _vault_state() -> dict:
    """État du coffre pour GET /vault/status (contrat consommé par le Hub).

    Verrouillé (clé locale absente/invalide, coffre absent) → {"status":
    "locked", "locked": True, "last_backup", "vault_size", "pra"} — sans
    entries/divergences (non calculables sans déverrouillage).
    Déverrouillé → contrat complet : entries {env, files, apps, db_dump},
    divergences (targets triés), pra, métadonnées du fichier coffre
    (last_backup = mtime en ISO UTC, vault_size en octets).
    """
    from hubctl_cli.coffre import Vault  # lazy : voir actions._get_vault
    from hubctl_cli.vault_manager import VaultManager

    path = hub_state.vault_path()
    vault = Vault(path)
    vault.unlock_stored()
    st = path.stat() if path.exists() else None
    meta = {
        "last_backup": (datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
                        if st else None),
        "vault_size": st.st_size if st else 0,
    }
    if vault.is_locked:
        return {"status": "locked", "locked": True, **meta, "pra": _pra_info()}
    mgr = VaultManager(vault)
    files = vault.list_files()
    # __apps__ = liste de métadonnées posée par sync() — get_entry ferait
    # dict() d'une liste ; lecture directe, comme vault_manager.
    apps = vault._data.get("__apps__") or []
    return {
        "status": "ok",
        "locked": False,
        **meta,
        "entries": {
            "env": sum(1 for k in vault.list_entries() if not k.startswith("__")),
            "files": len(files),
            "apps": len(apps),
            "db_dump": "HUB_base/backup/hub_dump.sql" in files,
        },
        "divergences": sorted(mgr.check_divergence().keys()),
        "pra": _pra_info(),
    }


def _backup_job() -> tuple[int, dict]:
    """Le backup lui-même (thread worker) : détient _vault_backup_lock."""
    from hubctl_cli.coffre import Vault  # lazy : voir actions._get_vault
    from hubctl_cli.vault_manager import VaultManager

    if not _vault_backup_lock.acquire(blocking=False):
        return 409, {"erreur": "backup déjà en cours"}
    try:
        vault = Vault(hub_state.vault_path())
        if not vault.unlock_stored() or vault.is_locked:
            return 200, {"status": "locked"}
        result = VaultManager(vault).backup_all()
        return 200, {"status": "ok", **result}
    finally:
        _vault_backup_lock.release()


def _run_backup() -> tuple[int, dict]:
    """POST /vault/backup → (rc, payload) (contrat consommé par le Hub).

    Fast-path : verrou tenu (backup en cours) → 409 immédiat, sans attendre.
    Sinon le backup tourne dans le worker unique (_vault_backup_pool), qui
    acquiert et détient le verrou ; l'appelant attend au plus
    _VAULT_BACKUP_TIMEOUT (110 s) : au-delà → 500, le verrou restant tenu
    par le worker jusqu'à la fin du job (les requêtes suivantes continuent
    de prendre 409). La sonde non-bloquante initiale est relâchée avant le
    submit : c'est le job qui détient le verrou pendant toute la durée.
    """
    if not _vault_backup_lock.acquire(blocking=False):
        return 409, {"erreur": "backup déjà en cours"}
    _vault_backup_lock.release()  # simple sonde : le job re-prend le verrou
    future = _vault_backup_pool.submit(_backup_job)
    try:
        return future.result(timeout=_VAULT_BACKUP_TIMEOUT)
    except FutureTimeoutError:
        return 500, {"erreur": f"backup > {int(_VAULT_BACKUP_TIMEOUT)} s "
                               "(toujours en cours côté hôte)"}


# ── Serveur HTTP ──

def _read_token(token_file: Path) -> bytes:
    """Lit le token d'auth (sans la nouvelle ligne finale). Refuse un fichier vide."""
    raw = Path(token_file).expanduser().read_text(encoding="utf-8")
    token = raw.strip()
    if not token:
        raise ValueError("fichier token vide")
    return token.encode("utf-8")


def build_server(port: int, token_file: Path,
                 host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Construit le serveur (bind host:port, token lu au démarrage).

    host : défaut 127.0.0.1 (comportement historique inchangé). 172.17.0.1
    (passerelle docker0) rend le serveur joignable des conteneurs via
    host.docker.internal sans être visible du LAN.

    port=0 → port libre choisi par l'OS (utile aux tests) : le port réel
    se lit sur server_address après construction.
    """
    token = _read_token(token_file)
    server = ThreadingHTTPServer((host, port), AtelierHandler)
    server.atelier_token = token
    return server


class AtelierHandler(BaseHTTPRequestHandler):
    """Handlers JSON des 5 routes. Auth X-Atelier-Token (sauf /health)."""

    server_version = "hubctl-atelier/0.1"

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        """401 si header absent, 403 si token invalide (comparaison constant-time)."""
        provided = self.headers.get("X-Atelier-Token")
        if not provided:
            self._send_json(401, {"erreur": "header X-Atelier-Token manquant"})
            return False
        expected = getattr(self.server, "atelier_token", b"")
        if not hmac.compare_digest(provided.encode("utf-8", "replace"), expected):
            self._send_json(403, {"erreur": "token invalide"})
            return False
        return True

    def _close_all(self) -> None:
        """Ferme tous les ateliers ; renvoie le contrat {closed, failed} + rc/output.

        {closed, failed} = diff des .workshop.env avant/après l'action
        (action_atelier_close_all ne renvoie qu'un rc — ruling R1). 200 si
        rc 0, 207 sinon (échec partiel).
        """
        before = {a["app"] for a in collect_ateliers()}
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=False, color_system=None,
                          width=100)
        with _close_all_lock:
            rc = actions.action_atelier_close_all(console, oui=True)
        after = {a["app"] for a in collect_ateliers()}
        payload = {
            "closed": sorted(before - after),
            "failed": [{"slug": slug,
                        "erreur": "atelier toujours présent après close-all"}
                       for slug in sorted(before & after)],
            "rc": rc,
            "output": buf.getvalue(),
        }
        self._send_json(200 if rc == 0 else 207, payload)

    def _route(self) -> str:
        return urlsplit(self.path).path

    def do_GET(self) -> None:  # noqa: N802 (API http.server)
        try:
            route = self._route()
            if route == "/health":
                # Liveness publique : ne divulgue rien, évite d'imposer le
                # token aux healthchecks/monitoring.
                return self._send_json(200, {"status": "ok"})
            if not self._authorized():
                return
            if route == "/atelier/list":
                return self._send_json(200, {"ateliers": collect_ateliers()})
            if route == "/atelier/close-all":
                return self._send_json(405, {"erreur": "méthode interdite : POST requis"})
            if route == "/vault/status":
                return self._send_json(200, _vault_state())
            if route == "/vault/backup":
                return self._send_json(405, {"erreur": "méthode interdite : POST requis"})
            return self._send_json(404, {"erreur": "route inconnue"})
        except Exception as e:  # réponse JSON propre plutôt que connexion coupée
            return self._send_json(500, {"erreur": str(e)})

    def do_POST(self) -> None:  # noqa: N802 (API http.server)
        try:
            route = self._route()
            if route == "/health":
                return self._send_json(405, {"erreur": "méthode interdite : GET requis"})
            if not self._authorized():
                return
            if route == "/atelier/close-all":
                return self._close_all()
            if route == "/atelier/list":
                return self._send_json(405, {"erreur": "méthode interdite : GET requis"})
            if route == "/vault/backup":
                rc, payload = _run_backup()
                return self._send_json(rc, payload)
            if route == "/vault/status":
                return self._send_json(405, {"erreur": "méthode interdite : GET requis"})
            return self._send_json(404, {"erreur": "route inconnue"})
        except Exception as e:
            return self._send_json(500, {"erreur": str(e)})


def serve(port: int = 8777, token_file: Path | None = None,
          host: str = "127.0.0.1") -> int:
    """`hubctl atelier --serve` — bloque jusqu'à Ctrl-C.

    Retour : 0 après arrêt (Ctrl-C), 2 si le token est indisponible/invalide.
    """
    tf = Path(token_file).expanduser() if token_file else DEFAULT_TOKEN_FILE
    try:
        server = build_server(port, tf, host=host)
    except FileNotFoundError:
        print(f"❌ Fichier token introuvable : {tf}")
        print(f"   Créez-le (ex : openssl rand -hex 32 > {tf}) — le Hub "
              "l'envoie ensuite comme header X-Atelier-Token.")
        return 2
    except ValueError as e:
        print(f"❌ Token atelier invalide ({tf}) : {e}")
        return 2
    host, bound = server.server_address[:2]
    print(f"✓ Serveur atelier : http://{host}:{bound}  (token : {tf})")
    print(f"  Routes : {_ROUTES_DOC}")
    print("  Ctrl-C pour arrêter.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt du serveur atelier.")
    finally:
        server.server_close()
    return 0
