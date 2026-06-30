"""Animation HUB en ASCII art (240 frames ~24 fps, ~10 s).

Les frames sont précalculées : grilles de caractères + runs de couleur.
Rejouées ici avec Rich (Text.stylize), sans dépendance Node/Bun.
Transparence préservée : seules les cellules avec couleur sont stylisées.
"""
import gzip
import json
import subprocess
import time
from pathlib import Path

from rich.text import Text

NAME = "hub-ascii"
DESCRIPTION = "Animation HUB (ASCII art, ~10 s, couleur)"

_DATA_FILE = Path(__file__).parent / "hub_ascii.frames.gz"
_TSX_FILE = Path(__file__).parent / "introHUB.tsx"
_cache = None


def _check_regenerate():
    """Re-extract frames if TSX is newer than cached .gz, or .gz is missing."""
    if not _TSX_FILE.exists():
        return
    if _DATA_FILE.exists() and _TSX_FILE.stat().st_mtime <= _DATA_FILE.stat().st_mtime:
        return
    extract_script = Path(__file__).parent / "extract_frames.py"
    if extract_script.exists():
        subprocess.run(["python3", str(extract_script)], capture_output=True, timeout=30)


def _load():
    global _cache
    if _cache is None:
        _check_regenerate()
        with gzip.open(_DATA_FILE, "rb") as g:
            _cache = json.loads(g.read().decode("utf-8"))
    return _cache


def _fallback_nap(secs):
    time.sleep(secs)
    return False


def _render_frame(chars: list[str], colors: list[list]) -> Text:
    """Build a Rich Text from a raw character grid + color runs."""
    text = Text()
    for r, row_chars in enumerate(chars):
        if r > 0:
            text.append("\n")
        text.append(row_chars)
    row_len = len(chars[0]) + 1 if chars else 0
    for run in colors:
        r, start, end, fg_hex, bg_hex = run
        style_parts = []
        if fg_hex:
            style_parts.append(fg_hex)
        if bg_hex:
            style_parts.append(f"on {bg_hex}")
        if style_parts:
            style = " ".join(style_parts)
            offset = r * row_len + start
            text.stylize(style, offset, offset + (end - start))
    return text


def play(viewport, state):
    """viewport is a Canvas instance providing .render(renderable)."""
    nap = state.get("sleep", _fallback_nap)
    data = _load()
    all_chars = data["chars"]
    all_colors = data["colors"]
    durations = data["durations"]
    for chars, colors, dur in zip(all_chars, all_colors, durations):
        frame = _render_frame(chars, colors)
        viewport.render(frame)
        if nap(dur / 1000.0):
            return
