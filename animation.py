from __future__ import annotations
import importlib
import pkgutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn as ProgressTextColumn
from rich.text import Text

from hubctl_cli import animations as _anim_pkg
from hubctl_cli import hub_state
from hubctl_cli import keys


@dataclass
class Animation:
    name: str
    description: str
    play: object


def discover_animations() -> list[Animation]:
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


DEFAULT_NAME = "hub-ascii"


def select_animation(animations: list[Animation], name: str | None = None) -> Animation:
    if not animations:
        raise RuntimeError("Aucune animation trouvée dans hubctl_cli/animations/")
    if name == "random":
        import random
        return random.choice(animations)
    if name:
        for a in animations:
            if a.name == name:
                return a
    for a in animations:
        if a.name == DEFAULT_NAME:
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


def preload_with_bar(canvas, *, animate: bool = True) -> dict:
    """Preload hub state with a progress indicator.

    Accepts a Canvas (framed, interactive) or a Console (non-interactive fallback).
    """
    state: dict = {}
    min_step = 0.18 if animate else 0.0

    if isinstance(canvas, Console):
        # Non-interactive: use Rich Progress bar on normal console
        with Progress(
            ProgressTextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=40),
            ProgressTextColumn("{task.percentage:>3.0f}%"),
            console=canvas, transient=True,
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

    # Canvas path: framed progress rendering
    total = len(_STEPS)
    for i, (label, fn) in enumerate(_STEPS):
        t0 = time.monotonic()
        key, value = fn()
        if key != "_done":
            state[key] = value
        pct = (i + 1) / total
        bar_w = min(40, canvas.inner_cols - 10)
        filled = int(bar_w * pct)
        bar = "█" * filled + "░" * (bar_w - filled)
        lines = [
            Text(f"{label}", style="bold blue"),
            Text(f"[{bar}] {pct * 100:.0f}%"),
        ]
        canvas.render_header(Text("\n").join(lines))
        elapsed = time.monotonic() - t0
        if animate and elapsed < min_step:
            time.sleep(min_step - elapsed)
    return state


def play_intro(canvas, *, anim: Animation, state: dict) -> None:
    """Play animation inside the canvas header viewport."""
    from hubctl_cli.canvas import ViewportHeader
    fd = sys.stdin.fileno()

    def nap(secs: float) -> bool:
        k = keys.read_key(fd, timeout=secs)
        return k is not None

    local_state = dict(state)
    local_state["sleep"] = nap
    vp = ViewportHeader(canvas)
    anim.play(vp, local_state)
