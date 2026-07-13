from __future__ import annotations
import datetime
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

from rich.console import Console
from rich.table import Table

from hubctl_cli import hub_state, utils, env_manager

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


def hub_make(target: str, *, dry_run: bool, console=None) -> int:
    return utils.run_shell(
        ["make", "-C", str(hub_state.HUB_DIR), target],
        dry_run=dry_run, console=console,
    )


def app_make(app_name: str, target: str, *, dry_run: bool, console=None) -> int:
    return utils.run_shell(
        ["make", "-C", str(hub_state.active_apps_dir() / app_name), target],
        dry_run=dry_run, console=console,
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
        con.print(f"[green]✓ Snapshot DB →[/green] {path.name}")
        return path
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        con.print(f"[yellow]⚠ Snapshot DB échoué : {e}[/yellow]")
        return None


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


def action_add_app(console, url: str, *, dry_run: bool) -> None:
    name = repo_name_from_url(url)
    if not name:
        console.print("[red]URL git invalide.[/red]")
        return
    dest = hub_state.APPS_DIR / name
    if dest.exists():
        console.print(f"[red]« {name} » existe déjà dans APPS/.[/red]")
        return
    if not disk_guard(console):
        return
    rc = utils.run_shell(["git", "clone", url, str(dest)], dry_run=dry_run, console=console)
    if not dry_run and rc != 0:
        console.print("[red]✗ Échec du clone.[/red]")
        return
    if dry_run or (dest / "INITIALISE.py").exists():
        utils.run_shell(
            ["python", "INITIALISE.py"],
            cwd=str(dest), dry_run=dry_run, console=console,
        )


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
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass


def action_start_stop(console, sub: str, target: str = "hub", *, dry_run: bool = False) -> None:
    """Démarre/arrête/redémarre le Hub ou une app.

    target="hub" (défaut) → hub_make. Sinon → app_make(target, ...).
    """
    if target == "hub":
        if sub in ("start", "restart"):
            _ensure_jwt_keys(console, dry_run=dry_run)
        if sub == "start":
            hub_make("up-prod", dry_run=dry_run, console=console)
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
        shutil.copytree(str(source_dir), str(dest_dir), symlinks=True)

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


def action_workshop_move(console, direction: str, app_name: str, *, dry_run: bool) -> None:
    """Déplace une app entre APPS/ et APP_Workshop/.

    direction: "to_workshop" (APPS → APP_Workshop) ou "to_production" (APP_Workshop → APPS)
    """
    con = console or _console

    if direction not in ("to_workshop", "to_production"):
        raise ValueError(f"direction must be 'to_workshop' or 'to_production', got {direction!r}")

    # ── 1. CHECK ──
    dev_marker = hub_state.HUB_DIR / ".dev-mode"
    if not dev_marker.exists():
        dev_marker.touch()
        con.print("[dim].dev-mode créé (compatibilité)[/dim]")

    if hub_state.check_docker() is False:
        con.print("[red]✗ Docker injoignable.[/red]")
        return

    if direction == "to_workshop":
        source_dir = hub_state.APPS_DIR / app_name
        dest_dir = hub_state.WORKSHOP_DIR / f"{app_name}-ws"
        source_slug = app_name
        dest_slug = f"{app_name}-ws"
    else:
        base_name = app_name[:-3] if app_name.endswith("-ws") else app_name
        source_dir = hub_state.WORKSHOP_DIR / app_name
        dest_dir = hub_state.APPS_DIR / base_name
        source_slug = app_name
        dest_slug = base_name

    if not source_dir.exists():
        con.print(f"[red]✗ Source introuvable : {source_dir}[/red]")
        return
    if dest_dir.exists():
        con.print(f"[red]✗ Destination existe déjà : {dest_dir}[/red]")
        return

    compose_file = source_dir / "docker-compose.yml"
    if not compose_file.exists():
        con.print(f"[red]✗ docker-compose.yml introuvable dans {source_dir}[/red]")
        return

    # ── 2. STOP (on garde le conteneur : redémarrage rapide, et rollback possible) ──
    con.print(f"[bold]→ Arrêt de {source_slug}…[/bold]")
    rc = utils.run_shell(
        ["docker", "compose", "-f", str(compose_file), "stop"],
        cwd=str(source_dir), dry_run=dry_run, console=con,
    )
    if not dry_run and rc != 0:
        con.print(f"[yellow]⚠ docker compose stop a retourné {rc} (peut être déjà arrêté).[/yellow]")

    # ── 3. COPY FILES ──
    con.print(f"[bold]→ Copie des fichiers : {source_dir.name} → {dest_dir.name}[/bold]")
    if dry_run:
        con.print(f"[dim]$ cp -r {source_dir} {dest_dir} (dry-run)[/dim]")
    else:
        shutil.copytree(str(source_dir), str(dest_dir), symlinks=True)

    # ── 4. VOLUME COPY ──
    src_vol = f"{source_slug}-postgres-data"
    dst_vol = f"{dest_slug}-postgres-data"
    con.print(f"[bold]→ Copie du volume : {src_vol} → {dst_vol}[/bold]")
    if not dry_run:
        subprocess.run(
            ["docker", "volume", "create", dst_vol],
            capture_output=True, timeout=10,
        )
        rc = utils.run_shell(
            ["docker", "run", "--rm",
             "-v", f"{src_vol}:/from:ro",
             "-v", f"{dst_vol}:/to",
             "alpine", "cp", "-a", "/from/.", "/to/"],
            dry_run=dry_run, console=con, timeout=60,
        )
        if rc != 0:
            con.print(f"[yellow]⚠ Copie du volume a échoué (volume source inexistant ?). La DB démarrera vide.[/yellow]")
    else:
        con.print(f"[dim]$ docker volume create {dst_vol} (dry-run)[/dim]")
        con.print(f"[dim]$ docker run --rm -v {src_vol}:/from:ro -v {dst_vol}:/to alpine cp -a /from/. /to/ (dry-run)[/dim]")

    # ── 5. RENAME SLUG ──
    env_path = dest_dir / ".env"
    con.print(f"[bold]→ Renommage slug : {source_slug} → {dest_slug}[/bold]")
    if dry_run:
        con.print(f"[dim]$ sed APP_SLUG={source_slug} → {dest_slug} dans {env_path} (dry-run)[/dim]")
    else:
        existing = env_manager.read_env(env_path)
        old_slug = existing.get("APP_SLUG", source_slug)
        env_manager.write_env(env_path, {"APP_SLUG": dest_slug})
        con.print(f"  APP_SLUG : {old_slug} → {dest_slug}")

    # ── 6. BUILD & UP ──
    dest_compose = dest_dir / "docker-compose.yml"
    con.print(f"[bold]→ Build & démarrage de {dest_slug}…[/bold]")
    rc = utils.run_shell(
        ["docker", "compose", "-f", str(dest_compose), "build"],
        cwd=str(dest_dir), dry_run=dry_run, console=con, timeout=120,
    )
    if not dry_run and rc != 0:
        con.print(f"[red]✗ Build échoué pour {dest_slug}. ROLLBACK: suppression de {dest_dir.name}[/red]")
        shutil.rmtree(str(dest_dir))
        return

    rc = utils.run_shell(
        ["docker", "compose", "-f", str(dest_compose), "up", "-d"],
        cwd=str(dest_dir), dry_run=dry_run, console=con, timeout=60,
    )
    if not dry_run and rc != 0:
        con.print(f"[red]✗ docker compose up échoué pour {dest_slug}. ROLLBACK.[/red]")
        shutil.rmtree(str(dest_dir))
        return

    # ── 7. HEALTH CHECK ──
    env_vars = env_manager.read_env(env_path) if not dry_run else {}
    api_port = env_vars.get("API_PORT", "8000")
    health_url = f"http://localhost:{api_port}/api/health"
    con.print(f"[bold]→ Vérification santé : {health_url} (retry 5×2s)…[/bold]")
    healthy = False
    if not dry_run:
        for i in range(1, 6):
            try:
                result = subprocess.run(
                    ["curl", "-sf", health_url],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    con.print(f"[green]✓ Santé OK (tentative {i})[/green]")
                    healthy = True
                    break
            except (subprocess.SubprocessError, FileNotFoundError):
                pass
            con.print(f"  Retry {i}/5…")
            time.sleep(2)
    else:
        con.print(f"[dim]$ curl -sf {health_url} (dry-run)[/dim]")
        healthy = True

    if not healthy:
        con.print(f"[red]✗ Health check échoué pour {dest_slug}. ROLLBACK.[/red]")
        if not dry_run:
            subprocess.run(
                ["docker", "compose", "-f", str(dest_compose), "down"],
                cwd=str(dest_dir), capture_output=True, timeout=30,
            )
            shutil.rmtree(str(dest_dir))
            # Relancer la source (prod) laissée arrêtée à l'étape 2.
            con.print(f"[yellow]↻ Redémarrage de la source {source_slug}…[/yellow]")
            subprocess.run(
                ["docker", "compose", "-f", str(compose_file), "up", "-d"],
                cwd=str(source_dir), capture_output=True, timeout=60,
            )
        return

    # ── 8. BACKUP ──
    backups = hub_state.PARC_ROOT / "hubctl_cli" / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = backups / f"{source_slug}-{stamp}.tar.gz"
    con.print(f"[bold]→ Backup : {source_dir.name} → {archive.name}[/bold]")
    if dry_run:
        con.print(f"[dim]$ tar czf {archive} -C {source_dir.parent} {source_dir.name} (dry-run)[/dim]")
    else:
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(str(source_dir), arcname=source_dir.name)
        con.print(f"[green]✓ Backup :[/green] {archive}")

    # ── 9. DELETE ──
    con.print(f"[bold]→ Suppression de la source : {source_dir.name}[/bold]")
    if dry_run:
        con.print(f"[dim]$ rm -rf {source_dir} (dry-run)[/dim]")
    else:
        trash = hub_state.PARC_ROOT / "hubctl_cli" / "_trash"
        trash.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_dir), str(trash / f"{source_slug}-{stamp}"))
        con.print(f"[green]✓ Source déplacée dans la corbeille : _trash/{source_slug}-{stamp}[/green]")

    con.print(f"[green]✓ Workshop move « {source_slug} » → « {dest_slug} » terminé ![/green]")


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
    """Met une app en atelier (APPS → APP_Workshop, clone DB). La prod est stoppée mais conservée."""
    con = console or _console
    con.print(f"[bold]→ Mise en atelier de « {slug} »[/bold]")
    action_workshop_create(con, slug, dry_run=dry_run)
    con.print(f"[green]✅ {slug} prête en atelier (APP_Workshop/{slug}-ws). Prod stoppée (en place).[/green]")


def action_ship(console, slug: str, *, copy_db: bool = False, dry_run: bool = False) -> None:
    """Déploie l'atelier vers la prod.

    - défaut (copy_db=False) : git pull code-only, DB prod préservée.
    - copy_db=True : écrase la DB prod (destructif) — réservé à `hubctl ship X --copy-db`.
    """
    con = console or _console
    if copy_db:
        con.print("\n[bold yellow]⚠️  Mode --copy-db : la DB de prod sera ÉCRASÉE par celle de l'atelier.[/bold yellow]")
    action_prod_deploy(con, slug, copy_db=copy_db, dry_run=dry_run)
