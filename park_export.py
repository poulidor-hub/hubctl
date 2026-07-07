"""
Park Export — sauvegarde complète du parc POULIDOR.
Produit une archive tar.gz contenant : DB dumps, .env, secrets Hub, manifeste.

Usage :
    python -m hubctl_cli park-export                     # tout le parc
    python -m hubctl_cli park-export --app exemple-apps  # une seule app
"""

from __future__ import annotations
import getpass
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from hubctl_cli.hub_state import PARC_ROOT, HUB_DIR, APPS_DIR
from hubctl_cli.coffre import derive_key

ARCHIVE_MAGIC = b"PARKEV1"


class WrongPassphraseError(Exception):
    """Levée quand la passphrase de déchiffrement d'archive est incorrecte."""


def encrypt_archive(tar_bytes: bytes, passphrase: str) -> bytes:
    """Chiffre un blob tar.gz avec une passphrase (Fernet + scrypt).

    Format : ARCHIVE_MAGIC (7) + salt (16) + ciphertext.
    Le salt est aléatoire à chaque appel → blobs différents pour la même entrée.
    """
    salt = os.urandom(16)
    key = derive_key(passphrase, salt)
    ciphertext = Fernet(key).encrypt(tar_bytes)
    return ARCHIVE_MAGIC + salt + ciphertext


def decrypt_archive(blob: bytes, passphrase: str) -> bytes:
    """Déchiffre un blob produit par encrypt_archive.

    Lève WrongPassphraseError si la passphrase est incorrecte.
    Lève ValueError si le blob n'a pas le magic attendu.
    """
    if not blob.startswith(ARCHIVE_MAGIC):
        raise ValueError("Blob sans magic PARKEV1 — pas une archive chiffrée")
    salt = blob[len(ARCHIVE_MAGIC):len(ARCHIVE_MAGIC) + 16]
    ciphertext = blob[len(ARCHIVE_MAGIC) + 16:]
    try:
        return Fernet(derive_key(passphrase, salt)).decrypt(ciphertext)
    except InvalidToken:
        raise WrongPassphraseError("Passphrase d'archive incorrecte")


def is_encrypted_archive(path) -> bool:
    """Détecte si un fichier (Path) ou des bytes sont une archive chiffrée PARKEV1."""
    if isinstance(path, (bytes, bytearray)):
        return bytes(path).startswith(ARCHIVE_MAGIC)
    data = Path(path).read_bytes()[:len(ARCHIVE_MAGIC) + 1]
    return data.startswith(ARCHIVE_MAGIC)


EXPORT_DIR = PARC_ROOT / "exports"

# Conteneurs PostgreSQL du Hub (nom fixe, pas suffixé par slug)
HUB_DB_CONTAINER = "hub-db"
HUB_DB_USER = "hubadmin"
HUB_DB_NAME = "hub_auth"

HUB_SECRETS_DIR = HUB_DIR / "secrets"


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
    """Copie récursivement tous les secrets Hub dans le dossier de travail.

    Capture l'intégralité de HUB_base/secrets/ (clés JWT, clés de backup,
    service_accounts/) en préservant l'arborescence relative. La clé fernet
    vit dans .env (SECRETS_ENCRYPTION_KEY), déjà exportée séparément.
    """
    dest = work_dir / "secrets"
    dest.mkdir(parents=True, exist_ok=True)
    collected: list[Path] = []
    if not HUB_SECRETS_DIR.exists():
        return collected
    for src in sorted(HUB_SECRETS_DIR.rglob("*")):
        if not src.is_file() or src.is_symlink():
            continue
        rel = src.relative_to(HUB_SECRETS_DIR)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        collected.append(target)
    return collected


def export_park(app_slug: Optional[str] = None, *, encrypt: bool = True,
                passphrase: Optional[str] = None) -> Path:
    """
    Exporte tout le parc (ou une app spécifique) dans une archive tar.gz.

    Par défaut (encrypt=True), produit une archive chiffrée park-<ts>.tar.gz.enc.
    Avec encrypt=False, produit une archive legacy park-<ts>.tar.gz (secrets en clair).

    Retourne le chemin de l'archive créée.
    """
    # Résolution de la passphrase si chiffrement demandé
    if encrypt:
        if passphrase is None:
            import sys
            if sys.stdin.isatty():
                passphrase = getpass.getpass("🔐 Passphrase d'archive : ")
                confirm = getpass.getpass("🔐 Confirmer : ")
                if passphrase != confirm:
                    print("❌ Les passphrases ne correspondent pas.")
                    raise SystemExit(1)
            else:
                print("❌ Aucune passphrase fournie (stdin non-TTY). "
                      "Utilisez --passphrase-stdin ou un TTY interactif.")
                raise SystemExit(1)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
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
        # Construire le tar.gz en mémoire (sera chiffré ou écrit tel quel)
        import io
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(work, arcname="")
        tar_bytes = buf.getvalue()

        if encrypt:
            print(f"\n📦 Chiffrement de l'archive (Fernet + scrypt)...")
            blob = encrypt_archive(tar_bytes, passphrase)
            archive_path = EXPORT_DIR / f"park-{timestamp}.tar.gz.enc"
            archive_path.write_bytes(blob)
            print(f"🔒 Archive chiffrée : {archive_path}")
        else:
            print(f"\n⚠️  Archive NON chiffrée (mode --no-encrypt) — secrets en clair.")
            archive_path = EXPORT_DIR / f"park-{timestamp}.tar.gz"
            archive_path.write_bytes(tar_bytes)
            print(f"📦 Archive : {archive_path}")

        # Restreindre les permissions (0600) même en mode non chiffré
        import os as _os
        _os.chmod(archive_path, 0o600)

        size_mb = archive_path.stat().st_size / (1024 * 1024)
        print(f"✅ Export terminé : {archive_path} ({size_mb:.1f} Mo)")
        return archive_path
