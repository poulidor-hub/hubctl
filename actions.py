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


_PG_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _build_alter_user_sql(pg_user: str, pg_password: str) -> str:
    """Construit l'instruction `ALTER USER ... PASSWORD '...'` de façon sûre.

    Le mot de passe vient du `.env` (éditable à la main) : il peut contenir une
    apostrophe, ce qui ouvrait une injection SQL (l'ancienne interpolation
    `f"...'{pg_password}'"` cassait le bootstrap silencieusement, voire exécutait
    du SQL involontaire). On valide l'identifiant `pg_user` et on double les
    quotes du password (échappement SQL standard pour les string literals).
    """
    if not _PG_IDENT_RE.match(pg_user or ""):
        raise ValueError(f"POSTGRES_USER invalide (identifiant attendu): {pg_user!r}")
    escaped = (pg_password or "").replace("'", "''")
    return f"ALTER USER {pg_user} WITH PASSWORD '{escaped}';"


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
    # Le mot de passe contient des caractères spéciaux → on passe via stdin psql,
    # ET on échappe les quotes pour éviter toute injection SQL (cf. _build_alter_user_sql).
    try:
        alter_sql = _build_alter_user_sql(pg_user, pg_password)
    except ValueError as e:
        con.print(f"[yellow]⚠ {e} — sync du mot de passe annulée.[/yellow]")
        return
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


def _load_seed_file(path) -> list[tuple[str, str]]:
    """Parse un fichier seed YAML. Retourne [(url, slug), ...].

    Lève FileNotFoundError si le fichier n'existe pas.
    Lève ValueError si le contenu est invalide (pas de clé apps, URL invalide, slug dupliqué).
    """
    import yaml
    from pathlib import Path
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Fichier seed introuvable : {path}")
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError("Le fichier seed doit être un dictionnaire YAML avec une clé 'apps'.")
    apps = data.get("apps")
    if not isinstance(apps, list):
        raise ValueError("Aucune entrée 'apps' (liste non vide) dans le fichier seed.")
    # empty list is valid → return [] (nothing to deploy)
    result: list[tuple[str, str]] = []
    seen_slugs: set[str] = set()
    for entry in apps:
        url = entry.strip() if isinstance(entry, str) else ""
        if not url or ("/" not in url and ":" not in url):
            raise ValueError(f"URL git invalide : {url}")
        name = repo_name_from_url(url)
        if not name:
            raise ValueError(f"URL git invalide : {url}")
        slug = name.lower().replace("_", "-")
        if not valid_app_name(slug):
            raise ValueError(f"Slug invalide dérivé de {url} : {slug}")
        if slug in seen_slugs:
            raise ValueError(f"Slug dupliqué : {slug} (deux URLs → même slug)")
        seen_slugs.add(slug)
        result.append((url, slug))
    return result


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
        "FRONTEND_HTTPS_PORT": str(config["frontend_port"] + 1),
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


def app_make(app_name: str, target: str, *, dry_run: bool, console=None, timeout: int = 120, apps_dir: str | None = None) -> int:
    base = Path(apps_dir) if apps_dir else hub_state.active_apps_dir()
    return utils.run_shell(
        ["make", "-C", str(base / app_name), target],
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
    vault_path = hub_state.PARC_ROOT / "hubctl_cli" / "vault" / "parc.vault.enc"
    return Vault(vault_path)


def _sync_app_coffre(console, name, *, dry_run: bool = False) -> None:
    """Pousse le .env de l'app vers le coffre chiffré. Non-bloquant."""
    con = console or _console
    if dry_run:
        con.print(f"[dim](dry-run) Sync coffre : {name}[/dim]")
        return
    try:
        from hubctl_cli.vault_manager import VaultManager
        vault = _get_vault()
        # Le vault peut être verrouillé — on ne force pas le déverrouillage
        if vault.is_locked:
            con.print(f"[yellow]⚠ Coffre verrouillé — secrets de {name} non sauvegardés. Sync manuelle via le menu Coffre.[/yellow]")
            return
        mgr = VaultManager(vault)
        mgr.backup_all()
        con.print(f"[green]✓ Secrets de {name} sauvegardés dans le coffre[/green]")
    except Exception as e:
        con.print(f"[yellow]⚠ Sync coffre échoué ({e}) — l'app est démarrée, le coffre peut être sync plus tard.[/yellow]")


def _seed_one_app(console, url: str, slug: str, *, dry_run: bool = False) -> tuple[bool, str]:
    """Déploie ou met à jour UNE app. Retourne (ok, detail). Ne lève jamais.

    CAS A : dossier absent → clone + detect + write_env + build_start + sync coffre
    CAS B : dossier sans .env → pull + detect + write_env + build_start + sync coffre
    CAS C : dossier avec .env → pull + build + up + health (.env PRÉSERVÉ)
    """
    con = console or _console
    dest = hub_state.active_apps_dir() / slug

    # Mode dry-run : simulation pure, aucune sous-fonction exécutée
    if dry_run:
        if not dest.exists():
            con.print(f"[dim](dry-run) clone {url} → init → build → up[/dim]")
            return (True, "(simulé) serait déployée")
        if not (dest / ".env").exists():
            con.print(f"[dim](dry-run) pull → init → build → up : {slug}[/dim]")
            return (True, "(simulé) serait initialisée")
        con.print(f"[dim](dry-run) pull → build → up : {slug} (.env préservé)[/dim]")
        return (True, "(simulé) serait mise à jour")

    has_env = (dest / ".env").exists()
    try:
        if not dest.exists():
            # ── CAS A : nouvelle app ──
            if not disk_guard(con):
                return (False, "espace disque insuffisant")
            if not _clone_app(con, url, dest, dry_run=False):
                return (False, "clone failed")
            if not _validate_app_structure(con, dest, dry_run=False):
                return (False, "structure template invalide")
            config = _detect_app_config(slug)
            _write_app_env(con, dest, config, dry_run=False)
            if not _build_and_start_app(con, dest, config, dry_run=False):
                return (False, "build/start failed")
            _sync_app_coffre(con, slug, dry_run=False)
            return (True, f"déployée (ports {config['api_port']}/{config['frontend_port']})")

        if not has_env:
            # ── CAS B : clonée, jamais initialisée ──
            rc = utils.run_shell(["git", "-C", str(dest), "pull"], dry_run=False, console=con)
            if rc != 0:
                return (False, "git pull failed")
            config = _detect_app_config(slug)
            _write_app_env(con, dest, config, dry_run=False)
            if not _build_and_start_app(con, dest, config, dry_run=False):
                return (False, "build/start failed")
            _sync_app_coffre(con, slug, dry_run=False)
            return (True, f"initialisée (ports {config['api_port']}/{config['frontend_port']})")

        # ── CAS C : déjà déployée (convergence) — .env PRÉSERVÉ ──
        rc = utils.run_shell(["git", "-C", str(dest), "pull"], dry_run=False, console=con)
        if rc != 0:
            return (False, "git pull failed")
        if app_make(slug, "build", dry_run=False, console=con, timeout=600) != 0:
            return (False, "build failed")
        if app_make(slug, "up", dry_run=False, console=con, timeout=120) != 0:
            return (False, "up failed")
        if not _health_check(slug, console=con, timeout=60):
            return (False, "health check failed")
        return (True, "mise à jour")

    except Exception as e:
        return (False, f"erreur : {e}")


def action_remove_app(console, name: str, vault=None, *, dry_run: bool = False) -> None:
    """Supprime physiquement une app : compose down -v + rm -rf dossier.

    Utilisé par ``seed --prune`` pour retirer les apps non listées dans le seed.
    Préserve un backup tar.gz du dossier avant suppression (best-effort).
    """
    con = console or _console
    app_dir = hub_state.APPS_DIR / name
    compose_file = app_dir / "docker-compose.yml"

    con.print(f"[bold]→ Suppression de « {name} »…[/bold]")

    # 1. compose down -v (stop conteneurs + volumes)
    if compose_file.exists():
        utils.run_shell(["docker", "compose", "-f", str(compose_file), "down", "-v"],
                        cwd=str(app_dir), dry_run=dry_run, console=con, timeout=60)

    # 2. backup tar.gz best-effort puis rm -rf
    if app_dir.exists():
        backups = hub_state.PARC_ROOT / "hubctl_cli" / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        archive = backups / f"{name}-{stamp}.tar.gz"
        con.print(f"[dim]→ Backup {archive.name}[/dim]")
        if not dry_run:
            try:
                with tarfile.open(archive, "w:gz") as tar:
                    tar.add(str(app_dir), arcname=app_dir.name)
                if archive.exists():
                    os.chmod(archive, 0o600)
            except (OSError, tarfile.TarError):
                con.print(f"[yellow]⚠ Backup échoué, suppression quand même.[/yellow]")

        con.print(f"[dim]→ rm -rf {app_dir.name}[/dim]")
        if not dry_run:
            shutil.rmtree(str(app_dir), ignore_errors=True)

    con.print(f"[green]✓ « {name} » supprimée[/green]")


def _seed_prune(console, listed_slugs: set[str], vault, *, dry_run: bool = False) -> int:
    """Supprime les apps présentes dans le dossier actif mais non listées dans le seed.

    Affiche la liste et demande une confirmation globale Oui/Non.
    Retourne le nombre d'apps supprimées.
    """
    con = console or _console
    present: set[str] = set()
    try:
        for app in hub_state.get_active_apps():
            present.add(app.name)
    except Exception:
        pass
    to_remove = sorted(present - listed_slugs)
    if not to_remove:
        con.print("[dim]Rien à supprimer (--prune).[/dim]")
        return 0
    con.print("[yellow]Apps à supprimer (non listées dans le seed) :[/yellow]")
    for name in to_remove:
        con.print(f"  • {name}")
    try:
        answer = input("  Supprimer ces apps ? [o/N] : ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    if answer not in ("o", "oui", "y", "yes"):
        con.print("[dim]Prune annulé.[/dim]")
        return 0
    count = 0
    for name in to_remove:
        action_remove_app(con, name, vault, dry_run=dry_run)
        count += 1
    return count


def _seed_report(console, results: list[tuple[str, bool, str]]) -> None:
    """Affiche le tableau de synthèse final."""
    con = console or _console
    from rich.table import Table
    t = Table(title="Résultat du seed")
    t.add_column("App", style="bold")
    t.add_column("Résultat", style="bold")
    t.add_column("Détail")
    for slug, ok, detail in results:
        icon = "✓" if ok else "✗"
        style = "green" if ok else "red"
        t.add_row(slug, f"[{style}]{icon}[/{style}]", detail)
    con.print()
    con.print(t)
    ok_count = sum(1 for _, ok, _ in results if ok)
    con.print(f"[bold]{ok_count}/{len(results)} app(s) réussies[/bold]")


def action_seed(console, seed_file: str, *, prune: bool = False, dry_run: bool = False) -> int:
    """Déploie/converge les apps listées dans un fichier seed YAML.

    Retourne 0 si toutes les apps ont réussi, 1 si au moins un échec.
    """
    con = console or _console

    # 1. Précheck hub (skippé en dry-run)
    if not dry_run:
        con.print("[cyan]♢ Précheck : le hub doit être sain…[/cyan]")
        if not _health_check("hub", console=con, timeout=30):
            con.print("[red]✗ Hub non disponible. Lancez `hubctl start` d'abord.[/red]")
            return 1

    # 2. Lecture du fichier seed
    from pathlib import Path
    try:
        entries = _load_seed_file(Path(seed_file))
    except (FileNotFoundError, ValueError) as e:
        con.print(f"[red]✗ {e}[/red]")
        return 1
    if not entries:
        con.print("[dim]Aucune app à déployer.[/dim]")
        return 0

    con.print(f"[bold]Seed : {len(entries)} app(s) à traiter[/bold]")

    # 3. Déploiement (best-effort)
    results: list[tuple[str, bool, str]] = []
    for url, slug in entries:
        con.print()
        con.print(f"[bold cyan]▸ {slug}[/bold cyan]")
        ok, detail = _seed_one_app(con, url, slug, dry_run=dry_run)
        results.append((slug, ok, detail))

    # 4. Prune (optionnel)
    if prune:
        con.print()
        listed = {slug for _, slug in entries}
        vault = _get_vault()
        _seed_prune(con, listed, vault, dry_run=dry_run)

    # 5. Rapport final + code de sortie
    _seed_report(con, results)
    return 0 if all(ok for _, ok, _ in results) else 1


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
        with open(path, "w") as f:
            subprocess.run(
                ["docker", "exec", "hub-db", "pg_dump", "-U", "hubadmin", "hub_auth"],
                stdout=f, stderr=subprocess.PIPE, timeout=30, check=True,
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


def _is_healthy_body(text: str | None) -> bool:
    """True si le corps de réponse /api/health indique un statut « healthy ».

    Parse réellement le JSON (et non une sous-chaîne littérale) afin de reconnaître
    `{"status": "healthy"}` (espaces après `:`, format json.dumps par défaut) qui
    échouait avec l'ancien test `'"status":"healthy"' in stdout` → faux négatif
    → rollbacks destructeurs sur une API pourtant saine.
    """
    if not text:
        return False
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and data.get("status") == "healthy"


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
    start = time.monotonic()
    deadline = start + timeout
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["curl", "-sk", health_url],
                capture_output=True, text=True, timeout=5,
            )
            if _is_healthy_body(result.stdout):
                con.print(f"[green]✓ Hub healthy après {int(time.monotonic() - start)}s[/green]")
                return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            # FileNotFoundError = curl absent ; on continue à réessayer jusqu'au timeout.
            pass
        time.sleep(3)
        con.print(f"  ... attente {int(time.monotonic() - start)}s/{timeout}s")
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


# ── Helpers d'idempotence (éviter de redémarrer/remettre à jour inutilement) ──


def _app_running(app_name: str) -> bool:
    """Vérifie si les conteneurs Docker de l'app tournent déjà."""
    norm = hub_state._norm(app_name)
    containers = hub_state._docker_api_containers()
    if norm in containers:
        state, _ = containers[norm]
        return state == "running"
    return False


def _hub_running() -> bool:
    """Vérifie si les services du Hub tournent déjà."""
    services = hub_state.get_hub_services()
    if not services:
        return False
    return all(s.state == "running" for s in services)


def _git_up_to_date(target_path, con) -> bool:
    """Vérifie si le dépôt git est déjà à jour avec le remote (fetch silencieux)."""
    try:
        subprocess.run(
            ["git", "-C", str(target_path), "fetch", "--quiet"],
            capture_output=True, timeout=30,
        )
        head = subprocess.run(
            ["git", "-C", str(target_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        upstream = subprocess.run(
            ["git", "-C", str(target_path), "rev-parse", "@{u}"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        return head == upstream
    except Exception:
        return False  # en cas d'erreur, on considère qu'il faut mettre à jour (safe)


def _start_hub(console, *, dry_run: bool) -> bool:
    if _hub_running():
        console.print("[dim]✓ hub déjà démarré[/dim]")
        return True
    _ensure_jwt_keys(console, dry_run=dry_run)
    rc = hub_make("up-prod", dry_run=dry_run, console=console)
    if rc == 0:
        _health_check("hub", console=console, timeout=60)
    return rc == 0


def _start_app(console, app_name: str, *, dry_run: bool, apps_dir: str | None = None) -> bool:
    if _app_running(app_name):
        console.print(f"[dim]✓ {app_name} déjà démarré[/dim]")
        return True
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
    if not _app_running(app_name):
        console.print(f"[dim]✓ {app_name} déjà arrêté[/dim]")
        return True
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
            if not _hub_running():
                con.print("[dim]✓ hub déjà arrêté[/dim]")
            else:
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

    # Idempotence : si git est déjà à jour, on saute tout le cycle.
    if _git_up_to_date(target_path, con):
        con.print(f"[dim]✓ {target} déjà à jour[/dim]")
        return True

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

        # Garde-fou : la prod a été stoppée ci-dessus. Toute exception non gérée
        # dans la création (copytree, volume create, write_env…) doit garantir le
        # redémarrage de la prod, sinon elle reste DOWN.
        try:
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
                subprocess.run(["docker", "volume", "create", dst_vol],
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
        except Exception as e:
            con.print(f"[red]✗ Erreur pendant la création de l'atelier : {e}[/red]")
            con.print(f"[red]  ROLLBACK de sécurité : nettoyage + redémarrage de la prod {source_slug}.[/red]")
            if not dry_run:
                try:
                    if dest_dir.exists():
                        shutil.rmtree(str(dest_dir))
                except OSError:
                    pass
                utils.run_shell(["docker", "compose", "-f", str(compose_file), "up", "-d"],
                                cwd=str(source_dir), dry_run=False, console=con, timeout=60)
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


