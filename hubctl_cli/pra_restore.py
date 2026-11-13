# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Assistant PRA — restaure un parc POULIDOR complet depuis un kit PRA
(coffre + snapshot chiffré), sur une machine neuve.

Chaque étape détecte l'état existant → idempotent/resumable : une relance
après un crash à mi-parcours ne refait rien deux fois.

Restauration scriptable (pipeline, cron, stdin fermé) — exemple complet :

    ADMIN_PASSWORD=… ./hubctl pra-restore ~/kit --yes --force --apps all

- `--apps all|none|slug1,slug2` : apps à reconstruire/réinjecter après le
  restore (défaut : prompt interactif — en pipeline, stdin fermé = aucune
  app sélectionnée, avec avertissement explicite).
- `--force` : autorise la restauration sur un parc non vide (re-PRA).
- `ADMIN_PASSWORD` / `ADMIN_USERNAME` (env, ou `.env` du hub) : credentials
  admin pour la réinjection des données apps (sinon getpass interactif).
"""
from __future__ import annotations

import enum
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.text import Text

from hubctl_cli import hub_state, pra_config
from hubctl_cli.coffre import Vault

VAULT_FILENAME = pra_config.VAULT_FILENAME
SNAPSHOT_GLOB = "snapshot_*.tar.zst.enc"
HUB_API_CONTAINER = "hub-api"
HEALTH_TIMEOUT_S = 300
RESTORE_TIMEOUT_S = 180


def _run(cmd: list[str], *, cwd: Path | None = None, console=None,
         dry_run: bool = False) -> bool:
    """Exécute cmd (output streamé hérité). dry_run → affiche seulement."""
    label = " ".join(str(c) for c in cmd)
    if console:
        console.print(f"[dim]$ {label}[/dim]" + ("  (dry-run)" if dry_run else ""))
    if dry_run:
        return True
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None)
    return proc.returncode == 0


# ── Étape 0 : garde-fous ──

def preflight_docker() -> None:
    """Vérifie Docker installé + daemon joignable (spéc étape 0). Ne modifie
    rien → appelé même en dry-run. Échec → SystemExit immédiat (sinon :
    FileNotFoundError brut ou faux « parc vierge » + attente 300 s trompeuse)."""
    if shutil.which("docker") is None:
        raise SystemExit("❌ Docker n'est pas installé — installer Docker "
                         "Engine + le plugin compose d'abord.")
    r = subprocess.run(["docker", "info"], capture_output=True)
    if r.returncode != 0:
        raise SystemExit("❌ Daemon Docker injoignable (docker info) — "
                         "démarrer Docker d'abord.")


def check_parc_empty(force: bool) -> None:
    """Refuse un PRA sur un parc déjà initialisé (sauf --force)."""
    from hubctl_cli.park_restore import _check_hub_is_empty
    if _check_hub_is_empty():
        return
    if force:
        print("⚠ --force : parc non vide — continuation forcée.")
        return
    raise SystemExit("❌ Parc déjà initialisé (hub-db contient des données). "
                     "PRA refusé sans --force.")


# ── Étape 1 : kit ──

def find_kit(kit_arg: str | None) -> Path:
    """Localise le kit : argument explicite > config locale > scan auto."""
    if kit_arg:
        p = Path(kit_arg)
        if not (p / VAULT_FILENAME).exists():
            raise SystemExit(f"❌ Pas de coffre ({VAULT_FILENAME}) dans {p}")
        return p
    cfg = pra_config.load_config()
    if cfg and cfg.get("kit_id"):
        loc = pra_config.find_kit_location(cfg["kit_id"], cfg.get("path"))
        if loc is not None:
            return loc
    kits = pra_config.discover_kits()
    if not kits:
        raise SystemExit("❌ Aucun kit PRA trouvé — brancher l'emplacement "
                         "ou passer --kit <chemin>.")
    if len(kits) == 1:
        return kits[0]
    print("Kits détectés :")
    for i, k in enumerate(kits, 1):
        print(f"  {i}. {k}")
    raw = input("Numéro du kit à utiliser : ").strip()
    idx = int(raw) - 1 if raw.isdigit() else -1
    if 0 <= idx < len(kits):
        return kits[idx]
    raise SystemExit("❌ Choix invalide.")


def pick_snapshot(kit: Path, snapshot_arg: str | None) -> Path:
    """Snapshot à restaurer : argument explicite > plus récent du kit (mtime)."""
    if snapshot_arg:
        p = Path(snapshot_arg)
        if not p.exists():
            p = kit / snapshot_arg
        if not p.exists():
            raise SystemExit(f"❌ Snapshot introuvable : {snapshot_arg}")
        return p
    snaps = sorted(kit.glob(SNAPSHOT_GLOB),
                   key=lambda x: x.stat().st_mtime, reverse=True)
    if not snaps:
        raise SystemExit(f"❌ Aucun snapshot ({SNAPSHOT_GLOB}) dans {kit}")
    return snaps[0]


# ── Étape 2 : passphrase + fichiers ──

def restore_files_from_kit(kit: Path, console, *, dry_run: bool = False) -> None:
    """Déverrouille le coffre DU KIT (lecture directe, sans copie) et réécrit
    .env + secrets sous PARC_ROOT. Le dump DB partiel du coffre n'est PAS
    appliqué : le snapshot (pg_restore full) est l'unique source pour la DB."""
    import getpass
    vault = Vault(kit / VAULT_FILENAME)
    # Clé du kit d'abord (kits récents : parc.vault.key répliqué avec le coffre).
    if not vault.unlock_stored():
        for _attempt in range(3):
            pw = getpass.getpass("🔐 Passphrase du coffre : ")
            if pw and vault.unlock(pw):
                break
            console.print("[red]Passphrase incorrecte.[/red]")
        else:
            raise SystemExit("❌ 3 tentatives échouées.")
    if dry_run:
        console.print("[dim]dry-run : restauration des fichiers ignorée[/dim]")
        return
    written = vault.restore()  # idempotent : saute les fichiers existants
    console.print(f"✓ {len(written)} fichiers restaurés depuis le coffre")


# ── Étape 3 : stack ──

def build_stack(console, *, dry_run: bool = False) -> None:
    hub = hub_state.HUB_DIR
    if not (hub / "docker-compose.yml").exists():
        raise SystemExit(f"❌ {hub} incomplet — git clone du repo POULIDOR "
                         "requis d'abord.")
    if not _run(["make", "build"], cwd=hub, console=console, dry_run=dry_run):
        raise SystemExit("❌ make build a échoué — voir logs ci-dessus")
    if not _run(["make", "up-prod"], cwd=hub, console=console, dry_run=dry_run):
        raise SystemExit("❌ make up-prod a échoué — voir logs ci-dessus")


def wait_healthy(console, timeout_s: int = HEALTH_TIMEOUT_S) -> bool:
    """Attend que le healthcheck Docker de hub-api passe à 'healthy'."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}",
             HUB_API_CONTAINER],
            capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip() == "healthy":
            return True
        time.sleep(3)
    return False


# ── Utilitaires .env ──

def _env_lookup(key: str) -> str | None:
    env = hub_state.HUB_DIR / ".env"
    if not env.exists():
        return None
    for line in env.read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip() or None
    return None


def _api_base_url() -> str:
    return f"http://localhost:{_env_lookup('HUB_API_PORT') or '8000'}"


# ── Étape 4 : injection du snapshot ──

def snapshot_id_from(filename: str) -> str:
    """'snapshot_<uuid>.tar.zst.enc' → '<uuid>'."""
    return filename[len("snapshot_"):-len(".tar.zst.enc")]


def _psql(sql: str, *, tty_out: bool = False,
          cwd: Path | None = None) -> subprocess.CompletedProcess:
    """docker compose exec db psql (retourne CompletedProcess). cwd par
    défaut : HUB_DIR (même contexte que les autres commandes compose).
    tty_out : tuples-only `-t -A` — stdout vide à 0 ligne, parsable ; SANS,
    psql émet un en-tête (« ?column? / (0 rows) ») même sur table vide."""
    cmd = ["docker", "compose", "exec", "-T", "db", "psql", "-U", "hubadmin",
           "-d", "hub_auth"]
    if tty_out:
        cmd += ["-t", "-A", "-F", "|"]
    cmd += ["-c", sql]
    return subprocess.run(cmd, cwd=str(cwd or hub_state.HUB_DIR),
                          capture_output=True, text=True)


def _report_psql_error(proc: subprocess.CompletedProcess, console) -> None:
    """Affiche le stderr d'une commande psql (console si dispo, sinon stderr)."""
    err = (proc.stderr or "").strip()
    if not err:
        return
    if console:
        console.print(f"[red]{err}[/red]")
    else:
        print(err, file=sys.stderr)


def inject_snapshot(kit: Path, snap: Path, console, *, dry_run: bool = False) -> str:
    """Copie l'archive, crée la ligne hub_snapshots si absente (et la
    VÉRIFIE par re-SELECT), écrit pending_restore.txt. Retourne le snapshot_id.

    Garde-fou (incident VM 28/10/2026) : si la ligne ne peut pas être créée
    ou vérifiée, on abandonne AVANT d'écrire pending_restore — le hub
    refuserait sinon le restore (« Snapshot introuvable ») sans que hubctl
    le voie."""
    import shutil
    snap_id = snapshot_id_from(snap.name)
    if dry_run:
        if console:
            console.print(f"[dim]dry-run : copie {snap.name} + INSERT + pending_restore[/dim]")
        return snap_id
    dest_dir = hub_state.HUB_DIR / "storage" / "snapshots"
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(snap, dest_dir / snap.name)

    select = f"SELECT 1 FROM hub_snapshots WHERE snapshot_id = '{snap_id}'"
    # tuples-only : stdout « 1 » = ligne présente, vide = absente. Sans `-t
    # -A`, l'en-tête psql (« ?column? / (0 rows) », émis même sur table vide)
    # rendait le stdout non vide → « déjà présente » à tort, jamais d'INSERT,
    # et neutralisait aussi la vérification post-INSERT (VM 29/10/2026).
    exists = _psql(select, tty_out=True, cwd=hub_state.HUB_DIR)
    if exists.returncode == 0 and exists.stdout.strip() == "1":
        if console:
            console.print(f"• Ligne hub_snapshots déjà présente ({snap_id})")
    else:
        _report_psql_error(exists, console)
        insert = _psql(
            f"INSERT INTO hub_snapshots (snapshot_id, status, created_by, created_at, local_path) "
            f"VALUES ('{snap_id}', 'success', 'pra', now(), "
            f"'/code/storage/snapshots/{snap.name}')",
            cwd=hub_state.HUB_DIR)
        if insert.returncode != 0:
            _report_psql_error(insert, console)
        # Vérification post-écriture (mêmes tuples-only + contenu) : on
        # n'écrit JAMAIS pending_restore pour un état que le hub ne peut
        # pas restaurer.
        verified = _psql(select, tty_out=True, cwd=hub_state.HUB_DIR)
        if verified.returncode != 0 or verified.stdout.strip() != "1":
            _report_psql_error(verified, console)
            raise SystemExit(
                "❌ Impossible d'enregistrer le snapshot dans hub_snapshots — abandon "
                "AVANT pending_restore (le hub refuserait le restore). "
                "Voir erreur ci-dessus.")
        if console:
            console.print(f"✓ Ligne hub_snapshots créée et vérifiée ({snap_id})")
    # pending_restore.txt vit dans le conteneur (pas de montage hôte) :
    # écriture via docker exec, comme dans BACKUP.md.
    _run(["docker", "exec", HUB_API_CONTAINER, "sh", "-c",
          f"echo '{snap_id}' > /code/storage/pending_restore.txt"], console=console)
    return snap_id


def restart_hub_api(console, *, dry_run: bool = False) -> None:
    _run(["docker", "compose", "restart", HUB_API_CONTAINER],
         cwd=hub_state.HUB_DIR, console=console, dry_run=dry_run)


# ── Étape 5 : attente du restore ──

RESTORE_FAILURE_MARKERS = ("Restauration ECHEC", "RESTAURATION ECHEC")


class RestoreWaitStatus(enum.Enum):
    """Résultat de l'attente du restore interne (tri-state)."""
    DONE = "done"        # pending parti, aucun marqueur d'échec
    FAILED = "failed"    # marqueur « Restauration ECHEC » dans les logs hub-api
    TIMEOUT = "timeout"  # pending toujours présent après timeout_s


def _scan_hub_api_failure_logs(since: str) -> str | None:
    """Cherche un marqueur d'échec de restauration dans les logs hub-api
    publiés depuis `since` (RFC3339). Retourne la ligne fautive, sinon None."""
    r = subprocess.run(
        ["docker", "logs", "--since", since, HUB_API_CONTAINER],
        capture_output=True, text=True)
    output = (r.stdout or "") + (r.stderr or "")
    for line in output.splitlines():
        for marker in RESTORE_FAILURE_MARKERS:
            if marker in line:
                return line.strip()
    return None


def _check_restore_failure(since: str, console) -> str | None:
    """Scan des logs hub-api depuis `since` ; affiche le motif si trouvé.
    Utilisée par wait_restore_done ET par le re-scan post-healthy du flux
    (même fonction de scan pour les deux fenêtres de détection)."""
    failure = _scan_hub_api_failure_logs(since)
    if failure and console:
        console.print(f"[red]⚠ Marqueur d'échec détecté dans les logs "
                      f"hub-api : {failure}[/red]")
    return failure


def wait_restore_done(snap_id: str, console, timeout_s: int = RESTORE_TIMEOUT_S,
                      *, since: str | None = None) -> RestoreWaitStatus:
    """DONE quand pending_restore.txt a disparu sans marqueur d'échec,
    FAILED si un marqueur « Restauration ECHEC » apparaît dans les logs
    hub-api (motif affiché), TIMEOUT en cas de dépassement.

    since : RFC3339 — défaut : début de l'attente. L'appelant le réutilise
    pour le re-scan post-healthy : le hub supprime pending_restore.txt AVANT
    de lancer le restore, donc un échec lent (pg_restore en cours) se logge
    APRÈS la disparition du pending — invisible pour cette attente, d'où le
    re-scan du flux une fois hub-api healthy."""
    since = since or datetime.now().astimezone().isoformat(timespec="seconds")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _check_restore_failure(since, console):
            return RestoreWaitStatus.FAILED
        r = subprocess.run(
            ["docker", "exec", HUB_API_CONTAINER, "sh", "-c",
             "test -f /code/storage/pending_restore.txt"], capture_output=True)
        if r.returncode != 0:
            if _check_restore_failure(since, console):  # rattrape la course
                return RestoreWaitStatus.FAILED
            return RestoreWaitStatus.DONE
        time.sleep(5)
    return RestoreWaitStatus.TIMEOUT


def count_apps() -> int:
    r = _psql("SELECT count(*) FROM applications", tty_out=True)
    try:
        return int(r.stdout.strip() or 0)
    except ValueError:
        return 0


# ── Étape 6 : apps ──

def list_apps_with_backups() -> list[dict]:
    sql = ("SELECT a.slug, b.backup_id FROM applications a "
           "LEFT JOIN LATERAL (SELECT backup_id FROM app_backups "
           "WHERE app_slug = a.slug ORDER BY created_at DESC LIMIT 1) b ON true "
           "WHERE a.is_active = true ORDER BY a.slug")
    r = _psql(sql, tty_out=True)
    apps: list[dict] = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        slug, backup_id = line.split("|", 1)
        apps.append({"slug": slug, "backup_id": backup_id or None})
    return apps


def parse_selection(raw: str | None, slugs: list[str]) -> list[str]:
    """'1,3' / 'all' / 'none' / 'slug1,slug2' → liste de slugs valides."""
    raw = (raw or "").strip().lower()
    if not raw or raw == "none":
        return []
    if raw == "all":
        return list(slugs)
    picked: list[str] = []
    for part in raw.replace(" ", "").split(","):
        if part.isdigit():
            i = int(part) - 1
            if 0 <= i < len(slugs) and slugs[i] not in picked:
                picked.append(slugs[i])
        elif part in slugs and part not in picked:
            picked.append(part)
    return picked


def admin_login(base_url: str, username: str, password: str) -> str:
    """POST /api/token (form password grant) → access_token. urllib stdlib."""
    import json as _json
    import urllib.parse
    import urllib.request
    data = urllib.parse.urlencode({"username": username, "password": password,
                                   "grant_type": "password"}).encode()
    req = urllib.request.Request(
        base_url + "/api/auth/token", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = _json.loads(resp.read())
    token = payload.get("access_token")
    if not token:
        raise ValueError("Réponse login sans access_token")
    return token


def restore_app_data(base_url: str, token: str, slug: str, backup_id: str,
                     *, timeout_s: int = 300) -> bool:
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        f"{base_url}/api/apps/{slug}/backup/{backup_id}/restore",
        data=b"{}", method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError:
        return False


def rebuild_app(slug: str, console, *, dry_run: bool = False) -> bool:
    app_dir = hub_state.APPS_DIR / slug
    if not (app_dir / "Makefile").exists():
        console.print(f"[yellow]⚠ {slug} : pas de Makefile — rebuild ignoré[/yellow]")
        return False
    ok_build = _run(["make", "build"], cwd=app_dir, console=console, dry_run=dry_run)
    ok_up = _run(["make", "up"], cwd=app_dir, console=console, dry_run=dry_run)
    return ok_build and ok_up


def _select_apps_interactive(console, all_apps: list[dict],
                             apps: str | None) -> list[str]:
    """Sélection des apps à restaurer : --apps prioritaire, sinon prompt.
    stdin fermé au prompt (pipeline) → avertissement explicite + liste vide
    (incident VM 28/10/2026 : « none » silencieux, aucune app restaurée)."""
    slugs = [a["slug"] for a in all_apps]
    if apps is not None:
        return parse_selection(apps, slugs)
    if not all_apps:
        console.print("Aucune app active en base — étape apps ignorée.")
        return []
    console.print("Apps actives (dernier backup) :")
    for i, a in enumerate(all_apps, 1):
        backup_info = f"backup {a['backup_id'][:8]}…" if a["backup_id"] else "aucun backup"
        console.print(f"  {i}. {a['slug']}  ({backup_info})")
    try:
        raw = input("Apps à restaurer (ex: 1,3 | all | none) : ")
    except EOFError:
        console.print("[yellow]⚠ stdin fermé — aucune app sélectionnée. "
                      "Relance avec --apps all (et ADMIN_PASSWORD=… pour la "
                      "réinjection des données).[/yellow]")
        raw = "none"
    return parse_selection(raw, slugs)


# ── Flux complet ──

def run_pra_restore(kit: str | None = None, *, snapshot: str | None = None,
                    apps: str | None = None, admin_user: str | None = None,
                    yes: bool = False, force: bool = False,
                    dry_run: bool = False) -> int:
    import getpass
    import os
    console = Console()
    console.rule("[bold cyan]PRA POULIDOR — restauration de secours")

    # Étape 0 — garde-fous (preflight d'abord, même en dry-run : il ne modifie rien)
    preflight_docker()
    check_parc_empty(force)

    # Étape 1 — kit + snapshot
    kit_path = find_kit(kit)
    console.print(f"📦 Kit : {kit_path}")
    snap = pick_snapshot(kit_path, snapshot)
    console.print(f"🧩 Snapshot : {snap.name}")

    # Étape 2 — passphrase + fichiers (secrets/.env)
    restore_files_from_kit(kit_path, console, dry_run=dry_run)

    # Étape 3 — stack vierge
    if not yes:
        try:
            input("↵ pour builder et démarrer la stack (Ctrl-C pour annuler) ")
        except EOFError:
            pass
    build_stack(console, dry_run=dry_run)
    if not dry_run:
        if not wait_healthy(console):
            _run(["docker", "logs", "--tail", "50", HUB_API_CONTAINER], console=console)
            raise SystemExit("❌ hub-api pas healthy après démarrage — voir logs ci-dessus")
        console.print("✓ hub-api healthy")

    # Étape 4 — injection + restart
    snap_id = inject_snapshot(kit_path, snap, console, dry_run=dry_run)
    restart_hub_api(console, dry_run=dry_run)

    # Étape 5 — attente du restore interne
    if not dry_run:
        since = datetime.now().astimezone().isoformat(timespec="seconds")
        status = wait_restore_done(snap_id, console, since=since)
        if status is RestoreWaitStatus.FAILED:
            _run(["docker", "logs", "--tail", "50", HUB_API_CONTAINER], console=console)
            raise SystemExit("❌ Restauration du snapshot en échec côté hub-api — "
                             "voir logs ci-dessus")
        if status is RestoreWaitStatus.TIMEOUT:
            _run(["docker", "logs", "--tail", "50", HUB_API_CONTAINER], console=console)
            raise SystemExit("❌ Restore non terminé (pending_restore.txt toujours "
                             "présent) — voir logs ci-dessus")
        if not wait_healthy(console):
            raise SystemExit("❌ hub-api pas healthy après restore")
        # Le hub supprime pending_restore.txt AVANT de lancer le restore : un
        # échec lent (pg_restore en cours) se logge APRÈS la disparition du
        # pending — invisible pour l'attente ci-dessus. Re-scan unique sur la
        # fenêtre complète (attente + healthy) avant de déclarer le succès.
        if _check_restore_failure(since, console):
            _run(["docker", "logs", "--tail", "50", HUB_API_CONTAINER], console=console)
            raise SystemExit("❌ Restauration du snapshot en échec côté hub-api "
                             "(échec lent détecté après restore) — voir logs "
                             "ci-dessus")
        n = count_apps()
        console.print(f"✓ Restore terminé — {n} app(s) en base")

    # Étape 6 — apps : liste + sélection + rebuild + réinjection
    all_apps = [] if dry_run else list_apps_with_backups()
    selection = _select_apps_interactive(console, all_apps, apps)

    todo = [a for a in all_apps if a["slug"] in selection]
    token: str | None = None
    if todo and not dry_run:
        username = (admin_user or os.environ.get("ADMIN_USERNAME")
                    or _env_lookup("ADMIN_USERNAME") or "admin")
        pw = os.environ.get("ADMIN_PASSWORD") or getpass.getpass(
            f"Mot de passe admin ({username}) : ")
        token = admin_login(_api_base_url(), username, pw)
        console.print("✓ Token admin obtenu")

    results: dict[str, str] = {}
    for a in todo:
        slug = a["slug"]
        if dry_run:
            console.print(f"[dim]dry-run : rebuild + restore {slug}[/dim]")
            continue
        ok_build = rebuild_app(slug, console)
        if a["backup_id"] and token:
            ok_data = restore_app_data(_api_base_url(), token, slug, a["backup_id"])
            results[slug] = ("✓ restaurée (data réinjectée)" if ok_build and ok_data
                             else "⚠ rebuild ok, réinjection échouée" if ok_build
                             else "✗ rebuild échoué")
        else:
            results[slug] = "✓ rebuildée (pas de backup)" if ok_build else "✗ rebuild échoué"
    if results:
        console.rule("Récap apps")
        for slug, status in results.items():
            console.print(f"  {slug}: {status}")

    console.print("[bold green]PRA terminé.[/bold green]")
    return 0


if __name__ == "__main__":
    import argparse
    _p = argparse.ArgumentParser(prog="pra-restore",
                                 description="Restauration PRA depuis un kit POULIDOR")
    _p.add_argument("kit", nargs="?", default=None, help="Chemin du kit PRA (scan auto si omis)")
    _p.add_argument("--snapshot", default=None, help="Nom ou chemin du snapshot (défaut: plus récent du kit)")
    _p.add_argument("--apps", default=None,
                    help="all | none | slugs CSV (défaut: interactif). Scriptable : "
                         "ADMIN_PASSWORD=… ./hubctl pra-restore ~/kit --yes --force --apps all")
    _p.add_argument("--admin-user", default=None,
                    help="Login admin pour la réinjection (défaut: ADMIN_USERNAME/.env/admin)")
    _p.add_argument("--yes", action="store_true", help="Saute les confirmations")
    _p.add_argument("--force", action="store_true",
                    help="Autorise la restauration sur un parc non vide (re-PRA)")
    _p.add_argument("--dry-run", action="store_true", help="Affiche le plan sans rien exécuter")
    _a = _p.parse_args()
    raise SystemExit(run_pra_restore(
        _a.kit, snapshot=_a.snapshot, apps=_a.apps, admin_user=_a.admin_user,
        yes=_a.yes, force=_a.force, dry_run=_a.dry_run))
