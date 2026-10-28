from __future__ import annotations
import datetime
import ipaddress
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
from hubctl_cli import park_checks

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


def _lan_cidr_from_ip(ip: str) -> str | None:
    """Déduit le CIDR /24 du subnet LAN de l'IP hôte détectée.

    Retourne None si l'IP est loopback/IPv6/invalide (aucun LAN exploitable
    → l'appelant conserve la whitelist legacy).
    """
    import ipaddress as _ipaddress
    try:
        addr = _ipaddress.ip_address(ip)
    except ValueError:
        return None
    if addr.is_loopback or addr.version != 4:
        return None
    return str(_ipaddress.ip_network(f"{ip}/24", strict=False))


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
    # Port API du template : une seule source pour HUB_API_PORT et OIDC_ISSUER
    hub_api_port = "8000"

    # APP_WHITELIST_NETWORKS : inclure le subnet LAN réel de l'hôte (le /24
    # de l'IP détectée) pour que allocate-ports / l'auto-enregistrement des
    # apps fonctionnent dès l'installation, quel que soit le réseau du
    # serveur. Fallback legacy si aucun réseau exploitable (IP loopback).
    _lan_cidr = _lan_cidr_from_ip(host_ip)
    app_whitelist = f"10.42.0.0/16,{_lan_cidr}" if _lan_cidr else "10.42.0.0/16,192.168.1.0/24"
    # Générer la Fernet key pour le chiffrement des secrets
    from cryptography.fernet import Fernet as _Fernet
    fernet_key = _Fernet.generate_key().decode() if not dry_run else "DRY_RUN_FERNET_KEY"

    # HOST_UID : uid de l'utilisateur qui exécute hubctl (pour les volumes montés)
    host_uid = str(os.getuid()) if not dry_run else "1000"

    # HOST_DOCKER_GID : gid du groupe docker sur l'hôte (pour le mount du socket
    # docker sur hub-api). Détecté via getent ; fallback 128 (Linux Mint default)
    # si le groupe docker n'existe pas encore (sera créé par install.sh plus tard).
    if not dry_run:
        try:
            _getent = subprocess.run(
                ["getent", "group", "docker"],
                capture_output=True, text=True, check=True,
            )
            host_docker_gid = _getent.stdout.split(":")[2]
        except (subprocess.CalledProcessError, IndexError, FileNotFoundError):
            host_docker_gid = "128"
    else:
        host_docker_gid = "128"

    # SA interne : secret pour le Service Account hub-internal (M2M HUB→lui-même)
    sa_secret = _secrets.token_urlsafe(32) if not dry_run else "DRY_RUN_SA_SECRET"

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
# 8h (LAN) — revenir à 30 si exposition externe
ACCESS_TOKEN_EXPIRE_MINUTES=480

# Admin par défaut
ADMIN_USERNAME=admin
ADMIN_PASSWORD={pwd}
ADMIN_EMAIL=admin@local.lan

# Ports
HUB_API_PORT={hub_api_port}
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
HOST_DOCKER_GID={host_docker_gid}

# Audit
AUDIT_SSE_COOLDOWN=5.0
AUDIT_RETENTION_DAYS=90
ERROR_RETENTION_DAYS=30
LOG_PURGE_INTERVAL_HOURS=24

# CORS
ALLOWED_ORIGINS=https://hub.local,https://{host_ip}
ALLOWED_ORIGIN_REGEX=

# Whitelist réseaux
APP_WHITELIST_NETWORKS={app_whitelist}

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

# OIDC Provider (bridge OIDC pour apps externes)
OIDC_ENABLED=true
OIDC_ISSUER=http://{host_ip}:{hub_api_port}
OIDC_EMAIL_TEMPLATE={{username}}@hub.internal
OIDC_CODE_TTL_SECONDS=30
OIDC_ID_TOKEN_TTL_MINUTES=10
OIDC_ACCESS_TOKEN_TTL_MINUTES=15
OIDC_REFRESH_TOKEN_DAYS=30
OIDC_BACKCHANNEL_LOGOUT_ENABLED=true
OIDC_SLO_ENABLED=false
OIDC_SLO_IFRAME_TIMEOUT_MS=1500

# Service Account interne du HUB (M2M)
HUB_INTERNAL_SA_CLIENT_ID=hub-internal
HUB_INTERNAL_SA_CLIENT_SECRET={sa_secret}
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
    ensure_hub_env(con, dry_run=dry_run)
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
    ensure_hub_env(con, dry_run=dry_run)
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
    pg_db = _read_hub_env_value("POSTGRES_DB", "hub_auth")
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
        ["docker", "exec", "-i", "hub-db", "psql", "-U", pg_user, "-d", pg_db],
        input=alter_sql, capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 0:
        con.print("[green]✓ Mot de passe PostgreSQL synchronisé avec le .env[/green]")
    else:
        con.print(f"[yellow]⚠ Sync mot de passe DB échoué : {result.stderr.strip()[:200]}[/yellow]")
        con.print("[yellow]  Si le hub-api échoue avec InvalidPasswordError, supprimez le volume :[/yellow]")
        con.print(f"[yellow]  docker volume rm {pg_volume_name}[/yellow]")


def _install_shell_marker(console) -> None:
    """Installe un marker shell portable pour le sourcing de .secrets.

    Rend le sourcing indépendant du chemin absolu du parc (qui peut varier
    d'une machine à l'autre). Stratégie :

    1. Écrit ``~/.config/poulidor/parc.sh`` contenant ``source <PARC_ROOT>/.secrets``
       (chemin absolu découvert via ``hub_state.PARC_ROOT``, robuste aux symlinks).
    2. Ajoute idempotemment une ligne à ``~/.bashrc`` qui source ce marker s'il existe.

    Le fichier ``~/.config/poulidor/parc.sh`` est réécrit à chaque ``hubctl init``
    (gère le cas où le parc a déménagé). La ligne dans ``~/.bashrc`` n'est ajoutée
    qu'une seule fois (idempotente sur la chaîne ``poulidor/parc.sh``).
    """
    con = console or _console
    import os as _os
    from pathlib import Path as _Path

    parc_root = str(hub_state.PARC_ROOT.resolve())
    config_dir = _Path(_os.path.expanduser("~/.config/poulidor"))
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        con.print(f"[yellow]⚠ Impossible de créer {config_dir} : {e}[/yellow]")
        return

    marker = config_dir / "parc.sh"
    marker_content = (
        "# Auto-généré par hubctl init — ne pas éditer manuellement\n"
        f"# Source les secrets API IA + credentials MCP hub pour le parc POULIDOR\n"
        f'# situé à : {parc_root}\n'
        f'[ -f "{parc_root}/.secrets" ] && set -a && . "{parc_root}/.secrets" && set +a\n'
    )
    try:
        marker.write_text(marker_content)
        marker.chmod(0o600)
    except OSError as e:
        con.print(f"[yellow]⚠ Impossible d'écrire {marker} : {e}[/yellow]")
        return

    # Idempotemment ajouter la ligne à ~/.bashrc
    bashrc = _Path(_os.path.expanduser("~/.bashrc"))
    try:
        bashrc.touch(exist_ok=True)
        bashrc_content = bashrc.read_text()
    except OSError as e:
        con.print(f"[yellow]⚠ Impossible de lire {bashrc} : {e}[/yellow]")
        return

    if "poulidor/parc.sh" in bashrc_content:
        con.print("[green]✓ ~/.bashrc source déjà ~/.config/poulidor/parc.sh[/green]")
        return

    marker_line = (
        "\n# POULIDOR — sourcing portable (auto-installé par hubctl init)\n"
        "[ -f ~/.config/poulidor/parc.sh ] && source ~/.config/poulidor/parc.sh\n"
    )
    try:
        with bashrc.open("a") as f:
            if bashrc_content and not bashrc_content.endswith("\n"):
                f.write("\n")
            f.write(marker_line)
        con.print("[green]✓ Ligne de sourcing ajoutée à ~/.bashrc (ré-ouvre ton shell ou : source ~/.bashrc)[/green]")
    except OSError as e:
        con.print(f"[yellow]⚠ Impossible d'écrire dans {bashrc} : {e}[/yellow]")


_ATELIER_UNIT = Path("/etc/systemd/system/hubctl-atelier.service")


def _ensure_atelier_service(console, dry_run: bool = False) -> None:
    """Étape `hubctl init` : service hôte hubctl-atelier (« ensure » idempotent).

    1. Token hôte ~/.config/hubctl/atelier_token — généré (secrets.token_hex,
       chmod 600) s'il est absent ou vide, JAMAIS écrasé sinon ;
    2. Copié vers HUB_base/secrets/atelier_token (créé seulement s'il manque) ;
    3. Unit systemd packaging/hubctl-atelier.service installée (sudo) si
       absente, service activé (`enable --now`) si inactif, rien si actif.

    N'échoue JAMAIS : sans TTY (prompt sudo impossible) ou en cas d'échec
    sudo, les commandes sont rappelées à la main — l'init ne doit pas casser
    à cause de cette étape.
    """
    con = console or _console
    import secrets as _secrets
    import sys as _sys
    # Import tardif : atelier_serve importe actions (routes du serveur) →
    # circularité si importé au niveau module.
    from hubctl_cli import atelier_serve

    # ── 1. Token hôte (chemin défini par atelier_serve.DEFAULT_TOKEN_FILE) ──
    token_file = atelier_serve.DEFAULT_TOKEN_FILE
    token: str | None = None
    try:
        if token_file.exists() and token_file.stat().st_size > 0:
            token = token_file.read_text(encoding="utf-8")
            con.print(f"[green]✓ Token atelier déjà présent : {token_file}[/green]")
        elif not dry_run:
            token = _secrets.token_hex(32)
            try:
                if token_file.exists() and token_file.stat().st_size == 0:
                    token_file.unlink()  # vide = inutilisable → recréation
                token_file.parent.mkdir(parents=True, exist_ok=True)
                # os.open O_EXCL + 0o600 : créé d'emblée owner-only, pas de
                # fenêtre umask (write_text+chmod laisserait un 644 lue).
                fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(token)
            except FileExistsError:
                # Course (autre process vient de créer le token) : on ne
                # touche JAMAIS à l'existant, on adopte le sien.
                token = token_file.read_text(encoding="utf-8")
            con.print(f"[green]✓ Token atelier généré : {token_file}[/green]")
        else:
            con.print(f"[dim](dry-run) génération du token {token_file}[/dim]")
    except OSError as e:
        token = None  # écriture KO : ne PAS copier un token absent de l'hôte
        con.print(f"[yellow]⚠ Token atelier ({token_file}) : {e}[/yellow]")

    # ── 2. Token Hub (HUB_base/secrets/atelier_token) : copie si absent ──
    hub_token = hub_state.HUB_DIR / "secrets" / "atelier_token"
    try:
        if dry_run:
            con.print(f"[dim](dry-run) copie du token vers {hub_token} (si absent)[/dim]")
        elif token is None:
            pass  # token hôte absent/illisible : rien à copier
        elif hub_token.exists():
            con.print(f"[green]✓ Token Hub déjà présent : {hub_token}[/green]")
        else:
            hub_token.parent.mkdir(parents=True, exist_ok=True)
            hub_token.write_text(token, encoding="utf-8")
            hub_token.chmod(0o600)
            con.print(f"[green]✓ Token copié vers {hub_token}[/green]")
    except OSError as e:
        con.print(f"[yellow]⚠ Token Hub ({hub_token}) : {e}[/yellow]")

    # ── 3. Unit systemd : installer / activer / rien ──
    gabarit = Path(__file__).resolve().parent.parent / "packaging" / "hubctl-atelier.service"
    if _ATELIER_UNIT.exists():
        try:
            st = subprocess.run(
                ["systemctl", "is-active", "hubctl-atelier"],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            st = None
        if st is not None and st.returncode == 0 and st.stdout.strip() == "active":
            con.print("[green]✓ Service atelier hubctl-atelier actif[/green]")
            return
        cmds = [["sudo", "systemctl", "enable", "--now", "hubctl-atelier"]]
    else:
        cmds = [
            ["sudo", "cp", str(gabarit), str(_ATELIER_UNIT)],
            ["sudo", "systemctl", "daemon-reload"],
            ["sudo", "systemctl", "enable", "--now", "hubctl-atelier"],
        ]

    # ── 4. Exécution (TTY requis pour le prompt sudo) ou rappel manuel ──
    # Filet jamais-fatal : stdin peut être None (daemon), l'étape système ne
    # doit JAMAIS casser l'init — toute imprévue → rappel des commandes.
    try:
        if dry_run or _sys.stdin.isatty():
            failed = False
            for cmd in cmds:
                if utils.run_shell(cmd, dry_run=dry_run, console=con) != 0:
                    failed = True
                    break
            if not failed:
                if dry_run:
                    con.print("[dim](dry-run) service hubctl-atelier installé + activé[/dim]")
                else:
                    con.print("[green]✓ Service atelier hubctl-atelier actif[/green]")
                return
    except Exception as e:
        con.print(f"[yellow]⚠ Service atelier (étape système) : {e}[/yellow]")

    # Pas de TTY (ou échec/imprévu sudo) : ne jamais faire échouer l'init —
    # rappeler les commandes à lancer à la main.
    con.print("[yellow]⚠ Service atelier : commandes à lancer à la main :[/yellow]")
    for cmd in cmds:
        con.print(f"[yellow]  {' '.join(cmd)}[/yellow]")


def _summary_urls(host_ip: str) -> list[tuple[str, str]]:
    """Lignes d'accès du résumé post-init, avec les ports réels du .env."""
    front = _read_hub_env_value("HUB_FRONTEND_PORT", "80")
    https = _read_hub_env_value("HUB_HTTPS_PORT", "443")
    api = _read_hub_env_value("HUB_API_PORT", "8000")
    https_url = f"https://{host_ip}" if https == "443" else f"https://{host_ip}:{https}"
    http_url = f"http://{host_ip}" if front == "80" else f"http://{host_ip}:{front}"
    return [("Hub (HTTPS)", https_url), ("Hub (HTTP)", http_url),
            ("API", f"http://{host_ip}:{api}")]


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

    # ── 6b. Installer le marker shell portable (sourcing .secrets) ──
    if not dry_run:
        _install_shell_marker(con)

    # ── 6c. Service hôte hubctl-atelier (token + unit systemd, ensure) ──
    _ensure_atelier_service(con, dry_run=dry_run)

    # ── 7. Résumé ──
    admin_username = credentials["admin_username"]
    host_ip = credentials["host_ip"]

    con.print("")
    con.print("[green bold]✅ Hub POULIDOR opérationnel ![/green bold]")
    con.print("")
    t = Table(title="Accès")
    t.add_column("Service", style="bold cyan")
    t.add_column("URL")
    for label, url in _summary_urls(host_ip):
        t.add_row(label, url)
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

    # Allocation officielle auprès du Hub : ports préservés par slug+host
    # (registre partagé — évite les collisions entre instances/seed) et
    # APP_SUBNET requis par les docker-compose récents. Fallback silencieux
    # sur les ports locaux si le Hub est injoignable (le sync-env de l'app
    # réinterrogera le Hub au build).
    app_subnet = None
    try:
        import json as _json
        import urllib.request as _ur

        payload = _json.dumps({"slug": slug, "host": host_ip}).encode()
        req = _ur.Request(
            f"http://{hub_host}:{hub_api_port}/api/apps/allocate-ports",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with _ur.urlopen(req, timeout=10) as resp:
            alloc = _json.loads(resp.read())
        if alloc.get("api_port"):
            api_port = int(alloc["api_port"])
        if alloc.get("frontend_port"):
            frontend_port = int(alloc["frontend_port"])
        app_subnet = alloc.get("subnet") or None
    except Exception:
        pass  # Hub injoignable : fallback local, l'app retentera au build

    return {
        "slug": slug,
        "api_port": api_port,
        "frontend_port": frontend_port,
        "app_subnet": app_subnet,
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
    if config.get("app_subnet"):
        t.add_row("Subnet", config["app_subnet"])
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
    # Subnet alloué par le Hub — requis par les docker-compose récents
    # (interpolation APP_SUBNET). Écrit dès la génération du .env : sans lui,
    # `make build` échoue avant que le sync-env de l'app n'ait pu intervenir.
    if config.get("app_subnet"):
        replacements["APP_SUBNET"] = config["app_subnet"]

    # Construire DATABASE_URL si présent dans le template
    db_url = f"postgresql+asyncpg://app_user:{config['postgres_password']}@db:5432/app_db"
    replacements["DATABASE_URL"] = db_url

    for key, val in replacements.items():
        content = re.sub(rf"^{key}=.*$", f"{key}={val}", content, flags=re.MULTILINE)

    # HTTPS par défaut (spec 2026-09-19) : garantir HTTPS_ENABLED=true quand
    # le .env.example de l'app ne définit pas la clé. Une app qui veut du
    # HTTP le déclare explicitement (valeur respectée, jamais écrasée).
    if not re.search(r"^HTTPS_ENABLED=.*$", content, flags=re.MULTILINE):
        content = content.rstrip("\n") + "\nHTTPS_ENABLED=true\n"

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
    vault_path = hub_state.vault_path()
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
        backups = hub_state.backups_dir()
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
    backups = hub_state.backups_dir()
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
    """True si le corps de réponse /api/health indique un statut sain.

    Parse réellement le JSON (et non une sous-chaîne littérale) afin de reconnaître
    `{"status": "healthy"}` (espaces après `:`, format json.dumps par défaut) qui
    échouait avec l'ancien test `'"status":"healthy"' in stdout` → faux négatif
    → rollbacks destructeurs sur une API pourtant saine.

    Les deux formats réels du parc sont acceptés :
    - `"healthy"` : template courant (HUB_base & apps alignées) ;
    - `"ok"` : apps Node d'ancien template (ex. exemple-app) — leur
      /api/health répond `{"status":"ok", …}`. Même garde grossière que le
      template courant : la validation métier reste manuelle avant --prod.
    """
    if not text:
        return False
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and data.get("status") in ("healthy", "ok")


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


def _git_head_sha(path: Path) -> str | None:
    """SHA du HEAD d'un dépôt, ou None si indisponible."""
    r = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                       capture_output=True, text=True, timeout=10)
    return r.stdout.strip() if r.returncode == 0 else None


def _rollback(target: str, target_path: Path, prev_commit: str | None,
              snapshot: Path | None, console=None,
              db_container: str | None = None, db_name: str | None = None,
              dry_run: bool = False,
              meta_root: Path | None = None,
              prev_meta_sha: str | None = None,
              meta_clean: bool = True) -> None:
    """Rollback: revert git, restore DB snapshot, re-up.

    ``meta_root``/``prev_meta_sha`` (plan unification updates) : restaure
    d'abord le pointeur du méta-repo, AVANT le reset du sous-module —
    sinon le submodule réaligné sur l'ancien SHA réapparaîtrait comme
    « modifié » au prochain pull méta.
    """
    con = console or _console
    con.print(f"[bold red]↩ Rollback de « {target} »[/bold red]")

    if meta_root and prev_meta_sha and meta_clean:
        # C1 (revue) : le reset --hard du méta ne tourne QUE si le méta était
        # propre avant l'update — sinon il détruirait les modifications non
        # commitées que les pré-checks ont expressément tolérées.
        con.print(f"  méta reset → {prev_meta_sha[:8]} (pointeur submodule)")
        utils.run_shell(
            ["git", "-C", str(meta_root), "reset", "--hard", prev_meta_sha],
            dry_run=dry_run, console=con, timeout=15,
        )

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
        rc = utils.run_shell(["make", "-C", str(target_path), _up_target_for(target)],
                             dry_run=dry_run, console=con, timeout=60)
    else:
        utils.run_shell(["make", "-C", str(target_path), "build"],
                        dry_run=dry_run, console=con, timeout=600)
        rc = utils.run_shell(["make", "-C", str(target_path), _up_target_for(target)],
                             dry_run=dry_run, console=con, timeout=60)

    if rc != 0:
        con.print("[red]✗ Rollback incomplet — prod peut être DOWN. Intervention manuelle requise.[/red]")
    else:
        con.print("[green]✓ Rollback terminé[/green]")


def cleanup_orphan_tests(console=None) -> None:
    """Remove leftover -test docker-compose projects owned by hubctl.

    Ne nettoie QUE les projets {slug}-test où slug est le hub ou une app réelle
    (dans APPS/). Les projets -test étrangers (load-test,
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
    ensure_hub_env(console, dry_run=dry_run)
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
                ``\"dev\"`` → ateliers (docker compose -p X-ws).
                En mode dev, les ateliers manquants sont automatiquement créés
                (clone DB + 2e instance Compose) avant le démarrage.
    """
    if target not in ("all", "prod", "dev"):
        raise ValueError(f"target doit être 'all', 'prod' ou 'dev' (reçu : {target!r})")

    con = console or _console

    if target == "dev":
        # ── Phase 1 : créer les ateliers manquants (clone DB + compose up) ──
        _ws_session_ports.clear()  # reset allocation pour cette session
        prod_apps = hub_state.get_apps()
        created, skipped, failed = 0, 0, 0
        for app in prod_apps:
            if _ws_exists(app.name):
                skipped += 1
                continue
            con.print(f"\n[bold]── {app.name} ──[/bold]")
            if _create_workshop(con, app.name, dry_run=dry_run):
                created += 1
            else:
                failed += 1
        if created or skipped or failed:
            con.print(f"\n[bold]Ateliers : {created} créé(s), {skipped} déjà présent(s), {failed} échec(s)[/bold]")

        # ── Phase 2 : démarrer les ateliers existants en parallèle ──
        # (ceux qui viennent d'être créés sont déjà démarrés par _create_workshop)
        workshop_slugs = [a.name for a in prod_apps if _ws_exists(a.name)]
        if dry_run and created > 0:
            # En dry-run, .workshop.env n'est pas écrit → _ws_exists retourne False.
            # Les ateliers créés ci-dessus ont déjà affiché leurs commandes "up -d".
            con.print(f"[dim]Phase 2 ignorée (dry-run) : les {created} atelier(s) ci-dessus ont déjà affiché leurs commandes.[/dim]")
            return
        if not workshop_slugs:
            con.print("[dim]Aucun atelier à démarrer.[/dim]")
            return

        from concurrent.futures import ThreadPoolExecutor, as_completed
        tasks: dict = {}
        with ThreadPoolExecutor(max_workers=max(1, len(workshop_slugs))) as pool:
            for app_slug in workshop_slugs:
                ws_slug = _ws_project(app_slug)
                con.print(f"[bold]→ Démarrage de « {ws_slug} »…[/bold]")
                tasks[pool.submit(_start_workshop, con, app_slug, dry_run=dry_run)] = ws_slug

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
        return

    # ── Mode prod/all : démarrage classique ──
    apps = [a.name for a in hub_state.get_apps()]
    apps_dir = str(hub_state.APPS_DIR)

    if not apps:
        con.print("[dim]Aucune app active à démarrer.[/dim]")
        return

    from concurrent.futures import ThreadPoolExecutor, as_completed
    tasks: dict = {}
    with ThreadPoolExecutor(max_workers=max(1, len(apps) + 1)) as pool:
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
                ``\"dev\"`` → ateliers (docker compose -p X-ws down), sans le Hub.
    """
    if target not in ("all", "prod", "dev"):
        raise ValueError(f"target doit être 'all', 'prod' ou 'dev' (reçu : {target!r})")

    con = console or _console
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if target == "dev":
        # Arrêter les ateliers via docker compose -p X-ws down
        workshop_slugs = [a.name for a in hub_state.get_apps() if _ws_exists(a.name)]
        if not workshop_slugs:
            con.print("[dim]Aucun atelier à arrêter.[/dim]")
            return

        tasks: dict = {}
        with ThreadPoolExecutor(max_workers=max(1, len(workshop_slugs))) as pool:
            for app_slug in workshop_slugs:
                ws_slug = _ws_project(app_slug)
                con.print(f"[bold]→ Arrêt de « {ws_slug} »…[/bold]")
                tasks[pool.submit(_stop_workshop, con, app_slug, dry_run=dry_run)] = ws_slug

            ok, fail = 0, 0
            for future in as_completed(tasks):
                name = tasks[future]
                try:
                    if future.result():
                        con.print(f"[green]✓ {name} arrêté[/green]")
                        ok += 1
                    else:
                        con.print(f"[red]✗ {name} échoué[/red]")
                        fail += 1
                except Exception as e:
                    con.print(f"[red]✗ {name} erreur : {e}[/red]")
                    fail += 1
            con.print(f"[bold]{ok} succès, {fail} échec(s)[/bold]")
        return

    # ── Mode prod/all : arrêt classique ──
    apps = [a.name for a in hub_state.get_apps()]
    apps_dir = str(hub_state.APPS_DIR)

    tasks: dict = {}
    with ThreadPoolExecutor(max_workers=max(1, len(apps) + 1)) as pool:
        for app_name in apps:
            con.print(f"[bold]→ Arrêt de « {app_name} »…[/bold]")
            tasks[pool.submit(_stop_app, con, app_name, dry_run=dry_run, apps_dir=apps_dir)] = app_name

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


def _ensure_branch_attached(path: Path, console=None, *, dry_run: bool = False) -> str | None:
    """Rattache un checkout détaché à « main » avant le pull. Retourne la branche.

    Un déploiement posé par copie/export (pas par clone) laisse HEAD détaché :
    `git pull` y échoue avec « You are not currently on a branch » et tout le
    cycle d'update part en rollback. On rattache alors main au commit déployé
    (l'état déployé fait foi — `checkout -B`), et on restaure l'upstream absent
    (repo jamais cloné) pour que le pull sache quoi tirer.

    Retourne le nom de la branche courante/rattachée, None si pas un repo git
    ou si le rattachement a échoué.
    """
    con = console or _console
    try:
        current = subprocess.run(
            ["git", "-C", str(path), "branch", "--show-current"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if current.returncode != 0:
        return None  # pas un dépôt git
    if current.stdout.strip():
        return current.stdout.strip()  # déjà rattaché, rien à faire

    con.print("[yellow]↩ HEAD détaché → rattachement à « main »[/yellow]")
    if utils.run_shell(["git", "-C", str(path), "checkout", "-B", "main"],
                       dry_run=dry_run, console=con) != 0:
        return None

    # Upstream manquant (repo jamais cloné) → le pull ne saurait pas quoi tirer
    try:
        upstream = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--abbrev-ref",
             "--symbolic-full-name", "main@{u}"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "main"
    if upstream.returncode == 0 and upstream.stdout.strip():
        return "main"
    remotes = subprocess.run(
        ["git", "-C", str(path), "remote"],
        capture_output=True, text=True, timeout=10,
    ).stdout.split()
    if remotes:
        remote = "origin" if "origin" in remotes else remotes[0]
        utils.run_shell(
            ["git", "-C", str(path), "branch", "--set-upstream-to",
             f"{remote}/main", "main"],
            dry_run=dry_run, console=con)
    return "main"


def ensure_hub_env(console=None, *, dry_run: bool = False) -> None:
    """Garantit APPS_HOST_PATH dans le .env du Hub (auto-réparation, zéro action).

    APPS_HOST_PATH est requis par le docker-compose.yml du Hub (montage APPS au
    même chemin hôte/conteneur — HUB_base 3.71.1+). Sur les serveurs installés
    avant, la clé est absente (ou vide) du .env : TOUTE interpolation compose
    échoue, y compris `docker compose down`. Appelée avant chaque invocation
    compose du Hub (update/start/cleanup/sync-db/vpn), l'écriture est
    persistante → même un `docker compose` lancé hors hubctl fonctionne ensuite.
    Une valeur existante non vide n'est jamais écrasée.
    """
    con = console or _console
    env_path = hub_state.HUB_DIR / ".env"
    current = env_manager.read_env(env_path).get("APPS_HOST_PATH", "").strip()
    if current:
        return  # déjà défini — valeur custom respectée
    apps_path = str(hub_state.APPS_DIR.resolve())
    if dry_run:
        con.print(f"[dim](dry-run) APPS_HOST_PATH={apps_path} serait ajouté à {env_path}[/dim]")
        return
    env_manager.write_env(env_path, {"APPS_HOST_PATH": apps_path})
    con.print(f"[green]✓ APPS_HOST_PATH ajouté au .env du Hub : {apps_path}[/green]")


def _up_target_for(target: str) -> str:
    """Cible make « up » selon la cible et le mode courant (plan Task 4).

    Mode dev → ``up-dev`` ; sinon ``up-prod`` pour le Hub, ``up`` pour les apps.
    """
    if hub_state.get_mode() == "dev":
        return "up-dev"
    return "up-prod" if target == "hub" else "up"


def _update_hub_via_meta(con, *, dry_run: bool) -> bool:
    """Flux méta pour le Hub (plan unification updates, Task 3).

    Le méta-repo POULIDOR est la source de vérité : pré-checks stricts →
    ``git pull --ff-only`` du méta → ``submodule update`` → build → up →
    health. Jamais de pull direct dans HUB_base. En cas d'échec après le
    pull : rollback étendu (pointeur méta + submodule + snapshot DB).
    """
    meta_root = hub_state.PARC_ROOT
    hub_path = hub_state.HUB_DIR

    # Pré-checks : jamais de pull dans une divergence (échec propre)
    problems = [f for f in park_checks.run_update_prechecks(meta_root)
                if not f.ok]
    if problems:
        for f in problems:
            con.print(f"[red]✗ {f.check} — {f.detail}[/red]")
            if f.remedy:
                con.print(f"    [yellow]remède : {f.remedy}[/yellow]")
        con.print("[red]✗ pré-checks échoués — réalignez d'abord "
                  "(`hubctl doctor --fix`).[/red]")
        return False

    ensure_hub_env(con, dry_run=dry_run)
    compose_file = hub_path / "docker-compose.yml"
    if not compose_file.exists():
        con.print(f"[red]✗ docker-compose.yml introuvable dans {hub_path}[/red]")
        return False

    prev_meta_sha = _git_head_sha(meta_root)
    prev_hub_sha = _git_head_sha(hub_path)
    # C1 (revue) : le rollback ne reset --hard le méta que s'il était propre
    meta_clean = not park_checks.meta_unrelated_dirty_files(meta_root)
    snapshot = _db_snapshot(con) if not dry_run else None

    con.print("[bold]→ git pull --ff-only du méta…[/bold]")
    rc = utils.run_shell(["git", "-C", str(meta_root), "pull", "--ff-only"],
                         dry_run=dry_run, console=con)
    if rc != 0:
        con.print("[red]✗ git pull --ff-only du méta échoué — "
                  "commits locaux ou divergence ?[/red]")
        return False

    con.print("[bold]→ submodule update…[/bold]")
    rc = utils.run_shell(["git", "-C", str(meta_root), "submodule", "update",
                          "--init", "--recursive"],
                         dry_run=dry_run, console=con)
    if rc != 0:
        con.print("[red]✗ submodule update échoué — rollback.[/red]")
        _rollback("hub", hub_path, prev_hub_sha, snapshot, con,
                  dry_run=dry_run, meta_root=meta_root,
                  prev_meta_sha=prev_meta_sha, meta_clean=meta_clean)
        return False

    con.print("[bold]→ Build « hub »…[/bold]")
    rc = utils.run_shell(["make", "-C", str(hub_path), "build"],
                         dry_run=dry_run, console=con, timeout=600)
    if rc != 0:
        con.print("[red]✗ Build échoué pour « hub » — rollback.[/red]")
        _rollback("hub", hub_path, prev_hub_sha, snapshot, con,
                  dry_run=dry_run, meta_root=meta_root,
                  prev_meta_sha=prev_meta_sha, meta_clean=meta_clean)
        return False

    con.print("[bold]→ Démarrage « hub »…[/bold]")
    rc = utils.run_shell(["make", "-C", str(hub_path), _up_target_for("hub")],
                         dry_run=dry_run, console=con, timeout=60)
    if rc != 0:
        con.print("[red]✗ Démarrage échoué pour « hub » — rollback.[/red]")
        _rollback("hub", hub_path, prev_hub_sha, snapshot, con,
                  dry_run=dry_run, meta_root=meta_root,
                  prev_meta_sha=prev_meta_sha, meta_clean=meta_clean)
        return False

    if not _health_check("hub", con, timeout=60):
        con.print("[red]✗ Health échoué pour « hub » — rollback.[/red]")
        _rollback("hub", hub_path, prev_hub_sha, snapshot, con,
                  dry_run=dry_run, meta_root=meta_root,
                  prev_meta_sha=prev_meta_sha, meta_clean=meta_clean)
        return False
    return True


def _update_target(console, target: str, *, dry_run: bool) -> bool:
    """Stop → git pull → build → up → health pour un hub ou une app. Retourne True si succès."""
    con = console or _console
    if target == "hub":
        # Plan unification updates (Task 3) : le Hub passe par le méta-repo
        # (source de vérité), jamais par un pull direct dans HUB_base.
        return _update_hub_via_meta(con, dry_run=dry_run)
    target_path = hub_state.active_apps_dir() / target
    compose_file = target_path / "docker-compose.yml"
    if not compose_file.exists():
        con.print(f"[red]✗ docker-compose.yml introuvable dans {target_path}[/red]")
        return False

    # I3 (revue) : pré-check app AVANT tout stop/pull — une app sale ou
    # divergente échoue proprement avec son remède (jamais de rollback
    # destructeur sur des modifications locales).
    app_check = park_checks.check_app_repo(target_path)
    if not app_check.ok:
        con.print(f"[red]✗ {app_check.check} — {app_check.detail}[/red]")
        if app_check.remedy:
            con.print(f"    [yellow]remède : {app_check.remedy}[/yellow]")
        return False

    # Idempotence : si git est déjà à jour, on saute tout le cycle.
    if _git_up_to_date(target_path, con):
        con.print(f"[dim]✓ {target} déjà à jour[/dim]")
        return True

    prev_commit = _git_previous_commit(target_path, con)
    snapshot = _db_snapshot(con) if target == "hub" else None

    con.print(f"[bold]→ Arrêt de « {target} »…[/bold]")
    utils.run_shell(["make", "-C", str(target_path), "down"], dry_run=dry_run, console=con)

    _ensure_branch_attached(target_path, con, dry_run=dry_run)

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

    up_target = _up_target_for(target)
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


def _resolve_update_targets(targets: list[str]) -> tuple[list[str], bool]:
    """Normalise et valide les cibles de ``hubctl update``.

    Retourne (cibles résolues, tout_demandé). Le joker « * » (ou l'alias
    « all ») sélectionne le Hub + toutes les apps actives. Lève ValueError
    si une cible n'est ni « hub », ni une app active, ni le joker.
    """
    apps = [a.name for a in hub_state.get_active_apps()]
    if any(t in ("*", "all") for t in targets):
        return ["hub"] + apps, True
    unknown = [t for t in targets if t != "hub" and t not in apps]
    if unknown:
        valid = ", ".join(["hub"] + apps) if apps else "hub (aucune app active)"
        raise ValueError(
            f"cible inconnue : {', '.join(unknown)} — cibles valides : {valid}. "
            "Astuce : « hubctl update \"*\" » met à jour le Hub et toutes les apps "
            "(guillemets obligatoires, sinon le shell développe le glob).")
    deduped: list[str] = []
    for t in targets:
        if t not in deduped:
            deduped.append(t)
    return deduped, False


def _update_targets_pool(con, targets: list[str], *, dry_run: bool) -> int:
    """Met à jour les cibles (stop → pull → build → up → health).

    Plan unification updates (Task 4) : le Hub est **bloquant** — il est mis
    à jour en premier et, s'il échoue, les apps ne sont pas touchées (elles
    dépendent du Hub pour le M2M). Les apps restantes sont mises à jour en
    parallèle ; leurs échecs sont comptés dans le retour.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    targets = list(targets)
    if "hub" in targets:
        con.print("[bold]→ Mise à jour du Hub…[/bold]")
        if not _update_target(con, "hub", dry_run=dry_run):
            con.print("[red]✗ Hub en échec — apps non mises à jour "
                      "(elles dépendent du Hub).[/red]")
            return 1
        con.print("[green]✓ hub à jour[/green]")
        targets = [t for t in targets if t != "hub"]
        if not targets:
            return 0

    tasks: dict = {}
    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        for name in targets:
            con.print(f"[bold]→ Mise à jour de « {name} »…[/bold]")
            tasks[pool.submit(_update_target, con, name, dry_run=dry_run)] = name

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
        return fail


def action_update(console=None, targets: list[str] | None = None, *,
                  dry_run: bool = False) -> int:
    """Mise à jour ciblée : hub, une ou plusieurs apps (slug), ou tout (« * »/« all »).

    Sans cible, lève ValueError (le CLI affiche l'usage sans rien exécuter).
    Retourne le nombre d'échecs (0 = succès).
    """
    con = console or _console
    if not targets:
        raise ValueError(
            'aucune cible fournie — usage : hubctl update hub | <slug> | "*"')
    resolved, all_requested = _resolve_update_targets(list(targets))
    if all_requested and resolved == ["hub"]:
        con.print("[dim]Aucune app active à mettre à jour.[/dim]")

    if not disk_guard(con):
        return 1

    _ensure_jwt_keys(console, dry_run=dry_run)

    return _update_targets_pool(con, resolved, dry_run=dry_run)


def action_update_all(console=None, *, dry_run: bool = False) -> None:
    """(menu) Met à jour le Hub et toutes les apps actives : git pull + rebuild."""
    action_update(console, ["*"], dry_run=dry_run)


def action_doctor(console=None, *, fix: bool = False, assume_yes: bool = False,
                  parc_root: Path | None = None,
                  apps: list[str] | None = None) -> int:
    """Diagnostic des clones du parc (plan unification updates, Task 2).

    Rapport `problème → remède` sur le méta, HUB_base et les apps demandées
    (défaut : apps actives détectées). ``fix=True`` applique les remèdes sûrs
    via ``park_checks.apply_safe_fixes`` (confirmation interactive sauf
    ``assume_yes``). Retour : 0 = sain (ou réaligné), 1 = problèmes restants.
    """
    con = console or _console
    root = Path(parc_root) if parc_root else hub_state.PARC_ROOT
    if apps is None:
        apps_dir = hub_state.APPS_DIR
        apps = sorted(
            p.name for p in apps_dir.iterdir()
            if p.is_dir() and (p / ".git").exists()
        ) if apps_dir.is_dir() else []

    findings = park_checks.run_checks(root, apps)
    problems = [f for f in findings if not f.ok]

    for f in findings:
        mark = "[green]✓[/green]" if f.ok else "[red]✗[/red]"
        line = f"{mark} {f.check} — {f.detail}"
        if not f.ok and f.remedy:
            line += f"\n    [yellow]remède : {f.remedy}[/yellow]"
        con.print(line)

    if not problems:
        con.print("[green]✓ parc sain[/green]")
        return 0

    if fix:
        applied = park_checks.apply_safe_fixes(
            problems, root, assume_yes=assume_yes, console=con)
        con.print(f"[bold]{applied} remède(s) appliqué(s)[/bold]")
        remaining = [f for f in park_checks.run_checks(root, apps) if not f.ok]
        if remaining:
            con.print(f"[red]✗ {len(remaining)} problème(s) restant(s)[/red]")
            for f in remaining:
                con.print(f"  [red]✗ {f.check} — {f.detail}[/red]"
                          + (f"\n    [yellow]remède : {f.remedy}[/yellow]"
                             if f.remedy else ""))
            return 1
        con.print("[green]✓ parc sain après correction[/green]")
        return 0

    con.print(f"[red]✗ {len(problems)} problème(s) — lancez `hubctl doctor --fix` "
              "ou appliquez les remèdes ci-dessus[/red]")
    return 1


# ── Workshop (ateliers Docker Compose -p) ──
# L'atelier tourne dans APPS/X via un 2e projet docker compose (-p X-ws)
# Docker Compose du même dossier APPS/X/ avec un project name différent
# (-p X-ws) et des variables d'environnement override (APP_SLUG, ports).
# L'existence d'un atelier est matérialisée par un fichier .workshop.env
# dans APPS/X/, qui contient les overrides (slug, ports).

# Plages atelier volontairement écartées de la prod pour éviter tout conflit
# (y compris ports HTTPS = FRONTEND_PORT + 1 qui montent jusque 4700 en prod).
_WS_API_PORT_START = 10000
_WS_API_PORT_END = 10499
_WS_FE_PORT_START = 5200
_WS_FE_PORT_END = 5699

# Pool de subnets atelier (/24) — disjoint des plages prod usuelles (10.42/10.44/10.90…),
# la vérification d'overlap reste le vrai garde-fou.
_WS_SUBNET_BASE = "10.210"      # → 10.210.0.0/24 … 10.210.63.0/24 (_WS_SUBNET_COUNT=64)
_WS_SUBNET_COUNT = 64           # 64 ateliers simultanés max, largement suffisant

# Ports alloués pendant la session courante (pour dry-run où les .workshop.env
# ne sont pas écrits sur disque, il faut tracker en mémoire).
_ws_session_ports: set[int] = set()


def _ws_project(app_slug: str) -> str:
    """Nom du projet Docker Compose pour l'atelier : {slug}-ws.

    Slugifié entièrement (lowercase + underscores→tirets) : les noms de
    projet/volume/conteneur Docker n'acceptent ni majuscules ni underscores
    interdits, et l'APP_SLUG atelier doit rester IDENTIQUE au slug
    enregistré auprès du Hub (qui normalise en tirets) — sinon la
    résolution des rôles utilisateur JWT ne matche plus (app « sans rôle »).
    """
    return f"{app_slug}-ws".lower().replace("_", "-")


def _ws_env_path(app_slug: str) -> Path:
    """Chemin du fichier de config atelier : APPS/X/.workshop.env."""
    return hub_state.APPS_DIR / app_slug / ".workshop.env"


def _ws_exists(app_slug: str) -> bool:
    """True si l'atelier existe (fichier .workshop.env présent)."""
    return _ws_env_path(app_slug).exists()


def _ws_read_env(app_slug: str) -> dict[str, str]:
    """Lit la config atelier depuis .workshop.env. Retourne {} si inexistant."""
    return env_manager.read_env(_ws_env_path(app_slug))


# Clés d'environnement transmises aux sous-processus docker compose atelier
# (accès docker + locales). Parité HUB_base 3.47.3 (_COMPOSE_ENV_ALLOWLIST) :
# ne JAMAIS fusionner os.environ entier — certaines clés du hub partagent leur
# nom avec des clés des apps mais avec un AUTRE sens (ex. HUB_HTTPS_ENABLED :
# hub = « le hub sert du HTTPS » ≠ app = « joindre l'API hub en HTTPS »).
# Une clé exportée dans le shell hôte écraserait le .env de l'app via
# l'interpolation ${VAR:-défaut} du compose → app en « Mode dégradé ».
_COMPOSE_ENV_ALLOWLIST = (
    "PATH", "HOME", "TZ", "LANG", "LC_ALL",
    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_API_VERSION",
    "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
)


def _compose_base_env() -> dict[str, str]:
    """Env minimal (allowlist) pour les sous-processus docker compose."""
    return {k: os.environ[k] for k in _COMPOSE_ENV_ALLOWLIST if k in os.environ}


def _ws_compose_env(app_slug: str) -> dict[str, str] | None:
    """Construit l'environnement pour docker compose -p X-ws.

    Allowlist (accès docker) + .workshop.env. Les valeurs de .workshop.env
    restent la source de vérité ; le .env de prod est lu par Docker Compose
    lui-même. L'environnement du shell hôte n'est volontairement PAS transmis
    (cf. _COMPOSE_ENV_ALLOWLIST — incident 2026-09, parité hub-api 3.47.3).
    Retourne None si l'atelier n'existe pas.
    """
    ws = _ws_read_env(app_slug)
    if not ws:
        return None
    env = _compose_base_env()
    env.update(ws)
    return env


def _ws_compose_files(app_dir, ws_env):
    """Liste des fichiers -f du compose atelier : docker-compose.yml + https.yml
    si HTTPS actif et que le checkout l'embarque (spec 2026-09-19 : atelier = miroir
    prod ; sans l'override, le :80 en 301 rendrait l'atelier inaccessible)."""
    files = [str(app_dir / "docker-compose.yml")]
    enabled = str((ws_env or {}).get("HTTPS_ENABLED", "true")).strip().lower()
    if enabled == "true" and (app_dir / "docker-compose.https.yml").exists():
        files.append(str(app_dir / "docker-compose.https.yml"))
    return files


def _ws_used_ports() -> set[int]:
    """Ports déjà utilisés par les ateliers existants."""
    used: set[int] = set()
    for app in hub_state.get_apps():
        if _ws_exists(app.name):
            ws = _ws_read_env(app.name)
            for key in ("API_PORT", "FRONTEND_PORT", "FRONTEND_HTTPS_PORT"):
                val = ws.get(key)
                if val:
                    try:
                        used.add(int(val))
                    except ValueError:
                        pass
    return used


def _ws_used_subnets() -> set[str] | None:
    """Subnets déjà pris : tous les réseaux docker existants + APP_SUBNET des ateliers.

    Retourne None si l'état docker est inconnu (network ls/inspect en échec,
    timeout, JSON invalide) : l'appelant doit alors REFUSER d'allouer —
    les APP_SUBNET des ateliers seuls ne suffisent pas, allouer à l'aveugle
    produirait un « Pool overlaps » au `up`.
    """
    used: set[str] = set()
    try:
        ids = subprocess.run(["docker", "network", "ls", "-q"],
                             capture_output=True, text=True, timeout=10)
        if ids.returncode != 0:
            return None
        if ids.stdout.strip():
            nets = subprocess.run(
                ["docker", "network", "inspect", *ids.stdout.split()],
                capture_output=True, text=True, timeout=15)
            if nets.returncode != 0:
                return None
            if nets.stdout.strip():
                for net in json.loads(nets.stdout):
                    # Config peut être None (réseaux host/none) : la clé existe
                    # avec la valeur None, le default de .get ne s'applique pas.
                    for cfg in (net.get("IPAM", {}).get("Config") or []):
                        s = cfg.get("Subnet")
                        if s:
                            used.add(s)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError, ValueError):
        return None
    for app in hub_state.get_apps():
        if _ws_exists(app.name):
            s = _ws_read_env(app.name).get("APP_SUBNET")
            if s:
                used.add(s)
    return used


def _find_free_subnet(used_cidrs: set[str]) -> str | None:
    """Premier /24 libre du pool atelier (aucun overlap avec les CIDR existants)."""
    taken = []
    for c in used_cidrs:
        c = c.strip()
        if not c:
            continue
        try:
            taken.append(ipaddress.ip_network(c, strict=False))
        except ValueError:
            # CIDR malformé : ignoré individuellement — il ne doit pas masquer
            # les CIDR valides (sinon allocation en overlap possible).
            continue
    for i in range(_WS_SUBNET_COUNT):
        cand = ipaddress.ip_network(f"{_WS_SUBNET_BASE}.{i}.0/24")
        if not any(cand.overlaps(t) for t in taken):
            return str(cand)
    return None


# ── Helpers git du cycle atelier (--maj / --prod) ──
# L'atelier et la prod partagent le working tree APPS/X/ : toute opération git
# est donc tracée (WS_REF/WS_COMMIT dans .workshop.env) et réversible
# (_ws_git_restore), avec exigence d'un working tree propre en pré-check.


def _ws_git_state(app_dir: Path) -> dict:
    """État git de APPS/X pour le cycle atelier.

    Retourne {"ok": bool, "branch": str | None, "head": str | None}.
    ok=False si APPS/X n'est pas un repo git ou n'a pas de remote ``origin``
    (cas dégénéré : --maj/--prod refusés, atelier marqué WS_REF=n/a).
    """
    def _git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(app_dir), *args],
                              capture_output=True, text=True, timeout=15)
    try:
        if _git("rev-parse", "--git-dir").returncode != 0:
            return {"ok": False, "branch": None, "head": None}
        if _git("remote", "get-url", "origin").returncode != 0:
            return {"ok": False, "branch": None, "head": None}
        head = _git("rev-parse", "HEAD").stdout.strip() or None
        branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or None
        return {"ok": True, "branch": branch, "head": head}
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {"ok": False, "branch": None, "head": None}


# ── Résolution du volume DB (datadir PostgreSQL) ────────────────────────────
# Les composes du parc nomment le volume DB de façons variées (naming compose
# par défaut `<projet>_<clé>`, `name:` explicite, convention historique
# `<slug>-postgres-data`). Pour que le clone DB soit réellement consommé par
# le projet atelier, on résout les noms réels au lieu de les deviner.

# Points de montage reconnus comme datadir PostgreSQL (image officielle ; les
# variants récents incluent la version dans le chemin, ex. /var/lib/postgresql/18).
_PGDATA_MARKERS = ("/var/lib/postgresql",)


def _resolve_compose_db_volume(app_dir: Path, files: list, project: str,
                               env: dict | None) -> str | None:
    """Nom du volume montant le datadir PostgreSQL dans un projet compose résolu.

    Lit `docker compose config --format json` et retourne le premier named
    volume monté sous /var/lib/postgresql : `name` explicite s'il existe,
    sinon le naming compose par défaut `<projet>_<clé>`. None si la résolution
    échoue (l'appelant garde son fallback historique).
    """
    cmd: list[str] = ["docker", "compose", "-p", project]
    for f in files:
        cmd += ["-f", str(f)]
    cmd += ["config", "--format", "json"]
    try:
        st = subprocess.run(cmd, cwd=str(app_dir), env=env,
                            capture_output=True, text=True, timeout=30)
        if st.returncode != 0 or not isinstance(st.stdout, str):
            return None
        cfg = json.loads(st.stdout or "{}")
        for svc in (cfg.get("services") or {}).values():
            for vol in (svc.get("volumes") or []):
                if vol.get("type") == "bind":
                    continue
                target = vol.get("target") or ""
                if not target.startswith(_PGDATA_MARKERS):
                    continue
                key = vol.get("source")
                if not key:
                    continue
                vdef = (cfg.get("volumes") or {}).get(key) or {}
                return vdef.get("name") or f"{project}_{key}"
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError, ValueError):
        return None
    return None


def _resolve_running_db_volume(container_name: str) -> str | None:
    """Nom du volume montant le datadir PostgreSQL d'un conteneur en cours.

    Retourne None si le conteneur est absent ou ne monte pas de datadir
    (l'appelant garde son fallback historique).
    """
    try:
        st = subprocess.run(
            ["docker", "inspect", "-f",
             "{{range .Mounts}}{{.Type}}|{{.Name}}|{{.Destination}}\n{{end}}",
             container_name],
            capture_output=True, text=True, timeout=15)
        if st.returncode != 0 or not isinstance(st.stdout, str):
            return None
        for line in st.stdout.splitlines():
            parts = line.split("|", 2)
            if len(parts) != 3:
                continue
            mtype, name, dest = parts
            if mtype == "volume" and dest.startswith(_PGDATA_MARKERS):
                return name
    except (subprocess.TimeoutExpired, OSError):
        return None
    return None


def _ws_worktree_clean(app_dir: Path) -> bool:
    """True si le working tree est propre ET sans commits locaux non poussés.

    Exigé avant --maj/--prod : l'atelier et la prod partagent APPS/X/,
    un pull/checkout ne doit jamais écraser ou fusionner du travail local.
    ``.workshop.env`` est l'état interne de hubctl (non suivi dans les dépôts
    du parc réel) : il ne rend pas le tree sale.
    """
    try:
        st = subprocess.run(["git", "-C", str(app_dir), "status", "--porcelain"],
                            capture_output=True, text=True, timeout=15)
        dirty = [line for line in st.stdout.splitlines()
                 if line[3:] != ".workshop.env"]
        if st.returncode != 0 or dirty:
            return False
        ahead = subprocess.run(["git", "-C", str(app_dir), "log", "--oneline", "@{u}..HEAD"],
                               capture_output=True, text=True, timeout=15)
        if ahead.returncode != 0:
            # Pas d'upstream tracking → repli sur origin/<branche courante> ;
            # si ce ref n'existe pas non plus, on refuse l'opération
            # (fail-closed : jamais de fail-open, un commit non poussé ne
            # doit pas pouvoir être écrasé par un pull/checkout d'atelier).
            branch = subprocess.run(
                ["git", "-C", str(app_dir), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=15)
            if branch.returncode != 0:
                return False
            ahead = subprocess.run(
                ["git", "-C", str(app_dir), "log", "--oneline",
                 f"origin/{branch.stdout.strip()}..HEAD"],
                capture_output=True, text=True, timeout=15)
            if ahead.returncode != 0:
                return False
        if ahead.stdout.strip():
            return False
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _ws_resolve_ref(app_dir: Path, ref: str) -> str | None:
    """Résout un ref demandé en commit sha (après fetch).

    "main" → origin/main uniquement. Autre branche → origin/<branche> puis
    <branche> locale. Un tag est résolu par son nom (auto-follow au fetch).
    """
    candidates = [f"origin/{ref}"] if ref == "main" else [f"origin/{ref}", ref]
    for cand in candidates:
        r = subprocess.run(["git", "-C", str(app_dir), "rev-parse", "--verify", cand],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return r.stdout.strip()
    return None


def _ws_git_restore(console, app_dir: Path, prev_branch: str | None,
                    prev_commit: str | None, *, dry_run: bool) -> None:
    """Restaure l'état git d'avant opération (rollback / refus de garde-fou).

    Si une branche était checkoutée, on y revient (le working tree était propre,
    sans commits d'avance : la branche pointe sur prev_commit). Sinon (detached),
    on re-checkout prev_commit.
    """
    con = console or _console
    if not prev_commit:
        return
    target = prev_branch if (prev_branch and prev_branch != "HEAD") else prev_commit
    con.print(f"[bold]→ Rollback git : {target} @ {prev_commit[:8]}[/bold]")
    utils.run_shell(["git", "-C", str(app_dir), "checkout", "--quiet", target],
                    dry_run=dry_run, console=con, timeout=30)
    if prev_branch and prev_branch != "HEAD":
        utils.run_shell(["git", "-C", str(app_dir), "reset", "--hard", prev_commit],
                        dry_run=dry_run, console=con, timeout=30)


def _update_workshop(console, app_slug: str, ref: str = "main", *, dry_run: bool = False) -> bool:
    """Met à jour l'atelier : fetch + checkout du ref + rebuild du projet -ws.

    Le clone de DB rejoue les migrations au boot — chemin réel de montée de
    version testé sans toucher à la prod. Retourne True si l'atelier est à
    jour sur <ref> après l'appel (y compris « déjà à jour »).
    """
    con = console or _console
    ws_slug = _ws_project(app_slug)
    app_dir = hub_state.APPS_DIR / app_slug
    ws_env = _ws_read_env(app_slug)

    # ── Pré-checks ──
    if not ws_env:
        con.print(f"[red]✗ Atelier « {ws_slug} » inexistant — créez-le d'abord : ./hubctl atelier {app_slug}[/red]")
        return False
    if not hub_state.check_docker():
        con.print("[red]✗ Docker inaccessible.[/red]")
        return False
    git_state = _ws_git_state(app_dir)
    if not git_state["ok"]:
        con.print(f"[red]✗ {app_dir} n'est pas un repo git avec remote origin — mise à jour d'atelier impossible.[/red]")
        return False
    if ws_env.get("WS_REF", "n/a") == "n/a":
        con.print("[red]✗ Atelier créé sans support git (WS_REF=n/a) — supprimez et recréez l'atelier.[/red]")
        return False
    if not _ws_worktree_clean(app_dir):
        con.print(f"[red]✗ Working tree de {app_dir} sale (modifications locales ou commits non poussés) — commit/push ou stash avant --maj.[/red]")
        return False

    prev_commit = git_state["head"]
    prev_branch = git_state["branch"]

    try:
        # 1-2. FETCH + résolution du ref demandé
        con.print(f"[bold]→ Récupération du code ({ref})…[/bold]")
        rc = utils.run_shell(["git", "-C", str(app_dir), "fetch", "--quiet", "origin", "--tags"],
                             dry_run=dry_run, console=con, timeout=60)
        if rc != 0:
            con.print("[red]✗ git fetch échoué (remote accessible ?).[/red]")
            return False
        target_commit = _ws_resolve_ref(app_dir, ref) if not dry_run else ws_env.get("WS_COMMIT", "dryrun")
        if not dry_run and target_commit is None:
            con.print(f"[red]✗ Réf « {ref} » introuvable (essayée : origin/{ref}, {ref}).[/red]")
            return False

        # Idempotence : même ref, même commit déjà testé → rien à faire
        if not dry_run and ref == ws_env.get("WS_REF") and target_commit == ws_env.get("WS_COMMIT"):
            con.print(f"[green]✓ Atelier déjà à jour sur {ref} @ {target_commit[:8]}.[/green]")
            return True

        # Aperçu du delta testé
        if prev_commit:
            delta = subprocess.run(
                ["git", "-C", str(app_dir), "log", "--oneline", f"{prev_commit}..{target_commit}"],
                capture_output=True, text=True, timeout=15)
            for line in (delta.stdout.strip().splitlines() if delta.returncode == 0 else [])[:10]:
                con.print(f"[dim]  {line}[/dim]")

        # 3. CHECKOUT du commit cible (detached : on teste un état exact)
        rc = utils.run_shell(["git", "-C", str(app_dir), "checkout", "--quiet", target_commit],
                             dry_run=dry_run, console=con, timeout=30)
        if rc != 0:
            con.print(f"[red]✗ checkout {target_commit[:8]} échoué.[/red]")
            return False

        # 4. BUILD avec APP_VERSION injectée depuis le VERSION du tree
        #    (hubctl bypass make sync-env → sans ça, l'atelier afficherait
        #     la version périmée du .env prod)
        compose_env = _ws_compose_env(app_slug) or _compose_base_env()
        version_file = app_dir / "VERSION"
        if version_file.exists():
            compose_env["APP_VERSION"] = version_file.read_text().strip()
        files = _ws_compose_files(app_dir, ws_env)
        con.print(f"[bold]→ Build {ws_slug} sur {ref} @ {target_commit[:8]}…[/bold]")
        rc = utils.run_shell(
            ["docker", "compose", "-p", ws_slug]
            + sum([["-f", f] for f in files], [])
            + ["build"],
            cwd=str(app_dir), dry_run=dry_run, console=con, timeout=600, env=compose_env)
        if rc != 0:
            return _ws_update_rollback(con, app_slug, prev_branch, prev_commit,
                                       reason="Build échoué", dry_run=dry_run)

        # 5. UP (les migrations rejouent sur le volume cloné au boot)
        rc = utils.run_shell(
            ["docker", "compose", "-p", ws_slug]
            + sum([["-f", f] for f in files], [])
            + ["up", "-d"],
            cwd=str(app_dir), dry_run=dry_run, console=con, timeout=60, env=compose_env)
        if rc != 0:
            return _ws_update_rollback(con, app_slug, prev_branch, prev_commit,
                                       reason="up échoué", dry_run=dry_run)

        # 6. HEALTH
        con.print(f"[bold]→ Santé atelier (port {ws_env.get('API_PORT')}, timeout 60s)…[/bold]")
        healthy = True if dry_run else _health_check(
            ws_env.get("APP_SLUG", ws_slug), console=con, timeout=60,
            port_override=ws_env.get("API_PORT"))
        if not healthy:
            return _ws_update_rollback(con, app_slug, prev_branch, prev_commit,
                                       reason="Health échoué", dry_run=dry_run)

        # 7. PERSIST + retour propre sur la branche si ref == main
        if not dry_run:
            env_manager.write_env(_ws_env_path(app_slug),
                                  {"WS_REF": ref, "WS_COMMIT": target_commit})
            if ref == "main":
                utils.run_shell(["git", "-C", str(app_dir), "checkout", "--quiet", "main"],
                                dry_run=False, console=con, timeout=30)
                utils.run_shell(["git", "-C", str(app_dir), "reset", "--hard", target_commit],
                                dry_run=False, console=con, timeout=30)
        prod_version = env_manager.read_env(app_dir / ".env").get("APP_VERSION", "?")
        tested_version = compose_env.get("APP_VERSION", "?")
        con.print(f"[green]✓ Atelier « {ws_slug} » à jour : {ref} @ {target_commit[:8]} — testé {tested_version} (prod : {prod_version})[/green]")
        con.print("[dim]  La DB du clone a rejoué les migrations — validez l'app sur les ports atelier.[/dim]")
        return True
    except KeyboardInterrupt:
        con.print("\n[yellow]⚠ Interrompu — rollback git best-effort.[/yellow]")
        _ws_update_rollback(con, app_slug, prev_branch, prev_commit,
                            reason="Interrompu", dry_run=dry_run)
        return False


def _ws_update_rollback(console, app_slug: str, prev_branch: str | None,
                        prev_commit: str | None, *, reason: str, dry_run: bool) -> bool:
    """Rollback d'un --maj échoué : git restauré + rebuild atelier best-effort."""
    con = console or _console
    app_dir = hub_state.APPS_DIR / app_slug
    con.print(f"[red]✗ {reason}. ROLLBACK git.[/red]")
    _ws_git_restore(con, app_dir, prev_branch, prev_commit, dry_run=dry_run)
    if not dry_run:
        compose_env = _ws_compose_env(app_slug) or _compose_base_env()
        ws_env = _ws_read_env(app_slug)
        files = _ws_compose_files(app_dir, ws_env)
        version_file = app_dir / "VERSION"
        if version_file.exists():
            compose_env["APP_VERSION"] = version_file.read_text().strip()
        utils.run_shell(
            ["docker", "compose", "-p", _ws_project(app_slug)]
            + sum([["-f", f] for f in files], [])
            + ["build"],
            cwd=str(app_dir), dry_run=False, console=con, timeout=600, env=compose_env)
        utils.run_shell(
            ["docker", "compose", "-p", _ws_project(app_slug)]
            + sum([["-f", f] for f in files], [])
            + ["up", "-d"],
            cwd=str(app_dir), dry_run=False, console=con, timeout=60, env=compose_env)
    con.print("[yellow]⚠ DB du clone possiblement migrée — pour repartir d'un clone propre : ./hubctl atelier "
              f"{app_slug} (supprimer) puis recréer.[/yellow]")
    return False


def _ship_workshop(console, app_slug: str, *, dry_run: bool = False) -> bool:
    """Déploie origin/main en prod après validation atelier, puis supprime l'atelier.

    Garde-fous : l'atelier doit être sain, avoir testé « main », et origin/main
    ne doit pas avoir avancé depuis le test (HEAD == WS_COMMIT après sync).
    En cas d'échec prod : rollback (atelier conservé pour diagnostic).
    """
    con = console or _console
    ws_slug = _ws_project(app_slug)
    app_dir = hub_state.APPS_DIR / app_slug
    ws_env = _ws_read_env(app_slug)

    # ── Pré-checks ──
    if not ws_env:
        con.print(f"[red]✗ Atelier « {ws_slug} » inexistant — créez-le d'abord : ./hubctl atelier {app_slug}[/red]")
        return False
    if not hub_state.check_docker():
        con.print("[red]✗ Docker inaccessible.[/red]")
        return False
    git_state = _ws_git_state(app_dir)
    if not git_state["ok"]:
        con.print(f"[red]✗ {app_dir} n'est pas un repo git avec remote origin — ship impossible.[/red]")
        return False
    ws_ref = ws_env.get("WS_REF", "n/a")
    ws_commit = ws_env.get("WS_COMMIT", "")
    if ws_ref != "main" or not ws_commit:
        con.print(f"[red]✗ L'atelier teste « {ws_ref} » — le ship déploie uniquement origin/main.[/red]")
        con.print(f"[dim]  Mergez vers main (release-on-main : bump + CHANGELOG), puis : "
                  f"./hubctl atelier {app_slug} --maj && ./hubctl atelier {app_slug} --prod[/dim]")
        return False
    if not _ws_worktree_clean(app_dir):
        con.print(f"[red]✗ Working tree de {app_dir} sale — commit/push ou stash avant --prod.[/red]")
        return False
    con.print("[bold]→ Santé atelier avant ship…[/bold]")
    healthy = True if dry_run else _health_check(
        ws_env.get("APP_SLUG", ws_slug), console=con, timeout=30,
        port_override=ws_env.get("API_PORT"))
    if not healthy:
        con.print("[red]✗ L'atelier n'est pas sain — on ne déploie pas. Diagnostiquez puis relancez.[/red]")
        return False

    prev_commit = git_state["head"]
    prev_branch = git_state["branch"]
    prev_version = env_manager.read_env(app_dir / ".env").get("APP_VERSION", "?")

    try:
        # 2. SYNC sur origin/main
        con.print("[bold]→ Synchronisation sur origin/main…[/bold]")
        for git_cmd in (["fetch", "--quiet", "origin"],
                        ["checkout", "--quiet", "main"],
                        ["reset", "--hard", "origin/main"]):
            rc = utils.run_shell(["git", "-C", str(app_dir), *git_cmd],
                                 dry_run=dry_run, console=con, timeout=60)
            if rc != 0:
                con.print(f"[red]✗ git {' '.join(git_cmd)} échoué.[/red]")
                _ws_git_restore(con, app_dir, prev_branch, prev_commit, dry_run=dry_run)
                return False

        # 3. Garde-fou commit : la prod reçoit EXACTEMENT le commit testé
        head = ws_commit if dry_run else _git_previous_commit(app_dir)
        if head != ws_commit:
            con.print(f"[red]✗ main a avancé depuis le test atelier (testé {ws_commit[:8]}, main {str(head)[:8]}).[/red]")
            con.print(f"[dim]  Relancez ./hubctl atelier {app_slug} --maj puis revalidez avant --prod.[/dim]")
            _ws_git_restore(con, app_dir, prev_branch, prev_commit, dry_run=dry_run)
            return False

        # 4-5. BUILD + UP prod (via make : sync-env injecte APP_VERSION)
        con.print(f"[bold]→ Build prod « {app_slug} » (main @ {ws_commit[:8]})…[/bold]")
        rc = app_make(app_slug, "build", dry_run=dry_run, console=con, timeout=600)
        if rc != 0:
            return _ship_rollback(con, app_slug, prev_branch, prev_commit,
                                  reason="Build prod échoué", dry_run=dry_run)
        con.print(f"[bold]→ Démarrage prod « {app_slug} »…[/bold]")
        rc = app_make(app_slug, "up", dry_run=dry_run, console=con, timeout=120)
        if rc != 0:
            return _ship_rollback(con, app_slug, prev_branch, prev_commit,
                                  reason="up prod échoué", dry_run=dry_run)

        # 6. HEALTH prod
        con.print("[bold]→ Santé prod (timeout 60s)…[/bold]")
        healthy = True if dry_run else _health_check(app_slug, console=con, timeout=60)
        if not healthy:
            return _ship_rollback(con, app_slug, prev_branch, prev_commit,
                                  reason="Health prod échoué", dry_run=dry_run)

        # 7. CLEANUP atelier (down -v + .workshop.env — seul usage légitime de -v)
        con.print(f"[bold]→ Suppression de l'atelier « {ws_slug} » (ports + disque libérés)…[/bold]")
        _remove_workshop(con, app_slug, dry_run=dry_run)

        # 8. BILAN
        version_file = app_dir / "VERSION"
        new_version = version_file.read_text().strip() if version_file.exists() else "?"
        con.print(f"[green]✓ Prod « {app_slug} » déployée : {prev_version} → {new_version} (main @ {ws_commit[:8]}).[/green]")
        con.print(f"[green]✓ Atelier « {ws_slug} » supprimé.[/green]")
        return True
    except KeyboardInterrupt:
        con.print("\n[yellow]⚠ Interrompu — rollback prod best-effort.[/yellow]")
        _ship_rollback(con, app_slug, prev_branch, prev_commit,
                       reason="Interrompu", dry_run=dry_run)
        return False


def _ship_rollback(console, app_slug: str, prev_branch: str | None,
                   prev_commit: str | None, *, reason: str, dry_run: bool) -> bool:
    """Rollback d'un --prod échoué : git restauré + rebuild prod best-effort.

    L'atelier est TOUJOURS conservé (diagnostic / nouvelle tentative).
    """
    con = console or _console
    app_dir = hub_state.APPS_DIR / app_slug
    con.print(f"[red]✗ {reason}. ROLLBACK prod (atelier conservé).[/red]")
    _ws_git_restore(con, app_dir, prev_branch, prev_commit, dry_run=dry_run)
    if not dry_run:
        app_make(app_slug, "build", dry_run=False, console=con, timeout=600)
        app_make(app_slug, "up", dry_run=False, console=con, timeout=120)
        _health_check(app_slug, console=con, timeout=60)
    con.print("[yellow]⚠ Atelier conservé pour diagnostic — retentez --prod ou diagnostiquez dessus.[/yellow]")
    return False


def _create_workshop(console, app_slug: str, *, dry_run: bool = False) -> bool:
    """Crée l'atelier d'une app : clone DB + 2e instance Docker Compose.

    Pas de copytree : on lance ``docker compose -p X-ws`` avec des variables
    d'environnement override depuis le même dossier ``APPS/X/``.
    Retourne True si la création a réussi, False sinon.
    """
    con = console or _console

    app_dir = hub_state.APPS_DIR / app_slug
    compose_file = app_dir / "docker-compose.yml"
    ws_slug = _ws_project(app_slug)

    if not app_dir.exists():
        con.print(f"[red]✗ Source introuvable : {app_dir}[/red]")
        return False
    if not compose_file.exists():
        con.print(f"[red]✗ docker-compose.yml introuvable dans {app_dir}[/red]")
        return False
    if _ws_exists(app_slug):
        con.print(f"[dim]  Atelier « {ws_slug} » déjà existant.[/dim]")
        return False

    git_state = _ws_git_state(app_dir)

    con.print(f"[bold]→ Création de l'atelier « {ws_slug} »…[/bold]")

    try:
        # 1. Allouer des ports libres pour l'atelier
        prod_env = env_manager.read_env(app_dir / ".env")
        prod_api = int(prod_env.get("API_PORT", "9000"))
        prod_fe = int(prod_env.get("FRONTEND_PORT", "4200"))
        used = _ws_used_ports() | _ws_session_ports | {prod_api, prod_fe}

        ws_api = _find_free_port(_WS_API_PORT_START, _WS_API_PORT_END, used)
        ws_fe = _find_free_port(_WS_FE_PORT_START, _WS_FE_PORT_END, used | ({ws_api} if ws_api else set()))
        if not ws_api or not ws_fe:
            con.print("[red]✗ Plus de ports libres pour l'atelier.[/red]")
            return False

        # Tracker les ports alloués (pour dry-run et allocations consécutives)
        _ws_session_ports.update({ws_api, ws_fe, ws_fe + 1})

        # Subnet dédié GARANTI (fail-hard) : les apps template récent épinglent
        # APP_SUBNET (mandatory) dans docker-compose.yml — sans override,
        # l'atelier réutilise le subnet prod interpolé depuis .env →
        # « Pool overlaps » quand la prod tourne. Politique : un atelier est
        # créé SUR un subnet dédié disjoint du pool 10.210.x, ou n'est pas
        # créé — jamais de repli sur le subnet prod.
        used_subnets = _ws_used_subnets()
        if used_subnets is None:
            con.print("[red]✗ Impossible d'inspecter les réseaux Docker — création annulée "
                      "(on n'alloue jamais un subnet à l'aveugle).[/red]")
            con.print("[dim]  Vérifiez : docker network ls[/dim]")
            return False
        ws_subnet = _find_free_subnet(used_subnets)
        if not ws_subnet:
            con.print("[red]✗ Plus de subnet libre dans le pool atelier "
                      "(10.210.0.0/24 … 10.210.63.0/24) — création annulée.[/red]")
            con.print("[dim]  Un réseau étranger squatte probablement le pool : "
                      "docker network inspect $(docker network ls -q) | grep -B2 10.210[/dim]")
            return False

        ws_config = {
            "APP_SLUG": ws_slug,
            "API_PORT": str(ws_api),
            "FRONTEND_PORT": str(ws_fe),
            "FRONTEND_HTTPS_PORT": str(ws_fe + 1),
            "HTTPS_ENABLED": "true",   # spec 2026-09-19 : atelier = miroir prod
            # Tracing du code testé en atelier (cycle --maj/--prod)
            "WS_REF": git_state["branch"] if git_state["ok"] else "n/a",
            "WS_COMMIT": git_state["head"] if git_state["ok"] else "n/a",
            # Subnet dédié — TOUJOURS écrit (parité voie UI workshop_service)
            "APP_SUBNET": ws_subnet,
        }

        # Warning apps legacy : compose sans épinglage APP_SUBNET → le réseau
        # atelier recevra un subnet dynamique Docker (non contrôlé par le pool).
        if not any(
            "APP_SUBNET" in (Path(f).read_text(encoding="utf-8", errors="replace")
                             if Path(f).exists() else "")
            for f in _ws_compose_files(app_dir, ws_config)
        ):
            con.print("[yellow]⚠ compose sans APP_SUBNET : le réseau atelier recevra un subnet "
                      "dynamique Docker (non contrôlé) — migration template recommandée.[/yellow]")

        # 2. Écrire .workshop.env
        env_path = _ws_env_path(app_slug)
        subnet_info = f", subnet {ws_subnet}"
        con.print(f"[bold]→ Config atelier : ports {ws_api}/{ws_fe}{subnet_info}[/bold]")
        if not dry_run:
            env_manager.write_env(env_path, ws_config)

        # 3. Cloner le volume DB
        # Résolution des volumes RÉELS (les composes du parc nomment le volume
        # DB de façons variées) :
        # - source : datadir PostgreSQL du conteneur prod en cours
        #   (APP_SLUG-postgres) — la prod tourne par définition au moment
        #   d'une création d'atelier ;
        # - cible : datadir PostgreSQL du projet -ws, résolu depuis la config
        #   compose (naming par défaut <projet>_<clé> ou name explicite) →
        #   le `up` montera exactement ce volume et consommera le clone.
        # Fallbacks : convention historique <slug>-postgres-data (comportement
        # antérieur à la résolution).
        files = _ws_compose_files(app_dir, ws_config)
        compose_env = {**os.environ, **ws_config}
        version_file = app_dir / "VERSION"
        if version_file.exists():
            # hubctl bypass make sync-env : sans ça, l'atelier afficherait
            # la version périmée du .env prod.
            compose_env["APP_VERSION"] = version_file.read_text().strip()
        prod_slug = (prod_env.get("APP_SLUG") or app_slug).lower()
        src_vol = (_resolve_running_db_volume(f"{prod_slug}-postgres")
                   or f"{prod_slug}-postgres-data")
        dst_vol = (_resolve_compose_db_volume(app_dir, files, ws_slug, compose_env)
                   or f"{ws_slug}-postgres-data")
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

        # 4. docker compose -p X-ws build + up
        con.print(f"[bold]→ Build & démarrage de {ws_slug}…[/bold]")
        rc = utils.run_shell(
            ["docker", "compose", "-p", ws_slug]
            + sum([["-f", f] for f in files], [])
            + ["build"],
            cwd=str(app_dir), dry_run=dry_run, console=con, timeout=600, env=compose_env)
        if not dry_run and rc != 0:
            con.print(f"[red]✗ Build échoué. ROLLBACK : suppression .workshop.env + volume.[/red]")
            env_path.unlink(missing_ok=True)
            subprocess.run(["docker", "volume", "rm", dst_vol], capture_output=True, timeout=10)
            return False

        rc = utils.run_shell(
            ["docker", "compose", "-p", ws_slug]
            + sum([["-f", f] for f in files], [])
            + ["up", "-d"],
            cwd=str(app_dir), dry_run=dry_run, console=con, timeout=60, env=compose_env)
        if not dry_run and rc != 0:
            con.print(f"[red]✗ up échoué. ROLLBACK.[/red]")
            env_path.unlink(missing_ok=True)
            subprocess.run(["docker", "volume", "rm", dst_vol], capture_output=True, timeout=10)
            return False

        # 5. Health check
        # Premier boot d'un atelier = initdb (si DB vierge) + migrations + seed :
        # au-delà du timeout court d'un reboot, on accorde 90 s (surchageable).
        health_timeout = int(os.environ.get("WORKSHOP_HEALTH_TIMEOUT_CREATE", "90"))
        con.print(f"[bold]→ Santé atelier (port {ws_api}, timeout {health_timeout}s)…[/bold]")
        healthy = True if dry_run else _health_check(
            ws_slug, console=con, timeout=health_timeout, port_override=str(ws_api))
        if not healthy:
            con.print(f"[red]✗ Health échoué. ROLLBACK : down atelier + suppression config.[/red]")
            if not dry_run:
                utils.run_shell(
                    ["docker", "compose", "-p", ws_slug]
                    + sum([["-f", str(f)] for f in files], [])
                    + ["down"],
                    cwd=str(app_dir), dry_run=False, console=con, timeout=30, env=compose_env)
                env_path.unlink(missing_ok=True)
                subprocess.run(["docker", "volume", "rm", dst_vol], capture_output=True, timeout=10)
            return False

        con.print(f"[green]✓ Atelier « {ws_slug} » créé (ports {ws_api}/{ws_fe}).[/green]")
        return True
    except Exception as e:
        con.print(f"[red]✗ Erreur pendant la création de l'atelier : {e}[/red]")
        if not dry_run:
            _ws_env_path(app_slug).unlink(missing_ok=True)
        return False


def _start_workshop(console, app_slug: str, *, dry_run: bool = False) -> bool:
    """Démarre un atelier existant : docker compose -p X-ws up -d."""
    con = console or _console
    ws_slug = _ws_project(app_slug)

    compose_env = _ws_compose_env(app_slug)
    if compose_env is None:
        con.print(f"[red]✗ Atelier « {ws_slug} » inexistant (.workshop.env manquant).[/red]")
        return False

    app_dir = hub_state.APPS_DIR / app_slug
    ws_config = _ws_read_env(app_slug)
    ws_slug_check = ws_config.get("APP_SLUG", ws_slug)

    if _app_running(ws_slug_check):
        con.print(f"[dim]✓ {ws_slug} déjà démarré[/dim]")
        return True

    rc = utils.run_shell(
        ["docker", "compose", "-p", ws_slug]
        + sum([["-f", f] for f in _ws_compose_files(app_dir, ws_config)], [])
        + ["up", "-d"],
        cwd=str(app_dir), dry_run=dry_run, console=con, timeout=60, env=compose_env)
    if rc == 0 and not dry_run:
        api_port = ws_config.get("API_PORT", "8000")
        _health_check(ws_slug_check, console=con, timeout=30, port_override=api_port)
    return rc == 0


def _stop_workshop(console, app_slug: str, *, dry_run: bool = False) -> bool:
    """Arrête un atelier : docker compose -p X-ws down (sans -v, préserve les volumes)."""
    con = console or _console
    ws_slug = _ws_project(app_slug)

    compose_env = _ws_compose_env(app_slug) or _compose_base_env()
    app_dir = hub_state.APPS_DIR / app_slug
    compose_file = app_dir / "docker-compose.yml"

    ws_config = _ws_read_env(app_slug)
    ws_slug_check = ws_config.get("APP_SLUG", ws_slug)
    if not _app_running(ws_slug_check):
        con.print(f"[dim]✓ {ws_slug} déjà arrêté[/dim]")
        return True

    rc = utils.run_shell(
        ["docker", "compose", "-p", ws_slug, "-f", str(compose_file), "down"],
        cwd=str(app_dir), dry_run=dry_run, console=con, timeout=60, env=compose_env)
    return rc == 0


def _remove_workshop(console, app_slug: str, *, dry_run: bool = False) -> bool:
    """Supprime un atelier : docker compose -p X-ws down -v + suppression .workshop.env."""
    con = console or _console
    ws_slug = _ws_project(app_slug)

    compose_env = _ws_compose_env(app_slug) or _compose_base_env()
    app_dir = hub_state.APPS_DIR / app_slug
    compose_file = app_dir / "docker-compose.yml"

    # Down -v (détruit conteneurs + volumes)
    utils.run_shell(
        ["docker", "compose", "-p", ws_slug, "-f", str(compose_file), "down", "-v"],
        cwd=str(app_dir), dry_run=dry_run, console=con, timeout=60, env=compose_env)

    # Supprimer .workshop.env
    env_path = _ws_env_path(app_slug)
    con.print(f"[bold]→ Suppression config atelier ({env_path.name})…[/bold]")
    if not dry_run:
        env_path.unlink(missing_ok=True)

    con.print(f"[green]✓ Atelier « {ws_slug} » supprimé.[/green]")
    return True


def action_atelier(console, app_slug: str, *, dry_run: bool = False,
                   maj: bool = False, prod: bool = False, ref: str | None = None) -> None:
    """Atelier d'une app : créer / mettre à jour (--maj) / déployer en prod (--prod) / supprimer.

    - Atelier inexistant, sans flags → création (clone DB + 2e instance Compose)
    - --maj [--ref R] → _update_workshop (test d'une montée de version sur le clone)
    - --prod → _ship_workshop (déploie origin/main validé, puis supprime l'atelier)
    - Sinon (interactif, TTY requis) → menu 4 choix
    """
    con = console or _console
    import sys as _sys

    app_dir = hub_state.APPS_DIR / app_slug
    ws_slug = _ws_project(app_slug)

    if not app_dir.exists():
        con.print(f"[red]✗ Source introuvable : {app_dir}[/red]")
        return
    compose_file = app_dir / "docker-compose.yml"
    if not compose_file.exists():
        con.print(f"[red]✗ docker-compose.yml introuvable dans {app_dir}[/red]")
        return

    if ref is not None and not maj:
        con.print("[red]✗ --ref n'est utilisable qu'avec --maj.[/red]")
        return
    if maj and prod:
        con.print("[red]✗ --maj et --prod sont mutuellement exclusifs.[/red]")
        return

    # ── FLAGS (scriptables, non interactifs) ──
    # NB : le refus « atelier inexistant » est logé dans _update_workshop /
    # _ship_workshop eux-mêmes (préchecks Task 2/3) — on route donc vers eux
    # sans vérifier _ws_exists au préalable.
    if maj:
        _update_workshop(con, app_slug, ref or "main", dry_run=dry_run)
        return
    if prod:
        _ship_workshop(con, app_slug, dry_run=dry_run)
        return

    # ── CRÉATION (atelier inexistant) ──
    if not _ws_exists(app_slug):
        _create_workshop(con, app_slug, dry_run=dry_run)
        return

    # ── MENU INTERACTIF (atelier existant) ──
    ws = _ws_read_env(app_slug)
    ws_ref = ws.get("WS_REF", "?")
    ws_commit = (ws.get("WS_COMMIT") or "")[:8] or "?"
    version_file = app_dir / "VERSION"
    version = version_file.read_text().strip() if version_file.exists() else "?"
    con.print(f"[yellow]⚠ Atelier « {ws.get('APP_SLUG', ws_slug)} » actif — "
              f"teste {ws_ref} @ {ws_commit} (code {version})[/yellow]")
    if not ws.get("APP_SUBNET"):
        # Atelier créé avant l'allocation systématique du subnet dédié :
        # son réseau n'est pas garanti disjoint de la prod → inciter à recréer.
        con.print("[yellow]⚠ Atelier sans subnet dédié (APP_SUBNET absent) — recréez-le "
                  "(choix 3 puis création) pour l'isolation réseau garantie.[/yellow]")
    con.print("   1) [bold]mettre à jour[/bold] — fetch + rebuild sur origin/main")
    con.print("   2) [bold]déployer en prod[/bold] — origin/main validé puis suppression de l'atelier")
    con.print("   3) [bold]supprimer[/bold] — down -v + suppression config")
    con.print("   4) [bold]annuler[/bold]")

    if not _sys.stdin.isatty():
        con.print("[dim]Mode non interactif — annulation. (Utilisez --maj / --prod pour scripter.)[/dim]")
        return

    try:
        choice = input("   Choix [1-4] : ").strip()
    except (EOFError, KeyboardInterrupt):
        con.print("[dim]Annulé.[/dim]")
        return

    if choice == "1":
        _update_workshop(con, app_slug, "main", dry_run=dry_run)
    elif choice == "2":
        _ship_workshop(con, app_slug, dry_run=dry_run)
    elif choice == "3":
        _remove_workshop(con, app_slug, dry_run=dry_run)
    else:
        con.print("[dim]Annulé.[/dim]")


def action_atelier_close_all(console, *, dry_run: bool = False, oui: bool = False) -> int:
    """Ferme TOUS les ateliers actifs (`./hubctl atelier --close-all`).

    Récapitulatif des ateliers détectés (``.workshop.env``), confirmation globale
    unique, puis ``_remove_workshop`` pour chacun (seul lieu légitime du
    ``down -v`` — la prod n'est jamais touchée). Un atelier en échec
    n'interrompt pas les suivants.

    Retour : 0 = fermés (ou rien à faire / annulation), 1 = au moins un échec,
    2 = refusé (non interactif sans --oui).
    """
    con = console or _console
    import sys as _sys

    apps = hub_state.get_workshop_apps()
    if not apps:
        con.print("[green]✓ Aucun atelier actif — base déjà propre.[/green]")
        return 0

    # Récapitulatif avant confirmation
    con.print(f"[bold]{len(apps)} atelier(s) actif(s) :[/bold]")
    for app in apps:
        ws = _ws_read_env(app.name)
        ws_ref = ws.get("WS_REF", "?")
        ws_commit = (ws.get("WS_COMMIT") or "")[:8] or "?"
        con.print(f"  • [bold]{app.name}[/bold] → {_ws_project(app.name)}"
                  f" (teste {ws_ref} @ {ws_commit}, code {app.version})")

    if not oui:
        if not _sys.stdin.isatty():
            con.print("[red]✗ Mode non interactif : ajoutez --yes pour confirmer "
                      "la fermeture en masse.[/red]")
            return 2
        try:
            answer = input(f"Fermer {len(apps)} atelier(s) ? Les DB des clones seront "
                           "détruites (la prod reste intacte) [o/N] : ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            con.print("[dim]Annulé.[/dim]")
            return 0
        if answer not in ("o", "oui", "y", "yes"):
            con.print("[dim]Annulé — aucun atelier fermé.[/dim]")
            return 0

    failures = 0
    for app in apps:
        try:
            if _remove_workshop(con, app.name, dry_run=dry_run):
                continue
            con.print(f"[red]✗ {app.name} : suppression en échec.[/red]")
            failures += 1
        except Exception as e:
            con.print(f"[red]✗ {app.name} : {e}[/red]")
            failures += 1

    if failures:
        con.print(f"[yellow]⚠ {failures} atelier(s) en échec sur {len(apps)} — "
                  "relancez la commande pour les restants.[/yellow]")
        return 1
    con.print(f"[green]✓ {len(apps)} atelier(s) fermé(s) — base propre.[/green]")
    return 0

# ── Task 2: nouvelles actions (start_all, stop_all, update_all, atelier) ──

VPN_VOLUME = "hub_base_vpn-config"
WG_CONTAINER = "hub-wireguard"
WG_SERVICE = "wireguard-server"   # nom du service compose (≠ container_name)
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
    ensure_hub_env(console, dry_run=dry_run)
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
            con.print("[yellow]⚠ Aucune wg0.conf — basculez le toggle VPN OFF→ON "
                      "dans l'UI admin (auto-réparation ≥ 3.62.4).[/yellow]")
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


def action_seed_export(console, output_path: str | None = None, *, force: bool = False) -> int:
    """Génère un fichier seed YAML à partir des apps installées dans APPS/.

    Retourne 0 si au moins une app exportée, 1 si aucune.
    """
    import yaml
    from datetime import date

    con = console or _console
    apps_dir = hub_state.APPS_DIR

    # 1. Vérifier que APPS/ existe et contient des dossiers
    if not apps_dir.exists() or not any(
        d.is_dir() and not d.name.startswith(".") for d in apps_dir.iterdir()
    ):
        con.print("[dim]Aucune app dans APPS/, rien à exporter.[/dim]")
        return 1

    # 2. Collecter les URLs git de chaque app
    urls: list[str] = []
    for app_path in sorted(apps_dir.iterdir()):
        if not app_path.is_dir() or app_path.name.startswith("."):
            continue
        if not (app_path / ".git").exists():
            con.print(f"[dim yellow]⚠ {app_path.name} ignorée (pas de dépôt git)[/dim yellow]")
            continue

        try:
            r = subprocess.run(
                ["git", "-C", str(app_path), "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=5,
            )
            stdout = r.stdout
            if isinstance(stdout, bytes):
                stdout = stdout.decode()
            if r.returncode != 0 or not stdout.strip():
                con.print(f"[dim yellow]⚠ {app_path.name} ignorée (pas de remote git)[/dim yellow]")
                continue
            urls.append(stdout.strip())
        except Exception:
            con.print(f"[dim yellow]⚠ {app_path.name} ignorée (erreur git)[/dim yellow]")
            continue

    # 3. Aucune URL collectée
    if not urls:
        con.print("[dim]Aucune app avec remote git trouvée dans APPS/.[/dim]")
        return 1

    # 4. Trier (déterministe)
    urls.sort()

    # 5. Déterminer le chemin de sortie
    if output_path:
        out = Path(output_path)
    else:
        today = date.today().isoformat()
        out = hub_state.PARC_ROOT / f"seed-export-{today}.yaml"

    # 6. Vérifier si le fichier existe déjà
    if out.exists() and not force:
        con.print(f"[red]✗ {out} existe déjà. Utilisez --force pour écraser.[/red]")
        return 1

    # 7. Écrire le YAML
    header = f"# seed-export-{date.today().isoformat()}.yaml — export du parc au {date.today().isoformat()}\n"
    content = header + yaml.dump({"apps": urls}, default_flow_style=False, allow_unicode=True, sort_keys=False)
    out.write_text(content)

    con.print(f"[green]✓ {len(urls)} app(s) exportée(s) vers {out}[/green]")
    return 0


