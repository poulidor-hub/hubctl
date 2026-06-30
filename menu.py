from __future__ import annotations
import sys
import time

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
    "9. Dev/Prod (mode app)",
    "10. Setup (configurer le hub)",
    "11. Init (cloner tous les dépôts)",
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
        break


# ── Zone builders (called each frame via canvas.set_layout) ──

def _build_welcome(state: dict) -> Text:
    """HUB block art centered, with delicate slow shimmer wave every ~17s."""
    t = time.monotonic()
    cycle = t % 17
    shimmer = cycle < 2.5

    chars = [
        list("██  ██  ██  ██  ██████  "),
        list("██  ██  ██  ██  ██   ██ "),
        list("██████  ██  ██  ██████  "),
        list("██  ██  ██  ██  ██   ██ "),
        list("██  ██  ██████  ██████  "),
    ]
    rows = len(chars)
    cols = len(chars[0])
    width = 96

    if shimmer:
        wave_phase = int(cycle * 8)
        wave_width = 12
        for r in range(rows):
            for c in range(cols):
                if chars[r][c] == "█":
                    pos = (c - wave_phase) % wave_width
                    if 0 <= pos < 3:
                        chars[r][c] = "▓"
                    elif 3 <= pos < 6:
                        chars[r][c] = "▒"
                    elif 6 <= pos < 9:
                        chars[r][c] = "░"
                    elif 9 <= pos < 11:
                        chars[r][c] = " "

    inner = Text()
    for row in chars:
        line = "".join(row)
        pad = (width - len(line)) // 2
        inner.append(" " * max(0, pad) + line + "\n", style="bold cyan")

    # Loading indicator
    if state.get("_loading"):
        loaded = sum(1 for k in ("docker", "services", "apps", "disk", "hub_version") if k in state)
        bar = "█" * loaded + "░" * (5 - loaded)
        inner.append(f"\n  Loading [{bar}]", style="dim cyan")
    return inner


def _build_hud(state: dict) -> Text:
    """HUD with scrolling docker services, disk bar, app count (cached)."""
    services = state.get("services") or []
    docker_ok = state.get("docker", False)
    text = Text()

    text.append("◆ HUB STATUS\n", style="bold yellow")

    if not docker_ok:
        text.append("  Docker injoignable\n", style="red")
    elif not services:
        text.append("  Aucun service\n", style="yellow")
    else:
        t = int(time.monotonic() / 2.5)
        offset = t % max(1, len(services))
        shown = services[offset:] + services[:offset]
        for s in shown[:5]:
            icon = "█" if s.state == "running" else "░"
            color = "green" if s.state == "running" else "red"
            text.append(f"  {icon} {s.name:18s} {s.state}\n", style=color)

    disk = state.get("disk")
    if disk:
        disk_pct = 100 - disk.percent_free
        disk_bars = int(disk_pct / 10)
        disk_bar = "█" * disk_bars + "░" * (10 - disk_bars)
        text.append(f"\n◆ DISK [{disk_bar}] {disk.percent_free:.0f}%\n", style="yellow")

    apps = state.get("apps") or []
    text.append(f"◆ APPS {len(apps)}\n", style="bold green")

    return text


def _build_help() -> Text:
    text = Text()
    text.append("▶ ↑↓ select  ▶ Enter confirm  ▶ Esc back  ▶ Ctrl-C cancel", style="bold white")
    return text


# ── Select with retro styling ──

def _retro_select(canvas: Canvas, message: str, choices: list[str]) -> str | object:
    selected_idx = 0
    scroll = 0
    fd = sys.stdin.fileno()
    max_vis = 9

    while True:
        visible = choices[scroll:scroll + max_vis]
        lines: list[Text] = []
        for i, c in enumerate(visible):
            idx = scroll + i
            short = c[:45]
            if idx == selected_idx:
                lines.append(Text(f"▸ {short}\n", style="bold white on cyan"))
            else:
                lines.append(Text(f"  {short}\n", style="bold white"))
        while len(lines) < max_vis:
            lines.append(Text("\n"))
        canvas.render(Text.assemble(*lines))

        key = keys.read_key(fd, timeout=0.15)
        if key is None:
            continue
        if isinstance(key, keys.KeyUp):
            selected_idx = (selected_idx - 1) % len(choices)
        elif isinstance(key, keys.KeyDown):
            selected_idx = (selected_idx + 1) % len(choices)
        elif isinstance(key, keys.KeyEnter):
            return choices[selected_idx]
        elif isinstance(key, keys.KeyEsc):
            return widgets.ESC
        elif isinstance(key, keys.KeyCtrlC):
            raise KeyboardInterrupt()
        if selected_idx < scroll:
            scroll = selected_idx
        elif selected_idx >= scroll + max_vis:
            scroll = selected_idx - max_vis + 1


# ── Views ──

def _status_banner(state: dict) -> Text:
    services = state.get("services") or []
    docker_ok = state.get("docker", False)
    if not docker_ok:
        return Text("Docker injoignable", style="red")
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


# ── Actions ──

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
        _devprod_toggle(canvas, dry_run)
        return False
    if n == "10":
        _setup_menu(canvas)
        return False
    if n == "11":
        with canvas.paused():
            actions.action_init(_ACTION_CONSOLE, dry_run=dry_run)
        return True
    return False


def _devprod_toggle(canvas: Canvas, dry_run: bool) -> None:
    apps = hub_state.get_apps()
    if not apps:
        canvas.render(Text("Aucune app dans APPS/.", style="yellow"))
        pause(canvas)
        return
    choices = []
    for a in apps:
        marker = a.path / ".dev-mode"
        mode = "dev" if marker.exists() else "prod"
        choices.append(f"{a.name}  [{mode}]")
    choices.append("← Retour")
    sel = widgets.select(canvas, "Dev/Prod — choisir une app :", choices)
    if sel is widgets.ESC or sel is None or sel == "← Retour":
        return
    app_name = sel.split()[0]
    marker = hub_state.APPS_DIR / app_name / ".dev-mode"
    current = "dev" if marker.exists() else "prod"
    new_mode = "production" if current == "dev" else "développement"
    msg = f"Basculer « {app_name} » de {current} → {new_mode} ?"
    if widgets.confirm(canvas, msg, default=True) is not True:
        return
    if dry_run:
        canvas.render(Text(f"[dry-run] {app_name} : {current} → {new_mode}"))
    else:
        if current == "dev":
            marker.unlink(missing_ok=True)
        else:
            marker.touch()
        canvas.render(Text(f"✓ {app_name} → {new_mode}"))
    pause(canvas)


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


# ── Détection clone frais ──

def _offer_init_fresh(canvas: Canvas, *, dry_run: bool) -> None:
    canvas.render(Text(
        "\n\nAucun dépôt HUB détecté.\n"
        "Pour faire fonctionner le parc POULIDOR,\n"
        "les dépôts suivants doivent être clonés depuis manifest.json :\n"
        "  HUB_base, HUBCSS, hub-mcp, HUBapp_TEMPLATE_base,\n"
        "  et les apps de l'APP_Workshop.\n",
        style="bold yellow"
    ))
    sel = widgets.select(canvas, "Action :", [
        "1. Initialiser le parc (recommandé)",
        "2. Passer au menu",
    ])
    if sel is widgets.ESC or sel is None or sel.startswith("2."):
        return
    with canvas.paused():
        actions.action_init(_ACTION_CONSOLE, dry_run=dry_run)
    pause(canvas)


# ── Main loop ──

def main_loop(canvas: Canvas, state: dict, vault, *, dry_run: bool) -> None:
    canvas.set_layout(lambda: _build_welcome(state), lambda: _build_hud(state), _build_help)
    while True:
        try:
            choice = _retro_select(canvas, "MENU", build_main_choices())
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
            num = choice.split(".")[0]
            executed = _handle(num, canvas, state, vault, dry_run=dry_run)
        except KeyboardInterrupt:
            canvas.render(Text("Action interrompue — retour au menu.", style="dim"))
            pause(canvas)
            executed = True

        if executed:
            pause(canvas)
