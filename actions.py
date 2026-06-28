from __future__ import annotations
import datetime
import re
import shutil
import tarfile
from pathlib import Path

from rich.console import Console

from hubctl_cli import hub_state, utils

APP_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_console = Console()


def valid_app_name(name: str) -> bool:
    return bool(APP_NAME_RE.match(name))


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
        return utils.confirm("Forcer malgré l'espace critique ?", console=con)
    if d.percent_free < 15:
        con.print(f"[yellow]⚠ Espace faible : {d.percent_free}% libre ({gb:.1f} Go).[/yellow]")
    return True


def action_update(console, target: str, *, dry_run: bool) -> None:
    if not disk_guard(console):
        return
    if not utils.confirm(f"Mettre à jour « {target} » ?", console=console):
        return
    if target == "hub":
        utils.run_shell(["git", "-C", str(hub_state.HUB_DIR), "pull"], dry_run=dry_run, console=console)
        hub_make("up-prod", dry_run=dry_run, console=console)
        utils.run_shell(
            ["python", str(hub_state.HUB_DIR / "sync_version.py")],
            dry_run=dry_run, console=console,
        )
    else:
        path = hub_state.APPS_DIR / target
        utils.run_shell(["git", "-C", str(path), "pull"], dry_run=dry_run, console=console)
        app_make(target, "build", dry_run=dry_run, console=console)
        app_make(target, "up", dry_run=dry_run, console=console)


def action_add_app(console, name: str, *, dry_run: bool) -> None:
    if not valid_app_name(name):
        console.print("[red]Nom invalide (attendu ^[a-z][a-z0-9_-]*$).[/red]")
        return
    if (hub_state.APPS_DIR / name).exists():
        console.print(f"[red]L'app « {name} » existe déjà.[/red]")
        return
    if not disk_guard(console):
        return
    if not utils.confirm(f"Créer l'app « {name} » ?", console=console):
        return
    utils.run_shell(
        ["bash", str(hub_state.PARC_ROOT / "scripts" / "create_app.sh"), name],
        dry_run=dry_run, console=console,
    )
    if utils.confirm("Initialiser maintenant (INITIALISE.py) ?", console=console):
        utils.run_shell(
            ["python", "INITIALISE.py"],
            cwd=str(hub_state.APPS_DIR / name), dry_run=dry_run, console=console,
        )


def action_remove_app(console, app_name: str, vault, *, dry_run: bool) -> None:
    path = hub_state.APPS_DIR / app_name
    if not path.exists():
        console.print(f"[red]App introuvable : {app_name}.[/red]")
        return
    console.print(f"[bold red]Suppression de « {app_name} »[/bold red] :")
    console.print(" 1. sync du coffre + backup tar.gz, 2. arrêt conteneur, 3. mise en corbeille.")
    if not utils.confirm_phrase(app_name, console=console):
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
    if utils.confirm("Re-up forcé du hub (force-recreate) ?", console=console):
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


def action_start_stop(console, sub: str, *, dry_run: bool) -> None:
    if sub == "start":
        hub_make("up-prod", dry_run=dry_run, console=console)
    elif sub in ("stop", "restart"):
        if not utils.confirm(f"Confirmer {sub} du hub ?", console=console):
            return
        hub_make("down", dry_run=dry_run, console=console)
        if sub == "restart":
            hub_make("up-prod", dry_run=dry_run, console=console)
