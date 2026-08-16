"""Assistant PRA — restaure un parc POULIDOR complet depuis un kit PRA
(coffre + snapshot chiffré), sur une machine neuve.

Chaque étape détecte l'état existant → idempotent/resumable : une relance
après un crash à mi-parcours ne refait rien deux fois.
"""
from __future__ import annotations

import subprocess
import sys
import time
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
    _run(["make", "build"], cwd=hub, console=console, dry_run=dry_run)
    _run(["make", "up-prod"], cwd=hub, console=console, dry_run=dry_run)


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
