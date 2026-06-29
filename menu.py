from __future__ import annotations
import sys

from rich.console import Console
from rich.text import Text

from hubctl_cli import actions, hub_state, keys, setup_wizard, widgets
from hubctl_cli.canvas import Canvas

_ACTION_CONSOLE = Console()

_MENU = [
    "1. Update (mise à jour)",
    "2. Ajouter une app",
    "3. Supprimer une app",
    "4. Réparer / Diagnostiquer",
    "5. Logs",
    "6. Start / Stop hub",
    "7. Apps (vue + disque)",
    "8. Coffre (secrets)",
    "9. Setup (configurer le hub)",
    "0. Quitter",
]


def build_main_choices() -> list[str]:
    return list(_MENU)


def pause(canvas: Canvas) -> None:
    canvas.render(Text("↵ Entrée pour revenir au menu…", style="dim"))
    fd = sys.stdin.fileno()
    while True:
        k = keys.read_key(fd, timeout=0.2)
        if k is None:
            continue
        if isinstance(k, keys.KeyEnter):
            break
        if isinstance(k, keys.KeyCtrlC):
            raise KeyboardInterrupt()
        # any other key → resume
        break


def _status_banner(state: dict) -> Text:
    services = state.get("services") or []
    docker_ok = state.get("docker", False)
    if not docker_ok:
        return Text("HUB STATUS\nDocker injoignable", style="red")
    lines = ["HUB STATUS"]
    for s in services:
        lines.append(f"  {s.name} ● {s.state}")
    lines.append("")
    lines.append("↑↓ naviguer · Entrée valider · Échap quitter · Ctrl-C annuler")
    return Text("\n".join(lines), style="green")


def choose_app(canvas: Canvas):
    apps = hub_state.get_apps()
    if not apps:
        canvas.render(Text("Aucune app dans APPS/.", style="yellow"))
        pause(canvas)
        return None
    choice = widgets.select(canvas, "Quelle app ?", [a.name for a in apps] + ["← Retour"])
    if choice is widgets.ESC or choice is None or choice == "← Retour":
        return None
    return choice


def _choose_target(canvas: Canvas):
    sel = widgets.select(canvas, "Cibler quoi ?", ["Le hub central", "Une app", "← Retour"])
    if sel is widgets.ESC or sel is None or sel == "← Retour":
        return None
    if sel == "Le hub central":
        return "hub"
    return choose_app(canvas)


def view_apps(canvas: Canvas, state: dict) -> None:
    d = hub_state.get_disk_usage()
    gb_free = d.free / 1024 ** 3
    gb_total = d.total / 1024 ** 3

    lines = [
        Text(f"ESPACE DISQUE\n{gb_free:.1f} / {gb_total:.1f} Go libres ({d.percent_free}%)\n", style="bold"),
        Text("Apps du parc\n", style="bold"),
    ]
    for a in hub_state.get_apps():
        size_mb = a.size_bytes / 1024 ** 2
        dirty = "M" if a.git_dirty else " "
        lines.append(Text(f"  {a.name:20s}  v{a.version:12s}  [{dirty}]  {size_mb:5.0f} Mo"))
    canvas.render(Text("\n").join(lines))
    pause(canvas)


def view_coffre(canvas: Canvas, vault) -> None:
    if vault is None:
        canvas.render(Text("Coffre indisponible.", style="yellow"))
        pause(canvas)
        return
    if vault.is_locked:
        pw = widgets.password(canvas, "Passphrase du coffre :")
        if pw is widgets.ESC or pw is None or not pw:
            return
        if not vault.unlock(pw):
            canvas.render(Text("Passphrase incorrecte.", style="red"))
            pause(canvas)
            return
    while True:
        entries = vault.list_entries() + ["↻ Synchroniser", "← Retour"]
        sel = widgets.select(canvas, "Coffre :", entries)
        if sel is widgets.ESC or sel is None or sel == "← Retour":
            return
        if sel == "↻ Synchroniser":
            vault.sync()
            vault.save()
            canvas.render(Text("✓ Coffre synchronisé.", style="green"))
            pause(canvas)
            continue
        secrets = vault.reveal(sel)
        list_lines = [Text(f"Entrée : {sel}\n", style="bold")]
        for k in secrets:
            list_lines.append(Text(f"  {k} = ••••••••"))
        canvas.render(Text("\n").join(list_lines))
        reveal = widgets.confirm(canvas, "Révéler les valeurs en clair ?", default=False)
        if reveal is True:
            clear_lines = []
            for k, v in secrets.items():
                clear_lines.append(Text(f"  {k} = {v}"))
            canvas.render(Text("\n").join(clear_lines))
            pause(canvas)


def _handle(n: str, canvas: Canvas, state: dict, vault, *, dry_run: bool) -> bool:
    if n == "1":
        target = _choose_target(canvas)
        if target is None:
            return False
        with canvas.paused():
            actions.action_update(_ACTION_CONSOLE, target, dry_run=dry_run)
        return True
    if n == "2":
        url = widgets.text(canvas, "URL git du dépôt (ex: git@github.com:org/repo.git) :")
        if url is widgets.ESC or url is None or not url.strip():
            return False
        with canvas.paused():
            actions.action_add_app(_ACTION_CONSOLE, url.strip(), dry_run=dry_run)
        return True
    if n == "3":
        app = choose_app(canvas)
        if app is None:
            return False
        with canvas.paused():
            actions.action_remove_app(_ACTION_CONSOLE, app, vault, dry_run=dry_run)
        return True
    if n == "4":
        with canvas.paused():
            actions.action_repair(_ACTION_CONSOLE, dry_run=dry_run)
        return True
    if n == "5":
        target = _choose_target(canvas)
        if target is None:
            return False
        with canvas.paused():
            actions.action_logs(_ACTION_CONSOLE, target)
        return True
    if n == "6":
        sub = widgets.select(canvas, "Action :", ["start", "stop", "restart", "← Retour"])
        if sub is widgets.ESC or sub is None or sub == "← Retour":
            return False
        with canvas.paused():
            actions.action_start_stop(_ACTION_CONSOLE, sub, dry_run=dry_run)
        return True
    if n == "7":
        view_apps(canvas, state)
        return True
    if n == "8":
        view_coffre(canvas, vault)
        return True
    if n == "9":
        _setup_menu(canvas)
        return False
    return False


def _setup_menu(canvas: Canvas) -> None:
    while True:
        choice = widgets.select(canvas, "Setup :", [
            "1. Quick setup (assistant rapide)",
            "2. Réseau (whitelist, CORS)",
            "3. Admin (identifiants)",
            "4. Mode & avancé",
            "0. Retour",
        ])
        if choice is widgets.ESC or choice is None or choice.startswith("0."):
            return
        if choice.startswith("1."):
            setup_wizard.quick_setup(canvas)
        elif choice.startswith("2."):
            setup_wizard.submenu_network(canvas)
        elif choice.startswith("3."):
            setup_wizard.submenu_admin(canvas)
        elif choice.startswith("4."):
            setup_wizard.submenu_advanced(canvas)


def main_loop(canvas: Canvas, state: dict, vault, *, dry_run: bool) -> None:
    while True:
        canvas.render(_status_banner(state))
        try:
            choice = widgets.select(canvas, "Que veux-tu faire ?", build_main_choices())
        except KeyboardInterrupt:
            continue

        if choice is widgets.ESC:
            canvas.render(Text("À bientôt.", style="bold"))
            return
        if choice is None:
            continue
        if choice.startswith("0."):
            canvas.render(Text("À bientôt.", style="bold"))
            return

        try:
            executed = _handle(choice[0], canvas, state, vault, dry_run=dry_run)
        except KeyboardInterrupt:
            canvas.render(Text("Action interrompue — retour au menu.", style="dim"))
            pause(canvas)
            executed = True

        if executed:
            pause(canvas)
