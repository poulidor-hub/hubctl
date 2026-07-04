from __future__ import annotations
import contextlib
import sys
import threading
import time
from collections import OrderedDict

from rich.console import Console
from rich.text import Text

from hubctl_cli import actions, hub_state, keys, setup_wizard, widgets
from hubctl_cli.canvas import Canvas
from hubctl_cli.vault_manager import VaultManager

_ACTION_CONSOLE = Console()
_vault_mgr: VaultManager | None = None

_APPS_WINDOW = 4
_APPS_SCROLL_PERIOD = 2.0

MENU_TREE: dict[str, OrderedDict] = {
    "apps": OrderedDict([
        ("1", ("Update app", "update_app")),
        ("2", ("Ajouter une app", "add_app")),
        ("3", ("Supprimer une app", "remove_app")),
        ("4", ("Logs app", "logs_app")),
        ("0", ("← Retour", "__back__")),
    ]),
    "hub": OrderedDict([
        ("1", ("Update hub", "update_hub")),
        ("2", ("Réparer / Diagnostiquer", "repair")),
        ("3", ("Start / Stop HUB", "start_stop")),
        ("4", ("Apps (Vue+Disque)", "view_apps")),
        ("5", ("Logs hub", "logs_hub")),
        ("0", ("← Retour", "__back__")),
    ]),
    "config": OrderedDict([
        ("1", ("Quick setup", "quick_setup")),
        ("2", ("Réseau (whitelist, CORS)", "network")),
        ("3", ("Admin (identifiants)", "admin")),
        ("4", ("Mode & avancé", "advanced")),
        ("5", ("Coffre (Secrets)", "coffre")),
        ("6", ("Workshop", "workshop")),
        ("0", ("← Retour", "__back__")),
    ]),
    "scenarios": OrderedDict([
        ("1", ("Quick diag (repair + logs + apps)", "sc_diag_quick")),
        ("2", ("Full update cycle", "sc_update_full")),
        ("3", ("App rescue (supprimer + réinstaller)", "sc_app_rescue")),
        ("4", ("Health check (vue apps + diag)", "sc_health")),
        ("0", ("← Retour", "__back__")),
    ]),
}

_nav_stack: list[str] = ["main"]


# ── Scenarios (pre-recorded action sequences) ──

def _sc_diag_quick(canvas: Canvas, state: dict, vault_mgr, *, dry_run: bool) -> bool:
    if not _exec_repair(canvas, state, vault_mgr, dry_run=dry_run):
        return False
    if not _exec_logs_hub(canvas, state, vault_mgr, dry_run=dry_run):
        return False
    return _exec_view_apps(canvas, state, vault_mgr, dry_run=dry_run)


def _sc_update_full(canvas: Canvas, state: dict, vault_mgr, *, dry_run: bool) -> bool:
    if not _exec_update_hub(canvas, state, vault_mgr, dry_run=dry_run):
        return False
    if not _exec_start_stop(canvas, state, vault_mgr, dry_run=dry_run, param="restart"):
        return False
    return _exec_logs_hub(canvas, state, vault_mgr, dry_run=dry_run)


def _sc_app_rescue(canvas: Canvas, state: dict, vault_mgr, *, dry_run: bool) -> bool:
    app = choose_app(canvas)
    if not app:
        return False
    if not _exec_remove_app(canvas, state, vault_mgr, dry_run=dry_run, param=app):
        return False
    with _modal(canvas):
        url = widgets.text(canvas, "URL git pour réinstaller :")
        if url is widgets.ESC or not url or not url.strip():
            return False
    return _exec_add_app(canvas, state, vault_mgr, dry_run=dry_run, param=url.strip())


def _sc_health(canvas: Canvas, state: dict, vault_mgr, *, dry_run: bool) -> bool:
    if not _exec_view_apps(canvas, state, vault_mgr, dry_run=dry_run):
        return False
    return _exec_repair(canvas, state, vault_mgr, dry_run=dry_run)


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

    text = Text()
    for row in chars:
        line = "".join(row)
        pad = (width - len(line)) // 2
        text.append(" " * max(0, pad) + line + "\n", style="bold magenta")

    version = state.get("hub_version", "v?.?")
    cpu = _cpu_pct()
    now = time.strftime("%H:%M UTC", time.gmtime())
    header = f" HUB MASTER OS {version}  |  UPTIME: {_uptime()}  |  LOAD: {cpu:.2f}  |  {now}"
    padded = header.ljust(96)
    text.append(padded, style="bold white")
    return text


def _build_resources(state: dict) -> Text:
    text = Text()
    text.append("[ SYSTEM RESOURCES ]\n", style="bold blue")
    text.append("\n")

    cpu = _cpu_pct()
    text.append(f"CPU {_bar(int(cpu), 100)} {cpu:.0f}%\n", style="white")

    load = _cpu_load()
    text.append(f"RAM {_bar(int(load * 10), 100)} {load:.1f}\n", style="white")

    text.append(f"SWP {_bar(0, 100)}   0%\n", style="dim")

    text.append("\n")

    disk = state.get("disk")
    if disk:
        disk_pct = 100 - disk.percent_free
        text.append(f"/     {_bar(int(disk_pct), 100)} {disk.percent_free:.0f}%\n", style="white")
        text.append(f"/data {_bar(0, 100)}   —\n", style="dim")
    else:
        text.append("/     " + _bar(0, 100) + "   —\n", style="dim")
        text.append("/data " + _bar(0, 100) + "   —\n", style="dim")

    text.append("\n")

    net = _net_sample()
    rx_total, tx_total = _host_net_totals()
    text.append(f"▼ Net RX: {_fmt_net(net['rx_kbs'])}\n", style="dim")
    text.append(f"▲ Net TX: {_fmt_net(net['tx_kbs'])}\n", style="dim")
    text.append(f"Σ {_fmt_bytes(rx_total)}↓ {_fmt_bytes(tx_total)}↑\n", style="dim")

    text.append("\n")
    apps = state.get("apps") or []
    legacy_count = sum(1 for a in apps if getattr(a, "legacy", False))
    if legacy_count:
        text.append(f"Apps : {len(apps)} ({legacy_count} legacy)", style="bold green")
    else:
        text.append(f"Apps : {len(apps)}", style="bold green")

    divs = state.get("_vault_divergences")
    if divs:
        text.append(f"\n⚠ {len(divs)} .env divergents", style="bold yellow")

    return text


def _build_services(state: dict) -> Text:
    text = Text()
    text.append("[ SERVICES STATUS ]\n", style="bold blue")
    text.append("\n")

    services = state.get("services") or []
    docker_ok = state.get("docker", False)

    if not docker_ok:
        text.append("  Docker injoignable\n", style="red")
    elif not services:
        text.append("  Aucun service\n", style="yellow")
    else:
        for s in services[:8]:
            name = s.name[:14]
            if s.state == "running":
                icon = Text(f"{name:<15s}", style="green")
                badge = Text("[ RUNNING ]", style="green")
            elif s.state in ("stopped", "exited"):
                icon = Text(f"{name:<15s}", style="red")
                badge = Text("[ STOPPED ]", style="red")
            else:
                icon = Text(f"{name:<15s}", style="yellow")
                badge = Text("[ WARNING ]", style="yellow")
            text.append(icon)
            text.append(badge)
            text.append("\n")

    rows = state.get("app_status")
    if rows is None:
        text.append("\n[ APPS ]\n", style="bold blue")
        text.append("  …\n", style="dim")
    elif rows:
        text.append("\n[ APPS ]\n", style="bold blue")
        n = len(rows)
        if n > _APPS_WINDOW:
            offset = int(time.monotonic() / _APPS_SCROLL_PERIOD) % n
            visible = (rows + rows)[offset:offset + _APPS_WINDOW]
        else:
            visible = rows
        colors = {
            "running": "green",
            "stopped": "red",
            "warning": "yellow",
            "legacy": "bright_black",
        }
        for r in visible:
            color = colors.get(r.status, "yellow")
            info = "legacy" if r.legacy else r.uptime
            text.append("● ", style=color)
            text.append(f"{r.name[:12]:<12} ", style=color)
            text.append(info, style="dim")
            text.append("\n")

    return text


_ui_state = {"hint": ""}


def _build_logs() -> Text:
    text = Text()
    text.append("[ RECENT LOGS ]\n", style="bold blue")
    text.append("> Pas de logs récents.\n", style="dim")
    return text


def _current_context() -> OrderedDict:
    ctx_name = _nav_stack[-1]
    if ctx_name == "main":
        return _build_main_menu()
    if ctx_name == "config":
        return _build_config_menu()
    return MENU_TREE.get(ctx_name, _build_main_menu())


def _build_main_menu() -> OrderedDict:
    label = "Passer en DEV" if hub_state.get_mode() == "prod" else "Passer en PROD"
    return OrderedDict([
        ("1", ("Apps (gérer les applications)", "apps")),
        ("2", ("Hub (actions sur le hub)", "hub")),
        ("3", ("Configuration (setup, réseau, coffre)", "config")),
        ("4", ("Scénarios (tests & maintenance)", "scenarios")),
        ("5", (label, "toggle_mode")),
        ("0", ("Quitter", None)),
    ])


def _build_config_menu() -> OrderedDict:
    items = OrderedDict([
        ("1", ("Quick setup", "quick_setup")),
        ("2", ("Réseau (whitelist, CORS)", "network")),
        ("3", ("Admin (identifiants)", "admin")),
        ("4", ("Mode & avancé", "advanced")),
        ("5", ("Coffre (Secrets)", "coffre")),
    ])
    if hub_state.get_mode() == "dev":
        items["6"] = ("Workshop", "workshop")
    items["0"] = ("← Retour", "__back__")
    return items


def _build_menu_static(selected: int = 0) -> Text:
    ctx = _current_context()
    lines: list[Text] = []

    breadcrumb = " > ".join(_nav_stack)
    lines.append(Text(f"[ {breadcrumb} ]\n", style="bold cyan"))

    for i, (digit, (label, _target)) in enumerate(ctx.items()):
        short = label[:37]
        line = f"{digit}. {short}"
        if i == selected:
            lines.append(Text(f" ▸ {line}", style="reverse"))
        elif digit == "0":
            lines.append(Text(f"   {line}", style="bold red"))
        else:
            lines.append(Text(f"   {line}", style="bold white"))

    lines.append(Text(""))
    lines.append(widgets.key_hints([
        (widgets.GLYPH_UPDN, "naviguer"),
        (widgets.GLYPH_ENTER, "valider"),
        (widgets.GLYPH_ESC, "quitter"),
    ]))
    return Text("\n").join(lines)


def _build_empty_col() -> Text:
    return Text("")


# ── Network / CPU / Uptime / Disk helpers ──

_prev_net = (0, 0, time.monotonic())


def _host_uptime() -> float:
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (FileNotFoundError, ValueError):
        return 0.0


def _host_net_totals() -> tuple[int, int]:
    try:
        with open("/proc/net/dev") as f:
            f.readline()
            f.readline()
            rx_total = tx_total = 0
            for line in f:
                parts = line.split()
                if len(parts) < 10:
                    continue
                iface = parts[0].rstrip(":")
                if iface == "lo":
                    continue
                rx = int(parts[1])
                tx = int(parts[9])
                if rx > 0 and rx < 2**63 and tx < 2**63:
                    rx_total += rx
                    tx_total += tx
            return rx_total, tx_total
    except (FileNotFoundError, IndexError, ValueError):
        return 0, 0


def _net_sample() -> dict:
    global _prev_net
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
        _prev_net = (rx, tx, now)

    return {"rx_kbs": max(0, rx_kbs), "tx_kbs": max(0, tx_kbs)}


def _uptime() -> str:
    secs = int(_host_uptime())
    d, h = divmod(secs, 86400)
    h, m = divmod(h, 3600)
    m, s = divmod(m, 60)
    if d:
        return f"{d}d {h:02d}h"
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


def _cpu_load() -> float:
    try:
        with open("/proc/loadavg") as f:
            return float(f.read().split()[0])
    except (FileNotFoundError, ValueError):
        return 0


def _bar(value: float, max_val: float) -> str:
    n = int(min(value, max_val) / max_val * 10)
    bar_fill = "█" * n + "░" * (10 - n)
    return f"[{bar_fill}]"


def _fmt_net(kbs: float) -> str:
    if kbs >= 1000:
        return f"{kbs / 1000:.1f}G"
    if kbs >= 1:
        return f"{kbs:.0f}K"
    return f"{kbs * 1000:.0f}"


def _fmt_bytes(n: int) -> str:
    if n >= 1024 ** 4:
        return f"{n / 1024 ** 4:.1f}T"
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f}G"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.0f}M"
    if n >= 1024:
        return f"{n / 1024:.0f}K"
    return f"{n}B"


# ── Views ──

def _status_banner(state: dict) -> Text:
    services = state.get("services") or []
    docker_ok = state.get("docker", False)
    if not docker_ok:
        return Text("Docker injoignable", style="red")
    lines = ["HUB STATUS"]
    for s in services:
        lines.append(f"  {s.name} ● {s.state}")
    return Text("\n".join(lines), style="green")


def choose_app(canvas: Canvas):
    with _modal(canvas):
        apps = hub_state.get_active_apps()
        active_dir = "APP_Workshop" if hub_state.get_mode() == "dev" else "APPS"
        if not apps:
            canvas.render(Text(f"Aucune app dans {active_dir}/.", style="yellow"))
            pause(canvas)
            return None
        disabled: set[str] = set()
        labels: list[str] = []
        label_to_name: dict[str, str] = {}
        for a in apps:
            if a.legacy:
                lbl = f"{a.name} (legacy)"
                disabled.add(lbl)
            else:
                lbl = a.name
            labels.append(lbl)
            label_to_name[lbl] = a.name
        labels.append("← Retour")
        choice = widgets.select(canvas, "Quelle app ?", labels, disabled=disabled)
        if choice is widgets.ESC or choice is None or choice == "← Retour":
            return None
        return label_to_name.get(choice, choice)


def view_apps(canvas: Canvas, state: dict) -> None:
    with _modal(canvas):
        d = hub_state.get_disk_usage()
        gb_free = d.free / 1024 ** 3
        gb_total = d.total / 1024 ** 3
        active_dir = "APP_Workshop" if hub_state.get_mode() == "dev" else "APPS"
        lines = [
            Text(f"ESPACE DISQUE ({active_dir})\n{gb_free:.1f} / {gb_total:.1f} Go libres ({d.percent_free}%)\n", style="bold"),
            Text(f"Apps ({active_dir})\n", style="bold"),
        ]
        for a in hub_state.get_active_apps():
            size_mb = a.size_bytes / 1024 ** 2
            dirty = "M" if a.git_dirty else " "
            tag = " ⚠legacy" if a.legacy else ""
            lines.append(Text(f"{a.name[:16]:16s} v{a.version[:6]:6s} {dirty} {size_mb:4.0f}M{tag}"))
        canvas.render(Text("\n").join(lines))
        pause(canvas)


def view_coffre(canvas: Canvas, vault_mgr) -> None:
    vault = vault_mgr.vault if vault_mgr else None
    with _modal(canvas):
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

    divs = vault_mgr.check_divergence() if vault_mgr else {}

    while True:
        managed_label = "Mode .env managé : ON" if vault_mgr and vault_mgr.managed_env else "Mode .env managé : OFF"
        entries = []
        for e in vault.list_entries():
            if e.startswith("__"):
                continue
            suffix = " [MODIFIÉ]" if e in divs else ""
            entries.append(e + suffix)
        entries += ["↻ Synchroniser", managed_label, "← Retour"]

        with _modal(canvas):
            sel = widgets.select(canvas, "Coffre :", entries)
        if sel is widgets.ESC or sel is None or sel == "← Retour":
            return
        if sel.startswith("Mode .env managé"):
            if vault_mgr:
                new_state = vault_mgr.toggle_managed()
                state_label = "ON" if new_state else "OFF"
                canvas.render(Text(f"Mode .env managé : {state_label}", style="bold cyan"))
                pause(canvas)
            continue
        if sel == "↻ Synchroniser":
            vault.sync(vault_mgr)
            vault.save()
            canvas.render(Text("✓ Coffre synchronisé.", style="green"))
            pause(canvas)
            continue
        entry = sel.replace(" [MODIFIÉ]", "")
        if entry in divs:
            with _modal(canvas):
                resolve = widgets.select(canvas, f"{entry} est divergent :", [
                    "← Importer dans le coffre",
                    "→ Écraser le disque",
                    "Annuler",
                ])
                if resolve is widgets.ESC or resolve is None or resolve == "Annuler":
                    continue
                if "Importer" in resolve:
                    vault_mgr.pull_env(entry)
                    vault.save()
                    canvas.render(Text(f"✓ {entry} importé dans le coffre", style="green"))
                elif "Écraser" in resolve:
                    vault_mgr.push_env(entry)
                    canvas.render(Text(f"✓ Disque écrasé pour {entry}", style="green"))
                pause(canvas)
            continue

        with _modal(canvas):
            secrets = vault.reveal(entry)
            list_lines = [Text(f"Entrée : {entry}\n", style="bold")]
            for k in secrets:
                list_lines.append(Text(f"  {k} = ••••••••"))
            list_lines.append(Text(""))
            canvas.render(Text("\n").join(list_lines))
            action = widgets.select(canvas, "Action :", [
                "👁 Révéler les valeurs",
                "✎ Modifier le .env",
                "← Retour",
            ])
            if action is widgets.ESC or action is None or action == "← Retour":
                continue
            if action and "Révéler" in action:
                clear_lines = []
                for k, v in secrets.items():
                    clear_lines.append(Text(f"  {k} = {v}"))
                canvas.render(Text("\n").join(clear_lines))
                pause(canvas)
            elif action and "Modifier" in action:
                current = "\n".join(f"{k}={v}" for k, v in secrets.items())
                with _modal(canvas):
                    new_text = widgets.text(canvas, f"Modifier {entry} (KEY=VALUE, une par ligne) :")
                    if new_text is widgets.ESC or new_text is None or not new_text.strip():
                        continue
                    new_data = {}
                    for line in new_text.strip().split("\n"):
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, _, v = line.partition("=")
                        new_data[k.strip()] = v.strip()
                    if new_data:
                        vault.set_entry(entry, new_data)
                        vault.save()
                        if vault_mgr:
                            vault_mgr.push_env(entry)
                        canvas.render(Text(f"✓ {entry} modifié et poussé sur disque", style="green"))
                        pause(canvas)


def _choose_target(canvas: Canvas):
    with _modal(canvas):
        sel = widgets.select(canvas, "Cibler quoi ?", ["Le hub central", "Une app", "← Retour"])
        if sel is widgets.ESC or sel is None or sel == "← Retour":
            return None
        if sel == "Le hub central":
            return "hub"
        return choose_app(canvas)


# ── Actions ──

def _vault_target(fn, args: tuple) -> str | None:
    if _vault_mgr is None or not _vault_mgr.managed_env:
        return None
    if fn is actions.action_update:
        t = args[1] if len(args) > 1 else None
        return "hub" if t == "hub" else f"app:{t}" if t else None
    if fn is actions.action_start_stop:
        return "hub"
    if fn is actions.action_repair:
        return "hub"
    if fn is actions.action_logs:
        t = args[1] if len(args) > 1 else None
        return "hub" if t == "hub" else f"app:{t}" if t else None
    if fn is actions.action_add_app:
        url = args[1] if len(args) > 1 else ""
        name = actions.repo_name_from_url(url) if url else None
        return f"app:{name}" if name else None
    if fn is actions.action_remove_app:
        return f"app:{args[1]}" if len(args) > 1 else None
    if fn is actions.action_workshop_move:
        app_name = args[2] if len(args) > 2 else None
        direction = args[1] if len(args) > 1 else ""
        if direction == "to_workshop":
            return f"app:{app_name}" if app_name else None
        else:
            return f"ws:{app_name}" if app_name else None
    if fn is actions.action_init:
        return "hub"
    return None


def _vault_pull_target(fn, args: tuple) -> str | None:
    if _vault_mgr is None or not _vault_mgr.managed_env:
        return None
    if fn is actions.action_workshop_move:
        app_name = args[2] if len(args) > 2 else None
        direction = args[1] if len(args) > 1 else ""
        if direction == "to_workshop":
            return f"ws:{app_name}-ws" if app_name else None
        else:
            base = app_name[:-3] if app_name and app_name.endswith("-ws") else app_name
            return f"app:{base}" if base else None
    return _vault_target(fn, args)


def _run_action(canvas: Canvas, label: str, fn, *args, **kw) -> bool:
    target = _vault_target(fn, args)
    pull_target = _vault_pull_target(fn, args)
    log_lines: list[str] = []

    def on_log(line: str) -> None:
        log_lines.append(line)

    if target and _vault_mgr:
        _vault_mgr.push_env(target, on_log=on_log)

    def runner() -> int:
        try:
            fn(*args, **kw)
            return 0
        except Exception:
            import traceback
            traceback.print_exc()
            return 1

    retcode, cancelled = canvas.stream_command(runner, label=label)
    success = not cancelled and retcode == 0

    if target and _vault_mgr and success:
        actual_pull = pull_target if pull_target else target
        _vault_mgr.pull_env(actual_pull, on_log=on_log)
        _vault_mgr.vault.save()

    canvas.render(Text(f"{'✓' if success else '✗'} {label}", style="green" if success else "red"))
    pause(canvas)
    return success


def _workshop_menu(canvas: Canvas, dry_run: bool) -> None:
    while True:
        with _modal(canvas):
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
                    _run_action(canvas, f"Workshop → {app_name}",
                                actions.action_workshop_move, _ACTION_CONSOLE, "to_workshop", app_name, dry_run=dry_run)
                    return
            elif sel.startswith("  [←]"):
                app_name = sel.split()[1]
                base = app_name[:-3] if app_name.endswith("-ws") else app_name
                msg = f"Remettre « {app_name} » en production dans APPS/ ({base}) ?"
                if widgets.confirm(canvas, msg, default=False) is True:
                    _run_action(canvas, f"Workshop ← {base}",
                                actions.action_workshop_move, _ACTION_CONSOLE, "to_production", app_name, dry_run=dry_run)
                    return


# ── Détection clone frais ──

def _offer_snapshot(canvas: Canvas, vault_mgr, *, dry_run: bool) -> None:
    apps = hub_state.get_apps()
    if not apps:
        return
    with _modal(canvas):
        canvas.render(Text(
            "📸 Premier passage en DEV\n\n"
            "Veux-tu snapshooter des apps de APPS/ vers APP_Workshop/\n"
            "pour tester avec des données réelles ?\n\n"
            "(Utilise workshop_move — code + volumes Docker)",
            style="bold cyan"
        ))
        choices = ["📦 Tout snapshooter"] + [f"  {a.name}" for a in apps] + ["Non, plus tard"]
        sel = widgets.select(canvas, "Snapshot :", choices)
        if sel is widgets.ESC or sel is None or sel == "Non, plus tard":
            return
        if sel == "📦 Tout snapshooter":
            for a in apps:
                if not _workshop_move_one(canvas, a.name, "to_workshop", vault_mgr, dry_run):
                    break
        else:
            app_name = sel.strip().split()[0] if sel.strip() else ""
            if app_name:
                _workshop_move_one(canvas, app_name, "to_workshop", vault_mgr, dry_run)


def _workshop_move_one(canvas: Canvas, app_name: str, direction: str, vault_mgr, dry_run: bool) -> bool:
    return _run_action(
        canvas, f"Snapshot {app_name}",
        actions.action_workshop_move, _ACTION_CONSOLE, direction, app_name, dry_run=dry_run,
    )


def _choose_mode(canvas: Canvas, vault_mgr) -> str:
    with _modal(canvas):
        lines = [
            Text("          Choisis le mode de travail :\n\n", style="bold cyan"),
            Text("  1. PRODUCTION\n", style="bold green"),
            Text("      · Apps actives : APPS/\n", style="dim"),
            Text("      · Coffre : OBLIGATOIRE\n", style="dim"),
            Text("      · Les .env sont gérés par le coffre\n\n", style="dim"),
            Text("  2. DÉVELOPPEMENT\n", style="bold yellow"),
            Text("      · Apps actives : APP_Workshop/\n", style="dim"),
            Text("      · Coffre : optionnel (Échap pour ignorer)\n", style="dim"),
            Text("      · Les .env ne sont jamais modifiés\n\n", style="dim"),
            Text("  Échap — Quitter", style="dim"),
        ]
        canvas.render(Text.assemble(*lines))
        fd = sys.stdin.fileno()
        while True:
            k = keys.read_key(fd, timeout=0.15)
            if k is None:
                continue
            if isinstance(k, keys.KeyChar) and k.char == "1":
                return _choose_prod(canvas, vault_mgr)
            if isinstance(k, keys.KeyChar) and k.char == "2":
                return _choose_dev(canvas, vault_mgr)
            if isinstance(k, (keys.KeyEsc, keys.KeyCtrlC)):
                canvas.render(Text("À bientôt.", style="bold"))
                raise SystemExit(0)


def _choose_prod(canvas: Canvas, vault_mgr) -> str:
    vault = vault_mgr.vault if vault_mgr else None
    if vault is None or vault.is_locked:
        if vault and vault.path.exists():
            canvas.render(Text("🔐 PRODUCTION — le coffre est obligatoire.\nEntre ta passphrase :", style="bold cyan"))
            pw = widgets.password(canvas, "Passphrase du coffre :")
            if pw is widgets.ESC or pw is None or not pw:
                canvas.render(Text("Coffre obligatoire en PROD. Retour au choix du mode.", style="red"))
                pause(canvas)
                return _choose_mode(canvas, vault_mgr)
            if not vault.unlock(pw):
                canvas.render(Text("Passphrase incorrecte.", style="red"))
                pause(canvas)
                return _choose_mode(canvas, vault_mgr)
            canvas.render(Text("✓ Coffre déverrouillé — mode PRODUCTION activé.", style="bold green"))
        else:
            canvas.render(Text("🔐 Crée d'abord un coffre.\nEntre une passphrase :", style="bold cyan"))
            pw = widgets.password(canvas, "Passphrase du coffre :")
            if pw is widgets.ESC or pw is None or not pw:
                canvas.render(Text("Coffre obligatoire en PROD. Retour au choix du mode.", style="red"))
                pause(canvas)
                return _choose_mode(canvas, vault_mgr)
            if vault.unlock(pw):
                vault.sync()
                vault.save()
                canvas.render(Text("✓ Coffre créé — mode PRODUCTION activé.", style="bold green"))
    pause(canvas)
    return "prod"


def _choose_dev(canvas: Canvas, vault_mgr) -> str:
    vault = vault_mgr.vault if vault_mgr else None
    if vault and vault.path.exists() and vault.is_locked:
        canvas.render(Text("🔐 DEV — coffre optionnel.\nEntre ta passphrase ou Échap pour ignorer.", style="bold cyan"))
        pw = widgets.password(canvas, "Passphrase (Échap=ignorer) :")
        if pw is widgets.ESC or pw is None:
            canvas.render(Text("Coffre ignoré — mode DÉVELOPPEMENT activé.", style="bold yellow"))
        elif vault.unlock(pw):
            canvas.render(Text("✓ Coffre déverrouillé — mode DÉVELOPPEMENT activé.", style="bold yellow"))
        else:
            canvas.render(Text("Passphrase incorrecte — coffre ignoré.", style="yellow"))
    elif vault and not vault.path.exists():
        canvas.render(Text("Aucun coffre trouvé — mode DÉVELOPPEMENT activé.", style="bold yellow"))
    pause(canvas)
    return "dev"


def _init_vault(canvas: Canvas, vault) -> None:
    with _modal(canvas):
        canvas.render(Text(
            "🔐 Création du coffre chiffré\n\n"
            "Cette passphrase protège tous les secrets du parc.\n"
            "⚠️  ELLE EST IRRÉCUPÉRABLE SI PERDUE.\n"
            "   Sans elle, le contenu du coffre est définitivement inaccessible.\n"
            "   (Les .env sur disque restent lisibles — seul le coffre est verrouillé.)\n\n"
            "Choisis une passphrase forte et conserve-la en lieu sûr.",
            style="bold cyan"
        ))
        pw = widgets.password(canvas, "Passphrase du coffre :")
        if pw is widgets.ESC or pw is None or not pw:
            canvas.render(Text("Coffre non créé — tu pourras le faire plus tard dans Config → Coffre.", style="yellow"))
            pause(canvas)
            return
        if vault.unlock(pw):
            vault.sync()
            vault.save()
            canvas.render(Text("✓ Coffre créé et synchronisé.", style="bold green"))
            pause(canvas)


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
    _run_action(canvas, "Initialisation parc",
                actions.action_init, _ACTION_CONSOLE, dry_run=dry_run)
    pause(canvas)


# ── Layout helper ──

@contextlib.contextmanager
def _modal(canvas: Canvas):
    yield


# ── Unit action executors (reusable by dispatch + scenarios) ──

def _exec_update_app(canvas: Canvas, state: dict, vault_mgr, *, dry_run: bool) -> bool:
    app = choose_app(canvas)
    if not app:
        return False
    return _run_action(canvas, f"Update {app}", actions.action_update, _ACTION_CONSOLE, app, dry_run=dry_run)


def _exec_add_app(canvas: Canvas, state: dict, vault_mgr, *, dry_run: bool, param: str = None) -> bool:
    url = param
    if url is None:
        with _modal(canvas):
            url = widgets.text(canvas, "URL git du dépôt :")
    if url is widgets.ESC or not url or not url.strip():
        return False
    return _run_action(canvas, "Ajout app", actions.action_add_app, _ACTION_CONSOLE, url.strip(), dry_run=dry_run)


def _exec_remove_app(canvas: Canvas, state: dict, vault_mgr, *, dry_run: bool, param: str = None) -> bool:
    app = param if param else choose_app(canvas)
    if not app:
        return False
    with _modal(canvas):
        if widgets.confirm(canvas, f"Supprimer « {app} » (→ corbeille) ?", default=False) is not True:
            return False
    return _run_action(canvas, f"Suppression {app}", actions.action_remove_app, _ACTION_CONSOLE, app, vault_mgr.vault if vault_mgr else None, dry_run=dry_run)


def _exec_logs_app(canvas: Canvas, state: dict, vault_mgr, *, dry_run: bool) -> bool:
    app = choose_app(canvas)
    if not app:
        return False
    return _run_action(canvas, f"Logs {app}", actions.action_logs, _ACTION_CONSOLE, app)


def _exec_update_hub(canvas: Canvas, state: dict, vault_mgr, *, dry_run: bool) -> bool:
    return _run_action(canvas, "Update hub", actions.action_update, _ACTION_CONSOLE, "hub", dry_run=dry_run)


def _exec_repair(canvas: Canvas, state: dict, vault_mgr, *, dry_run: bool) -> bool:
    return _run_action(canvas, "Diagnostic", actions.action_repair, _ACTION_CONSOLE, dry_run=dry_run)


def _exec_start_stop(canvas: Canvas, state: dict, vault_mgr, *, dry_run: bool, param: str = None) -> bool:
    sub = param
    if sub is None:
        with _modal(canvas):
            sub = widgets.select(canvas, "Action :", ["start", "stop", "restart", "← Retour"])
    if sub is widgets.ESC or sub is None or sub == "← Retour":
        return False
    return _run_action(canvas, f"{sub} hub", actions.action_start_stop, _ACTION_CONSOLE, sub, dry_run=dry_run)


def _exec_view_apps(canvas: Canvas, state: dict, vault_mgr, *, dry_run: bool) -> bool:
    with _modal(canvas):
        view_apps(canvas, state)
    return True


def _exec_logs_hub(canvas: Canvas, state: dict, vault_mgr, *, dry_run: bool) -> bool:
    return _run_action(canvas, "Logs hub", actions.action_logs, _ACTION_CONSOLE, "hub")


def _exec_quick_setup(canvas: Canvas, state: dict, vault_mgr, *, dry_run: bool) -> bool:
    if dry_run:
        canvas.render(Text("[dry-run] Setup wizard non disponible", style="yellow"))
        pause(canvas)
        return True
    setup_wizard.quick_setup(canvas)
    return True


def _exec_network(canvas: Canvas, state: dict, vault_mgr, *, dry_run: bool) -> bool:
    if dry_run:
        canvas.render(Text("[dry-run] Réseau non disponible", style="yellow"))
        pause(canvas)
        return True
    setup_wizard.submenu_network(canvas)
    return True


def _exec_admin(canvas: Canvas, state: dict, vault_mgr, *, dry_run: bool) -> bool:
    if dry_run:
        canvas.render(Text("[dry-run] Admin non disponible", style="yellow"))
        pause(canvas)
        return True
    setup_wizard.submenu_admin(canvas)
    return True


def _exec_advanced(canvas: Canvas, state: dict, vault_mgr, *, dry_run: bool) -> bool:
    if dry_run:
        canvas.render(Text("[dry-run] Mode & avancé non disponible", style="yellow"))
        pause(canvas)
        return True
    setup_wizard.submenu_advanced(canvas)
    return True


def _exec_coffre(canvas: Canvas, state: dict, vault_mgr, *, dry_run: bool) -> bool:
    if dry_run:
        canvas.render(Text("[dry-run] Coffre non disponible", style="yellow"))
        pause(canvas)
        return True
    view_coffre(canvas, vault_mgr)
    return True


def _exec_workshop(canvas: Canvas, state: dict, vault_mgr, *, dry_run: bool) -> bool:
    _workshop_menu(canvas, dry_run)
    return True


def _exec_toggle_mode(canvas: Canvas, state: dict, vault_mgr, *, dry_run: bool) -> bool:
    new_mode = "dev" if hub_state.get_mode() == "prod" else "prod"
    with _modal(canvas):
        if widgets.confirm(canvas, f"Passer en mode {new_mode.upper()} ?", default=True) is not True:
            return False
    if new_mode == "dev" and hub_state.APPS_DIR.exists():
        _offer_snapshot(canvas, vault_mgr, dry_run=dry_run)
    hub_state.set_mode(new_mode)
    global _nav_stack
    _nav_stack = ["main"]
    canvas.render(Text(f"✓ Mode {new_mode.upper()} activé.", style="bold green"))
    pause(canvas)
    return False


ACTION_HANDLERS = {
    "update_app": _exec_update_app,
    "add_app": _exec_add_app,
    "remove_app": _exec_remove_app,
    "logs_app": _exec_logs_app,
    "update_hub": _exec_update_hub,
    "repair": _exec_repair,
    "start_stop": _exec_start_stop,
    "view_apps": _exec_view_apps,
    "logs_hub": _exec_logs_hub,
    "quick_setup": _exec_quick_setup,
    "network": _exec_network,
    "admin": _exec_admin,
    "advanced": _exec_advanced,
    "coffre": _exec_coffre,
    "workshop": _exec_workshop,
    "toggle_mode": _exec_toggle_mode,
    "sc_diag_quick": _sc_diag_quick,
    "sc_update_full": _sc_update_full,
    "sc_app_rescue": _sc_app_rescue,
    "sc_health": _sc_health,
}


# ── Navigation dispatch ──

def _dispatch(buf: str, canvas: Canvas, state: dict, vault_mgr, *, dry_run: bool) -> bool:
    ctx = _current_context()
    if buf not in ctx:
        canvas.render(Text(f"✗ Choix invalide : {buf}", style="red"))
        pause(canvas)
        return False

    _label, target = ctx[buf]
    if target is None:
        canvas.render(Text("À bientôt.", style="bold"))
        return True  # quit

    if target == "__back__":
        if len(_nav_stack) > 1:
            _nav_stack.pop()
        return False

    if target in MENU_TREE:
        _nav_stack.append(target)
        return False

    handler = ACTION_HANDLERS.get(target)
    if handler is not None:
        try:
            handler(canvas, state, vault_mgr, dry_run=dry_run)
        except KeyboardInterrupt:
            canvas.render(Text("Action interrompue — retour au menu.", style="dim"))
            pause(canvas)
    return False


# ── Main loop ──

def install_layout(canvas: Canvas, state: dict) -> None:
    canvas.set_layout(
        z3_builder=lambda: _build_welcome(state),
        z1_builder=lambda: _build_resources(state),
        z4_builder=_build_logs,
        z2_builder=lambda: _build_services(state),
    )


def _refresh_apps_status(state: dict) -> None:
    try:
        state["app_status"] = hub_state.get_apps_status()
    except Exception:
        pass


def _apps_poll_worker(state: dict, stop_event: threading.Event, interval: float = 3.0) -> None:
    while not stop_event.is_set():
        _refresh_apps_status(state)
        stop_event.wait(interval)


def main_loop(canvas: Canvas, state: dict, vault_mgr, *, dry_run: bool) -> None:
    global _nav_stack, _vault_mgr
    _nav_stack = ["main"]
    _vault_mgr = vault_mgr

    if vault_mgr is not None:
        imported = vault_mgr.auto_import_missing()
        if imported:
            state["_vault_imported"] = imported
        divs = vault_mgr.check_divergence()
        if divs:
            state["_vault_divergences"] = list(divs.values())

    install_layout(canvas, state)
    fd = sys.stdin.fileno()

    stop_event = threading.Event()
    poller = threading.Thread(
        target=_apps_poll_worker, args=(state, stop_event), daemon=True
    )
    poller.start()

    selected = 0
    prev_nav: tuple | None = None

    try:
        while True:
            ctx = _current_context()
            item_keys = list(ctx.keys())
            nav_sig = tuple(_nav_stack)
            if nav_sig != prev_nav:
                selected = 0
                prev_nav = nav_sig
            if selected >= len(item_keys):
                selected = 0

            canvas.render(_build_menu_static(selected))

            key = keys.read_key(fd, timeout=0.12)
            if key is None:
                continue

            if isinstance(key, keys.KeyCtrlC):
                raise KeyboardInterrupt()

            if isinstance(key, keys.KeyUp):
                selected = (selected - 1) % len(item_keys)
            elif isinstance(key, keys.KeyDown):
                selected = (selected + 1) % len(item_keys)
            elif isinstance(key, keys.KeyEnter):
                if _dispatch(item_keys[selected], canvas, state, vault_mgr, dry_run=dry_run):
                    return
                selected = 0
            elif isinstance(key, keys.KeyEsc):
                if len(_nav_stack) > 1:
                    _nav_stack.pop()
                    selected = 0
                else:
                    if widgets.confirm(canvas, "Quitter hubctl ?", default=False) is True:
                        canvas.render(Text("À bientôt.", style="bold"))
                        return
            elif isinstance(key, keys.KeyChar):
                if key.char in item_keys:
                    selected = item_keys.index(key.char)
    finally:
        stop_event.set()
