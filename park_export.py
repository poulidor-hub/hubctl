"""
Park Export — sauvegarde complète du parc POULIDOR.
Produit une archive tar.gz contenant : DB dumps, .env, secrets Hub, manifeste.

Usage :
    python -m hubctl_cli park-export                     # tout le parc
    python -m hubctl_cli park-export --app exemple-apps  # une seule app
"""

from __future__ import annotations
import json
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hubctl_cli.hub_state import PARC_ROOT, HUB_DIR, APPS_DIR

EXPORT_DIR = PARC_ROOT / "exports"

# Conteneurs PostgreSQL du Hub (nom fixe, pas suffixé par slug)
HUB_DB_CONTAINER = "hub-db"
HUB_DB_USER = "hubadmin"
HUB_DB_NAME = "hub_auth"

HUB_SECRETS_DIR = HUB_DIR / "secrets"
HUB_JWT_PRIVATE = HUB_SECRETS_DIR / "jwt_private.pem"
HUB_JWT_PUBLIC = HUB_SECRETS_DIR / "jwt_public.pem"
HUB_FERNET_KEY = HUB_DIR / ".fernet_key"


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _detect_apps() -> list[dict]:
    """Détecte toutes les apps dans APPS/ avec leur config DB."""
    apps = []
    for app_dir in sorted(APPS_DIR.iterdir()):
        if not app_dir.is_dir():
            continue
        env_file = app_dir / ".env"
        compose_file = app_dir / "docker-compose.yml"
        if not env_file.exists() or not compose_file.exists():
            continue

        slug = app_dir.name
        pg_container = f"{slug}-postgres"
        db_user = _read_env_var(env_file, "POSTGRES_USER", "app_user")
        db_name = _read_env_var(env_file, "POSTGRES_DB", "app_db")
        api_port = _read_env_var(env_file, "API_PORT", "")
        frontend_port = _read_env_var(env_file, "FRONTEND_PORT", "")

        apps.append({
            "slug": slug,
            "pg_container": pg_container,
            "db_user": db_user,
            "db_name": db_name,
            "api_port": api_port,
            "frontend_port": frontend_port,
            "env_file": str(env_file),
        })
    return apps


def _read_env_var(env_file: Path, key: str, default: str = "") -> str:
    try:
        for line in env_file.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return default


def _pg_dump(container: str, user: str, db: str, output: Path) -> bool:
    """Dump une base PostgreSQL dans un fichier."""
    cmd = [
        "docker", "exec", container,
        "pg_dump", "-U", user, "-d", db,
        "--format=custom", "--no-owner", "--no-acl",
    ]
    try:
        with open(output, "wb") as f:
            subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, check=True)
        size = output.stat().st_size
        print(f"  ✅ {container}:{db} → {output.name} ({size:,} o)")
        return True
    except subprocess.CalledProcessError as e:
        print(f"  ❌ {container}:{db}: {e.stderr.strip()}")
        return False


def _collect_hub_env() -> Optional[Path]:
    """Retourne le .env du Hub s'il existe."""
    env_file = HUB_DIR / ".env"
    return env_file if env_file.exists() else None


def _collect_hub_secrets(work_dir: Path) -> list[Path]:
    """Copie les secrets Hub dans le dossier de travail."""
    secrets = []
    dest = work_dir / "secrets"
    dest.mkdir(exist_ok=True)

    for src in [HUB_JWT_PRIVATE, HUB_JWT_PUBLIC, HUB_FERNET_KEY]:
        if src.exists():
            shutil.copy2(src, dest / src.name)
            secrets.append(dest / src.name)

    return secrets


def export_park(app_slug: Optional[str] = None) -> Path:
    """
    Exporte tout le parc (ou une app spécifique) dans une archive tar.gz.

    Retourne le chemin de l'archive créée.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    archive_name = f"park-{timestamp}.tar.gz"
    archive_path = EXPORT_DIR / archive_name
    EXPORT_DIR.mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="park-export-") as tmp:
        work = Path(tmp)
        db_dir = work / "databases"
        env_dir = work / "envs"
        secrets_dir = work / "secrets"
        db_dir.mkdir()
        env_dir.mkdir()
        secrets_dir.mkdir()

        print("📦 Park Export — POULIDOR\n")

        # ── Apps ──
        apps = _detect_apps()
        if app_slug:
            apps = [a for a in apps if a["slug"] == app_slug]
            if not apps:
                print(f"❌ App '{app_slug}' introuvable dans APPS/")
                raise SystemExit(1)

        exported_apps = []
        for app in apps:
            print(f"📂 {app['slug']}")
            # DB dump
            if not _pg_dump(app["pg_container"], app["db_user"], app["db_name"],
                            db_dir / f"{app['slug']}.pgdump"):
                print(f"  ⚠️  DB dump échoué pour {app['slug']}, ignoré")
                continue

            # .env
            env_src = Path(app["env_file"])
            if env_src.exists():
                shutil.copy2(env_src, env_dir / f"{app['slug']}.env")

            exported_apps.append({
                "slug": app["slug"],
                "api_port": app["api_port"],
                "frontend_port": app["frontend_port"],
                "db_name": app["db_name"],
                "db_user": app["db_user"],
                "pgdump": f"databases/{app['slug']}.pgdump",
                "env": f"envs/{app['slug']}.env",
            })

        # ── Hub ──
        print("🏠 Hub")
        if not _pg_dump(HUB_DB_CONTAINER, HUB_DB_USER, HUB_DB_NAME,
                         db_dir / "hub.pgdump"):
            print("  ⚠️  Hub DB dump échoué")

        hub_env = _collect_hub_env()
        if hub_env:
            shutil.copy2(hub_env, env_dir / "HUB_base.env")

        _collect_hub_secrets(work)

        # ── Manifest ──
        manifest = {
            "version": "1.0",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "hub_db": {
                "pgdump": "databases/hub.pgdump",
                "env": "envs/HUB_base.env",
            },
            "apps": exported_apps,
        }
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2))

        # ── Archive ──
        print(f"\n📦 Création de l'archive {archive_name}...")
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(work, arcname="")

        size_mb = archive_path.stat().st_size / (1024 * 1024)
        print(f"✅ Export terminé : {archive_path} ({size_mb:.1f} Mo)")
        return archive_path
