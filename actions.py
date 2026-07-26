from __future__ import annotations
import datetime
import json
import os
import shutil
import socket
import subprocess
import tarfile
import time
from pathlib import Path

from rich.console import Console
from rich.table import Table

from hubctl_cli import hub_state, utils, env_manager

import string as _string_module

def _detect_host_ip() -> str:
    """Détecte l'IP hôte qui sera utilisée dans le .env (HUB_HOST)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def _generate_dotenv(console, *, force: bool = False, dry_run: bool = False) -> dict | None:
    """Génère HUB_base/.env avec des secrets aléatoires.

    Retourne un dict avec admin_username, admin_password, host_ip,
    ou None si le .env existe déjà et que l'utilisateur refuse de l'écraser.
    """
    import secrets as _secrets
    env_path = hub_state.HUB_DIR / ".env"

    # ── Vérifier si .env existe déjà ──
    if env_path.exists() and not force:
        import sys as _sys
        if not (_sys.stdin.isatty() and _sys.stdout.isatty()):
            console.print(
                "[red]✗ .env existe déjà et mode non interactif — "
                "utilisez --force pour écraser.[/red]"
            )
            return None
        # Prompt interactif
        console.print("[yellow]⚠ .env existe déjà.[/yellow]")
        try:
            answer = input("   Écraser le .env existant ? (o/N) : ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("[dim]Abandon.[/dim]")
            return None
        if answer not in ("o", "oui", "y", "yes"):
            console.print("[dim].env conservé.[/dim]")
            return None

    if dry_run:
        pwd = "DRY_RUN_PWD_" + _secrets.token_hex(8)
    else:
        alphabet = _string_module.ascii_letters + _string_module.digits
        pwd = "".join(_secrets.choice(alphabet) for _ in range(16))

    import socket as _socket
    host_ip = _detect_host_ip()
    # Générer la Fernet key pour le chiffrement des secrets
    from cryptography.fernet import Fernet as _Fernet
    fernet_key = _Fernet.generate_key().decode() if not dry_run else "DRY_RUN_FERNET_KEY"

    # HOST_UID : uid de l'utilisateur qui exécute hubctl (pour les volumes montés)
    host_uid = str(os.getuid()) if not dry_run else "1000"

    lines = f"""# Hub POULIDOR — généré automatiquement par hubctl init
# {__import__('datetime').datetime.now().isoformat()}

# PostgreSQL
POSTGRES_USER=hubadmin
POSTGRES_PASSWORD={pwd}
POSTGRES_DB=hub_auth

# Hub API
DATABASE_URL=postgresql+asyncpg://hubadmin:{pwd}@db:5432/hub_auth
JWT_ALGORITHM=RS256
APP_ENV=production
ACCESS_TOKEN_EXPIRE_MINUTES=30

# Admin par défaut
ADMIN_USERNAME=admin
ADMIN_PASSWORD={pwd}
ADMIN_EMAIL=admin@local.lan

# Ports
HUB_API_PORT=8000
HUB_HOST={host_ip}
HUB_FRONTEND_PORT=80
HUB_HTTPS_PORT=443

# Clés JWT
AUTO_GENERATE_KEYS=true

# Chiffrement secrets
SECRETS_ENCRYPTION_KEY={fernet_key}
ALLOW_PLAINTEXT_SECRETS=false
SKIP_PASSWORD_CHECK=false

# Docker
DOCKER_SYNC_INTERVAL=300
DOCKER_SCAN_ENABLED=false
HOST_UID={host_uid}

# Audit
AUDIT_SSE_COOLDOWN=5.0
AUDIT_RETENTION_DAYS=90
ERROR_RETENTION_DAYS=30
LOG_PURGE_INTERVAL_HOURS=24

# CORS
ALLOWED_ORIGINS=https://hub.local,https://{host_ip}
ALLOWED_ORIGIN_REGEX=

# Whitelist réseaux
APP_WHITELIST_NETWORKS=10.42.0.0/16,192.168.1.0/24

# PKI / HTTPS
HUB_HTTPS_ENABLED=true
PKI_CA_COMMON_NAME=Hub Local CA
PKI_CA_ORGANIZATION=Hub Internal
PKI_CA_VALIDITY_YEARS=10
PKI_CERT_DOMAINS=localhost,hub.local,hub-frontend
PKI_CERT_IPS=
PKI_CERT_VALIDITY_DAYS=365
PKI_KEY_SIZE=4096

# Ports apps
APP_PORT_API_START=9000
APP_PORT_API_END=9499
APP_PORT_FRONTEND_START=4200
APP_PORT_FRONTEND_END=4699
PORT_SCAN_DOCKER_ENABLED=true

# Divers
FORCE_REPLACE_APP=false
LEGACY_COMPAT_ENABLED=false
"""

    if not dry_run:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(lines)
        env_path.chmod(0o600)

    return {
        "admin_username": "admin",
        "admin_password": pwd,
        "host_ip": host_ip,
    }


def _cleanup_existing_hub_containers(console=None, *, dry_run: bool = False) -> None:
    """Arrête et supprime les conteneurs Hub existants avant un démarrage propre.

    D'abord ``docker compose down`` (gracieux, préserve les volumes), puis
    ``docker rm -f`` pour les orphelins issus d'un autre projet compose qui
    partagent les mêmes noms (hub-db, hub-api, hub-frontend).
    """
    con = console or _console
    if dry_run:
        con.print("[dim](dry-run) docker compose down + cleanup orphelins[/dim]")
        return

    compose_file = hub_state.HUB_DIR / "docker-compose.yml"

    # 1. docker compose down (gracieux, préserve les volumes)
    con.print("[dim]Nettoyage des conteneurs existants...[/dim]")
    try:
        subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "down"],
            capture_output=True, text=True, timeout=30,
            cwd=str(hub_state.HUB_DIR),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass  # Non bloquant — on essaie le rm -f ci-dessous

    # 2. Force-remove des conteneurs orphelins par nom
    for name in ("hub-db", "hub-api", "hub-frontend"):
        try:
            r = subprocess.run(
                ["docker", "inspect", name],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                con.print(f"[dim]Suppression du conteneur orphelin : {name}[/dim]")
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass


def _read_hub_env_value(key: str, default: str = "") -> str:
    """Lit une valeur depuis HUB_base/.env (parsing simple KEY=VALUE)."""
    env_path = hub_state.HUB_DIR / ".env"
    try:
        for line in env_path.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return default


def _sync_db_password_if_needed(console=None, *, dry_run: bool = False) -> None:
    """Synchronise le mot de passe PostgreSQL du volume avec le .env actuel.

    PostgreSQL n'honore ``POSTGRES_PASSWORD`` que lors de la première
    initialisation du volume. Si le .env a été régénéré (``--force``) avec un
    nouveau mot de passe alors que le volume existe déjà, le hub-api obtiendra
    une ``InvalidPasswordError``. Cette fonction détecte ce cas et resync le
    mot de passe via ``ALTER USER`` (non-destructif — préserve les données).

    Skip si le volume n'existe pas (fresh install) ou si le conteneur DB
    est introuvable.
    """
    con = console or _console
    if dry_run:
        con.print("[dim](dry-run) Sync mot de passe DB si volume existant[/dim]")
        return

    compose_file = hub_state.HUB_DIR / "docker-compose.yml"

    # 1. Vérifier si le volume PostgreSQL existe déjà
    vol_check = subprocess.run(
        ["docker", "volume", "ls", "--format", "{{.Name}}"],
        capture_output=True, text=True, timeout=10,
    )
    existing_volumes = vol_check.stdout.strip().splitlines() if vol_check.stdout.strip() else []
    pg_volume_name = "hub_base_postgres_data"
    if pg_volume_name not in existing_volumes:
        return  # Volume inexistant → fresh install, rien à sync

    # 2. Démarrer le DB seul (si pas déjà running)
    con.print("[dim]Volume DB existant détecté — sync du mot de passe...[/dim]")
    try:
        subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "up", "-d", "db"],
            capture_output=True, text=True, timeout=60,
            cwd=str(hub_state.HUB_DIR),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        con.print("[yellow]⚠ Impossible de démarrer le DB pour la sync. Le volume pourrait avoir un mot de passe périmé.[/yellow]")
        return

    # 3. Attendre que le DB soit sain (max 60s)
    for _ in range(30):
        health = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", "hub-db"],
            capture_output=True, text=True, timeout=5,
        )
        if "healthy" in health.stdout:
            break
        time.sleep(2)
    else:
        con.print("[yellow]⚠ DB non sain après 60s — sync du mot de passe annulée.[/yellow]")
        return

    # 4. Lire le mot de passe + user du .env et sync via ALTER USER
    pg_password = _read_hub_env_value("POSTGRES_PASSWORD")
    pg_user = _read_hub_env_value("POSTGRES_USER", "hubadmin")
    if not pg_password:
        con.print("[yellow]⚠ POSTGRES_PASSWORD introuvable dans le .env — sync annulée.[/yellow]")
        return

    # Connexion en tant que superuser POSTGRES_USER via trust auth locale.
    # Dans l'image Docker postgres, POSTGRES_USER crée le superuser (pas 'postgres').
    # Les connexions socket locales utilisent 'trust' (pas de mot de passe requis).
    # Le mot de passe contient des caractères spéciaux → on passe via stdin psql.
    alter_sql = f"ALTER USER {pg_user} WITH PASSWORD '{pg_password}';"
    result = subprocess.run(
        ["docker", "exec", "-i", "hub-db", "psql", "-U", pg_user, "-d", pg_user],
        input=alter_sql, capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 0:
        con.print("[green]✓ Mot de passe PostgreSQL synchronisé avec le .env[/green]")
    else:
        con.print(f"[yellow]⚠ Sync mot de passe DB échoué : {result.stderr.strip()[:200]}[/yellow]")
        con.print("[yellow]  Si le hub-api échoue avec InvalidPasswordError, supprimez le volume :[/yellow]")
        con.print(f"[yellow]  docker volume rm {pg_volume_name}[/yellow]")


def action_bootstrap(console, *, force: bool = False, dry_run: bool = False) -> int:
    """Bootstrap complet du Hub : .env → clés JWT → build+up → health → résumé.

    Point d'entrée de `hubctl init`.
    Retourne 0 si succès, 1 si échec.
    """
    con = console or _console

    # ── 1. Vérifier Docker ──
    if hub_state.check_docker() is False:
        con.print("[red]✗ Docker injoignable. Vérifiez que le daemon tourne.[/red]")
        return 1

    # ── 2. Générer le .env ──
    credentials = _generate_dotenv(con, force=force, dry_run=dry_run)
    if credentials is None:
        con.print("[red]✗ Impossible de générer le .env. Abandon.[/red]")
        return 1

    # ── 3. Générer les clés JWT ──
    _ensure_jwt_keys(con, dry_run=dry_run)

    # ── 4. Nettoyer les anciens conteneurs ──
    _cleanup_existing_hub_containers(con, dry_run=dry_run)

    # ── 4b. Sync mot de passe DB si le volume existe (évite InvalidPasswordError) ──
    _sync_db_password_if_needed(con, dry_run=dry_run)

    # ── 5. Build & démarrage ──
    if dry_run:
        con.print("[dim](dry-run) make -C HUB_base up-prod[/dim]")
    else:
        con.print("[bold]🔨 Build & démarrage du Hub...[/bold]")
        rc = hub_make("up-prod", dry_run=False, console=con)
        if rc != 0:
            con.print("[red]✗ Échec du build/démarrage du Hub.[/red]")
            return 1

    # ── 6. Health check ──
    if dry_run:
        con.print("[dim](dry-run) Attente health check...[/dim]")
    else:
        con.print("[bold]💚 Attente du health check...[/bold]")
        ok = _health_check("hub", console=con, timeout=120)
        if not ok:
            con.print("[red]✗ Le Hub ne répond pas après démarrage. Vérifiez `docker compose logs`.[/red]")
            return 1

    # ── 7. Résumé ──
    admin_username = credentials["admin_username"]
    host_ip = credentials["host_ip"]

    con.print("")
    con.print("[green bold]✅ Hub POULIDOR opérationnel ![/green bold]")
    con.print("")
    t = Table(title="Accès")
    t.add_column("Service", style="bold cyan")
    t.add_column("URL")
    t.add_row("Hub (HTTPS)", f"https://{host_ip}")
    t.add_row("Hub (HTTP)", f"http://{host_ip}:80")
    t.add_row("API", f"https://{host_ip}:8000")
    con.print(t)
    con.print("")
    con.print(f"[bold]Login :[/bold] {admin_username}")
    con.print(f"[bold]Mot de passe :[/bold] [dim]stocké dans {hub_state.HUB_DIR / '.env'} et dans le coffre[/dim]")
    con.print("")
    con.print("[dim yellow]⚠ Changez le mot de passe admin dès la première connexion.[/dim yellow]")

    return 0


def hub_make(target: str, *, dry_run: bool, console=None) -> int:
    return utils.run_shell(
        ["make", "-C", str(hub_state.HUB_DIR), target],
        dry_run=dry_run, console=console,
    )


def app_make(app_name: str, target: str, *, dry_run: bool, console=None, timeout: int = 120, apps_dir: str | None = None) -> int:
    base = Path(apps_dir) if apps_dir else hub_state.active_apps_dir()
    return utils.run_shell(
        ["make", "-C", str(base / app_name), target],
        dry_run=dry_run, console=console, timeout=timeout,
    )


def _ensure_jwt_keys(console=None, *, dry_run: bool = False) -> None:
    secrets_dir = hub_state.HUB_DIR / "secrets"
    priv = secrets_dir / "jwt_private_key.pem"
    pub = secrets_dir / "jwt_public_key.pem"
    if priv.exists() and pub.exists():
        return
    con = console or _console
    secrets_dir.mkdir(parents=True, exist_ok=True)
    con.print("[yellow]🔐 Clés JWT manquantes — génération…[/yellow]")
    if dry_run:
        con.print(f"[dim]$ openssl genrsa -out {priv} 4096 (dry-run)[/dim]")
        con.print(f"[dim]$ openssl rsa -in {priv} -pubout -out {pub} (dry-run)[/dim]")
        return
    rc = utils.run_shell(["openssl", "genrsa", "-out", str(priv), "4096"], console=con)
    if rc != 0:
        con.print("[red]✗ Échec génération clé privée JWT.[/red]")
        return
    rc = utils.run_shell(["openssl", "rsa", "-in", str(priv), "-pubout", "-out", str(pub)], console=con)
    if rc != 0:
        con.print("[red]✗ Échec génération clé publique JWT.[/red]")
        return
    utils.run_shell(["chmod", "600", str(priv)], console=con)
    con.print(f"[green]✓ Clés JWT générées dans {secrets_dir}[/green]")


def disk_guard(console=None) -> bool:
    con = console or _console
    d = hub_state.get_disk_usage()
    gb = d.free / 1024 ** 3
    if d.percent_free < 5:
        con.print(f"[red]⚠ Espace critique : {d.percent_free}% libre ({gb:.1f} Go). Abandon.[/red]")
        return False
    if d.percent_free < 15:
        con.print(f"[yellow]⚠ Espace faible : {d.percent_free}% libre ({gb:.1f} Go).[/yellow]")
    return True


def _db_snapshot(console=None) -> Path | None:
    """Take a PostgreSQL snapshot of hub-db. Returns path or None on failure."""
    con = console or _console
    backups = hub_state.PARC_ROOT / "hubctl_cli" / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = backups / f"snap_{stamp}.sql"
    try:
        subprocess.run(
            ["docker", "exec", "hub-db", "pg_dump", "-U", "hubadmin", "hub_auth"],
            stdout=open(path, "w"), stderr=subprocess.PIPE, timeout=30, check=True,
        )
        if path.exists():
            os.chmod(path, 0o600)
        con.print(f"[green]✓ Snapshot DB →[/green] {path.name}")
        return path
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        con.print(f"[yellow]⚠ Snapshot DB échoué : {e}[/yellow]")
        if path.exists():
            os.chmod(path, 0o600)  # sécuriser même le dump partiel
        return None


def _git_previous_commit(path: Path, console=None) -> str | None:
    """Return current HEAD hash before update. None if not a git repo."""
    con = console or _console
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _health_check(target: str, console=None, timeout: int = 60, port_override: str | None = None) -> bool:
    """Wait for hub API to be healthy. Returns True if healthy within timeout."""
    con = console or _console
    # Port API : HUB_API_PORT du .env Hub (défaut 8000) pour target "hub",
    # sinon API_PORT de l'app cible.
    if port_override is not None:
        api_port = port_override
    else:
        api_port = "8000"
        try:
            if target == "hub":
                env_path = hub_state.HUB_DIR / ".env"
                if env_path.exists():
                    api_port = env_manager.read_env(env_path).get("HUB_API_PORT", api_port)
            else:
                app_env = hub_state.active_apps_dir() / target / ".env"
                if app_env.exists():
                    api_port = env_manager.read_env(app_env).get("API_PORT", api_port)
        except Exception:
            pass
    health_url = f"http://localhost:{api_port}/api/health"
    elapsed = 0
    while elapsed < timeout:
        try:
            result = subprocess.run(
                ["curl", "-sk", health_url],
                capture_output=True, text=True, timeout=5,
            )
            if '"status":"healthy"' in result.stdout:
                con.print(f"[green]✓ Hub healthy après {elapsed}s[/green]")
                return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        time.sleep(3)
        elapsed += 3
        con.print(f"  ... attente {elapsed}s/{timeout}s")
    con.print(f"[yellow]⚠ Hub pas healthy après {timeout}s[/yellow]")
    return False


def _rollback(target: str, target_path: Path, prev_commit: str | None,
              snapshot: Path | None, console=None,
              db_container: str | None = None, db_name: str | None = None,
              dry_run: bool = False) -> None:
    """Rollback: revert git, restore DB snapshot, re-up."""
    con = console or _console
    con.print(f"[bold red]↩ Rollback de « {target} »[/bold red]")

    if prev_commit:
        con.print(f"  git reset → {prev_commit[:8]}")
        utils.run_shell(
            ["git", "-C", str(target_path), "reset", "--hard", prev_commit],
            dry_run=dry_run, console=con, timeout=15,
        )

    if snapshot and snapshot.exists():
        con.print(f"  restauration DB → {snapshot.name}")
        container = db_container or "hub-db"
        database = db_name or "hub_auth"
        if dry_run:
            con.print(f"[dim]$ docker exec -i {container} psql -U hubadmin {database} < {snapshot.name} (dry-run)[/dim]")
        else:
            subprocess.run(
                ["docker", "exec", "-i", container, "psql", "-U", "hubadmin", database],
                input=snapshot.read_bytes(), capture_output=True, timeout=30,
            )

    if target == "hub":
        utils.run_shell(["make", "-C", str(target_path), "build"],
                        dry_run=dry_run, console=con, timeout=600)
        rc = utils.run_shell(["make", "-C", str(target_path), "up-prod"],
                             dry_run=dry_run, console=con, timeout=60)
    else:
        utils.run_shell(["make", "-C", str(target_path), "build"],
                        dry_run=dry_run, console=con, timeout=600)
        rc = utils.run_shell(["make", "-C", str(target_path), "up"],
                             dry_run=dry_run, console=con, timeout=60)

    if rc != 0:
        con.print("[red]✗ Rollback incomplet — prod peut être DOWN. Intervention manuelle requise.[/red]")
    else:
        con.print("[green]✓ Rollback terminé[/green]")


def cleanup_orphan_tests(console=None) -> None:
    """Remove leftover -test docker-compose projects owned by hubctl.

    Ne nettoie QUE les projets {slug}-test où slug est le hub ou une app réelle
    (APPS/ ou APP_Workshop/). Les projets -test étrangers (load-test,
    integration-test, etc.) sont préservés — la règle d'or du parc est
    « jamais docker compose down -v » hors de notre périmètre.
    """
    con = console or _console
    try:
        result = subprocess.run(
            ["docker", "compose", "ls", "--format", "json"],
            capture_output=True, text=True, timeout=10,
        )
        projects = json.loads(result.stdout) if result.stdout.strip() else []

        # Whitelist des projets test légitimes (hubctl-owned)
        valid_test_projects: set[str] = {"hub-test"}
        for lister in (hub_state.get_apps, hub_state.get_workshop_apps):
            try:
                for app in lister():
                    valid_test_projects.add(f"{app.name}-test")
            except Exception:
                pass

        for p in (projects if isinstance(projects, list) else []):
            name = p.get("Name", "")
            if name.endswith("-test") and name in valid_test_projects:
                con.print(f"[dim]Nettoyage instance orpheline : {name}[/dim]")
                subprocess.run(
                    ["docker", "compose", "-p", name, "down", "-v"],
                    capture_output=True, timeout=30,
                )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass


def _workshop_ignore(src_dir: str, names: list[str]) -> set[str]:
    """Filtre les répertoires/fichiers à exclure lors de la copie atelier."""
    import fnmatch
    ignored: set[str] = set()
    for name in names:
        full = os.path.join(src_dir, name)
        if os.path.isdir(full) and name in _WORKSHOP_IGNORE_DIRS:
            ignored.add(name)
        elif os.path.isfile(full):
            for pat in _WS_IGNORE_NAMES:
                if fnmatch.fnmatch(name, pat):
                    ignored.add(name)
                    break
    return ignored


def _start_hub(console, *, dry_run: bool) -> bool:
    _ensure_jwt_keys(console, dry_run=dry_run)
    rc = hub_make("up-prod", dry_run=dry_run, console=console)
    if rc == 0:
        _health_check("hub", console=console, timeout=60)
    return rc == 0


def _start_app(console, app_name: str, *, dry_run: bool, apps_dir: str | None = None) -> bool:
    rc = app_make(app_name, "up", dry_run=dry_run, console=console, apps_dir=apps_dir)
    if rc == 0:
        _health_check(app_name, console=console, timeout=30)
    return rc == 0


def action_start_all(console=None, *, target: str = "all", dry_run: bool = False) -> None:
    """Démarre le Hub et toutes les apps actives en parallèle.

    Args:
        target: ``\"all\"`` ou ``\"prod\"`` → apps de APPS_DIR + Hub.
                ``\"dev\"`` → apps de WORKSHOP_DIR, **sans** le Hub.
    """
    if target not in ("all", "prod", "dev"):
        raise ValueError(f"target doit être 'all', 'prod' ou 'dev' (reçu : {target!r})")

    con = console or _console
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if target == "dev":
        apps = [a.name for a in hub_state.get_workshop_apps()]
        apps_dir = str(hub_state.WORKSHOP_DIR)
    else:
        apps = [a.name for a in hub_state.get_apps()]
        apps_dir = str(hub_state.APPS_DIR)

    if not apps:
        con.print("[dim]Aucune app active à démarrer.[/dim]")

    tasks: dict = {}
    with ThreadPoolExecutor(max_workers=max(1, len(apps) + 1)) as pool:
        if target in ("all", "prod"):
            con.print("[bold]→ Démarrage du Hub…[/bold]")
            tasks[pool.submit(_start_hub, con, dry_run=dry_run)] = "hub"

        for app_name in apps:
            con.print(f"[bold]→ Démarrage de « {app_name} »…[/bold]")
            tasks[pool.submit(_start_app, con, app_name, dry_run=dry_run, apps_dir=apps_dir)] = app_name

        ok, fail = 0, 0
        for future in as_completed(tasks):
            name = tasks[future]
            try:
                if future.result():
                    con.print(f"[green]✓ {name} démarré[/green]")
                    ok += 1
                else:
                    con.print(f"[red]✗ {name} échoué[/red]")
                    fail += 1
            except Exception as e:
                con.print(f"[red]✗ {name} erreur : {e}[/red]")
                fail += 1
        con.print(f"[bold]{ok} succès, {fail} échec(s)[/bold]")


def _stop_app(console, app_name: str, *, dry_run: bool, apps_dir: str | None = None) -> bool:
    rc = app_make(app_name, "down", dry_run=dry_run, console=console, apps_dir=apps_dir)
    return rc == 0


def action_stop_all(console=None, *, target: str = "all", dry_run: bool = False) -> None:
    """Arrête toutes les apps actives puis le Hub en parallèle.

    Args:
        target: ``\"all\"`` ou ``\"prod\"`` → apps de APPS_DIR puis Hub.
                ``\"dev\"`` → apps de WORKSHOP_DIR, **sans** le Hub.
    """
    if target not in ("all", "prod", "dev"):
        raise ValueError(f"target doit être 'all', 'prod' ou 'dev' (reçu : {target!r})")

    con = console or _console
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if target == "dev":
        apps = [a.name for a in hub_state.get_workshop_apps()]
        apps_dir = str(hub_state.WORKSHOP_DIR)
    else:
        apps = [a.name for a in hub_state.get_apps()]
        apps_dir = str(hub_state.APPS_DIR)

    tasks: dict = {}
    with ThreadPoolExecutor(max_workers=max(1, len(apps) + 1)) as pool:
        for app_name in apps:
            con.print(f"[bold]→ Arrêt de « {app_name} »…[/bold]")
            tasks[pool.submit(_stop_app, con, app_name, dry_run=dry_run, apps_dir=apps_dir)] = app_name

        if target in ("all", "prod"):
            con.print("[bold]→ Arrêt du Hub…[/bold]")
            f_hub = pool.submit(hub_make, "down", dry_run=dry_run, console=con)
            tasks[f_hub] = "hub"

        ok, fail = 0, 0
        for future in as_completed(tasks):
            name = tasks[future]
            try:
                if name == "hub":
                    future.result()
                    con.print(f"[green]✓ hub arrêté[/green]")
                    ok += 1
                elif future.result():
                    con.print(f"[green]✓ {name} arrêté[/green]")
                    ok += 1
                else:
                    con.print(f"[red]✗ {name} échoué[/red]")
                    fail += 1
            except Exception as e:
                con.print(f"[red]✗ {name} erreur : {e}[/red]")
                fail += 1
        con.print(f"[bold]{ok} succès, {fail} échec(s)[/bold]")


def _update_target(console, target: str, *, dry_run: bool) -> bool:
    """Stop → git pull → build → up → health pour un hub ou une app. Retourne True si succès."""
    con = console or _console
    target_path = hub_state.HUB_DIR if target == "hub" else hub_state.active_apps_dir() / target
    compose_file = target_path / "docker-compose.yml"
    if not compose_file.exists():
        con.print(f"[red]✗ docker-compose.yml introuvable dans {target_path}[/red]")
        return False

    prev_commit = _git_previous_commit(target_path, con)
    snapshot = _db_snapshot(con) if target == "hub" else None

    con.print(f"[bold]→ Arrêt de « {target} »…[/bold]")
    utils.run_shell(["make", "-C", str(target_path), "down"], dry_run=dry_run, console=con)

    con.print(f"[bold]→ git pull « {target} »…[/bold]")
    rc = utils.run_shell(["git", "-C", str(target_path), "pull", "--recurse-submodules"],
                         dry_run=dry_run, console=con)
    if rc != 0:
        rc = utils.run_shell(["git", "-C", str(target_path), "pull"],
                             dry_run=dry_run, console=con)
    if rc != 0:
        con.print(f"[red]✗ git pull échoué pour {target} — rollback.[/red]")
        _rollback(target, target_path, prev_commit, snapshot, con)
        return False

    con.print(f"[bold]→ Build « {target} »…[/bold]")
    rc = utils.run_shell(["make", "-C", str(target_path), "build"],
                         dry_run=dry_run, console=con, timeout=600)
    if rc != 0:
        con.print(f"[red]✗ Build échoué pour {target} — rollback.[/red]")
        _rollback(target, target_path, prev_commit, snapshot, con)
        return False

    up_target = "up-prod" if target == "hub" else "up"
    con.print(f"[bold]→ Démarrage « {target} »…[/bold]")
    rc = utils.run_shell(["make", "-C", str(target_path), up_target],
                         dry_run=dry_run, console=con, timeout=60)
    if rc != 0:
        con.print(f"[red]✗ Démarrage échoué pour {target} — rollback.[/red]")
        _rollback(target, target_path, prev_commit, snapshot, con)
        return False

    if not _health_check(target, con, timeout=60):
        con.print(f"[red]✗ Health échoué pour {target} — rollback.[/red]")
        _rollback(target, target_path, prev_commit, snapshot, con)
        return False
    return True


def action_update_all(console=None, *, dry_run: bool = False) -> None:
    """Met à jour le Hub et toutes les apps actives : git pull + rebuild."""
    con = console or _console
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not disk_guard(con):
        return

    _ensure_jwt_keys(console, dry_run=dry_run)

    apps = [a.name for a in hub_state.get_active_apps()]

    if not apps:
        con.print("[dim]Aucune app active à mettre à jour.[/dim]")

    tasks: dict = {}
    with ThreadPoolExecutor(max_workers=max(1, len(apps) + 1)) as pool:
        con.print("[bold]→ Mise à jour du Hub…[/bold]")
        tasks[pool.submit(_update_target, con, "hub", dry_run=dry_run)] = "hub"

        for app_name in apps:
            con.print(f"[bold]→ Mise à jour de « {app_name} »…[/bold]")
            tasks[pool.submit(_update_target, con, app_name, dry_run=dry_run)] = app_name

        ok, fail = 0, 0
        for future in as_completed(tasks):
            name = tasks[future]
            try:
                if future.result():
                    con.print(f"[green]✓ {name} à jour[/green]")
                    ok += 1
                else:
                    con.print(f"[red]✗ {name} échec[/red]")
                    fail += 1
            except Exception as e:
                con.print(f"[red]✗ {name} erreur : {e}[/red]")
                fail += 1
        con.print(f"[bold]{ok} succès, {fail} échec(s)[/bold]")


def action_atelier(console, app_slug: str, *, dry_run: bool = False) -> None:
    """Atelier simplifié : crée, déploie ou supprime l'atelier d'une app.

    - Si l'atelier APP_Workshop/<app>-ws n'existe pas → création (clone DB, rename slug, build&up)
    - Si l'atelier existe déjà → prompt 3 choix (déployer / supprimer / annuler)
    """
    con = console or _console
    import sys as _sys

    source_dir = hub_state.APPS_DIR / app_slug
    dest_dir = hub_state.WORKSHOP_DIR / f"{app_slug}-ws"
    source_slug = app_slug
    dest_slug = f"{app_slug}-ws"

    if not source_dir.exists():
        con.print(f"[red]✗ Source introuvable : {source_dir}[/red]")
        return
    compose_file = source_dir / "docker-compose.yml"
    if not compose_file.exists():
        con.print(f"[red]✗ docker-compose.yml introuvable dans {source_dir}[/red]")
        return

    # ── CRÉATION (atelier inexistant) ──
    if not dest_dir.exists():
        con.print(f"[bold]→ Création de l'atelier « {dest_slug} »…[/bold]")

        # 1. Stop prod
        con.print(f"[bold]→ Arrêt de la prod {source_slug}…[/bold]")
        utils.run_shell(["docker", "compose", "-f", str(compose_file), "stop"],
                        cwd=str(source_dir), dry_run=dry_run, console=con)

        # 2. rsync files
        con.print(f"[bold]→ Copie fichiers : {source_dir.name} → {dest_dir.name}[/bold]")
        if dry_run:
            con.print(f"[dim]$ rsync -a {source_dir}/ {dest_dir}/ (dry-run)[/dim]")
        else:
            shutil.copytree(str(source_dir), str(dest_dir), symlinks=True,
                            ignore=_workshop_ignore)

        # 3. Clone DB volume
        src_vol = f"{source_slug}-postgres-data"
        dst_vol = f"{dest_slug}-postgres-data"
        con.print(f"[bold]→ Clone DB : {src_vol} → {dst_vol}[/bold]")
        if not dry_run:
            run_shell(["docker", "volume", "create", dst_vol],
                       capture_output=True, timeout=10)
            rc = utils.run_shell(
                ["docker", "run", "--rm", "-v", f"{src_vol}:/from:ro",
                 "-v", f"{dst_vol}:/to", "alpine", "cp", "-a", "/from/.", "/to/"],
                dry_run=dry_run, console=con, timeout=60)
            if rc != 0:
                con.print("[yellow]⚠ Clone DB échoué (atelier démarrera avec DB vide).[/yellow]")

        # 4. Rename slug in .env
        env_path = dest_dir / ".env"
        con.print(f"[bold]→ Slug : {source_slug} → {dest_slug}[/bold]")
        if not dry_run:
            env_manager.write_env(env_path, {"APP_SLUG": dest_slug})

        # 5. docker compose up -d --build
        dest_compose = dest_dir / "docker-compose.yml"
        con.print(f"[bold]→ Build & démarrage de {dest_slug}…[/bold]")
        rc = utils.run_shell(["docker", "compose", "-f", str(dest_compose), "build"],
                             cwd=str(dest_dir), dry_run=dry_run, console=con, timeout=120)
        if not dry_run and rc != 0:
            con.print(f"[red]✗ Build échoué. ROLLBACK : suppression atelier + restart prod.[/red]")
            shutil.rmtree(str(dest_dir))
            utils.run_shell(["docker", "compose", "-f", str(compose_file), "up", "-d"],
                            cwd=str(source_dir), dry_run=False, console=con, timeout=60)
            return
        rc = utils.run_shell(["docker", "compose", "-f", str(dest_compose), "up", "-d"],
                             cwd=str(dest_dir), dry_run=dry_run, console=con, timeout=60)
        if not dry_run and rc != 0:
            con.print(f"[red]✗ up échoué. ROLLBACK.[/red]")
            shutil.rmtree(str(dest_dir))
            utils.run_shell(["docker", "compose", "-f", str(compose_file), "up", "-d"],
                            cwd=str(source_dir), dry_run=False, console=con, timeout=60)
            return

        # 6. Health check
        if not dry_run:
            env_vars = env_manager.read_env(env_path)
            api_port = env_vars.get("API_PORT", "8000")
        else:
            api_port = "8000"
        con.print(f"[bold]→ Santé atelier (port {api_port}, timeout 30s)…[/bold]")
        healthy = True if dry_run else _health_check(dest_slug, console=con, timeout=30, port_override=api_port)
        if not healthy:
            con.print(f"[red]✗ Health échoué. ROLLBACK : down atelier + restart prod.[/red]")
            if not dry_run:
                utils.run_shell(["docker", "compose", "-f", str(dest_compose), "down"],
                                cwd=str(dest_dir), dry_run=False, console=con, timeout=30)
                shutil.rmtree(str(dest_dir))
                utils.run_shell(["docker", "compose", "-f", str(compose_file), "up", "-d"],
                                cwd=str(source_dir), dry_run=False, console=con, timeout=60)
            return

        con.print(f"[green]✓ Atelier « {dest_slug} » créé. Prod {source_slug} stoppée (en place).[/green]")
        return

    # ── ATELIER EXISTANT ──
    ws_compose = dest_dir / "docker-compose.yml"
    con.print(f"[yellow]⚠ Atelier « {dest_slug} » déjà existant.[/yellow]")
    con.print("   1) [bold]déployer[/bold] — git pull prod + rebuild + restart")
    con.print("   2) [bold]supprimer[/bold] — backup tar.gz + rm -rf + restart prod")
    con.print("   3) [bold]annuler[/bold]")

    if not _sys.stdin.isatty():
        con.print("[dim]Mode non interactif — annulation.[/dim]")
        return

    try:
        choice = input("   Choix [1/2/3] : ").strip()
    except (EOFError, KeyboardInterrupt):
        con.print("[dim]Annulé.[/dim]")
        return

    if choice == "1":
        # Déployer : git pull prod + rebuild + restart
        con.print(f"[bold]→ Déployer « {dest_slug} » vers la prod (code-only)…[/bold]")
        prev_commit = _git_previous_commit(source_dir, con)
        con.print("[bold]→ git pull prod…[/bold]")
        rc = utils.run_shell(["git", "-C", str(source_dir), "pull", "--recurse-submodules"],
                             dry_run=dry_run, console=con)
        if rc != 0:
            rc = utils.run_shell(["git", "-C", str(source_dir), "pull"],
                                 dry_run=dry_run, console=con)
        if rc != 0:
            con.print(f"[red]✗ git pull échoué pour {source_slug} — rollback.[/red]")
            _rollback(source_slug, source_dir, prev_commit, None, con)
            return
        con.print(f"[bold]→ Build « {source_slug} »…[/bold]")
        rc = utils.run_shell(["make", "-C", str(source_dir), "build"],
                             dry_run=dry_run, console=con, timeout=600)
        if rc != 0:
            con.print(f"[red]✗ Build échoué pour {source_slug} — rollback.[/red]")
            _rollback(source_slug, source_dir, prev_commit, None, con)
            return
        con.print(f"[bold]→ Démarrage « {source_slug} »…[/bold]")
        rc = utils.run_shell(["make", "-C", str(source_dir), "up"],
                             dry_run=dry_run, console=con, timeout=60)
        if rc != 0:
            con.print(f"[red]✗ Démarrage échoué pour {source_slug} — rollback.[/red]")
            _rollback(source_slug, source_dir, prev_commit, None, con)
            return
        if not _health_check(source_slug, con, timeout=60):
            con.print(f"[red]✗ Health échoué — rollback.[/red]")
            _rollback(source_slug, source_dir, prev_commit, snapshot=None, console=con, dry_run=dry_run)
            return
        con.print(f"[green]✓ « {source_slug} » déployée (code-only). Atelier conservé.[/green]")

    elif choice == "2":
        # Supprimer : backup tar.gz + rm -rf + restart prod
        con.print(f"[bold red]→ Suppression de l'atelier « {dest_slug} »…[/bold red]")

        # Down -v atelier
        if ws_compose.exists():
            utils.run_shell(["docker", "compose", "-f", str(ws_compose), "down", "-v"],
                            cwd=str(dest_dir), dry_run=dry_run, console=con, timeout=60)

        # Backup tar.gz
        backups = hub_state.PARC_ROOT / "hubctl_cli" / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        archive = backups / f"{dest_slug}-{stamp}.tar.gz"
        con.print(f"[bold]→ Backup atelier → {archive.name}[/bold]")
        if not dry_run:
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(str(dest_dir), arcname=dest_dir.name)
            if archive.exists():
                os.chmod(archive, 0o600)

        # rm -rf
        con.print(f"[bold]→ Suppression de {dest_dir.name}…[/bold]")
        if not dry_run:
            shutil.rmtree(str(dest_dir))

        # Restart prod
        if source_dir.exists() and (source_dir / "docker-compose.yml").exists():
            con.print(f"[bold]→ Redémarrage prod « {source_slug} »…[/bold]")
            utils.run_shell(["make", "-C", str(source_dir), "up"],
                            dry_run=dry_run, console=con, timeout=60)
        con.print(f"[green]✓ Atelier « {dest_slug} » supprimé.[/green]")

    else:
        con.print("[dim]Annulé.[/dim]")


_console = Console()

# ── Workshop: répertoires exclus de la copie (lourds, régénérables) ──

_WORKSHOP_IGNORE_DIRS: set[str] = {
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".venv", "venv", "env",
    "node_modules", ".next", "dist", "build",
    ".git",
}

_WS_IGNORE_NAMES: tuple[str, ...] = (
    "*.pyc", "*.pyo", "*.egg-info", ".DS_Store", "*.log",
)

# ── Task 2: nouvelles actions (start_all, stop_all, update_all, atelier) ──

VPN_VOLUME = "hub_base_vpn-config"
WG_CONTAINER = "hub-wireguard"
WG_SERVICE = "wireguard-client"   # nom du service compose (≠ container_name)
WG_PROFILE = "wireguard"


def _vpn_read_enabled(console=None) -> str | None:
    """Lit vpn_enabled en DB. Retourne 'true'/'false', '' si la ligne est absente, ou None si hub-db injoignable."""
    for _ in range(3):
        try:
            r = subprocess.run(
                ["docker", "exec", "hub-db", "psql", "-U", "hubadmin", "-d", "hub_auth",
                 "-tAc", "SELECT value FROM app_settings WHERE key='vpn_enabled'"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                return r.stdout.strip()
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
        time.sleep(2)
    return None


def _vpn_set_enabled(value: bool, console=None) -> bool:
    """UPSERT psql de vpn_enabled. Retourne True si l'écriture a réussi."""
    val = "true" if value else "false"
    sql = (
        "INSERT INTO app_settings (key, value) VALUES ('vpn_enabled', '" + val + "') "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();"
    )
    try:
        r = subprocess.run(
            ["docker", "exec", "hub-db", "psql", "-U", "hubadmin", "-d", "hub_auth", "-c", sql],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def _vpn_conf_exists() -> bool:
    """Vrai si wg0.conf est présent dans le volume vpn-config (via alpine éphémère)."""
    try:
        r = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{VPN_VOLUME}:/vpn:ro",
             "alpine", "sh", "-c", "test -f /vpn/wg_confs/wg0.conf"],
            capture_output=True, timeout=15,
        )
        return r.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def _vpn_container_state() -> str:
    """Retourne 'running'/'exited'/'absent'."""
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", WG_CONTAINER],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return "absent"
        return r.stdout.strip() or "absent"
    except (subprocess.SubprocessError, FileNotFoundError):
        return "absent"


def _vpn_compose_up(*, dry_run: bool, console=None) -> int:
    return utils.run_shell(
        ["docker", "compose", "-f", str(hub_state.HUB_DIR / "docker-compose.yml"),
         "--profile", WG_PROFILE, "up", "-d", WG_SERVICE],
        dry_run=dry_run, console=console, timeout=120,
    )


def _vpn_stop(*, dry_run: bool, console=None) -> int:
    return utils.run_shell(
        ["docker", "stop", WG_CONTAINER],
        dry_run=dry_run, console=console, timeout=30,
    )


def _vpn_reconcile(enabled: bool, *, dry_run: bool, console=None) -> None:
    """Démarre (enabled=true) ou arrête (enabled=false) le conteneur WG."""
    con = console or _console
    if enabled:
        conf = _vpn_conf_exists()
        con.print(f"[bold]→ vpn_enabled=true, conf={'présente' if conf else 'ABSENTE'} — démarrage conteneur…[/bold]")
        _vpn_compose_up(dry_run=dry_run, console=con)
        if not conf:
            con.print("[yellow]⚠ Aucune wg0.conf — wg0 ne montera pas. Déploie la conf via l'UI admin.[/yellow]")
    else:
        con.print("[bold]→ vpn_enabled=false — arrêt conteneur…[/bold]")
        _vpn_stop(dry_run=dry_run, console=con)


def action_vpn(console, subcmd: str, *, dry_run: bool = False) -> None:
    """Cycle de vie du conteneur WireGuard : sync | status | on | off."""
    con = console or _console

    if subcmd == "status":
        enabled = _vpn_read_enabled(con)
        state = _vpn_container_state()
        con.print(f"vpn_enabled (DB) : {enabled if enabled is not None else '(DB injoignable)'}")
        con.print(f"conteneur        : {state}")
        if state == "running" and not dry_run:
            try:
                wg = subprocess.run(
                    ["docker", "exec", WG_CONTAINER, "wg", "show", "wg0"],
                    capture_output=True, text=True, timeout=10,
                )
                if wg.stdout.strip():
                    con.print(f"[dim]{wg.stdout.strip()}[/dim]")
            except subprocess.SubprocessError:
                pass
        return

    if subcmd == "sync":
        enabled = _vpn_read_enabled(con)
        if enabled is None:
            con.print("[yellow]⚠ hub-db injoignable — sync abandonné.[/yellow]")
            return
        _vpn_reconcile(enabled == "true", dry_run=dry_run, console=con)
        return

    if subcmd == "on":
        if dry_run:
            con.print("[dim](dry-run) vpn_enabled=true (non écrit en DB)[/dim]")
        else:
            if not _vpn_set_enabled(True, con):
                con.print("[red]✗ Impossible d'écrire vpn_enabled (hub-db injoignable ?).[/red]")
                return
            con.print("[green]✓ vpn_enabled=true[/green]")
        _vpn_reconcile(True, dry_run=dry_run, console=con)
        return

    if subcmd == "off":
        if dry_run:
            con.print("[dim](dry-run) vpn_enabled=false (non écrit en DB)[/dim]")
        else:
            if not _vpn_set_enabled(False, con):
                con.print("[red]✗ Impossible d'écrire vpn_enabled (hub-db injoignable ?).[/red]")
                return
            con.print("[green]✓ vpn_enabled=false[/green]")
        _vpn_reconcile(False, dry_run=dry_run, console=con)
        return

    raise ValueError(f"subcmd inconnu: {subcmd!r} (attendu: sync|status|on|off)")


def _port_in_use(port: int) -> bool:
    try:
        result = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=5)
        return f":{port} " in result.stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


