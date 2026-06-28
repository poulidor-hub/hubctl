from __future__ import annotations
from rich.panel import Panel
from rich.table import Table

from hubctl_cli import actions, hub_state, prompts

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


def pause(console) -> None:
    console.print("\n[dim]↵ Entrée pour revenir au menu…[/dim]")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass


def _status_banner(console, state: dict) -> None:
    services = state.get("services") or []
    docker_ok = state.get("docker", False)
    if not docker_ok:
        console.print(Panel("[red]Docker injoignable[/red]", title="HUB STATUS"))
        return
    line = "  ".join(f"{s.name} [green]●[/green] {s.state}" for s in services) or "aucun service"
    console.print(Panel(line, title="HUB STATUS"))
    console.print("[dim]↑↓ naviguer · Entrée valider · Échap quitter · Ctrl-C annuler[/dim]")


def choose_app(console):
    apps = hub_state.get_apps()
    if not apps:
        console.print("[yellow]Aucune app dans APPS/.[/yellow]")
        return None
    choice = prompts.select("Quelle app ?", [a.name for a in apps] + ["← Retour"])
    if choice is prompts.ESC or choice is None or choice == "← Retour":
        return None
    return choice


def _choose_target(console, hub_question):
    """Retourne 'hub', un nom d'app, ou None (annulé)."""
    answer = prompts.confirm(hub_question, default=True)
    if answer is prompts.ESC or answer is None:
        return None
    if answer:
        return "hub"
    return choose_app(console)


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
        pw = prompts.password("Passphrase du coffre :")
        if pw is prompts.ESC or pw is None or not pw:
            return
        if not vault.unlock(pw):
            console.print("[red]Passphrase incorrecte.[/red]")
            return
    while True:
        entries = vault.list_entries() + ["↻ Synchroniser", "← Retour"]
        sel = prompts.select("Coffre :", entries)
        if sel is prompts.ESC or sel is None or sel == "← Retour":
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
        if prompts.confirm("Révéler les valeurs en clair ?", default=False) is True:
            for k, v in secrets.items():
                console.print(f"  [bold]{k}[/bold] = {v}")


def _handle(n, console, state, vault, *, dry_run) -> bool:
    """Exécute l'action n. Retourne True si une action a été menée (→ pause)."""
    if n == "1":
        target = _choose_target(console, "Cibler le hub central ?")
        if target is None:
            return False
        actions.action_update(console, target, dry_run=dry_run)
        return True
    if n == "2":
        name = prompts.text("Nom de la nouvelle app :")
        if name is prompts.ESC or name is None or not name:
            return False
        actions.action_add_app(console, name, dry_run=dry_run)
        return True
    if n == "3":
        app = choose_app(console)
        if app is None:
            return False
        actions.action_remove_app(console, app, vault, dry_run=dry_run)
        return True
    if n == "4":
        actions.action_repair(console, dry_run=dry_run)
        return True
    if n == "5":
        target = _choose_target(console, "Logs du hub ?")
        if target is None:
            return False
        actions.action_logs(console, target)
        return True
    if n == "6":
        sub = prompts.select("Action :", ["start", "stop", "restart", "← Retour"])
        if sub is prompts.ESC or sub is None or sub == "← Retour":
            return False
        actions.action_start_stop(console, sub, dry_run=dry_run)
        return True
    if n == "7":
        view_apps(console, state)
        return True
    if n == "8":
        view_coffre(console, vault)
        return True
    return False


def main_loop(console, state: dict, vault, *, dry_run: bool) -> None:
    while True:
        console.clear()
        _status_banner(console, state)
        choice = prompts.select("Que veux-tu faire ?", build_main_choices())
        if choice is prompts.ESC:
            console.print("À bientôt.")
            return
        if choice is None:                      # Ctrl-C au menu → rester
            continue
        if choice.startswith("0."):
            console.print("À bientôt.")
            return
        try:
            executed = _handle(choice[0], console, state, vault, dry_run=dry_run)
        except KeyboardInterrupt:               # Ctrl-C pendant l'action → retour menu
            console.print("\n[dim]Action interrompue — retour au menu.[/dim]")
            executed = True
        if executed:
            pause(console)
