from __future__ import annotations
import contextlib
import sys
import threading
import time

from rich.console import Console
from rich.text import Text

from hubctl_cli import actions, hub_state, keys, utils, widgets
from hubctl_cli.canvas import Canvas
from hubctl_cli.vault_manager import VaultManager, restore_all

_ACTION_CONSOLE = Console(file=utils.StreamWriter(), force_terminal=False, width=88)
_vault_mgr: VaultManager | None = None

_APPS_WINDOW = 4
_APPS_SCROLL_PERIOD = 2.0

_app_status_rows = None

# ── Snapshot d'état (cache rafraîchi en arrière-plan) ──
# Les builders du dashboard ne lisent JAMAIS directement les fonctions bloquantes
# de hub_state (check_docker / get_hub_services / get_active_apps) : celles-ci
# lancent des subprocesss Docker + `git status` × N et parcourent récursivement
# chaque app (_dir_size), soit ~400 ms par frame. Appelées sur le thread de rendu,
# elles gèlent la lecture clavier → lag perceptible sur TOUS les inputs.
# On délègue donc à un poll worker (cf. _state_poll_worker) qui peuple ce cache.
_state_lock = threading.Lock()
_snap: dict = {
    "docker_ok": None,     # bool | None  (None = pas encore calculé)
    "services": None,      # list[ServiceInfo] | None
    "apps_count": None,    # tuple[int, int] = (total, legacy) | None
}


def _refresh_state_snapshot() -> None:
    """Rafraîchit le cache d'état du dashboard. À n'appeler que depuis le poll
    worker (appels bloquants Docker/git). Ne jamais appeler sur le thread de rendu.
    """
    try:
        docker_ok = hub_state.check_docker()
        services = hub_state.get_hub_services() if docker_ok else []
        apps = hub_state.get_active_apps()
        legacy = sum(1 for a in apps if getattr(a, "legacy", False))
        with _state_lock:
            _snap["docker_ok"] = docker_ok
            _snap["services"] = services
            _snap["apps_count"] = (len(apps), legacy)
    except Exception:
        # Best-effort : on garde le précédent snapshot si l'appel échoue.
        pass


def seed_state_snapshot(initial: dict | None = None) -> None:
    """Amorce le cache depuis un état pré-calculé (ex: preload_with_bar),
    pour éviter tout appel bloquant au démarrage. Clés attendues (optionnelles) :
    'docker' (bool), 'services' (list[ServiceInfo]).
    """
    if not initial:
        return
    with _state_lock:
        if _snap["docker_ok"] is None and initial.get("docker") is not None:
            _snap["docker_ok"] = bool(initial["docker"])
        if _snap["services"] is None and initial.get("services") is not None:
            _snap["services"] = initial["services"]


def pause(canvas: Canvas, message=None) -> None:
    """Affiche un message optionnel + invite Entrée.

    Quand `message` est fourni, il est composé AVEC l'invite (pas écrasé).
    """
    if message is not None:
        if isinstance(message, str):
            message = Text(message)
        canvas.render(Text.assemble(message, Text("\n\n"), Text("↵ Entrée pour continuer…", style="dim")))
    else:
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


# ── Monitoring helpers ──

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


def _mem_usage() -> tuple[float, float]:
    """Retourne (ram_utilisee_pct, swap_utilisee_pct) depuis /proc/meminfo."""
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split(":", 1)
                if len(parts) == 2:
                    info[parts[0].strip()] = int(parts[1].strip().split()[0])
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        ram_pct = ((total - avail) / total * 100) if total > 0 else 0.0
        swap_total = info.get("SwapTotal", 0)
        swap_free = info.get("SwapFree", 0)
        swap_pct = ((swap_total - swap_free) / swap_total * 100) if swap_total > 0 else 0.0
        return ram_pct, swap_pct
    except (FileNotFoundError, ValueError, ZeroDivisionError):
        return 0.0, 0.0


def _bar(value: float, max_val: float) -> str:
    n = int(min(value, max_val) / max_val * 10)
    bar_fill = "█" * n + "░" * (10 - n)
    return f"[{bar_fill}]"


def _fmt_net(kbs: float) -> str:
    # kbs = kilo-octets/s. 1000 KB/s ≈ 1 MB/s, 1_000_000 KB/s ≈ 1 GB/s.
    if kbs >= 1_000_000:
        return f"{kbs / 1_000_000:.1f}G"
    if kbs >= 1000:
        return f"{kbs / 1000:.1f}M"
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


# ── Dashboard builders ──

def _build_welcome() -> Text:
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

    is_dev = hub_state.get_mode() == "dev"
    hub_color = "bold cyan" if is_dev else "bold magenta"

    text = Text()
    for row in chars:
        line = "".join(row)
        pad = (width - len(line)) // 2
        text.append(" " * max(0, pad) + line + "\n", style=hub_color)

    version = hub_state.get_hub_version()
    cpu = _cpu_pct()
    now = time.strftime("%H:%M UTC", time.gmtime())
    mode_badge_str = " DEV " if is_dev else " PROD "
    badge_style = "reverse bold cyan" if is_dev else "bold magenta"
    header_line = f" HUB MASTER OS {version}  |  UPTIME: {_uptime()}  |  LOAD: {cpu:.2f}  |  {now}  {mode_badge_str}"
    padded = header_line.ljust(width)

    header_text = Text(padded, style="bold white")
    badge_start = len(header_line) - len(mode_badge_str)
    badge_end = len(header_line)
    header_text.stylize(badge_style, badge_start, badge_end)

    text.append(header_text)
    return text


def _build_resources() -> Text:
    text = Text()
    text.append("[ SYSTEM RESOURCES ]\n", style="bold blue")
    text.append("\n")

    cpu = _cpu_pct()
    text.append(f"CPU {_bar(int(cpu), 100)} {cpu:.0f}%\n", style="white")

    ram_pct, swap_pct = _mem_usage()
    text.append(f"RAM {_bar(int(ram_pct), 100)} {ram_pct:.0f}%\n", style="white")

    text.append(f"SWP {_bar(int(swap_pct), 100)} {swap_pct:.0f}%\n", style="dim")

    text.append("\n")

    disk = hub_state.get_disk_usage()
    disk_pct = 100 - disk.percent_free
    # Barre et chiffre alignés sur le % utilisé (avant : barre = utilisé mais
    # chiffre = % libre → lecture contradictoire pour l'opérateur).
    text.append(f"/     {_bar(int(disk_pct), 100)} {disk_pct:.0f}%\n", style="white")
    text.append(f"/data {_bar(0, 100)}   —\n", style="dim")

    text.append("\n")

    net = _net_sample()
    rx_total, tx_total = _host_net_totals()
    text.append(f"▼ Net RX: {_fmt_net(net['rx_kbs'])}\n", style="dim")
    text.append(f"▲ Net TX: {_fmt_net(net['tx_kbs'])}\n", style="dim")
    text.append(f"Σ {_fmt_bytes(rx_total)}↓ {_fmt_bytes(tx_total)}↑\n", style="dim")

    text.append("\n")
    with _state_lock:
        apps_count = _snap["apps_count"]
    if apps_count is None:
        text.append("Apps : …", style="bold green")
    else:
        total, legacy_count = apps_count
        if legacy_count:
            text.append(f"Apps : {total} ({legacy_count} legacy)", style="bold green")
        else:
            text.append(f"Apps : {total}", style="bold green")

    return text


def _build_services() -> Text:
    text = Text()
    text.append("[ SERVICES STATUS ]\n", style="bold blue")
    text.append("\n")

    with _state_lock:
        docker_ok = _snap["docker_ok"]
        services = _snap["services"]

    if docker_ok is None or services is None:
        # Premier rendu : le poll worker n'a pas encore rempli le cache.
        text.append("  …\n", style="dim")
    elif not docker_ok:
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

    rows = _app_status_rows
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


def _build_logs() -> Text:
    text = Text()
    text.append("[ RECENT LOGS ]\n", style="bold blue")
    text.append("> Pas de logs récents.\n", style="dim")
    return text


def _build_key_hints() -> Text:
    is_dev = hub_state.get_mode() == "dev"
    text = Text()
    text.append("[ COMMANDES ]\n", style="bold blue")
    text.append("\n")

    if is_dev:
        text.append(" 1  ", style="bold green")
        text.append("Démarrer prod\n", style="white")
        text.append(" 2  ", style="bold red")
        text.append("Arrêter prod\n", style="white")
        text.append(" 3  ", style="bold cyan")
        text.append("Démarrer dev\n", style="white")
        text.append(" 4  ", style="bold yellow")
        text.append("Arrêter dev\n", style="white")
        text.append(" 5  ", style="bold blue")
        text.append("Mettre à jour\n", style="white")
        text.append(" 6  ", style="bold magenta")
        text.append("Coffre\n", style="white")
    else:
        text.append(" 1  ", style="bold green")
        text.append("Démarrer tout\n", style="white")
        text.append(" 2  ", style="bold red")
        text.append("Arrêter tout\n", style="white")
        text.append(" 3  ", style="bold blue")
        text.append("Mettre à jour\n", style="white")
        text.append(" 4  ", style="bold magenta")
        text.append("Coffre\n", style="white")

    text.append("\n")
    text.append(" 0  ", style="dim")
    text.append("Quitter\n", style="dim")
    text.append("Esc  ", style="dim")
    text.append("Quitter\n", style="dim")
    return text


# ── Layout ──

def install_layout(canvas: Canvas) -> None:
    canvas.set_layout(
        z3_builder=_build_welcome,
        z1_builder=_build_resources,
        z4_builder=_build_logs,
        z2_builder=_build_services,
    )


# ── Polling ──

def _refresh_apps_status() -> None:
    global _app_status_rows
    try:
        _app_status_rows = hub_state.get_apps_status()
    except Exception:
        pass


def _state_poll_worker(canvas: Canvas) -> None:
    """Poll worker d'arrière-plan : rafraîchit les données coûteuses du dashboard
    (Docker ps/info, git status × N, _dir_size × N, apps status) toutes les 3 s.
    Ces appels sont bloquants (~400 ms) : ils DOIVENT rester hors du thread de
    rendu, sinon la lecture clavier est gelée → lag sur les inputs.
    """
    # Premier refresh immédiat : on veut le cache peuplé au plus vite au démarrage.
    _refresh_state_snapshot()
    _refresh_apps_status()
    while True:
        time.sleep(3.0)
        _refresh_state_snapshot()
        _refresh_apps_status()


# ── Views ──

def _status_banner() -> Text:
    services = hub_state.get_hub_services()
    docker_ok = hub_state.check_docker()
    if not docker_ok:
        return Text("Docker injoignable", style="red")
    lines = ["HUB STATUS"]
    for s in services:
        lines.append(f"  {s.name} ● {s.state}")
    return Text("\n".join(lines), style="green")


def choose_app(canvas: Canvas, prompt: str = "Quelle app ?"):
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
        choice = widgets.select(canvas, prompt, labels, disabled=disabled)
        if choice is widgets.ESC or choice is None or choice == "← Retour":
            return None
        return label_to_name.get(choice, choice)


def view_coffre(canvas: Canvas, vault_mgr) -> None:
    vault = vault_mgr.vault if vault_mgr else None
    if vault is None:
        pause(canvas, Text("Coffre indisponible.", style="yellow"))
        return
    with _modal(canvas):
        if vault.is_locked:
            pw = widgets.password(canvas, "Passphrase du coffre :")
            if pw is widgets.ESC or pw is None or not pw or not vault.unlock(pw):
                pause(canvas, Text("Coffre verrouillé.", style="red"))
                return

    while True:
        divs = vault_mgr.check_divergence() if vault_mgr else {}
        entries = []
        for e in vault.list_entries():
            if e.startswith("__"):
                continue
            entries.append(e + (" [MODIFIÉ]" if e in divs else ""))
        actions_lbl = ["💾 Sauvegarder maintenant", "⬇ Récupérer (export)", "🔑 Changer la passphrase", "← Retour"]

        with _modal(canvas):
            sel = widgets.select(canvas, "Coffre (sauvegarde) :", entries + actions_lbl)
        if sel is widgets.ESC or sel is None or sel == "← Retour":
            return
        if sel == "💾 Sauvegarder maintenant":
            # Si aucun .env n'a divergé, la sauvegarde est inutile
            # (le contenu disque est identique à celui du coffre).
            if not divs:
                pause(canvas, Text("✓ Coffre déjà à jour — rien à sauvegarder.", style="green"))
                continue
            try:
                summary = vault_mgr.backup_all()
                pause(canvas, Text(
                    f"✓ Sauvegardé : {summary['env']} .env, {summary['files']} fichiers.",
                    style="green"))
            except Exception as e:
                pause(canvas, Text(f"✗ Erreur lors de la sauvegarde :\n{e}", style="red"))
            continue
        if sel == "⬇ Récupérer (export)":
            try:
                dest = vault_mgr.export_recovery()
                pause(canvas, Text(f"✓ Export de récupération :\n{dest}", style="green"))
            except Exception as e:
                pause(canvas, Text(f"✗ Erreur lors de l'export :\n{e}", style="red"))
            continue
        if sel == "🔑 Changer la passphrase":
            with _modal(canvas):
                pw1 = widgets.password(canvas, "Nouvelle passphrase :")
                pw2 = widgets.password(canvas, "Confirme la passphrase :")
            if pw1 is widgets.ESC or pw2 is widgets.ESC or not pw1 or pw1 != pw2:
                pause(canvas, Text("Passphrase inchangée (vide ou non concordante).", style="yellow"))
                continue
            try:
                vault.change_passphrase(pw1)
                vault.save()
                pause(canvas, Text("✓ Passphrase changée.", style="green"))
            except Exception as e:
                pause(canvas, Text(f"✗ Erreur lors du changement de passphrase :\n{e}", style="red"))
            continue
        entry = sel.replace(" [MODIFIÉ]", "")
        with _modal(canvas):
            secrets = vault.reveal(entry)
            list_lines = [Text(f"Entrée : {entry}\n", style="bold")]
            for k in secrets:
                list_lines.append(Text(f"  {k} = ••••••••"))
            list_lines.append(Text(""))
            canvas.render(Text("\n").join(list_lines))
            action = widgets.select(canvas, "Action :", ["👁 Révéler les valeurs", "← Retour"])
            if action and "Révéler" in action:
                clear_lines = [Text(f"  {k} = {v}") for k, v in secrets.items()]
                canvas.render(Text("\n").join(clear_lines))
                pause(canvas)


# ── Action executor ──

def _run_action(canvas: Canvas, label: str, fn, *args, **kw) -> bool:
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
    # stream_command a déjà affiché le résultat avec les logs dans son
    # dernier render — on compose juste le résumé avec l'invite Entrée.
    pause(canvas, Text(
        f"{'✓' if success else '✗'} {label}",
        style="green" if success else "red"))
    return success


# ── Vault unlock ──

def _require_vault(canvas: Canvas, vault_mgr) -> None:
    vault = vault_mgr.vault if vault_mgr else None
    if vault is None:
        return
    with _modal(canvas):
        if vault.path.exists():
            canvas.render(Text("🔐 Coffre — passphrase obligatoire.", style="bold cyan"))
            while True:
                pw = widgets.password(canvas, "Passphrase du coffre :")
                if pw is widgets.ESC or pw is None:
                    canvas.render(Text("À bientôt.", style="bold"))
                    raise SystemExit(0)
                if pw and vault.unlock(pw):
                    break
                canvas.render(Text("Passphrase incorrecte.", style="red"))
                pause(canvas)
        else:
            canvas.render(Text(
                "🔐 Création du coffre (obligatoire)\n\n"
                "Cette passphrase protège et sauvegarde tous les secrets du parc.\n"
                "⚠️  IRRÉCUPÉRABLE SI PERDUE.", style="bold cyan"))
            while True:
                pw = widgets.password(canvas, "Nouvelle passphrase du coffre :")
                if pw is widgets.ESC or pw is None:
                    canvas.render(Text("À bientôt.", style="bold"))
                    raise SystemExit(0)
                if not pw:
                    canvas.render(Text("Passphrase vide — recommence.", style="red"))
                    pause(canvas)
                    continue
                pw2 = widgets.password(canvas, "Confirmer la passphrase :")
                if pw2 is widgets.ESC or pw2 is None:
                    canvas.render(Text("À bientôt.", style="bold"))
                    raise SystemExit(0)
                if pw != pw2:
                    canvas.render(Text("Les passphrases ne correspondent pas — recommence.", style="red"))
                    pause(canvas)
                    continue
                vault.unlock(pw)
                break
    if vault_mgr:
        summary = vault_mgr.backup_all()
        canvas.render(Text(
            f"✓ Coffre prêt — sauvegarde : {summary['env']} .env, {summary['files']} fichiers.",
            style="bold green"))
        pause(canvas)


@contextlib.contextmanager
def _modal(canvas: Canvas):
    yield


# ── Coffre submenu ──

def _coffre_submenu(canvas: Canvas, vault_mgr: VaultManager) -> None:
    with _modal(canvas):
        choice = widgets.select(canvas, "Coffre :", [
            "Sauvegarder (backup)",
            "Restaurer (restore)",
            "Statut (status)",
            "← Retour",
        ])
    if choice is widgets.ESC or choice is None or choice == "← Retour":
        return

    if "Sauvegarder" in choice:
        _run_action(canvas, "Sauvegarde du coffre", lambda: vault_mgr.backup_all())
    elif "Restaurer" in choice:
        _run_action(canvas, "Restauration depuis le coffre", lambda: restore_all(vault_mgr.vault))
    elif "Statut" in choice:
        divs = vault_mgr.check_divergence()
        if divs:
            lines = ["Divergences coffre/disque :"]
            for k, v in divs.items():
                lines.append(f"  {k}: {v}")
            canvas.render(Text("\n".join(lines), style="yellow"))
        else:
            canvas.render(Text("✓ Coffre à jour.", style="green"))
        pause(canvas)


# ── Main loop ──

def main_loop(canvas: Canvas, vault: VaultManager, initial_state: dict | None = None) -> None:
    global _vault_mgr
    _vault_mgr = vault
    is_dev = hub_state.get_mode() == "dev"

    install_layout(canvas)

    # Amorce le cache depuis l'état pré-calculé par le preload (docker/services),
    # puis lance le poll worker qui maintiendra le cache à jour en arrière-plan.
    seed_state_snapshot(initial_state)
    threading.Thread(target=_state_poll_worker, args=(canvas,), daemon=True).start()

    _render_interval = 0.25  # 4 fps max — évite l'étranglement du terminal
    _last_render = 0.0

    while True:
        now = time.monotonic()
        if now - _last_render >= _render_interval:
            canvas.render(_build_key_hints())
            _last_render = now

        key = keys.read_key(sys.stdin.fileno(), timeout=0.12)
        if key is None:
            continue
        if isinstance(key, keys.KeyCtrlC):
            raise KeyboardInterrupt()
        if isinstance(key, keys.KeyEsc):
            confirm = widgets.confirm(canvas, "Quitter hubctl ?")
            if confirm is True:
                break
        if not isinstance(key, keys.KeyChar):
            continue

        c = key.char

        if is_dev:
            if c == '1':
                _run_action(canvas, "Démarrage prod", actions.action_start_all, target="prod")
            elif c == '2':
                _run_action(canvas, "Arrêt prod", actions.action_stop_all, target="prod")
            elif c == '3':
                _run_action(canvas, "Démarrage dev", actions.action_start_all, target="dev")
            elif c == '4':
                _run_action(canvas, "Arrêt dev", actions.action_stop_all, target="dev")
            elif c == '5':
                _run_action(canvas, "Mise à jour complète", actions.action_update_all)
            elif c == '6':
                view_coffre(canvas, vault)
            elif c == '0':
                confirm = widgets.confirm(canvas, "Quitter hubctl ?")
                if confirm is True:
                    break
        else:
            if c == '1':
                _run_action(canvas, "Démarrage de toutes les apps", actions.action_start_all, target="all")
            elif c == '2':
                _run_action(canvas, "Arrêt de toutes les apps", actions.action_stop_all, target="all")
            elif c == '3':
                _run_action(canvas, "Mise à jour complète", actions.action_update_all)
            elif c == '4':
                view_coffre(canvas, vault)
            elif c == '0':
                confirm = widgets.confirm(canvas, "Quitter hubctl ?")
                if confirm is True:
                    break

        # Rendu immédiat après action pour réactivité
        canvas.render(_build_key_hints())
        _last_render = time.monotonic()
