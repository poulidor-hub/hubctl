from __future__ import annotations
import datetime
import json
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

from rich.console import Console
from rich.table import Table

from hubctl_cli import hub_state, prompts, utils

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
        ["make", "-C", str(hub_state.APPS_DIR / app_name), target],
        dry_run=dry_run, console=console,
    )


def disk_guard(console=None) -> bool:
    con = console or _console
    d = hub_state.get_disk_usage()
    gb = d.free / 1024 ** 3
    if d.percent_free < 5:
        con.print(f"[red]⚠ Espace critique : {d.percent_free}% libre ({gb:.1f} Go).[/red]")
        return prompts.yesno("Forcer malgré l'espace critique ?", default=False) is True
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
    elapsed = 0
    while elapsed < timeout:
        try:
            result = subprocess.run(
                ["curl", "-sk", "http://localhost:8000/api/health"],
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
        subprocess.run(["git", "-C", str(target_path), "reset", "--hard", prev_commit],
                       capture_output=True, timeout=15)

    if snapshot and snapshot.exists():
        con.print(f"  restauration DB → {snapshot.name}")
        subprocess.run(
            ["docker", "exec", "-i", "hub-db", "psql", "-U", "hubadmin", "hub_auth"],
            input=snapshot.read_bytes(), capture_output=True, timeout=30,
        )

    if target == "hub":
        subprocess.run(
            ["make", "-C", str(target_path), "up-prod"],
            capture_output=True, timeout=60,
        )
    else:
        subprocess.run(
            ["make", "-C", str(target_path), "build"], capture_output=True, timeout=120,
        )
        subprocess.run(
            ["make", "-C", str(target_path), "up"], capture_output=True, timeout=60,
        )

    con.print("[green]✓ Rollback terminé[/green]")


def action_update(console, target: str, *, dry_run: bool) -> None:
    con = console or _console
    if not disk_guard(con):
        return
    if prompts.yesno(f"Mettre à jour « {target} » ?", default=False) is not True:
        return

    target_path = hub_state.HUB_DIR if target == "hub" else hub_state.APPS_DIR / target
    if not target_path.exists():
        con.print(f"[red]✗ Cible introuvable : {target_path}[/red]")
        return

    prev_commit = _git_previous_commit(target_path, con)
    snapshot = _db_snapshot(con)
    ok = True

    con.print(f"[bold]→ git pull[/bold]")
    rc = utils.run_shell(["git", "-C", str(target_path), "pull"], dry_run=dry_run, console=con)
    if rc != 0:
        con.print("[red]✗ git pull a échoué[/red]")
        ok = False

    if ok and target == "hub":
        rc = hub_make("up-prod", dry_run=dry_run, console=con)
        if rc != 0:
            con.print("[red]✗ up-prod a échoué[/red]")
            ok = False
        else:
            utils.run_shell(
                ["python", str(hub_state.HUB_DIR / "sync_version.py")],
                dry_run=dry_run, console=con,
            )
    elif ok:
        rc = app_make(target, "build", dry_run=dry_run, console=con)
        if rc != 0:
            con.print("[red]✗ build a échoué[/red]")
            ok = False
        if ok:
            rc = app_make(target, "up", dry_run=dry_run, console=con)
            if rc != 0:
                con.print("[red]✗ up a échoué[/red]")
                ok = False

    if not ok:
        con.print("[red]✗ Mise à jour échouée[/red]")
        if not dry_run and prev_commit:
            _rollback(target, target_path, prev_commit, snapshot, con)
        return

    # Wait for health + user confirmation
    con.print("[bold]→ Vérification santé…[/bold]")
    healthy = _health_check(target, con, timeout=60)

    if not dry_run:
        con.print("")
        con.print("[bold yellow]Vérifie visuellement que tout fonctionne.[/bold yellow]")
        is_ok = prompts.yesno("Tout est OK ? (non = rollback)", default=True)
        if is_ok is not True:
            con.print("[yellow]Rollback demandé par l'utilisateur.[/yellow]")
            _rollback(target, target_path, prev_commit, snapshot, con)
            return

    con.print(f"[green]✓ « {target} » à jour[/green]")


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
    if prompts.yesno(f"Cloner {url} → APPS/{name} ?", default=True) is not True:
        return
    rc = utils.run_shell(["git", "clone", url, str(dest)], dry_run=dry_run, console=console)
    if not dry_run and rc != 0:
        console.print("[red]✗ Échec du clone.[/red]")
        return
    if dry_run or (dest / "INITIALISE.py").exists():
        if prompts.yesno("Initialiser l'app (INITIALISE.py) ?", default=True) is True:
            utils.run_shell(
                ["python", "INITIALISE.py"],
                cwd=str(dest), dry_run=dry_run, console=console,
            )


def action_remove_app(console, app_name: str, vault, *, dry_run: bool) -> None:
    path = hub_state.APPS_DIR / app_name
    if not path.exists():
        console.print(f"[red]App introuvable : {app_name}.[/red]")
        return
    console.print(
        f"[bold red]Suppression de « {app_name} »[/bold red] : "
        "sync du coffre + backup tar.gz, arrêt conteneur, mise en corbeille."
    )
    keep = "Annuler"
    go = f"Oui, supprimer « {app_name} » (vers la corbeille)"
    choice = prompts.select(f"Confirmer la suppression de « {app_name} » ?", [keep, go])
    if choice is prompts.ESC or choice is None or choice == keep:
        console.print("[yellow]Annulé.[/yellow]")
        return

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
    console.print("[bold]Diagnostic (lecture seule)[/bold]")
    hub_make("ps", dry_run=False, console=console)
    d = hub_state.get_disk_usage()
    console.print(f"Disque : {d.percent_free}% libre.")
    if prompts.yesno("Re-up forcé du hub (force-recreate) ?", default=False) is True:
        utils.run_shell(
            ["docker", "compose", "-f", str(hub_state.HUB_DIR / "docker-compose.yml"),
             "up", "-d", "--force-recreate"],
            dry_run=dry_run, console=console,
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


def action_start_stop(console, sub: str, *, dry_run: bool) -> None:
    if sub == "start":
        hub_make("up-prod", dry_run=dry_run, console=console)
    elif sub in ("stop", "restart"):
        if prompts.yesno(f"Confirmer {sub} du hub ?", default=False) is not True:
            return
        hub_make("down", dry_run=dry_run, console=console)
        if sub == "restart":
            hub_make("up-prod", dry_run=dry_run, console=console)
