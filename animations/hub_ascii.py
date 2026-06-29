"""Animation HUB en ASCII art (240 frames ~24 fps, ~10 s).

Les frames sont précalculées et stockées compressées dans `hub_ascii.frames.gz`
(extraites de l'animation @opentui d'origine). Rejouées ici avec Rich, sans
dépendance Node/Bun. Les frames contiennent du markup Rich pour le rendu couleur
avec transparence.
"""
import gzip
import json
import time
from pathlib import Path

from rich.text import Text

NAME = "hub-ascii"
DESCRIPTION = "Animation HUB (ASCII art, ~10 s, couleur)"

_DATA_FILE = Path(__file__).parent / "hub_ascii.frames.gz"
_cache = None


def _load():
    global _cache
    if _cache is None:
        with gzip.open(_DATA_FILE, "rb") as g:
            _cache = json.loads(g.read().decode("utf-8"))
    return _cache


def _fallback_nap(secs):
    time.sleep(secs)
    return False


def play(viewport, state):
    """viewport is a Canvas instance providing .render(renderable)."""
    nap = state.get("sleep", _fallback_nap)
    data = _load()
    frames = data["frames"]
    durations = data["durations"]
    for frame, dur in zip(frames, durations):
        inner = Text.from_markup(frame)
        viewport.render(inner)
        if nap(dur / 1000.0):
            return
