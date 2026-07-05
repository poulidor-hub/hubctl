"""
Park Restore — restauration d'une archive park-export sur un Hub VIDE.
Ne fonctionne QUE si le Hub n'est pas initialisé (pas de données en DB).

Usage :
    python -m hubctl_cli park-restore park-20260705-120000.tar.gz
"""

from __future__ import annotations
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from hubctl_cli.hub_state import PARC_ROOT, HUB_DIR, APPS_DIR

HUB_DB_CONTAINER = "hub-db"
HUB_DB_USER = "hubadmin"
HUB_DB_NAME = "hub_auth"


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _check_hub_is_empty() -> bool:
    """Vérifie que le Hub n'a PAS encore été initialisé (DB vide)."""
    # Vérifier si le conteneur existe
    result = _run(["docker", "ps", "-a", "--filter", f"name={HUB_DB_CONTAINER}",
                   "--format", "{{.Status}}"], check=False)
    status = result.stdout.strip()
    if HUB_DB_CONTAINER not in result.stdout or not status:
        return True  # Pas de conteneur → vide

    if status.startswith("Up"):
        # Conteneur tourne, vérifier s'il y a des données
        try:
            r = _run(["docker", "exec", HUB_DB_CONTAINER,
                      "psql", "-U", HUB_DB_USER, "-d", HUB_DB_NAME,
                      "-tAc",
                      "SELECT COUNT(*) FROM information_schema.tables "
                      "WHERE table_schema='public' AND table_name='users'"],
                     check=False)
            if r.returncode == 0 and r.stdout.strip() == "0":
                return True  # Table users vide ou inexistante → Hub non initialisé
            return False  # Table users a des données → Hub initialisé
        except Exception:
            pass

    # Conteneur arrêté : on ne peut pas interroger la DB → on suppose vierge.
    return True


def _wait_db_ready(container: str, timeout: int = 90) -> bool:
    """Attend que le conteneur Postgres accepte les connexions."""
    import time
    elapsed = 0
    while elapsed < timeout:
        r = _run(["docker", "exec", container, "pg_isready", "-U", HUB_DB_USER],
                 check=False)
        if r.returncode == 0:
            return True
        time.sleep(3)
        elapsed += 3
    return False


def _restore_pgdump(container: str, user: str, db: str, dump_path: Path) -> bool:
    """Restaure un pg_dump dans un conteneur PostgreSQL."""
    if not dump_path.exists():
        print(f"  ⚠️  Fichier introuvable: {dump_path}")
        return False

    cmd = [
        "docker", "exec", "-i", container,
        "pg_restore", "-U", user, "-d", db,
        "--clean", "--if-exists", "--no-owner", "--no-acl",
    ]
    try:
        with open(dump_path, "rb") as f:
            subprocess.run(cmd, stdin=f, stderr=subprocess.PIPE, check=True)
        print(f"  ✅ {container}:{db} restauré")
        return True
    except subprocess.CalledProcessError as e:
        print(f"  ❌ {container}:{db}: {e.stderr.strip()}")
        return False


def _restore_tree(src: Path, dest: Path) -> list[tuple[str, bool]]:
    """Copie récursivement src/* vers dest/ sans écraser les fichiers existants.

    Retourne la liste (relpath, created) pour journalisation. Crée dest si absent.
    """
    dest.mkdir(parents=True, exist_ok=True)
    report: list[tuple[str, bool]] = []
    if not src.exists():
        return report
    for f in sorted(src.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(src)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            report.append((rel.as_posix(), False))
        else:
            shutil.copy2(f, target)
            report.append((rel.as_posix(), True))
    return report


def restore_park(archive_path: str) -> bool:
    """
    Restaure tout le parc depuis une archive park-export.
    Ne fonctionne que sur un Hub VIDE.
    """
    archive = Path(archive_path)
    if not archive.exists():
        print(f"❌ Archive introuvable : {archive_path}")
        return False

    print("🔍 Vérification de l'état du Hub...")
    if not _check_hub_is_empty():
        print("\n❌ ERREUR : Le Hub est déjà initialisé (données présentes dans la DB).")
        print("   park-restore ne peut être utilisé QUE sur un Hub vierge.")
        print("   Consultez le guide de migration pour un Hub existant.")
        return False
    print("   ✅ Hub vierge — restauration autorisée.\n")

    with tempfile.TemporaryDirectory(prefix="park-restore-") as tmp:
        work = Path(tmp)

        print(f"📦 Extraction de {archive.name}...")
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(work)

        manifest_file = work / "manifest.json"
        if not manifest_file.exists():
            print("❌ Archive invalide : manifest.json introuvable")
            return False

        manifest = json.loads(manifest_file.read_text())
        print(f"   Version: {manifest.get('version')}, "
              f"Exporté le: {manifest.get('exported_at')}")
        print(f"   Apps: {len(manifest.get('apps', []))}\n")

        # ── Hub secrets (avant compose up : nécessaires au démarrage) ──
        secrets_dir = work / "secrets"
        hub_secrets_dest = HUB_DIR / "secrets"
        hub_secrets_dest.mkdir(exist_ok=True)
        if secrets_dir.exists():
            print("🔐 Restauration secrets Hub...")
            for rel, created in _restore_tree(secrets_dir, hub_secrets_dest):
                mark = "✅" if created else "⚠️"
                suffix = "" if created else " existe déjà, ignoré"
                print(f"  {mark} {rel}{suffix}")

        # ── Hub .env (avant compose up) ──
        hub_env = work / "envs" / "HUB_base.env"
        hub_env_dest = HUB_DIR / ".env"
        if hub_env.exists():
            if not hub_env_dest.exists():
                shutil.copy2(hub_env, hub_env_dest)
                print(f"  ✅ HUB_base/.env restauré")
            else:
                print(f"  ⚠️  HUB_base/.env existe déjà, ignoré")

        # ── Hub docker compose up (AVANT le restore DB : pg_restore exige un conteneur lancé) ──
        print("\n🚀 Démarrage du Hub...")
        try:
            _run(["docker", "compose", "-f", str(HUB_DIR / "docker-compose.yml"),
                  "up", "-d"])
            print("   ✅ Hub démarré")
        except subprocess.CalledProcessError:
            print("   ❌ Échec démarrage Hub — abandon.")
            return False

        # ── Hub DB : attendre que Postgres accepte les connexions ──
        print("⏳ Attente que hub-db soit prêt...")
        if not _wait_db_ready(HUB_DB_CONTAINER, timeout=90):
            print("   ❌ hub-db injoignable après 90s — abandon.")
            return False

        # ── Hub DB : pg_restore (maintenant que le conteneur tourne) ──
        hub_db = manifest.get("hub_db", {})
        hub_pgdump = work / hub_db.get("pgdump", "databases/hub.pgdump")
        print("🏠 Restauration Hub DB...")
        if not _restore_pgdump(HUB_DB_CONTAINER, HUB_DB_USER, HUB_DB_NAME, hub_pgdump):
            print("   ❌ Restauration Hub DB échouée — abandon.")
            return False

        # ── Apps ──
        apps = manifest.get("apps", [])
        if not apps:
            print("\n✅ Aucune app à restaurer.")
            return True

        print(f"\n📱 Restauration de {len(apps)} application(s)...")
        failed_apps: list[str] = []
        for app in apps:
            slug = app["slug"]
            app_dir = APPS_DIR / slug
            print(f"\n📂 {slug}")

            if not app_dir.exists():
                print(f"  ⚠️  Dossier APPS/{slug} introuvable — clonez le dépôt git d'abord")
                failed_apps.append(slug)
                continue

            # DB
            pgdump = work / app.get("pgdump", f"databases/{slug}.pgdump")
            container = f"{slug}-postgres"
            db_user = app.get("db_user", "app_user")
            db_name = app.get("db_name", "app_db")
            if not _restore_pgdump(container, db_user, db_name, pgdump):
                failed_apps.append(slug)

            # .env
            env_src = work / "envs" / f"{slug}.env"
            env_dest = app_dir / ".env"
            if env_src.exists() and not env_dest.exists():
                shutil.copy2(env_src, env_dest)
                print(f"  ✅ .env restauré")

            # Docker compose up
            compose_file = app_dir / "docker-compose.yml"
            if compose_file.exists():
                try:
                    _run(["docker", "compose", "-f", str(compose_file),
                          "up", "-d"])
                    print(f"  ✅ {slug} démarré")
                except subprocess.CalledProcessError as e:
                    print(f"  ⚠️  Échec démarrage {slug}: {e}")
                    failed_apps.append(slug)

        if failed_apps:
            print(f"\n⚠️ Restauration terminée avec {len(failed_apps)} app(s) en échec : {', '.join(failed_apps)}")
            return False
        print(f"\n✅ Restauration terminée.")
        return True
