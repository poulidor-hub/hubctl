from __future__ import annotations
import sys
import time

from rich.console import Console
from rich.text import Text

from hubctl_cli import actions, hub_state, keys, setup_wizard, widgets
from hubctl_cli.canvas import Canvas
from hubctl_cli.hub_state import WORKSHOP_DIR

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
    "12. Workshop (déplacer app en atelier)",
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
        inner.append(" " * max(0, pad) + line + "\n", style="bold magenta")

    # Loading indicator
    if state.get("_loading"):
        loaded = sum(1 for k in ("docker", "services", "apps", "disk", "hub_version") if k in state)
        bar = "█" * loaded + "░" * (5 - loaded)
        inner.append(f"\n  Loading [{bar}]", style="dim cyan")
    return inner


def _build_hud(state: dict) -> Text:
    """HUD with docker services, disk bar, app count, live network/cpu graphs."""
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
        for s in shown[:4]:
            icon = "█" if s.state == "running" else "░"
            color = "green" if s.state == "running" else "red"
            text.append(f"  {icon} {s.name:16s} {s.state}\n", style=color)

    # Live network + CPU mini graphs
    net = _net_sample()
    text.append("\n◆ LIVE  ", style="bold magenta")
    text.append(f"up {_uptime()}\n", style="bright_black")
    text.append(f"  RX {_bar(net['rx_kbs'], 1000)} {_fmt(net['rx_kbs'])}/s\n", style="bright_black")
    text.append(f"  TX {_bar(net['tx_kbs'], 500)} {_fmt(net['tx_kbs'])}/s\n", style="bright_black")
    text.append(f"  Σ {_fmt(_cumul_rx * 1024)}↓ {_fmt(_cumul_tx * 1024)}↑\n", style="dim magenta")

    cpu = _cpu_pct()
    text.append(f"  CPU {_bar(int(cpu), 100)}\n", style="yellow")

    disk = state.get("disk")
    if disk:
        disk_pct = 100 - disk.percent_free
        text.append(f"◆ DISK {_bar(int(disk_pct), 100)} {disk.percent_free:.0f}%\n", style="yellow")

    apps = state.get("apps") or []
    text.append(f"◆ APPS {len(apps)}\n", style="bold green")

    return text


# ── Network / CPU / Uptime helpers ──

_prev_net = (0, 0, time.monotonic())
_start_time = time.monotonic()
_cumul_rx = 0.0
_cumul_tx = 0.0


def _net_sample() -> dict:
    global _prev_net, _cumul_rx, _cumul_tx
    now = time.monotonic()
    try:
        with open("/proc/net/dev") as f:
            found = None
            for line in f:
                if any(x in line for x in ("eth0", "ens", "enp", "wlan", "docker")):
                    parts = line.split()
                    rx = int(parts[1])
                    tx = int(parts[9])
                    if found is None or "docker" not in line:
                        found = (rx, tx)
                        if "docker" not in line:
                            break
            if found:
                rx, tx = found
            else:
                rx = tx = 0
    except (FileNotFoundError, IndexError, ValueError):
        rx = tx = 0

    p_rx, p_tx, p_ts = _prev_net
    elapsed = now - p_ts
    if elapsed > 0.5 and p_rx > 0:
        rx_kbs = (rx - p_rx) / elapsed / 1024
        tx_kbs = (tx - p_tx) / elapsed / 1024
    else:
        rx_kbs = tx_kbs = 0

    if elapsed > 0.8:
        _cumul_rx += max(0, rx_kbs) * elapsed / 1024
        _cumul_tx += max(0, tx_kbs) * elapsed / 1024
        _prev_net = (rx, tx, now)

    return {"rx_kbs": max(0, rx_kbs), "tx_kbs": max(0, tx_kbs)}


def _uptime() -> str:
    elapsed = int(time.monotonic() - _start_time)
    h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _cpu_pct() -> float:
    try:
        with open("/proc/loadavg") as f:
            return float(f.read().split()[0]) * 25
    except (FileNotFoundError, ValueError):
        return 0


def _bar(value: float, max_val: float) -> str:
    n = int(min(value, max_val) / max_val * 8)
    bars = [" ", "▏", "▎", "▍", "▌", "▋", "▊", "▉", "█"]
    full = n // 8
    rem = n % 8
    return "█" * full + (bars[rem] if rem else "") + "░" * (8 - full - (1 if rem else 0))


def _fmt(kbs: float) -> str:
    if kbs >= 1000:
        return f"{kbs / 1000:.1f}M"
    if kbs >= 1:
        return f"{kbs:.0f}K"
    return f"{kbs * 1000:.0f}"


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

def _run_action(canvas: Canvas, label: str, fn, *args, **kw) -> bool:
    """Show loading screen, run action in passthrough, show result."""
    canvas.render(Text(f"▸ {label} en cours…\n\nCtrl-C pour annuler", style="bold yellow"))
    success = True
    try:
        with canvas.paused():
            fn(*args, **kw)
    except KeyboardInterrupt:
        success = False
    canvas.render(Text(f"{'✓' if success else '✗'} {label}", style="green" if success else "red"))
    pause(canvas)
    return True


def _handle(n: str, canvas: Canvas, state: dict, vault, *, dry_run: bool) -> bool:
    if n == "1":
        target = _choose_target(canvas)
        if target is None:
            return False
        return _run_action(canvas, f"Update {target}", actions.action_update, _ACTION_CONSOLE, target, dry_run=dry_run)
    if n == "2":
        url = widgets.text(canvas, "URL git du dépôt (ex: git@github.com:org/repo.git) :")
        if url is widgets.ESC or url is None or not url.strip():
            return False
        return _run_action(canvas, f"Ajout {url}", actions.action_add_app, _ACTION_CONSOLE, url.strip(), dry_run=dry_run)
    if n == "3":
        app = choose_app(canvas)
        if app is None:
            return False
        return _run_action(canvas, f"Suppression {app}", actions.action_remove_app, _ACTION_CONSOLE, app, vault, dry_run=dry_run)
    if n == "4":
        return _run_action(canvas, "Diagnostic", actions.action_repair, _ACTION_CONSOLE, dry_run=dry_run)
    if n == "5":
        target = _choose_target(canvas)
        if target is None:
            return False
        return _run_action(canvas, f"Logs {target}", actions.action_logs, _ACTION_CONSOLE, target)
    if n == "6":
        sub = widgets.select(canvas, "Action :", ["start", "stop", "restart", "← Retour"])
        if sub is widgets.ESC or sub is None or sub == "← Retour":
            return False
        return _run_action(canvas, f"{sub} hub", actions.action_start_stop, _ACTION_CONSOLE, sub, dry_run=dry_run)
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
    if n == "12":
        _workshop_menu(canvas, dry_run)
        return True
    return False


def _workshop_menu(canvas: Canvas, dry_run: bool) -> None:
    """Sous-menu pour déplacer des apps entre APPS/ et APP_Workshop/."""
    while True:
        apps_prod = hub_state.get_apps()
        apps_ws = hub_state.get_workshop_apps()

        choices = []
        if apps_prod:
            choices.append("── APPS/ (production) ──")
            for a in apps_prod:
                choices.append(f"  [→] {a.name}  → mettre en atelier")
        if apps_ws:
            choices.append("── APP_Workshop/ (développement) ──")
            for a in apps_ws:
                choices.append(f"  [←] {a.name}  → remettre en production")
        if not apps_prod and not apps_ws:
            choices.append("Aucune app trouvée.")
        choices.append("← Retour")

        sel = widgets.select(canvas, "Workshop — déplacer une app :", choices)
        if sel is widgets.ESC or sel is None or sel == "← Retour":
            return

        if sel.startswith("  [→]"):
            app_name = sel.split()[1]
            msg = f"Déplacer « {app_name} » de APPS/ vers APP_Workshop/ ({app_name}-ws) ?"
            if widgets.confirm(canvas, msg, default=False) is True:
                with canvas.paused():
                    actions.action_workshop_move(
                        _ACTION_CONSOLE, "to_workshop", app_name, dry_run=dry_run,
                    )
                pause(canvas)
                return

        elif sel.startswith("  [←]"):
            app_name = sel.split()[1]
            base = app_name[:-3] if app_name.endswith("-ws") else app_name
            msg = f"Remettre « {app_name} » en production dans APPS/ ({base}) ?"
            if widgets.confirm(canvas, msg, default=False) is True:
                with canvas.paused():
                    actions.action_workshop_move(
                        _ACTION_CONSOLE, "to_production", app_name, dry_run=dry_run,
                    )
                pause(canvas)
                return


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
