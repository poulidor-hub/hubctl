# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""`hubctl update serve` — statut + job de mise à jour du parc.

Brique hôte du feature « Centre de mises à jour » : état git par composant
(méta + submodules déclarés dans ``.gitmodules``), consommé par la route
GET /update/status, et exécution du job ``hubctl update hub``, consommé
par POST /update/run + GET /update/job (Task 3 — shapes JSON = contrat,
spec §5.1/§5.2).

Contraintes (spec §5.1) :
- stdlib uniquement : réutilise ``park_checks._run`` (git non-interactif
  ``GIT_TERMINAL_PROMPT=0``, timeout HUBCTL_GIT_TIMEOUT) et
  ``park_checks._submodule_paths`` par import — aucune dépendance nouvelle,
  aucune logique git recopiée ;
- un composant en échec ne fait JAMAIS échouer la collecte globale : son
  entrée porte ``error`` et les champs calculables sur les refs locales ;
- cache mémoire TTL ``_STATUS_TTL`` (60 s) : le rafraîchissement UI est
  gratuit ; ``force=True`` (équivalent ``?refresh=1``) contourne le cache.

Shape (contrat) :
- :func:`collect_update_status` → ``{"service": "ok", "checked_at":
  <ISO8601>, "meta": <component>, "components": {rel_path: <component>}}`` ;
- ``<component>`` = ``{"name", "path", "version", "version_target",
  "branch", "commits_behind", "dirty", "drifted", "update_available",
  "error"}`` ;
  ``version`` = ``VERSION`` local (à défaut champ ``version`` du
  ``package.json`` pour les composants JS, sinon ``None``) ;
  ``version_target`` = ``git show origin/main:<fichier>`` (lisible sans
  toucher à l'arbre local, ``None`` si absent) ;
  ``drifted`` = rel_paths des submodules dont le HEAD local ≠ gitlink
  enregistré par le méta (calculé pour l'entrée méta, ``[]`` pour un
  submodule) ;
  ``update_available`` du méta = ``commits_behind > 0 or drifted`` —
  une mise à jour réalignera les pointeurs (spec §5.1) ; pour un
  submodule, ``commits_behind > 0`` seul.

Job (contrat, spec §5.2) :
- l'état du job ne vit QUE dans ``update_state.json`` (écriture JSON
  atomique ``.tmp`` + ``os.replace``) : le verdict du dernier job
  survit aux redémarrages du service comme du Hub ;
  :func:`read_job_state` ne lève JAMAIS — fichier absent ou JSON
  corrompu → ``{"state": "idle"}`` ;
- le job exécute exactement ``<parc>/hubctl update hub`` (``--dry-run``
  si demandé), subprocess cwd=parc_root, stdout+stderr → log horodaté,
  timeout ``_JOB_TIMEOUT`` (3600 s) — zéro duplication de la logique
  CLI : pré-checks et rollback viennent du point d'entrée réel ;
- ``_JOB_RUNNING`` (bool mémoire sous ``_JOB_LOCK``, worker unique
  ``_EXECUTOR`` — recette atelier_serve vault/backup) sert UNIQUEMENT
  au refus concurrent immédiat : :func:`start_update` →
  ``(202, {"job": {"state": "running", "started_at", "log_path"}})``
  ou ``(409, {"detail": "update deja en cours", "job": <état disque>})``
  sans soumettre de second processus ;
- état final ``{"state": "success"|"failed", "started_at",
  "finished_at", "exit_code", "log_path", "tail"}`` — ``tail`` = les
  ``_TAIL_LINES`` (50) dernières lignes du log ; timeout ou log
  inouvrable → ``failed`` avec ``exit_code`` ``None``.

Serveur HTTP (contrat, spec §4.1) :
- mêmes mécaniques que ``atelier_serve`` (ThreadingHTTPServer stdlib,
  auth header ``X-Atelier-Token`` comparé en constant-time, token par
  défaut ``~/.config/hubctl/atelier_token`` lu au démarrage) ;
  ``/health`` SANS auth (liveness, ne divulgue rien) ;
- GET /update/status → :func:`collect_update_status` (``?refresh=1`` →
  ``force=True``), GET /update/job → :func:`read_job_state` (proxifié
  tel quel par le Hub), POST /update/run → :func:`start_update` (body
  JSON optionnel ``{"dry_run": bool}``, défaut False ; 202/409
  passthrough du couple ``(code, payload)``) ;
- le handler ne lève JAMAIS : chaque route répond un JSON (400 body
  invalide, 404 route inconnue, 405 méthode interdite, 500 générique en
  dernier recours — même convention atelier_serve).
"""
from __future__ import annotations

import copy
import hmac
import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from hubctl_cli import hub_state, park_checks

# Durée de vie (secondes) du cache statut : le front rafraîchit souvent,
# un seul cycle de fetch par minute suffit (spec §5.1).
_STATUS_TTL: float = 60.0
_STATUS_CACHE: dict | None = None
_STATUS_TS: float = 0.0

# Timeout (s) du subprocess ``hubctl update hub`` : un update parc complet
# (pull + build + up-prod + rollback) reste largement sous une heure ; au
# délai dépassé le subprocess est tué et l'état passe ``failed`` (spec §5.2).
_JOB_TIMEOUT: float = 3600.0

# Garde d'obsolescence (revue finale D2) : un état ``running`` vieux de plus
# de ``_JOB_TIMEOUT + 300`` s ne peut plus être un vrai job (le subprocess
# serait déjà mort de timeout depuis 5 min) — c'est le service hôte qui a
# été tué pendant le job (restart/OOM/reboot), laissant l'état bloqué.
# :func:`read_job_state` transforme alors l'état en ``failed`` : le front
# (bouton de mise à jour) ne reste pas désactivé indéfiniment.
_JOB_STALE_AFTER: float = _JOB_TIMEOUT + 300.0

# Dernières lignes du log embarquées dans l'état final : le front affiche
# le verdict sans re-télécharger tout le log (spec §5.2).
_TAIL_LINES: int = 50

# Recette atelier_serve (vault/backup) : worker UNIQUE + verrou. Le bool
# mémoire sert uniquement au refus 409 concurrent — la source de vérité
# de l'état est le fichier sur disque, qui survit aux redémarrages.
_EXECUTOR = ThreadPoolExecutor(max_workers=1)
_JOB_LOCK = threading.Lock()
_JOB_RUNNING: bool = False


def _read_version(repo: Path, ref: str = "HEAD") -> str | None:
    """Version d'un composant : ``VERSION`` brut, sinon ``package.json``,
    sinon ``None``.

    ``ref="HEAD"`` lit l'arbre local ; toute autre ref (``origin/main``)
    passe par ``git show ref:<fichier>`` — sans toucher à l'arbre local.
    """
    for name in ("VERSION", "package.json"):
        if ref == "HEAD":
            path = repo / name
            if not path.is_file():
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
        else:
            rc, raw, _err = park_checks._run("git", "show", f"{ref}:{name}",
                                             cwd=repo)
            if rc != 0:
                continue
        if name == "package.json":
            try:
                version = json.loads(raw).get("version")
            except (json.JSONDecodeError, AttributeError):
                continue
            if isinstance(version, str) and version:
                return version
            continue
        raw = raw.strip()
        if raw:
            return raw
    return None


def _meta_drift(parc_root: Path) -> list[str]:
    """Submodules dont le HEAD local ≠ gitlink enregistré par le méta.

    Comparaison purement locale (``git ls-tree HEAD <path>`` côté méta vs
    ``git rev-parse HEAD`` côté submodule, même lecture que
    ``park_checks.check_hub_base``) : un submodule en avance locale ou
    posé sur un autre commit rend le gitlink méta obsolète — une mise à
    jour du méta réalignera le pointeur (spec §5.1). Ne lève jamais ;
    submodule non initialisé ou comparaison illisible → ignoré.
    """
    drifted: list[str] = []
    for rel in sorted(park_checks._submodule_paths(parc_root)):
        sub = parc_root / rel
        if not (sub / ".git").exists() and not (sub / ".git").is_file():
            continue  # jamais initialisé : pas de HEAD local à comparer
        rc, out, _ = park_checks._run("git", "ls-tree", "HEAD", rel,
                                      cwd=parc_root)
        parts = out.split() if rc == 0 else []
        recorded = parts[2] if len(parts) > 2 else None
        if recorded is None:
            continue
        rc, local, _ = park_checks._run("git", "rev-parse", "HEAD", cwd=sub)
        if rc == 0 and local.strip() and local.strip() != recorded:
            drifted.append(rel)
    return drifted


def _component_status(parc_root: Path, rel: str) -> dict:
    """Statut git d'un composant (``rel="."`` → méta). Ne lève jamais.

    Échecs isolés dans ``error`` : fetch impossible → on continue sur les
    refs locales (le fetch précédent, jamais de diagnostic destructeur) ;
    sous-repo non initialisé → ``error="not initialized"``. Pour le méta,
    ``drifted`` liste les submodules désalignés (``_meta_drift``) et
    alimente ``update_available``.
    """
    repo = parc_root if rel == "." else parc_root / rel
    entry: dict = {
        "name": parc_root.name if rel == "." else Path(rel).name,
        "path": rel,
        "version": None,
        "version_target": None,
        "branch": None,
        "commits_behind": 0,
        "dirty": False,
        "drifted": [],
        "update_available": False,
        "error": None,
    }
    if not (repo / ".git").exists() and not (repo / ".git").is_file():
        entry["error"] = "not initialized"
        return entry

    frc, _fout, ferr = park_checks._run("git", "fetch", "--prune", "origin",
                                        cwd=repo)
    if frc != 0:
        # détail stderr tronqué : le front affiche la cause sans ouvrir le log
        entry["error"] = f"fetch failed: {ferr.strip()[:120]}"

    rc, out, _ = park_checks._run("git", "rev-parse", "--abbrev-ref", "HEAD",
                                  cwd=repo)
    if rc == 0 and out and out != "HEAD":
        entry["branch"] = out  # HEAD détaché → None (convention park_checks)

    rc, out, _ = park_checks._run("git", "rev-list", "--count",
                                  "HEAD..origin/main", cwd=repo)
    if rc == 0 and out.isdigit():
        entry["commits_behind"] = int(out)
    else:
        # refs absentes (jamais fetché, repo vide) → 0 + error
        entry["error"] = entry["error"] or "origin/main introuvable"

    # « sale » = fichiers SUIVIS modifiés seulement (_dirty_tracked) : le
    # non-suivi (artefacts runtime : secrets, uploads…) ne rend plus l'arbre
    # « sale » — même définition que les pré-checks de ``update hub``
    # (Bug 1 volet dirty-check). Échec git → [] → False (comme avant).
    entry["dirty"] = bool(park_checks._dirty_tracked(repo))

    entry["version"] = _read_version(repo)
    entry["version_target"] = _read_version(repo, "origin/main")
    if rel == ".":
        # spec §5.1 : le méta est aussi « à mettre à jour » quand un gitlink
        # ne pointe plus le HEAD local d'un submodule (drift) — une mise à
        # jour réalignera les pointeurs. Entrées submodules : commits_behind
        # seul (elles ne portent pas de pointeurs).
        entry["drifted"] = _meta_drift(parc_root)
        entry["update_available"] = (entry["commits_behind"] > 0
                                     or bool(entry["drifted"]))
    else:
        entry["update_available"] = entry["commits_behind"] > 0
    return entry


def collect_update_status(parc_root: Path, *, force: bool = False) -> dict:
    """Statut complet du parc (méta + submodules initialisés), cache TTL.

    Cache mémoire MONO-PARC (contrat) : le serveur hôte n'observe qu'UN
    seul parc — le cache n'est PAS indexé par parc_root ; appeler avec un
    parc différent suppose ``force=True`` ou un cache vierge (tests).
    ``force=True`` ignore le cache et refait le cycle fetch (route
    ``?refresh=1``). Au hit, une copie profonde est renvoyée : le
    consommateur (sérialiseur HTTP) ne peut pas muter le cache interne.
    Les submodules déclarés mais jamais initialisés (répertoire absent)
    sont exclus de ``components``.
    """
    global _STATUS_CACHE, _STATUS_TS
    parc_root = Path(parc_root)
    now = time.monotonic()
    if not force and _STATUS_CACHE is not None and (now - _STATUS_TS) < _STATUS_TTL:
        return copy.deepcopy(_STATUS_CACHE)

    meta = _component_status(parc_root, ".")
    components: dict[str, dict] = {}
    for rel in sorted(park_checks._submodule_paths(parc_root)):
        if not (parc_root / rel).is_dir():
            continue
        components[rel] = _component_status(parc_root, rel)

    status = {
        "service": "ok",
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "meta": meta,
        "components": components,
    }
    _STATUS_CACHE = status
    _STATUS_TS = now
    return status


# ── Job de mise à jour (état persistant + exécution, spec §5.2) ─────────────


def _now_iso() -> str:
    """Horodatage ISO8601 local, même format que ``checked_at`` (Task 1)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_state(state_file: Path, payload: dict) -> None:
    """État du job → JSON atomique (``.tmp`` + ``os.replace``) : un
    lecteur concurrent (GET /update/job) ne lit jamais un état à moitié
    écrit, et un service tué en pleine écriture laisse au pire un
    ``.tmp`` orphelin — pas un état corrompu."""
    state_file = Path(state_file)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_name(state_file.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, state_file)


def _parse_started_at(raw) -> datetime | None:
    """ISO8601 → datetime aware (tz locale assumée si absente). Toute
    valeur absente/illisible → ``None`` : la garde d'obsolescence de
    :func:`read_job_state` est alors désactivée (comportement lecture
    pure inchangé) — parse tolérant, jamais d'exception."""
    try:
        dt = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return dt.astimezone() if dt.tzinfo is None else dt


def read_job_state(state_file: Path) -> dict:
    """Contenu courant/dernier de ``update_state.json`` (contrat
    GET /update/job, proxifié tel quel par le Hub).

    Source de vérité = le fichier, jamais la mémoire : un service
    « redémarré » relit le verdict du dernier job (spec §4.1). Jamais
    d'exception : fichier absent ou JSON corrompu/illisible →
    ``{"state": "idle"}`` — le front affiche alors un état neutre.

    Garde d'obsolescence (revue finale D2) : un état ``running`` dont
    ``started_at`` date de plus de ``_JOB_STALE_AFTER`` ne peut plus être
    un job vivant (service hôte tué pendant le job) → l'état est
    transformé en ``{"state": "failed", ...mêmes champs..., "exit_code":
    None, "detail": "job obsolet (service hote redemarre pendant le job ?)"}``
    ET réécrit sur disque (écriture atomique ``_write_state``) pour que la
    correction persiste — choix documenté du contrat : tous les lecteurs
    (poll front, proxy Hub, payload 409) voient ensuite ``failed`` au
    lieu d'un ``running`` fantôme. Échec d'écriture (OSError, ex. état
    en lecture seule) → la transformation est quand même retournée
    (lecture pure en secours). ``started_at`` illisible → aucun
    traitement : comportement lecture pure inchangé.
    """
    try:
        data = json.loads(Path(state_file).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"state": "idle"}
    if not isinstance(data, dict):
        return {"state": "idle"}
    if data.get("state") == "running":
        started = _parse_started_at(data.get("started_at"))
        if started is not None:
            age = (datetime.now().astimezone() - started).total_seconds()
            if age > _JOB_STALE_AFTER:
                data = {**data, "state": "failed", "exit_code": None,
                        "detail": "job obsolet (service hote redemarre "
                                  "pendant le job ?)"}
                try:
                    _write_state(state_file, data)
                except OSError:
                    pass  # lecture pure en secours : transformation retournée
    return data


def _log_tail(log_path: Path, lines: int = _TAIL_LINES) -> list[str]:
    """Les ``lines`` dernières lignes du log (verdict dans l'état final,
    spec §5.2). Jamais d'exception : log absent/illisible → ``[]`` ;
    sortie binaire éventuelle décodée avec ``errors="replace"``."""
    try:
        raw = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return raw.splitlines()[-lines:]


def _run_update_job(parc_root: Path, state_file: Path, log_path: Path,
                    dry_run: bool) -> None:
    """Thread worker (:func:`start_update`) : exécute le point d'entrée
    CLI réel ``<parc>/hubctl update hub`` (``--dry-run`` inséré AVANT le
    subcommand — option globale du parser) et finalise l'état sur disque.

    stdout+stderr confondus → ``log_path``, cwd=parc_root, timeout
    ``_JOB_TIMEOUT``. ``exit_code`` 0 → ``success``, sinon ``failed`` ;
    timeout (subprocess tué) ou log/exec impossible (OSError) →
    ``failed`` avec ``exit_code`` ``None``. Toute sortie de job — même
    en erreur — relâche ``_JOB_RUNNING`` : le refus 409 ne survit jamais
    au worker (pas de 409 fantôme après un crash).
    """
    global _JOB_RUNNING
    # --dry-run est une option GLOBALE du parser (hubctl_cli/__main__.py,
    # build_parser) : elle doit PRÉCÉDER le subcommand « update », sinon
    # argparse répond « unrecognized arguments » (SystemExit 2) — cf. test
    # test_dry_run_global_option_contract (revue finale D1).
    cmd = [str(Path(parc_root) / "hubctl")]
    if dry_run:
        cmd.append("--dry-run")
    cmd += ["update", "hub"]
    try:
        exit_code: int | None
        try:
            with Path(log_path).open("w", encoding="utf-8") as log:
                try:
                    proc = subprocess.run(cmd, stdout=log,
                                          stderr=subprocess.STDOUT,
                                          cwd=parc_root, timeout=_JOB_TIMEOUT)
                    exit_code = proc.returncode
                except subprocess.TimeoutExpired:
                    exit_code = None
        except OSError:
            exit_code = None
        # started_at du state ``running`` conservé (état final = même job) ;
        # absent (fichier effacé en cours de route) → horodatage de secours.
        previous = read_job_state(state_file)
        _write_state(state_file, {
            "state": "success" if exit_code == 0 else "failed",
            "started_at": previous.get("started_at") or _now_iso(),
            "finished_at": _now_iso(),
            "exit_code": exit_code,
            "log_path": str(log_path),
            "tail": _log_tail(log_path),
        })
    finally:
        with _JOB_LOCK:
            _JOB_RUNNING = False


def start_update(*, parc_root: Path, state_file: Path, log_dir: Path,
                 dry_run: bool = False) -> tuple[int, dict]:
    """POST /update/run → ``(code, payload)`` (contrat Task 3, spec §5.2).

    Job en cours (``_JOB_RUNNING``, recette atelier vault/backup) →
    ``(409, {"detail": "update deja en cours", "job": <état disque>})``
    sans toucher au worker. Sinon : état ``running`` écrit SUR DISQUE
    avant tout, log horodaté créé dans ``log_dir`` (créé si absent), job
    soumis au worker unique, retour immédiat ``(202, {"job": {...}})`` —
    l'appelant HTTP n'attend jamais la fin du subprocess (suivi par
    GET /update/job). Un échec d'amorçage (log_dir inexistant et non
    créable…) relâche le flag et remonte : pas de 409 permanent.
    """
    global _JOB_RUNNING
    with _JOB_LOCK:
        if _JOB_RUNNING:
            return 409, {"detail": "update deja en cours",
                         "job": read_job_state(state_file)}
        _JOB_RUNNING = True
    try:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"update-{datetime.now():%Y%m%d-%H%M%S}.log"
        job = {"state": "running", "started_at": _now_iso(),
               "log_path": str(log_path)}
        _write_state(state_file, job)
    except Exception:
        with _JOB_LOCK:
            _JOB_RUNNING = False
        raise
    _EXECUTOR.submit(_run_update_job, Path(parc_root), Path(state_file),
                     log_path, dry_run)
    return 202, {"job": job}


# ── Serveur HTTP (Task 3, spec §4.1) ────────────────────────────────────────

DEFAULT_TOKEN_FILE = Path.home() / ".config" / "hubctl" / "atelier_token"

_ROUTES_DOC = ("GET /health · GET /update/status · POST /update/run · "
               "GET /update/job")


def _read_token(token_file: Path) -> bytes:
    """Lit le token d'auth (sans la nouvelle ligne finale). Refuse un fichier vide."""
    raw = Path(token_file).expanduser().read_text(encoding="utf-8")
    token = raw.strip()
    if not token:
        raise ValueError("fichier token vide")
    return token.encode("utf-8")


def build_server(port: int, token_file: Path, host: str = "127.0.0.1", *,
                 parc_root: Path, state_file: Path,
                 log_dir: Path) -> ThreadingHTTPServer:
    """Construit le serveur (bind host:port, token lu au démarrage).

    host : défaut 127.0.0.1 (recette atelier_serve). La passerelle docker0
    (ip -4 addr show docker0 — PAS toujours 172.17.0.1 : les pools
    personnalisés du daemon peuvent la déplacer, ex. 10.200.0.1) rend le
    serveur joignable des conteneurs via host.docker.internal sans être
    visible du LAN.

    port=0 → port libre choisi par l'OS (utile aux tests) : le port réel
    se lit sur server_address après construction.
    """
    token = _read_token(token_file)
    server = ThreadingHTTPServer((host, port), UpdateHandler)
    server.update_token = token
    server.parc_root = Path(parc_root)
    server.state_file = Path(state_file)
    server.log_dir = Path(log_dir)
    return server


class UpdateHandler(BaseHTTPRequestHandler):
    """Handlers JSON des 4 routes (contrat §4.1). Auth X-Atelier-Token
    (sauf /health). Ne lève JAMAIS : chaque route répond un JSON — 400
    body invalide, 404 route inconnue, 405 méthode interdite, 500
    générique en dernier recours (même convention atelier_serve)."""

    server_version = "hubctl-update/0.1"

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
        expected = getattr(self.server, "update_token", b"")
        if not hmac.compare_digest(provided.encode("utf-8", "replace"), expected):
            self._send_json(403, {"erreur": "token invalide"})
            return False
        return True

    def _route(self) -> str:
        return urlsplit(self.path).path

    def _refresh(self) -> bool:
        """``?refresh=1`` → ``force=True`` : contourne le cache TTL du
        statut (spec §5.1, bouton « Vérifier maintenant » du front)."""
        return parse_qs(urlsplit(self.path).query).get("refresh", [""])[0] == "1"

    def _read_dry_run(self) -> bool | None:
        """Body JSON optionnel de POST /update/run : ``{"dry_run": bool}``
        (défaut False). Body absent/vide → False ; JSON illisible ou non
        objet → ``None`` (l'appelant répond 400)."""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return False
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(body, dict):
            return None
        return bool(body.get("dry_run", False))

    def do_GET(self) -> None:  # noqa: N802 (API http.server)
        try:
            route = self._route()
            if route == "/health":
                # Liveness publique : ne divulgue rien, évite d'imposer le
                # token aux healthchecks/monitoring.
                return self._send_json(200, {"service": "update-agent",
                                             "ok": True})
            if not self._authorized():
                return
            if route == "/update/status":
                status = collect_update_status(self.server.parc_root,
                                               force=self._refresh())
                return self._send_json(200, status)
            if route == "/update/job":
                return self._send_json(200,
                                       read_job_state(self.server.state_file))
            if route == "/update/run":
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
            if route == "/update/run":
                dry_run = self._read_dry_run()
                if dry_run is None:
                    return self._send_json(
                        400, {"erreur": "body JSON invalide : "
                                        "objet {\"dry_run\": bool} attendu"})
                code, payload = start_update(
                    parc_root=self.server.parc_root,
                    state_file=self.server.state_file,
                    log_dir=self.server.log_dir, dry_run=dry_run)
                return self._send_json(code, payload)
            if route in ("/update/status", "/update/job"):
                return self._send_json(405, {"erreur": "méthode interdite : GET requis"})
            return self._send_json(404, {"erreur": "route inconnue"})
        except Exception as e:
            return self._send_json(500, {"erreur": str(e)})


def serve(port: int = 8778, token_file: Path | None = None,
          host: str = "127.0.0.1") -> int:
    """`hubctl update --serve` — bloque jusqu'à Ctrl-C.

    Défauts déduits (spec §4.1) : parc = ``PARC_ROOT`` (détection
    manifest.json de ``hub_state``, même lecture que le reste du CLI),
    état ``~/.config/hubctl/update_state.json``, logs
    ``~/.config/hubctl/update_logs/``, token ``~/.config/hubctl/atelier_token``.

    Retour : 0 après arrêt (Ctrl-C), 2 si le token est indisponible/invalide.
    """
    tf = Path(token_file).expanduser() if token_file else DEFAULT_TOKEN_FILE
    config_dir = Path.home() / ".config" / "hubctl"
    try:
        server = build_server(port, tf, host=host,
                              parc_root=hub_state.PARC_ROOT,
                              state_file=config_dir / "update_state.json",
                              log_dir=config_dir / "update_logs")
    except FileNotFoundError:
        print(f"❌ Fichier token introuvable : {tf}")
        print(f"   Créez-le (ex : openssl rand -hex 32 > {tf}) — le Hub "
              "l'envoie ensuite comme header X-Atelier-Token.")
        return 2
    except ValueError as e:
        print(f"❌ Token update invalide ({tf}) : {e}")
        return 2
    host, bound = server.server_address[:2]
    print(f"✓ Serveur update : http://{host}:{bound}  (token : {tf})")
    print(f"  Routes : {_ROUTES_DOC}")
    print("  Ctrl-C pour arrêter.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt du serveur update.")
    finally:
        server.server_close()
    return 0
