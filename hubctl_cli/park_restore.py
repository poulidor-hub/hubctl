"""
Park Restore — restauration d'une archive park-export sur un Hub VIDE.
Ne fonctionne QUE si le Hub n'est pas initialisé (pas de données en DB).

Usage :
    python -m hubctl_cli park-restore park-20260705-120000.tar.gz
"""

from __future__ import annotations
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from hubctl_cli.hub_state import PARC_ROOT, HUB_DIR, APPS_DIR
from hubctl_cli.park_export import is_encrypted_archive, decrypt_archive, WrongPassphraseError

HUB_DB_CONTAINER = "hub-db"


def _safe_extractall(tar: tarfile.TarFile, dest: Path) -> bool:
    """Extrait une archive de façon sûre.

    - Compat Python 3.10/3.11 : `extractall(filter=)` n'existe qu'en 3.12+.
    - Sur < 3.12 (pas de filtre natif), on valide manuellement chaque membre
      (anti path-traversal + rejet des symlinks) avant extraction.
    Retourne True si extraction OK, False si une entrée est refusée.
    """
    dest_resolved = dest.resolve()
    for m in tar.getmembers():
        target = (dest / m.name).resolve()
        try:
            inside = target.is_relative_to(dest_resolved)
        except (ValueError, OSError):
            inside = False
        if not inside:
            print(f"❌ Entrée d'archive suspecte (path traversal) : {m.name}")
            return False
        if m.issym() or m.islnk():
            print(f"❌ Lien symbolique interdit dans l'archive : {m.name}")
            return False
    if sys.version_info >= (3, 12):
        tar.extractall(dest, filter="data")
    else:
        tar.extractall(dest)
    return True
HUB_DB_USER = "hubadmin"
HUB_DB_NAME = "hub_auth"


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _check_hub_is_empty() -> bool:
    """Vérifie que le Hub n'a PAS encore été initialisé (DB vide).

    Retourne True uniquement si :
    - Le conteneur hub-db n'existe pas, OU
    - Le conteneur tourne ET la table users n'existe pas / est vide.

    Refuse (False) dans tous les autres cas, y compris conteneur arrêté
    (on ne peut pas vérifier → safe default).
    """
    # Vérifier si le conteneur existe (et son statut)
    result = _run(["docker", "ps", "-a", "--filter", f"name={HUB_DB_CONTAINER}",
                   "--format", "{{.Status}}"], check=False)
    status = result.stdout.strip()

    if not status:
        return True  # Pas de conteneur → Hub vierge

    if status.startswith("Up"):
        # Conteneur tourne, vérifier s'il y a des données
        try:
            r = _run(["docker", "exec", HUB_DB_CONTAINER,
                      "psql", "-U", HUB_DB_USER, "-d", HUB_DB_NAME,
                      "-tAc",
                      "SELECT COUNT(*) FROM users"],
                     check=False)
            # 0 = table vide ou inexistante → Hub non initialisé
            if r.returncode == 0 and r.stdout.strip() in ("0", ""):
                return True  # Table users vide ou inexistante → Hub non initialisé
            return False  # Table users a des données → Hub initialisé
        except Exception as e:
            print(f"⚠️  Impossible d'interroger la DB pour vérifier l'état du Hub : {e}",
                  file=sys.stderr)

    # Conteneur arrêté, ou requête DB échouée : on refuse (safe default)
    return False


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


# Migrations « fourchues » : même liste que HUB_base/hub/app/restore_snapshot.py
# (FORKED_MIGRATIONS). Un dump hub legacy (ex. hub-legacy 3.9.x) porte un numéro 027
# avec un SQL différent du nôtre (normalisation de données au lieu de créer
# port_allocations). Sans purge, le boot du hub neuf sauterait NOTRE 027 → table
# port_allocations manquante → hub cassé. Cf. docs/handover/2026-09-20-migration-legacy-hub-neuf-design.md §5.
FORKED_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("027", "port_allocations"),
)


def _reconcile_forked_migrations(container: str = HUB_DB_CONTAINER) -> bool:
    """Purge les lignes schema_migrations « fourchues » après un pg_restore legacy.

    Pour chaque (version, table) de FORKED_MIGRATIONS : si la ligne version est
    présente mais que la table attendue n'existe pas, on supprime la ligne → le
    boot du hub rejouera notre migration (idempotente).

    Retourne True si OK (purge ou no-op), False si la requête psql échoue.
    Non fatal pour restore_park (warning) — le runbook vérifie la table au boot.
    """
    ok = True
    for version, table in FORKED_MIGRATIONS:
        sql = (
            f"DELETE FROM schema_migrations WHERE version = '{version}' "
            f"AND NOT EXISTS (SELECT 1 FROM information_schema.tables "
            f"WHERE table_name = '{table}');"
        )
        r = _run(["docker", "exec", container, "psql", "-U", HUB_DB_USER,
                  "-d", HUB_DB_NAME, "-c", sql], check=False)
        if r.returncode != 0:
            print(f"  ⚠️  Réconciliation migration {version} échouée : {r.stderr.strip()}")
            print("      → Vérifier manuellement au boot du hub "
                  "(SELECT count(*) FROM port_allocations).")
            ok = False
        elif "DELETE 1" in r.stdout:
            print(f"  ✅ Migration {version} fourchue purgée — le boot rejugera la nôtre")
        else:
            print(f"  ✅ Migration {version} : rien à réconcilier")
    return ok


def _reset_hub_db(container: str = HUB_DB_CONTAINER) -> bool:
    """Recrée la base hub VIERGE (DROP WITH (FORCE) + CREATE) avant le pg_restore.

    Le pg_restore --clean ne suffit pas face à un dump legacy : les tables du
    schéma courant (créées au boot du hub neuf) absentes du dump bloquent les
    DROP (FKs croisées) et les COPY legacy échouent sur les colonnes récentes
    NOT NULL. Réinitialiser la base garantit que le schéma restauré est
    exactement celui du dump ; les migrations du hub rejouent ensuite le delta
    (cf. _reconcile_forked_migrations + apply_migrations au boot).
    """
    # Deux -c distincts : psql exécute plusieurs -c HORS transaction, mais un
    # seul -c multi-statements dans UNE transaction — et DROP DATABASE est
    # interdit dans un bloc transactionnel (prouvé en répétition VM 2026-09-21).
    r = _run(["docker", "exec", container, "psql", "-U", HUB_DB_USER,
              "-d", "postgres",
              "-c", f"DROP DATABASE IF EXISTS {HUB_DB_NAME} WITH (FORCE);",
              "-c", f"CREATE DATABASE {HUB_DB_NAME} OWNER {HUB_DB_USER};"],
             check=False)
    if r.returncode != 0:
        print(f"  ⚠️  Reset de la base hub échoué : {r.stderr.strip()}")
        return False
    print("  ✅ Base hub réinitialisée — le dump restauré fait foi, "
          "les migrations rejoueront le delta au boot")
    return True


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
        # Protection traversée : refuser les chemins qui sortent de dest
        if not target.resolve().is_relative_to(dest.resolve()):
            print(f"⚠️  Membre rejeté (hors destination) : {rel.as_posix()}", file=sys.stderr)
            report.append((rel.as_posix(), False))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            report.append((rel.as_posix(), False))
        else:
            shutil.copy2(f, target)
            report.append((rel.as_posix(), True))
    return report


def restore_park(archive_path: str, passphrase: str | None = None) -> bool:
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

    # Détection du format : chiffré (PARKEV1) ou legacy (tar.gz en clair)
    encrypted = is_encrypted_archive(archive)
    if encrypted:
        if passphrase is None:
            import sys
            if sys.stdin.isatty():
                import getpass
                passphrase = getpass.getpass("🔐 Passphrase d'archive : ")
            else:
                print("❌ Archive chiffrée mais aucune passphrase (stdin non-TTY).")
                print("   Utilisez --passphrase-stdin.")
                return False
        print(f"🔓 Déchiffrement de {archive.name}...")
        try:
            tar_bytes = decrypt_archive(archive.read_bytes(), passphrase)
        except WrongPassphraseError:
            print("❌ Passphrase d'archive incorrecte.")
            return False
    else:
        print(f"⚠️  Archive non chiffrée (legacy) : {archive.name}")
        tar_bytes = archive.read_bytes()

    with tempfile.TemporaryDirectory(prefix="park-restore-") as tmp:
        work = Path(tmp)

        print(f"📦 Extraction...")
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
            if not _safe_extractall(tar, work):
                print("❌ Restauration annulée : archive non sûre.")
                return False

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

        # ── Reset de la base hub (le dump restauré fait foi — cf. dump legacy) ──
        if not _reset_hub_db(HUB_DB_CONTAINER):
            print("   ❌ Impossible de réinitialiser la DB hub — abandon.")
            return False

        # ── Hub DB : pg_restore (maintenant que le conteneur tourne) ──
        hub_db = manifest.get("hub_db", {})
        hub_pgdump = work / hub_db.get("pgdump", "databases/hub.pgdump")
        print("🏠 Restauration Hub DB...")
        if not _restore_pgdump(HUB_DB_CONTAINER, HUB_DB_USER, HUB_DB_NAME, hub_pgdump):
            print("   ❌ Restauration Hub DB échouée — abandon.")
            return False

        # ── Réconciliation migrations fourchues (dump legacy → hub neuf) ──
        if not _reconcile_forked_migrations(HUB_DB_CONTAINER):
            print("   ⚠️  Réconciliation incomplète — vérifier le boot du hub "
                  "(table port_allocations).")

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
