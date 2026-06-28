from __future__ import annotations
import contextlib
import importlib
import os
import pkgutil
import select
import sys
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import termios
    import tty
    _HAS_TTY = True
except ImportError:  # plateformes non-Unix
    _HAS_TTY = False

from rich.progress import BarColumn, Progress, TextColumn

from hubctl_cli import animations as _anim_pkg
from hubctl_cli import hub_state


@dataclass
class Animation:
    name: str
    description: str
    play: object


def discover_animations(anim_dir: Path | None = None) -> list[Animation]:
    found: list[Animation] = []
    for mod in pkgutil.iter_modules(_anim_pkg.__path__):
        if mod.name.startswith("_"):
            continue
        module = importlib.import_module(f"hubctl_cli.animations.{mod.name}")
        if hasattr(module, "NAME") and callable(getattr(module, "play", None)):
            found.append(Animation(
                name=module.NAME,
                description=getattr(module, "DESCRIPTION", ""),
                play=module.play,
            ))
    found.sort(key=lambda a: a.name)
    return found


def select_animation(animations, name=None) -> Animation:
    if not animations:
        raise RuntimeError("Aucune animation trouvée dans hubctl_cli/animations/")
    if name:
        for a in animations:
            if a.name == name:
                return a
    return animations[0]


_STEPS = [
    ("Connexion à Docker…", lambda: ("docker", hub_state.check_docker())),
    ("Lecture des services du hub…", lambda: ("services", hub_state.get_hub_services())),
    ("Inventaire des apps…", lambda: ("apps", hub_state.get_apps())),
    ("Analyse de l'espace disque…", lambda: ("disk", hub_state.get_disk_usage())),
    ("Version du hub…", lambda: ("hub_version", hub_state.get_hub_version())),
    ("Finalisation…", lambda: ("_done", True)),
]


def preload_with_bar(console, *, animate=True) -> dict:
    state: dict = {}
    min_step = 0.18 if animate else 0.0
    with Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=40),
        TextColumn("{task.percentage:>3.0f}%"),
        console=console, transient=True,
    ) as progress:
        task = progress.add_task("Chargement…", total=len(_STEPS))
        for label, fn in _STEPS:
            progress.update(task, description=label)
            t0 = time.monotonic()
            key, value = fn()
            if key != "_done":
                state[key] = value
            elapsed = time.monotonic() - t0
            if animate and elapsed < min_step:
                time.sleep(min_step - elapsed)
            progress.advance(task)
    return state


@contextlib.contextmanager
def _interruptible_input():
    """Fournit nap(secs)->bool : dort 'secs' mais renvoie True si une touche
    a été pressée pendant ce laps (demande de skip de l'intro)."""
    if not (_HAS_TTY and sys.stdin.isatty()):
        def nap(secs):
            time.sleep(secs)
            return False
        yield nap
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)

        def nap(secs):
            ready, _, _ = select.select([sys.stdin], [], [], secs)
            if ready:
                try:
                    os.read(fd, 64)
                except OSError:
                    pass
                return True
            return False

        yield nap
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def play_intro(console, *, anim: Animation, state: dict, skip=False) -> None:
    if skip:
        return
    with _interruptible_input() as nap:
        local_state = dict(state)
        local_state["sleep"] = nap
        anim.play(console, local_state)
