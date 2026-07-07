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
    con.print("[yellow]🔐 Clés JWT absentes dans HUB_base/secrets/ (jwt_private_key.pem + jwt_public_key.pem).[/yellow]")
    con.print("[yellow]   → Exécutez 'cd HUB_base && ./install.sh' pour les générer.[/yellow]")
    con.print("[yellow]   → Ou placez-les manuellement dans HUB_base/secrets/.[/yellow]")


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


def _health_check(target: str, console=None, timeout: int = 60) -> bool:
    """Wait for hub API to be healthy. Returns True if healthy within timeout."""
    con = console or _console
    # Port API : HUB_API_PORT du .env Hub (défaut 8000) pour target "hub",
    # sinon API_PORT de l'app cible.
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
              snapshot: Path | None, console=None) -> None:
    """Rollback: revert git, restore DB snapshot, re-up."""
    con = console or _console
    con.print(f"[bold red]↩ Rollback de « {target} »[/bold red]")

    if prev_commit:
        con.print(f"  git reset → {prev_commit[:8]}")
        utils.run_shell(
            ["git", "-C", str(target_path), "reset", "--hard", prev_commit],
            console=con, timeout=15,
        )

    if snapshot and snapshot.exists():
        con.print(f"  restauration DB → {snapshot.name}")
        subprocess.run(
            ["docker", "exec", "-i", "hub-db", "psql", "-U", "hubadmin", "hub_auth"],
            input=snapshot.read_bytes(), capture_output=True, timeout=30,
        )

    if target == "hub":
        utils.run_shell(["make", "-C", str(target_path), "build"],
                        console=con, timeout=600)
        rc = utils.run_shell(["make", "-C", str(target_path), "up-prod"],
                             console=con, timeout=60)
    else:
        utils.run_shell(["make", "-C", str(target_path), "build"],
                        console=con, timeout=600)
        rc = utils.run_shell(["make", "-C", str(target_path), "up"],
                             console=con, timeout=60)

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


def action_logs(console, target: str) -> None:
    if target == "hub":
        hub_make("logs", dry_run=False, console=console)
    else:
        app_make(target, "logs", dry_run=False, console=console)


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


def action_work(console, slug: str, *, dry_run: bool = False) -> None:
    """Met une app en atelier (APPS → APP_Workshop) avec clone DB, puis démarre.
    Wrapper CLI autour de action_workshop_move(direction='to_workshop').
    """
    con = console or _console
    con.print(f"[bold]→ Mise en atelier de « {slug} »[/bold]")
    action_workshop_move(con, "to_workshop", slug, dry_run=dry_run)
    con.print(f"[green]✅ {slug} prête en atelier (APP_Workshop/{slug}-ws)[/green]")


def action_ship(console, slug: str, *, dry_run: bool = False) -> None:
    """Ramène une app de l'atelier vers la production.

    ATTENTION : remplace la DB de prod par la DB de l'atelier.
    L'utilisateur doit confirmer en retapant le slug.
    """
    con = console or _console
    con.print(f"\n[bold yellow]⚠️  ATTENTION : cette opération va REMPLACER la base de données[/bold yellow]")
    con.print(f"[bold yellow]    de production de « {slug} » par celle de votre atelier.[/bold yellow]")
    con.print(f"[dim]    Toute donnée de production modifiée depuis le clone sera PERDUE.[/dim]")
    con.print(f"[dim]    Pour déployer le code sans toucher la DB : ./hubctl update {slug}[/dim]")
    con.print()

    if not dry_run:
        import sys
        confirmation = input(f"    Tapez '{slug}' pour confirmer : ")
        if confirmation.strip() != slug:
            con.print(f"[red]✗ Confirmation incorrecte — opération annulée.[/red]")
            return
    else:
        con.print(f"[dim][dry-run] confirmation ignorée[/dim]")

    con.print(f"\n[bold]→ Retour en production de « {slug} »[/bold]")
    action_workshop_move(con, "to_production", slug, dry_run=dry_run)
    con.print(f"[green]✅ {slug} remise en production (APPS/{slug})[/green]")
