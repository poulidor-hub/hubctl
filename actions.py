from __future__ import annotations
import datetime
import json
import os
import re
import shutil
import socket
import subprocess
import tarfile
import time
from pathlib import Path

from rich.console import Console
from rich.table import Table

from hubctl_cli import hub_state, utils, env_manager


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


import string as _string_module


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
    admin_password = credentials["admin_password"]
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
    con.print(f"[bold]Mot de passe :[/bold] {admin_password}")
    con.print("")
    con.print("[dim yellow]⚠ Changez le mot de passe admin dès la première connexion.[/dim yellow]")

    return 0


APP_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_console = Console()


def valid_app_name(name: str) -> bool:
    return bool(APP_NAME_RE.match(name))


def repo_name_from_url(url: str) -> str | None:
    """Déduit le nom du dépôt depuis une URL git (ssh ou https)."""
    u = (url or "").strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    if not u:
        return None
    name = re.split(r"[/:]", u)[-1]
    if not name or name.startswith(".") or not _REPO_NAME_RE.match(name):
        return None
    return name


def _clone_app(console, url, dest, *, dry_run: bool = False) -> bool:
    """Clone un dépôt git vers dest. Retourne True si succès."""
    con = console or _console
    if dest.exists():
        con.print(f"[red]✗ L'app existe déjà : {dest}[/red]")
        return False
    con.print(f"[cyan]→ Clonage de {url}...[/cyan]")
    rc = utils.run_shell(["git", "clone", url, str(dest)], dry_run=dry_run, console=con)
    if rc != 0:
        con.print(f"[red]✗ Échec du clone : {url}[/red]")
        return False
    return True


def _validate_app_structure(console, dest, *, dry_run: bool = False) -> bool:
    """Vérifie la structure template POULIDOR. Supprime dest si invalide."""
    con = console or _console
    required = ["docker-compose.yml", "Makefile", ".env.example"]
    missing = [f for f in required if not (dest / f).exists()]
    if missing:
        con.print(f"[red]✗ Cette app ne suit pas le template POULIDOR.[/red]")
        con.print(f"[red]  Fichiers manquants : {', '.join(missing)}[/red]")
        if not dry_run:
            shutil.rmtree(dest, ignore_errors=True)
            con.print("[dim]Dossier cloné supprimé.[/dim]")
        return False
    con.print("[green]✓ Structure template valide[/green]")
    return True


def _read_hub_env_value_at(env_path: Path, key: str, default: str = "") -> str:
    """Lit une valeur KEY=VALUE depuis un fichier .env dont le chemin est explicite."""
    try:
        for line in env_path.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return default


def _find_free_port(start: int, end: int, used: set[int] | None = None) -> int | None:
    """Trouve le premier port libre dans [start, end] via ss."""
    used = used or set()
    # Ports déjà occupés sur le système
    try:
        result = subprocess.run(
            ["ss", "-tlnH"], capture_output=True, text=True, timeout=5,
        )
        system_used: set[int] = set()
        for line in result.stdout.splitlines():
            # Format réel ss : LISTEN 0 128 *:9000 *:*
            parts = line.split()
            for p in parts:
                if p.startswith("*:") or p.startswith("0.0.0.0:") or p.startswith("127.0.0.1:") or p.startswith("[::]:"):
                    try:
                        port = int(p.rsplit(":", 1)[1])
                        system_used.add(port)
                    except (ValueError, IndexError):
                        pass
            # Format simplifié (mocks / sortie brute) : une ligne = un port
            stripped = line.strip()
            if stripped.isdigit():
                try:
                    port = int(stripped)
                    if 1 <= port <= 65535:
                        system_used.add(port)
                except ValueError:
                    pass
        used |= system_used
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    for port in range(start, end + 1):
        if port not in used:
            return port
    return None


def _detect_app_config(name: str) -> dict:
    """Auto-détecte slug, ports libres, secrets, Hub URL."""
    import secrets as _secrets
    import string as _string
    from cryptography.fernet import Fernet as _Fernet

    # Slug : minuscules + underscores convertis en tirets.
    # Le Hub exige ^[a-z0-9-]+$ (pas d'underscores), mais les noms de repos
    # peuvent en contenir (ex: exemple-app-2 → exemple-app).
    slug = name.lower().replace("_", "-")
    if not valid_app_name(slug):
        raise ValueError(f"Nom d'app invalide (doit matcher ^[a-z][a-z0-9_-]*$) : {slug}")

    # Lire les plages de ports depuis HUB_base/.env
    api_start = int(_read_hub_env_value("APP_PORT_API_START", "9000"))
    api_end = int(_read_hub_env_value("APP_PORT_API_END", "9499"))
    fe_start = int(_read_hub_env_value("APP_PORT_FRONTEND_START", "4200"))
    fe_end = int(_read_hub_env_value("APP_PORT_FRONTEND_END", "4699"))

    # Ports déjà utilisés par les apps existantes
    used_ports: set[int] = set()
    try:
        for app in hub_state.get_active_apps():
            app_env = hub_state.active_apps_dir() / app.name / ".env"
            if app_env.exists():
                for key in ("APP_PORT", "APP_PORT_API", "FRONTEND_PORT", "API_PORT"):
                    val = _read_hub_env_value_at(app_env, key)
                    if val:
                        try:
                            used_ports.add(int(val))
                        except ValueError:
                            pass
    except Exception:
        pass

    api_port = _find_free_port(api_start, api_end, used_ports)
    if api_port is None:
        raise RuntimeError(f"Aucun port API libre dans la plage {api_start}-{api_end}")
    used_ports.add(api_port)

    frontend_port = _find_free_port(fe_start, fe_end, used_ports)
    if frontend_port is None:
        raise RuntimeError(f"Aucun port frontend libre dans la plage {fe_start}-{fe_end}")

    # Secrets
    alphabet = _string.ascii_letters + _string.digits
    pg_password = "".join(_secrets.choice(alphabet) for _ in range(16))
    fernet_key = _Fernet.generate_key().decode()

    # Hub
    hub_host = _read_hub_env_value("HUB_HOST", "localhost")
    hub_api_port = _read_hub_env_value("HUB_API_PORT", "8000")
    host_ip = _detect_host_ip()

    return {
        "slug": slug,
        "api_port": api_port,
        "frontend_port": frontend_port,
        "postgres_password": pg_password,
        "fernet_key": fernet_key,
        "hub_host": hub_host,
        "hub_url": f"http://{hub_host}:{hub_api_port}",
        "host_ip": host_ip,
    }


def _show_and_confirm_config(console, config, dest) -> bool:
    """Affiche un tableau Rich et demande validation."""
    con = console or _console
    t = Table(title="Nouvelle application détectée")
    t.add_column("Paramètre", style="bold cyan")
    t.add_column("Valeur")
    t.add_row("Slug", config["slug"])
    t.add_row("Destination", str(dest))
    t.add_row("Port API", str(config["api_port"]))
    t.add_row("Port Frontend", str(config["frontend_port"]))
    t.add_row("Hub", config["hub_url"])
    con.print(t)
    con.print("[dim]Secrets générés : mot de passe DB + clé Fernet (stockés dans .env + coffre)[/dim]")
    con.print()
    try:
        answer = input("  Valider et démarrer ? [O/n] : ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("", "o", "oui", "y", "yes")


def _write_app_env(console, dest, config, *, dry_run: bool = False) -> None:
    """Génère dest/.env depuis .env.example avec les valeurs détectées."""
    con = console or _console
    example = dest / ".env.example"
    if not example.exists():
        con.print("[yellow]⚠ .env.example introuvable — .env non généré[/yellow]")
        return
    content = example.read_text()

    host_uid = str(os.getuid())
    api_port = str(config["api_port"])
    frontend_port = str(config["frontend_port"])

    # Map de substitution : chaque clé = nom de variable dans .env.example.
    # On couvre toutes les variantes utilisées par les apps réelles du parc :
    #   - API_PORT (template canonique, exemple-app-2, exemple-app)
    #   - APP_PORT / APP_PORT_API (variants historiques)
    #   - APP_HOST (template canonique, exemple-app)
    #   - SERVER_IP (exemple-app-2)
    #   - HOST_IP (variant)
    replacements = {
        "APP_SLUG": config["slug"],
        "APP_NAME": config["slug"],
        # Ports — toutes les variantes connues
        "API_PORT": api_port,
        "APP_PORT": api_port,
        "APP_PORT_API": api_port,
        "FRONTEND_PORT": frontend_port,
        # Host — toutes les variantes connues
        "APP_HOST": config["host_ip"],
        "SERVER_IP": config["host_ip"],
        "HOST_IP": config["host_ip"],
        # Secrets
        "POSTGRES_PASSWORD": config["postgres_password"],
        "SECRETS_ENCRYPTION_KEY": config["fernet_key"],
        # Hub
        "HUB_URL": config["hub_url"],
        "HUB_HOST": config["hub_host"],
        "HOST_UID": host_uid,
    }

    # Construire DATABASE_URL si présent dans le template
    db_url = f"postgresql+asyncpg://app_user:{config['postgres_password']}@db:5432/app_db"
    replacements["DATABASE_URL"] = db_url

    for key, val in replacements.items():
        content = re.sub(rf"^{key}=.*$", f"{key}={val}", content, flags=re.MULTILINE)

    env_path = dest / ".env"
    env_path.write_text(content)
    os.chmod(env_path, 0o600)
    con.print("[green]✓ .env généré[/green]")


def hub_make(target: str, *, dry_run: bool, console=None) -> int:
    return utils.run_shell(
        ["make", "-C", str(hub_state.HUB_DIR), target],
        dry_run=dry_run, console=console,
    )


def app_make(app_name: str, target: str, *, dry_run: bool, console=None, timeout: int = 120) -> int:
    return utils.run_shell(
        ["make", "-C", str(hub_state.active_apps_dir() / app_name), target],
        dry_run=dry_run, console=console, timeout=timeout,
    )


def _build_and_start_app(console, dest, config, *, dry_run: bool = False) -> bool:
    """Build + up + health check. Retourne True si l'app est saine.

    Étapes :
    1. make build (docker compose build, avec cache) — timeout 600s
    2. make up (docker compose up -d) — timeout 120s
    3. Health check — timeout 60s

    On ne peut pas faire seulement 'make up' car certains Makefiles n'ont
    pas --build dans la cible up (template canonique). On ne fait pas non
    plus 'make up --build' car la cible up peut avoir des pré-traitements
    (sync-env, SSL) qui ne s'appliquent pas au build.
    """
    con = console or _console
    app_name = dest.name

    if dry_run:
        con.print(f"[dim](dry-run) make -C {dest} build + up[/dim]")
        return True

    con.print(f"[cyan]🔨 Build de {app_name}...[/cyan]")
    rc = app_make(app_name, "build", dry_run=False, console=con, timeout=600)
    if rc != 0:
        con.print("[red]✗ Échec du build.[/red]")
        return False

    con.print(f"[cyan]🚀 Démarrage de {app_name}...[/cyan]")
    rc = app_make(app_name, "up", dry_run=False, console=con, timeout=120)
    if rc != 0:
        con.print("[red]✗ Échec du démarrage.[/red]")
        return False

    con.print("[cyan]💚 Health check...[/cyan]")
    ok = _health_check(app_name, console=con, timeout=60)
    if not ok:
        con.print("[yellow]⚠ L'app n'a pas passé le health check.[/yellow]")
        con.print("[dim]Logs récents :[/dim]")
        try:
            result = subprocess.run(
                ["docker", "compose", "logs", "api", "--tail", "20"],
                capture_output=True, text=True, timeout=10, cwd=str(dest),
            )
            con.print(f"[dim]{result.stdout[-500:]}[/dim]")
        except Exception:
            pass
        con.print(f"[yellow]L'app est clonée dans {dest} — corrige le .env et relance 'make up'.[/yellow]")
        return False

    return True


def _get_vault():
    """Récupère le coffre du parc (lazy import pour éviter les dépendances circulaires)."""
    from hubctl_cli.coffre import Vault
    vault_path = hub_state.PARC_ROOT / ".coffre"
    return Vault(vault_path)


def _sync_app_coffre(console, name, *, dry_run: bool = False) -> None:
    """Pousse le .env de l'app vers le coffre chiffré. Non-bloquant."""
    con = console or _console
    if dry_run:
        con.print(f"[dim](dry-run) Sync coffre : {name}[/dim]")
        return
    try:
        vault = _get_vault()
        app_env = hub_state.active_apps_dir() / name / ".env"
        if app_env.exists():
            vault.backup_app_env(name, app_env)
            con.print(f"[green]✓ Secrets de {name} sauvegardés dans le coffre[/green]")
    except Exception as e:
        con.print(f"[yellow]⚠ Sync coffre échoué ({e}) — l'app est démarrée, le coffre peut être sync plus tard.[/yellow]")


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
        con.print(f"[green]✓ Snapshot DB →[/green] {path.name}")
        return path
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        con.print(f"[yellow]⚠ Snapshot DB échoué : {e}[/yellow]")
        return None


# =============================================================================
# VPN WireGuard — cycle de vie du conteneur (réconciliation host-level)
# =============================================================================

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
        if not _vpn_set_enabled(True, con):
            con.print("[red]✗ Impossible d'écrire vpn_enabled (hub-db injoignable ?).[/red]")
            return
        con.print("[green]✓ vpn_enabled=true[/green]")
        _vpn_reconcile(True, dry_run=dry_run, console=con)
        return

    if subcmd == "off":
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


def _find_free_ports(base_api: int, base_frontend: int, base_https: int) -> tuple[int, int, int]:
    for offset in range(10):
        api, fe, https = base_api + offset, base_frontend + offset, base_https + offset
        if not _port_in_use(api) and not _port_in_use(fe) and not _port_in_use(https):
            return api, fe, https
    raise RuntimeError("Aucun port libre trouvé (10 essais)")


def _db_snapshot_app(app_name: str, console=None) -> Path | None:
    con = console or _console
    backups = hub_state.PARC_ROOT / "hubctl_cli" / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = backups / f"snap_{app_name}_{stamp}.sql"
    compose = hub_state.active_apps_dir() / app_name / "docker-compose.yml"
    user, db = _app_db_creds(app_name)
    try:
        with open(path, "w") as f:
            subprocess.run(
                ["docker", "compose", "-f", str(compose), "exec", "-T", "postgres",
                 "pg_dump", "-U", user, db],
                stdout=f, stderr=subprocess.PIPE, timeout=60, check=True,
            )
        con.print(f"[green]✓ Snapshot DB {app_name} →[/green] {path.name}")
        return path
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        con.print(f"[yellow]⚠ Snapshot DB {app_name} échoué : {e}[/yellow]")
        path.unlink(missing_ok=True)
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


def action_status(console=None) -> None:
    """Affiche une vue d'ensemble texte du parc (Hub + apps + mode)."""
    con = console or _console
    mode = hub_state.get_mode()
    mode_label = "DEV (APP_Workshop/)" if mode == "dev" else "PROD (APPS/)"

    # Hub
    services = hub_state.get_hub_services()
    hub_version = hub_state.get_hub_version()
    api_running = any(s.state == "running" and "api" in s.name.lower() for s in services)
    hub_icon = "🟢" if api_running else "🔴"
    con.print(f"[bold]Hub[/bold]      : {hub_icon} {'running' if api_running else 'stopped'}"
              f"  v{hub_version}")
    for s in services:
        con.print(f"  {s.name:<20} {s.state} ({s.health})")

    # Apps
    apps = hub_state.get_apps_status()
    if apps:
        con.print(f"[bold]Apps[/bold]     :")
        for app in apps:
            icon = "🟢" if app.status == "running" else ("🟡" if app.status == "stopped" else "⚫")
            con.print(f"  {app.name:<20} {icon} {app.status}")
    else:
        con.print(f"[bold]Apps[/bold]     : (aucune)")

    con.print(f"[bold]Mode[/bold]     : {mode_label}")


def action_update(console, target: str, *, dry_run: bool) -> None:
    """Met à jour le Hub ou une app : stop → git pull → build → up → health.
    Rollback automatique (git reset + DB restore) si le health check échoue.
    """
    con = console or _console
    if not disk_guard(con):
        return

    if target == "hub":
        _ensure_jwt_keys(console, dry_run=dry_run)

    target_path = hub_state.HUB_DIR if target == "hub" else hub_state.active_apps_dir() / target
    compose_file = target_path / "docker-compose.yml"
    if not compose_file.exists():
        con.print(f"[red]✗ docker-compose.yml introuvable dans {target_path}[/red]")
        return

    # ── Capture pour rollback ──
    prev_commit = _git_previous_commit(target_path, con)
    snapshot = _db_snapshot(con) if target == "hub" else None

    # ── Stop prod ──
    con.print(f"[bold]→ Arrêt de « {target} »[/bold]")
    rc = utils.run_shell(["make", "-C", str(target_path), "down"],
                         dry_run=dry_run, console=con)
    if rc != 0:
        con.print("[yellow]⚠ make down a échoué (continue)[/yellow]")

    # ── git pull ──
    con.print(f"[bold]→ git pull[/bold]")
    rc = utils.run_shell(["git", "-C", str(target_path), "pull", "--recurse-submodules"],
                         dry_run=dry_run, console=con)
    if rc != 0:
        rc = utils.run_shell(["git", "-C", str(target_path), "pull"],
                             dry_run=dry_run, console=con)
    if rc != 0:
        con.print("[red]✗ git pull a échoué — rollback et abort.[/red]")
        _rollback(target, target_path, prev_commit, snapshot, con)
        return

    # ── Build ──
    con.print(f"[bold]→ Build de « {target} »[/bold]")
    rc = utils.run_shell(["make", "-C", str(target_path), "build"],
                         dry_run=dry_run, console=con, timeout=600)
    if rc != 0:
        con.print("[red]✗ Build a échoué — rollback et abort.[/red]")
        _rollback(target, target_path, prev_commit, snapshot, con)
        return

    # ── Start prod ──
    up_target = "up-prod" if target == "hub" else "up"
    con.print(f"[bold]→ Démarrage de « {target} »[/bold]")
    rc = utils.run_shell(["make", "-C", str(target_path), up_target],
                         dry_run=dry_run, console=con, timeout=60)
    if rc != 0:
        con.print("[red]✗ Démarrage a échoué — rollback et abort.[/red]")
        _rollback(target, target_path, prev_commit, snapshot, con)
        return

    # ── Health check ──
    if not _health_check(target, con, timeout=60):
        con.print(f"[red]✗ Health check échoué après mise à jour — rollback automatique.[/red]")
        _rollback(target, target_path, prev_commit, snapshot, con)
        return

    con.print(f"[green]✓ « {target} » mis à jour avec succès[/green]")


def action_add_app(console, url, *, dry_run: bool = False) -> None:
    """Ajoute une app depuis une URL git : clone → validate → configure → build → start → sync."""
    con = console or _console
    name = repo_name_from_url(url)
    if not name:
        con.print(f"[red]✗ URL git invalide : {url}[/red]")
        return

    dest = hub_state.active_apps_dir() / name

    # 1. Clone
    if not _clone_app(con, url, dest, dry_run=dry_run):
        return

    # 2. Validation structure
    if not _validate_app_structure(con, dest, dry_run=dry_run):
        return

    # 3. Auto-détection config
    try:
        config = _detect_app_config(name)
    except (RuntimeError, ValueError) as e:
        con.print(f"[red]✗ {e}[/red]")
        if not dry_run:
            shutil.rmtree(dest, ignore_errors=True)
        return

    # 4. Résumé + validation utilisateur
    if not _show_and_confirm_config(con, config, dest):
        con.print("[yellow]Annulé.[/yellow]")
        if not dry_run:
            shutil.rmtree(dest, ignore_errors=True)
            con.print("[dim]Dossier cloné supprimé.[/dim]")
        return

    # 5. Écriture .env
    _write_app_env(con, dest, config, dry_run=dry_run)

    # 6. Build + démarrage
    if not _build_and_start_app(con, dest, config, dry_run=dry_run):
        return  # Dossier conservé pour debug

    # 7. Sync coffre
    _sync_app_coffre(con, name, dry_run=dry_run)

    # 8. Résumé final
    con.print()
    con.print(f"[green bold]✅ Application {config['slug']} démarrée ![/green bold]")
    con.print()
    con.print(f"  [bold]Frontend[/bold]    http://{config['host_ip']}:{config['frontend_port']}")
    con.print(f"  [bold]API[/bold]         http://{config['host_ip']}:{config['api_port']}/api/health")
    con.print(f"  [bold]Coffre[/bold]      ✓ secrets sauvegardés")
    con.print()
    con.print(f"  [yellow]⚠ Action requise : validez l'app sur le Hub[/yellow]")
    con.print(f"     https://{config['hub_host']} → Administration → Applications")
    con.print(f"     Statut actuel : en attente d'approbation")
    con.print()


def action_attach_app(console, app_name: str, *, dry_run: bool) -> None:
    con = console or _console
    active_dir = hub_state.WORKSHOP_DIR if hub_state.get_mode() == "dev" else hub_state.APPS_DIR
    path = active_dir / app_name
    if not path.exists():
        con.print(f"[red]✗ App introuvable : {path}[/red]")
        return
    compose = path / "docker-compose.yml"
    if not compose.exists():
        con.print(f"[red]✗ docker-compose.yml introuvable dans {path}[/red]")
        return
    if not disk_guard(con):
        return
    if dry_run or (path / "INITIALISE.py").exists():
        utils.run_shell(["python", "INITIALISE.py"], cwd=str(path), dry_run=dry_run, console=con)
    con.print(f"[bold]→ Build de « {app_name} »[/bold]")
    rc = utils.run_shell(["docker", "compose", "-f", str(compose), "build"],
                         cwd=str(path), dry_run=dry_run, console=con, timeout=600)
    if not dry_run and rc != 0:
        con.print("[red]✗ Build échoué[/red]")
        return
    con.print(f"[bold]→ Démarrage de « {app_name} »[/bold]")
    rc = utils.run_shell(["docker", "compose", "-f", str(compose), "up", "-d"],
                         cwd=str(path), dry_run=dry_run, console=con, timeout=60)
    if not dry_run and rc != 0:
        con.print("[red]✗ Démarrage échoué[/red]")
        return
    con.print(f"[green]✓ « {app_name} » rattachée et démarrée[/green]")


def action_remove_app(console, app_name: str, vault, *, dry_run: bool) -> None:
    path = hub_state.active_apps_dir() / app_name
    if not path.exists():
        console.print(f"[red]App introuvable : {app_name}.[/red]")
        return
    console.print(
        f"[bold red]Suppression de « {app_name} »[/bold red] : "
        "sync du coffre + backup tar.gz, arrêt conteneur, mise en corbeille."
    )

    if vault is not None and not vault.is_locked:
        vault.sync(); vault.save()
        console.print("[green]✓ Coffre synchronisé.[/green]")

    backups = hub_state.PARC_ROOT / "hubctl_cli" / "backups"
    trash = hub_state.PARC_ROOT / "hubctl_cli" / "_trash"
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    if dry_run:
        console.print(f"[dim]$ tar czf {backups}/{app_name}-{stamp}.tar.gz {path}  (dry-run)[/dim]")
        console.print(f"[dim]$ mv {path} {trash}/{app_name}-{stamp}  (dry-run)[/dim]")
        return
    app_make(app_name, "down", dry_run=False, console=console)
    backups.mkdir(parents=True, exist_ok=True)
    trash.mkdir(parents=True, exist_ok=True)
    archive = backups / f"{app_name}-{stamp}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(path, arcname=app_name)
    console.print(f"[green]✓ Backup :[/green] {archive}")
    shutil.move(str(path), str(trash / f"{app_name}-{stamp}"))
    console.print(f"[green]✓ App déplacée dans la corbeille[/green] ({trash}).")


def action_repair(console, *, dry_run: bool) -> None:
    con = console or _console
    _ensure_jwt_keys(console, dry_run=dry_run)
    con.print("[bold]Diagnostic (lecture seule)[/bold]")
    hub_make("ps", dry_run=False, console=console)
    d = hub_state.get_disk_usage()
    con.print(f"Disque : {d.percent_free}% libre.")
    utils.run_shell(
        ["docker", "compose", "-f", str(hub_state.HUB_DIR / "docker-compose.yml"),
         "up", "-d", "--force-recreate"],
        dry_run=dry_run, console=con,
    )


def _compose_file(target: str) -> Path | None:
    """Retourne le docker-compose.yml du hub ou de l'app, ou None si introuvable."""
    if target == "hub":
        p = hub_state.HUB_DIR / "docker-compose.yml"
    else:
        p = hub_state.active_apps_dir() / target / "docker-compose.yml"
    return p if p.exists() else None


def _resolve_api_port(target: str) -> str | None:
    try:
        if target == "hub":
            env_path = hub_state.HUB_DIR / ".env"
            if env_path.exists():
                return env_manager.read_env(env_path).get("HUB_API_PORT", "8000")
            return "8000"
        app_env = hub_state.active_apps_dir() / target / ".env"
        if app_env.exists():
            return env_manager.read_env(app_env).get("API_PORT", "8000")
        return None
    except Exception:
        return None


def _app_db_creds(app_name: str) -> tuple[str, str]:
    env_path = hub_state.active_apps_dir() / app_name / ".env"
    try:
        env = env_manager.read_env(env_path) if env_path.exists() else {}
    except Exception:
        env = {}
    return env.get("POSTGRES_USER", "app_user"), env.get("POSTGRES_DB", "app_db")


def action_health(console, target: str, *, detailed: bool = False) -> int:
    import json
    con = console or _console
    api_port = _resolve_api_port(target)
    if api_port is None:
        con.print(f"[red]✗ Port API introuvable pour : {target}[/red]")
        return 1
    path = "/api/health/detailed" if detailed else "/api/health"
    url = f"http://localhost:{api_port}{path}"
    try:
        result = subprocess.run(["curl", "-sk", url], capture_output=True, text=True, timeout=5)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        con.print(f"[red]✗ Cible injoignable : {url}[/red]")
        return 1
    if result.returncode != 0:
        con.print(f"[red]✗ Cible injoignable : {url}[/red]")
        return 1
    try:
        data = json.loads(result.stdout)
        con.print_json(data=data)
        return 0 if data.get("status") == "healthy" else 1
    except json.JSONDecodeError:
        con.print(result.stdout)
        return 1


def action_db_shell(console, app_name: str) -> None:
    con = console or _console
    compose = _compose_file(app_name)
    if compose is None:
        con.print(f"[red]✗ App introuvable : {app_name}[/red]")
        return
    user, db = _app_db_creds(app_name)
    utils.run_shell(
        ["docker", "compose", "-f", str(compose), "exec", "postgres",
         "psql", "-U", user, "-d", db],
        console=con,
    )


def action_logs(console, target: str, *, service: str | None = None) -> None:
    con = console or _console
    compose = _compose_file(target)
    if compose is None:
        con.print(f"[red]✗ Cible introuvable : {target}[/red]")
        return
    cmd = ["docker", "compose", "-f", str(compose), "logs", "-f", "--tail=100"]
    if service:
        cmd.append(service)
    utils.run_shell(cmd, console=con)


def action_restart(console, target: str, *, service: str | None = None) -> None:
    con = console or _console
    compose = _compose_file(target)
    if compose is None:
        con.print(f"[red]✗ Cible introuvable : {target}[/red]")
        return
    cmd = ["docker", "compose", "-f", str(compose), "restart"]
    if service:
        cmd.append(service)
    utils.run_shell(cmd, console=con)


def action_init(console, *, dry_run: bool) -> None:
    manifest_path = hub_state.PARC_ROOT / "manifest.json"
    if not manifest_path.exists():
        console.print("[red]✗ manifest.json introuvable à la racine du parc.[/red]")
        return
    try:
        projects = json.loads(manifest_path.read_text())
    except json.JSONDecodeError:
        console.print("[red]✗ manifest.json invalide.[/red]")
        return
    if not isinstance(projects, dict) or not projects:
        console.print("[yellow]Aucun projet listé dans manifest.json.[/yellow]")
        return
    table = Table(title="Résultat de l'init")
    table.add_column("Projet", style="bold")
    table.add_column("État", style="bold")
    for rel_path, url in projects.items():
        dest = hub_state.PARC_ROOT / rel_path
        if dest.exists():
            table.add_row(rel_path, "[dim]déjà présent[/dim]")
            continue
        if not disk_guard(console):
            table.add_row(rel_path, "[red]espace disque insuffisant[/red]")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        rc = utils.run_shell(["git", "clone", url, str(dest)], dry_run=dry_run, console=console)
        if rc == 0:
            table.add_row(rel_path, "[green]✓ cloné[/green]")
        else:
            table.add_row(rel_path, "[red]✗ échec[/red]")
    console.print(table)


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


def action_start_stop(console, sub: str, target: str = "hub", *, dry_run: bool = False) -> None:
    """Démarre/arrête/redémarre le Hub ou une app.

    target="hub" (défaut) → hub_make. Sinon → app_make(target, ...).
    """
    if target == "hub":
        if sub in ("start", "restart"):
            _ensure_jwt_keys(console, dry_run=dry_run)
        if sub == "start":
            rc = hub_make("up-prod", dry_run=dry_run, console=console)
            if rc == 0:
                # Réconcilier le conteneur WireGuard avec vpn_enabled (démarrage ssi activé).
                try:
                    action_vpn(console, "sync", dry_run=dry_run)
                except Exception as e:
                    console.print(f"[yellow]⚠ VPN sync au boot échoué : {e}[/yellow]") if console else None
        elif sub in ("stop", "restart"):
            hub_make("down", dry_run=dry_run, console=console)
            if sub == "restart":
                hub_make("up-prod", dry_run=dry_run, console=console)
    else:
        if sub == "start":
            app_make(target, "up", dry_run=dry_run, console=console)
        elif sub in ("stop", "restart"):
            app_make(target, "down", dry_run=dry_run, console=console)
            if sub == "restart":
                app_make(target, "up", dry_run=dry_run, console=console)


# ── Workshop: répertoires exclus de la copie (lourds, régénérables) ──

_WORKSHOP_IGNORE_DIRS: set[str] = {
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".venv", "venv", "env", ".env",
    "node_modules", ".next", "dist", "build",
    ".git",  # le git clone sera refait au premier update
}

_WS_IGNORE_NAMES: tuple[str, ...] = (
    "*.pyc", "*.pyo", "*.egg-info", ".DS_Store", "*.log",
)


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


def action_workshop_create(console, app_name: str, *, dry_run: bool = False) -> None:
    """Met une app en atelier : APPS/X → APP_Workshop/X-ws (clone DB).
    La prod X est stoppée mais LAISSÉE EN PLACE (jamais supprimée).
    """
    con = console or _console

    # ── 1. CHECK ──
    dev_marker = hub_state.HUB_DIR / ".dev-mode"
    if not dev_marker.exists():
        dev_marker.touch()
        con.print("[dim].dev-mode créé (compatibilité)[/dim]")
    if hub_state.check_docker() is False:
        con.print("[red]✗ Docker injoignable.[/red]")
        return

    source_dir = hub_state.APPS_DIR / app_name
    dest_dir = hub_state.WORKSHOP_DIR / f"{app_name}-ws"
    source_slug, dest_slug = app_name, f"{app_name}-ws"

    if not source_dir.exists():
        con.print(f"[red]✗ Source introuvable : {source_dir}[/red]")
        return
    if dest_dir.exists():
        con.print(f"[red]✗ Atelier déjà existant : {dest_dir} (utilise [×] pour le supprimer)[/red]")
        return
    compose_file = source_dir / "docker-compose.yml"
    if not compose_file.exists():
        con.print(f"[red]✗ docker-compose.yml introuvable dans {source_dir}[/red]")
        return

    # ── 2. STOP prod (laissée en place) ──
    con.print(f"[bold]→ Arrêt de la prod {source_slug}…[/bold]")
    rc = utils.run_shell(["docker", "compose", "-f", str(compose_file), "stop"],
                         cwd=str(source_dir), dry_run=dry_run, console=con)
    if not dry_run and rc != 0:
        con.print(f"[yellow]⚠ docker compose stop a retourné {rc}.[/yellow]")

    # ── 3. COPY FILES ──
    con.print(f"[bold]→ Copie fichiers : {source_dir.name} → {dest_dir.name}[/bold]")
    if dry_run:
        con.print(f"[dim]$ cp -r {source_dir} {dest_dir} (dry-run)[/dim]")
    else:
        shutil.copytree(str(source_dir), str(dest_dir), symlinks=True,
                        ignore=_workshop_ignore)

    # ── 4. VOLUME COPY (clone DB prod → atelier) ──
    src_vol, dst_vol = f"{source_slug}-postgres-data", f"{dest_slug}-postgres-data"
    con.print(f"[bold]→ Clone DB : {src_vol} → {dst_vol}[/bold]")
    if not dry_run:
        subprocess.run(["docker", "volume", "create", dst_vol],
                       capture_output=True, timeout=10)
        rc = utils.run_shell(
            ["docker", "run", "--rm", "-v", f"{src_vol}:/from:ro",
             "-v", f"{dst_vol}:/to", "alpine", "cp", "-a", "/from/.", "/to/"],
            dry_run=dry_run, console=con, timeout=60)
        if rc != 0:
            con.print("[yellow]⚠ Clone DB échoué (atelier démarrera avec DB vide).[/yellow]")
    else:
        con.print(f"[dim]$ docker volume create {dst_vol} (dry-run)[/dim]")

    # ── 5. RENAME SLUG ──
    env_path = dest_dir / ".env"
    con.print(f"[bold]→ Slug : {source_slug} → {dest_slug}[/bold]")
    if dry_run:
        con.print(f"[dim]$ APP_SLUG={dest_slug} dans {env_path} (dry-run)[/dim]")
    else:
        env_manager.write_env(env_path, {"APP_SLUG": dest_slug})

    # ── 6. BUILD & UP atelier ──
    dest_compose = dest_dir / "docker-compose.yml"
    con.print(f"[bold]→ Build & démarrage de {dest_slug}…[/bold]")
    rc = utils.run_shell(["docker", "compose", "-f", str(dest_compose), "build"],
                         cwd=str(dest_dir), dry_run=dry_run, console=con, timeout=120)
    if not dry_run and rc != 0:
        con.print(f"[red]✗ Build échoué. ROLLBACK : suppression atelier + restart prod.[/red]")
        if not dry_run:
            shutil.rmtree(str(dest_dir))
            subprocess.run(["docker", "compose", "-f", str(compose_file), "up", "-d"],
                           cwd=str(source_dir), capture_output=True, timeout=60)
        return
    rc = utils.run_shell(["docker", "compose", "-f", str(dest_compose), "up", "-d"],
                         cwd=str(dest_dir), dry_run=dry_run, console=con, timeout=60)
    if not dry_run and rc != 0:
        con.print(f"[red]✗ up échoué. ROLLBACK.[/red]")
        shutil.rmtree(str(dest_dir))
        subprocess.run(["docker", "compose", "-f", str(compose_file), "up", "-d"],
                       cwd=str(source_dir), capture_output=True, timeout=60)
        return

    # ── 7. HEALTH CHECK atelier ──
    env_vars = env_manager.read_env(env_path) if not dry_run else {}
    api_port = env_vars.get("API_PORT", "8000")
    con.print(f"[bold]→ Santé atelier (port {api_port}, timeout 30s)…[/bold]")
    healthy = True if dry_run else _health_check(dest_slug, console=con, timeout=30, port_override=api_port)
    if not healthy:
        con.print(f"[red]✗ Health échoué. ROLLBACK : down atelier + restart prod.[/red]")
        if not dry_run:
            subprocess.run(["docker", "compose", "-f", str(dest_compose), "down"],
                           cwd=str(dest_dir), capture_output=True, timeout=30)
            shutil.rmtree(str(dest_dir))
            subprocess.run(["docker", "compose", "-f", str(compose_file), "up", "-d"],
                           cwd=str(source_dir), capture_output=True, timeout=60)
        return

    con.print(f"[green]✓ Atelier « {dest_slug} » créé. Prod {source_slug} stoppée (en place).[/green]")


def action_prod_deploy_copy_db(con, slug, prod_dir, ws_dir, compose_file, *, dry_run):
    """Chemin destructif d'action_prod_deploy : écrase la DB prod par celle de l'atelier."""
    con.print(f"[bold yellow]⚠️  ATTENTION : écrasement de la DB prod « {slug}-postgres-data »[/bold yellow]")
    con.print(f"[dim]    par celle de l'atelier. Toute donnée de prod modifiée depuis le clone sera PERDUE.[/dim]")

    # Gate forte (CLI-only, via input()).
    if not dry_run:
        typed = input(f"Tape exactement « {slug} » pour confirmer l'écrasement de la DB prod : ")
        if typed.strip() != slug:
            con.print("[yellow]Annulé : confirmation incorrecte. Aucune modification.[/yellow]")
            return

    src_vol = f"{slug}-ws-postgres-data"
    dst_vol = f"{slug}-postgres-data"

    # Stop prod
    con.print(f"[bold]→ Arrêt prod « {slug} »…[/bold]")
    utils.run_shell(["docker", "compose", "-f", str(compose_file), "stop"],
                    cwd=str(prod_dir), dry_run=dry_run, console=con)

    # Backup du volume prod (alpine tar → fichier) pour rollback
    backup_path = None
    if not dry_run:
        backups = hub_state.PARC_ROOT / "hubctl_cli" / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = backups / f"{slug}-postgres-volume-{stamp}.tar.gz"
        con.print(f"[bold]→ Backup volume prod → {backup_path.name}[/bold]")
        with open(backup_path, "wb") as f:
            bk = subprocess.run(["docker", "run", "--rm", "-v", f"{dst_vol}:/data:ro",
                                 "alpine", "tar", "czf", "-", "/data"],
                                stdout=f, stderr=subprocess.PIPE, timeout=120)
        if bk.returncode != 0:
            con.print("[yellow]⚠ Backup volume échoué — rollback ne pourra pas restaurer le volume.[/yellow]")
            backup_path = None

    # Overwrite volume : rm + create + alpine cp
    con.print(f"[bold]→ Overwrite volume : {src_vol} → {dst_vol}[/bold]")
    if not dry_run:
        subprocess.run(["docker", "volume", "rm", dst_vol], capture_output=True, timeout=15)
        subprocess.run(["docker", "volume", "create", dst_vol], capture_output=True, timeout=10)
    utils.run_shell(["docker", "run", "--rm", "-v", f"{src_vol}:/from:ro",
                     "-v", f"{dst_vol}:/to", "alpine", "cp", "-a", "/from/.", "/to/"],
                    dry_run=dry_run, console=con, timeout=120)

    # git pull (code cohérent avec la DB migrée)
    prev_commit = _git_previous_commit(prod_dir, con)
    con.print("[bold]→ git pull prod (migrations cohérentes)…[/bold]")
    utils.run_shell(["git", "-C", str(prod_dir), "pull", "--recurse-submodules"],
                    dry_run=dry_run, console=con)

    con.print(f"[bold]→ Build « {slug} »…[/bold]")
    utils.run_shell(["make", "-C", str(prod_dir), "build"],
                    dry_run=dry_run, console=con, timeout=600)
    con.print(f"[bold]→ Démarrage « {slug} »…[/bold]")
    utils.run_shell(["make", "-C", str(prod_dir), "up"],
                    dry_run=dry_run, console=con, timeout=60)

    if not _health_check(slug, con, timeout=60):
        con.print("[red]✗ Health échoué — rollback lourd (restore volume + git reset).[/red]")
        if not dry_run and backup_path and backup_path.exists():
            subprocess.run(["docker", "volume", "rm", dst_vol], capture_output=True, timeout=15)
            subprocess.run(["docker", "volume", "create", dst_vol], capture_output=True, timeout=10)
            # Monte dst_vol à /data (symétrique au backup) + extrait -C / pour que data/... atterrisse DANS le volume.
            with open(backup_path, "rb") as f:
                subprocess.run(["docker", "run", "--rm", "-i", "-v", f"{dst_vol}:/data",
                                "alpine", "tar", "xzf", "-", "-C", "/"],
                               stdin=f, capture_output=True, timeout=120)
        _rollback(slug, prod_dir, prev_commit, snapshot=None, console=con, dry_run=dry_run)
        return

    con.print(f"[green]✓ « {slug} » déployée (DB atelier → prod). Atelier conservé.[/green]")


def action_workshop_remove(console, app_name: str, *, dry_run: bool = False) -> None:
    """Supprime l'atelier X-ws (down -v + backup + trash) et restart la prod X si présente."""
    con = console or _console

    dev_marker = hub_state.HUB_DIR / ".dev-mode"
    if not dev_marker.exists():
        dev_marker.touch()
        con.print("[dim].dev-mode créé (compatibilité)[/dim]")
    if hub_state.check_docker() is False:
        con.print("[red]✗ Docker injoignable.[/red]")
        return

    ws_dir = hub_state.WORKSHOP_DIR / app_name
    if not ws_dir.exists():
        con.print(f"[red]✗ Atelier introuvable : {ws_dir}[/red]")
        return
    compose_file = ws_dir / "docker-compose.yml"
    base = app_name[:-3] if app_name.endswith("-ws") else app_name
    prod_dir = hub_state.APPS_DIR / base

    con.print(f"[bold]→ Arrêt + suppression volume atelier « {app_name} »…[/bold]")
    # SEUL endroit légitime pour down -v dans tout le cycle Workshop.
    if compose_file.exists():
        utils.run_shell(["docker", "compose", "-f", str(compose_file), "down", "-v"],
                        cwd=str(ws_dir), dry_run=dry_run, console=con, timeout=60)

    # Backup tar.gz (sécurité avant suppression définitive)
    backups = hub_state.PARC_ROOT / "hubctl_cli" / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = backups / f"{app_name}-{stamp}.tar.gz"
    con.print(f"[bold]→ Backup atelier → {archive.name}[/bold]")
    if dry_run:
        con.print(f"[dim]$ tar czf {archive} {ws_dir.name} (dry-run)[/dim]")
    else:
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(str(ws_dir), arcname=ws_dir.name)

    con.print(f"[bold]→ Déplacement atelier vers corbeille…[/bold]")
    if dry_run:
        con.print(f"[dim]$ mv {ws_dir} → _trash/{app_name}-{stamp} (dry-run)[/dim]")
    else:
        trash = hub_state.PARC_ROOT / "hubctl_cli" / "_trash"
        trash.mkdir(parents=True, exist_ok=True)
        shutil.move(str(ws_dir), str(trash / f"{app_name}-{stamp}"))

    # Restart prod si elle existe
    if prod_dir.exists() and (prod_dir / "docker-compose.yml").exists():
        con.print(f"[bold]→ Redémarrage prod « {base} »…[/bold]")
        utils.run_shell(["make", "-C", str(prod_dir), "up"],
                        dry_run=dry_run, console=con, timeout=60)
    else:
        con.print(f"[yellow]⚠ Prod « {base} » absente — aucun restart.[/yellow]")

    con.print(f"[green]✓ Atelier « {app_name} » supprimé.[/green]")


def _workshop_git_hygiene(ws_dir: Path, console=None) -> None:
    """Warnings non bloquants : atelier sale ou commits non poussés."""
    con = console or _console
    if not ws_dir.exists():
        return
    try:
        dirty = subprocess.run(["git", "-C", str(ws_dir), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=10)
        if dirty.returncode == 0 and dirty.stdout.strip():
            con.print("[yellow]⚠ Atelier sale : changements non commités (git pull ne les ramènera pas).[/yellow]")
        ahead = subprocess.run(["git", "-C", str(ws_dir), "log", "@{u}..HEAD", "--oneline"],
                               capture_output=True, text=True, timeout=10)
        if ahead.returncode == 0 and ahead.stdout.strip():
            con.print("[yellow]⚠ Commits non poussés depuis l'atelier (git pull ne ramènera rien).[/yellow]")
    except (subprocess.SubprocessError, FileNotFoundError):
        pass


def action_prod_deploy(console, slug: str, *, copy_db: bool = False, dry_run: bool = False) -> None:
    """Déploie l'atelier X-ws vers la prod APPS/X.

    - copy_db=False (DÉFAUT) : git pull code-only, DB prod préservée.
    - copy_db=True  : écrase la DB prod par celle de l'atelier (destructif, CLI-only).
    """
    con = console or _console

    # ── 1. CHECK ──
    dev_marker = hub_state.HUB_DIR / ".dev-mode"
    if not dev_marker.exists():
        dev_marker.touch()
        con.print("[dim].dev-mode créé (compatibilité)[/dim]")
    if hub_state.check_docker() is False:
        con.print("[red]✗ Docker injoignable.[/red]")
        return

    prod_dir = hub_state.APPS_DIR / slug
    ws_dir = hub_state.WORKSHOP_DIR / f"{slug}-ws"
    if not prod_dir.exists():
        con.print(f"[red]✗ Prod introuvable : {prod_dir}[/red]")
        con.print("[dim]    Si l'app était en atelier avant la refonte, utilise --copy-db ou restaure la prod.[/dim]")
        return
    compose_file = prod_dir / "docker-compose.yml"
    if not compose_file.exists():
        con.print(f"[red]✗ docker-compose.yml manquant dans {prod_dir}[/red]")
        return
    if not ws_dir.exists():
        con.print(f"[yellow]⚠ Atelier {ws_dir} introuvable — seul le code prod sera mis à jour.[/yellow]")

    if not disk_guard(con):
        return

    if copy_db:
        action_prod_deploy_copy_db(con, slug, prod_dir, ws_dir, compose_file, dry_run=dry_run)
        return

    # ── CHEMIN CODE-ONLY ──
    con.print(f"[bold]→ Déployer « {slug}-ws » vers la prod (code-only, DB préservée)[/bold]")
    _workshop_git_hygiene(ws_dir, con)

    prev_commit = _git_previous_commit(prod_dir, con)

    con.print("[bold]→ git pull prod…[/bold]")
    utils.run_shell(["git", "-C", str(prod_dir), "pull", "--recurse-submodules"],
                    dry_run=dry_run, console=con)

    con.print(f"[bold]→ Build « {slug} »…[/bold]")
    utils.run_shell(["make", "-C", str(prod_dir), "build"],
                    dry_run=dry_run, console=con, timeout=600)

    con.print(f"[bold]→ Démarrage « {slug} »…[/bold]")
    utils.run_shell(["make", "-C", str(prod_dir), "up"],
                    dry_run=dry_run, console=con, timeout=60)

    if not _health_check(slug, con, timeout=60):
        con.print("[red]✗ Health échoué — rollback.[/red]")
        _rollback(slug, prod_dir, prev_commit, snapshot=None, console=con, dry_run=dry_run)
        return

    con.print(f"[green]✓ « {slug} » déployée (code-only). Atelier conservé.[/green]")


def _read_prod_ports(target: str) -> tuple[int, int, int]:
    """Lit les ports prod d'une cible depuis son .env."""
    if target == "hub":
        env_path = hub_state.HUB_DIR / ".env"
        env = env_manager.read_env(env_path) if env_path.exists() else {}
        return (int(env.get("HUB_API_PORT", "8000")),
                int(env.get("HUB_FRONTEND_PORT", "80")),
                int(env.get("HUB_HTTPS_PORT", "443")))
    app_env = hub_state.active_apps_dir() / target / ".env"
    env = env_manager.read_env(app_env) if app_env.exists() else {}
    return (int(env.get("API_PORT", "8000")),
            int(env.get("FRONTEND_PORT", "80")),
            int(env.get("FRONTEND_HTTPS_PORT", "443")))


def _build_test_env(target: str, ports: tuple[int, int, int]) -> dict:
    """Construit l'environnement d'override de ports pour l'instance verte."""
    env = os.environ.copy()
    api, fe, https = ports
    if target == "hub":
        env["HUB_API_PORT"] = str(api)
        env["HUB_FRONTEND_PORT"] = str(fe)
        env["HUB_HTTPS_PORT"] = str(https)
    else:
        env["API_PORT"] = str(api)
        env["FRONTEND_PORT"] = str(fe)
        env["FRONTEND_HTTPS_PORT"] = str(https)
    return env


def action_deploy(console, target: str, *, validate: bool = False, dry_run: bool = False) -> None:
    """Déploiement blue-green : instance verte sur ports+1 → validation → swap.
    Flow détaillé : spec 2026-07-12-hubctl-refonte-quickwins-design.md §B.2.
    """
    import sys
    con = console or _console
    if validate and not sys.stdin.isatty():
        con.print("[yellow]--validate requiert un TTY ; bascule en mode auto.[/yellow]")
        validate = False
    if not disk_guard(con):
        return
    target_path = hub_state.HUB_DIR if target == "hub" else hub_state.active_apps_dir() / target
    compose_file = target_path / "docker-compose.yml"
    if not compose_file.exists():
        con.print(f"[red]✗ docker-compose.yml introuvable dans {target_path}[/red]")
        return

    # 1. Capture rollback
    prev_commit = _git_previous_commit(target_path, con)
    if dry_run:
        snapshot = None
        con.print("[dim]$ pg_dump ... (dry-run, snapshot skippé)[/dim]")
    else:
        snapshot = _db_snapshot(con) if target == "hub" else _db_snapshot_app(target, con)
    db_container = None if target == "hub" else f"{target}-postgres-1"
    db_name = None if target == "hub" else target

    # 2. git pull
    con.print(f"[bold]→ git pull ({target})[/bold]")
    rc = utils.run_shell(["git", "-C", str(target_path), "pull", "--recurse-submodules"],
                         dry_run=dry_run, console=con)
    if rc != 0:
        rc = utils.run_shell(["git", "-C", str(target_path), "pull"], dry_run=dry_run, console=con)
    if rc != 0:
        con.print("[red]✗ git pull échoué — rollback et abort.[/red]")
        _rollback(target, target_path, prev_commit, snapshot, con,
                  db_container=db_container, db_name=db_name, dry_run=dry_run)
        return

    # 3. build
    con.print(f"[bold]→ Build de « {target} »[/bold]")
    rc = utils.run_shell(["make", "-C", str(target_path), "build"],
                         dry_run=dry_run, console=con, timeout=600)
    if rc != 0:
        con.print("[red]✗ Build échoué — rollback et abort.[/red]")
        _rollback(target, target_path, prev_commit, snapshot, con,
                  db_container=db_container, db_name=db_name, dry_run=dry_run)
        return

    # 4. ports test libres
    base = _read_prod_ports(target)
    test_ports = _find_free_ports(*base)
    con.print(f"[bold]→ Instance verte sur ports API={test_ports[0]} FE={test_ports[1]} HTTPS={test_ports[2]}[/bold]")

    # 5. instance verte
    project = f"{target}-test"
    test_env = _build_test_env(target, test_ports)
    rc = utils.run_shell(["docker", "compose", "-p", project, "-f", str(compose_file), "up", "-d"],
                         dry_run=dry_run, console=con, timeout=120, env=test_env)
    if rc != 0:
        con.print("[red]✗ Instance verte échouée — rollback et abort.[/red]")
        _rollback(target, target_path, prev_commit, snapshot, con,
                  db_container=db_container, db_name=db_name, dry_run=dry_run)
        return

    # 6. restore snapshot dans db-test (hub only — apps partagent la DB via volume séparé)
    if target == "hub" and snapshot and snapshot.exists():
        con.print("[bold]→ Restauration snapshot dans db-test…[/bold]")
        if dry_run:
            con.print("[dim]$ docker exec -i hub-test-db-1 psql -U hubadmin hub_auth < snapshot (dry-run)[/dim]")
        else:
            subprocess.run(
                ["docker", "exec", "-i", "hub-test-db-1", "psql", "-U", "hubadmin", "hub_auth"],
                input=snapshot.read_bytes(), capture_output=True, timeout=30,
            )

    # 7. health check port test
    if dry_run:
        con.print(f"[dim]$ curl localhost:{test_ports[0]}/api/health (dry-run, simulé OK)[/dim]")
    elif not _health_check(target, con, timeout=60, port_override=str(test_ports[0])):
        con.print("[red]✗ Health check vert échoué — rollback et cleanup vert.[/red]")
        utils.run_shell(["docker", "compose", "-p", project, "-f", str(compose_file), "down", "-v"],
                        dry_run=dry_run, console=con, timeout=60)
        _rollback(target, target_path, prev_commit, snapshot, con,
                  db_container=db_container, db_name=db_name, dry_run=dry_run)
        return

    # 8. validation humaine (opt-in)
    if validate:
        con.print(f"[bold cyan]🌐 Instance verte : http://localhost:{test_ports[1]} | https://localhost:{test_ports[2]}[/bold cyan]")
        # widgets.confirm nécessite un Canvas TUI ; en CLI on utilise input()
        try:
            answer = input("Tout OK ? Passer en prod ? [o/N] : ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("o", "oui", "y", "yes"):
            con.print("[yellow]↩ Annulé — cleanup instance verte, prod intacte.[/yellow]")
            utils.run_shell(["docker", "compose", "-p", project, "-f", str(compose_file), "down", "-v"],
                            dry_run=dry_run, console=con, timeout=60)
            # restaurer le disque (git pull a eu lieu)
            _rollback(target, target_path, prev_commit, snapshot, con,
                      db_container=db_container, db_name=db_name, dry_run=dry_run)
            return

    # 9. swap
    con.print(f"[bold]→ Swap : arrêt prod actuelle…[/bold]")
    utils.run_shell(["docker", "compose", "-p", target, "-f", str(compose_file), "down"],
                    dry_run=dry_run, console=con, timeout=60)
    con.print(f"[bold]→ Swap : démarrage nouvelle prod…[/bold]")
    rc = utils.run_shell(["docker", "compose", "-p", target, "-f", str(compose_file), "up", "-d"],
                         dry_run=dry_run, console=con, timeout=120)
    if rc != 0:
        swap_ok = False
    elif dry_run:
        prod_port = _resolve_api_port(target) or "8000"
        con.print(f"[dim]$ curl localhost:{prod_port}/api/health (dry-run, simulé OK)[/dim]")
        swap_ok = True
    else:
        swap_ok = _health_check(target, con, timeout=60)
    if not swap_ok:
        con.print("[red]✗ Swap échoué — rollback (relance ancienne prod).[/red]")
        _rollback(target, target_path, prev_commit, snapshot, con,
                  db_container=db_container, db_name=db_name, dry_run=dry_run)
        return

    # 10. cleanup vert
    con.print("[bold]→ Cleanup instance verte…[/bold]")
    utils.run_shell(["docker", "compose", "-p", project, "-f", str(compose_file), "down", "-v"],
                    dry_run=dry_run, console=con, timeout=60)
    con.print(f"[green]✓ « {target} » déployé en prod (blue-green)[/green]")


def action_work(console, slug: str, *, dry_run: bool = False) -> None:
    """Met une ou plusieurs apps en atelier (APPS → APP_Workshop, clone DB).

    Accepte un slug simple ("X") ou plusieurs séparés par virgules ("X,Y,Z").
    La prod est stoppée mais conservée pour chaque app.
    Si un slug échoue, les suivants sont quand même traités (best-effort).
    """
    con = console or _console
    slugs = [s.strip() for s in slug.split(",") if s.strip()]
    if not slugs:
        con.print("[red]✗ Aucun slug fourni.[/red]")
        return
    ok_count = 0
    for s in slugs:
        con.print(f"[bold]→ Mise en atelier de « {s} »[/bold]")
        try:
            action_workshop_create(con, s, dry_run=dry_run)
            con.print(f"[green]✅ {s} prête en atelier (APP_Workshop/{s}-ws). Prod stoppée (en place).[/green]")
            ok_count += 1
        except Exception:
            con.print(f"[red]✗ Échec de la mise en atelier de « {s} ».[/red]")
    if len(slugs) > 1:
        fail_count = len(slugs) - ok_count
        if fail_count:
            con.print(f"[yellow]⚠ {ok_count}/{len(slugs)} app(s) mises en atelier, {fail_count} échec(s).[/yellow]")
        else:
            con.print(f"[green]✅ {ok_count}/{len(slugs)} app(s) mises en atelier.[/green]")


def action_ship(console, slug: str, *, copy_db: bool = False, dry_run: bool = False) -> None:
    """Déploie l'atelier vers la prod.

    - défaut (copy_db=False) : git pull code-only, DB prod préservée.
    - copy_db=True : écrase la DB prod (destructif) — réservé à `hubctl ship X --copy-db`.
    """
    con = console or _console
    if copy_db:
        con.print("\n[bold yellow]⚠️  Mode --copy-db : la DB de prod sera ÉCRASÉE par celle de l'atelier.[/bold yellow]")
    action_prod_deploy(con, slug, copy_db=copy_db, dry_run=dry_run)
