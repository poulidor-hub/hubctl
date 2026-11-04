# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

from __future__ import annotations
import time

from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn as ProgressTextColumn
from rich.text import Text

from hubctl_cli import hub_state


_STEPS = [
    ("Connexion à Docker…", lambda: ("docker", hub_state.check_docker())),
    ("Lecture des services du hub…", lambda: ("services", hub_state.get_hub_services())),
    ("Inventaire des apps…", lambda: ("apps", hub_state.get_apps())),
    ("Analyse de l'espace disque…", lambda: ("disk", hub_state.get_disk_usage())),
    ("Version du hub…", lambda: ("hub_version", hub_state.get_hub_version())),
    ("Finalisation…", lambda: ("_done", True)),
]


def preload_with_bar(canvas, *, animate: bool = True, state_dict: dict | None = None) -> dict:
    """Preload hub state with a progress indicator.

    Accepts a Canvas (framed, interactive) or a Console (non-interactive fallback).
    If state_dict is provided, populates it in-place (for threaded use).
    """
    state: dict = state_dict if state_dict is not None else {}
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
        canvas.render(Text("\n").join(lines))
        elapsed = time.monotonic() - t0
        if animate and elapsed < min_step:
            time.sleep(min_step - elapsed)
    return state

