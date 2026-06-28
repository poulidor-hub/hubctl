from __future__ import annotations
import questionary
from rich.panel import Panel
from rich.table import Table

from hubctl_cli import actions, hub_state

_MENU = [
    "1. Update (mise à jour)",
    "2. Ajouter une app",
    "3. Supprimer une app",
    "4. Réparer / Diagnostiquer",
    "5. Logs",
    "6. Start / Stop hub",
    "7. Apps (vue + disque)",
    "8. Coffre (secrets)",
    "0. Quitter",
]


def build_main_choices() -> list[str]:
    return list(_MENU)


def _status_banner(console, state: dict) -> None:
    services = state.get("services") or []
    docker_ok = state.get("docker", False)
    if not docker_ok:
        console.print(Panel("[red]Docker injoignable[/red]", title="HUB STATUS"))
        return
    line = "  ".join(f"{s.name} [green]●[/green] {s.state}" for s in services) or "aucun service"
    console.print(Panel(line, title="HUB STATUS"))


def choose_app(console):
    apps = hub_state.get_apps()
    if not apps:
        console.print("[yellow]Aucune app dans APPS/.[/yellow]")
        return None
    choice = questionary.select(
        "Quelle app ?", choices=[a.name for a in apps] + ["← Retour"]
    ).ask()
    return None if choice in (None, "← Retour") else choice


def view_apps(console, state: dict) -> None:
    d = hub_state.get_disk_usage()
    gb_free = d.free / 1024 ** 3
    gb_total = d.total / 1024 ** 3
    console.print(Panel(
        f"Disque serveur : {gb_free:.1f} / {gb_total:.1f} Go libres ({d.percent_free}%)",
        title="ESPACE DISQUE",
    ))
    table = Table(title="Apps du parc")
    table.add_column("Nom"); table.add_column("Version")
    table.add_column("Git"); table.add_column("Taille")
    for a in hub_state.get_apps():
        size_mb = a.size_bytes / 1024 ** 2
        table.add_row(
            a.name, a.version,
            "[yellow]modifié[/yellow]" if a.git_dirty else "[green]propre[/green]",
            f"{size_mb:.0f} Mo",
        )
    console.print(table)


def view_coffre(console, vault) -> None:
    if vault is None:
        console.print("[yellow]Coffre indisponible.[/yellow]")
        return
    if vault.is_locked:
        passphrase = questionary.password("Passphrase du coffre :").ask()
        if not passphrase or not vault.unlock(passphrase):
            console.print("[red]Passphrase incorrecte.[/red]")
            return
    while True:
        entries = vault.list_entries() + ["↻ Synchroniser", "← Retour"]
        sel = questionary.select("Coffre :", choices=entries).ask()
        if sel in (None, "← Retour"):
            return
        if sel == "↻ Synchroniser":
            vault.sync(); vault.save()
            console.print("[green]✓ Coffre synchronisé.[/green]")
            continue
        secrets = vault.reveal(sel)
        table = Table(title=f"Entrée : {sel}")
        table.add_column("Clé"); table.add_column("Valeur")
        for k in secrets:
            table.add_row(k, "••••••••")
        console.print(table)
        if questionary.confirm("Révéler les valeurs en clair ?", default=False).ask():
            for k, v in secrets.items():
                console.print(f"  [bold]{k}[/bold] = {v}")


def main_loop(console, state: dict, vault, *, dry_run: bool) -> None:
    while True:
        _status_banner(console, state)
        choice = questionary.select("Que veux-tu faire ?", choices=build_main_choices()).ask()
        if choice is None or choice.startswith("0."):
            console.print("À bientôt.")
            return
        n = choice[0]
        if n == "1":
            target = "hub" if questionary.confirm("Cibler le hub central ?", default=True).ask() else choose_app(console)
            if target:
                actions.action_update(console, target, dry_run=dry_run)
        elif n == "2":
            name = questionary.text("Nom de la nouvelle app :").ask()
            if name:
                actions.action_add_app(console, name, dry_run=dry_run)
        elif n == "3":
            app = choose_app(console)
            if app:
                actions.action_remove_app(console, app, vault, dry_run=dry_run)
        elif n == "4":
            actions.action_repair(console, dry_run=dry_run)
        elif n == "5":
            target = "hub" if questionary.confirm("Logs du hub ?", default=True).ask() else choose_app(console)
            if target:
                actions.action_logs(console, target)
        elif n == "6":
            sub = questionary.select("Action :", choices=["start", "stop", "restart", "← Retour"]).ask()
            if sub and sub != "← Retour":
                actions.action_start_stop(console, sub, dry_run=dry_run)
        elif n == "7":
            view_apps(console, state)
        elif n == "8":
            view_coffre(console, vault)
