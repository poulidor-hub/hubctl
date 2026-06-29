from __future__ import annotations
import sys
from rich.text import Text
from hubctl_cli import keys
from hubctl_cli.canvas import Canvas

ESC = object()


def select(canvas, message: str, choices: list[str]):
    selected_idx = 0
    scroll = 0
    fd = sys.stdin.fileno()
    max_vis = max(1, canvas.inner_rows - 3)

    while True:
        canvas.render(_build_select(message, choices, selected_idx, scroll, max_vis))
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
            return ESC
        elif isinstance(key, keys.KeyCtrlC):
            raise KeyboardInterrupt()
        if selected_idx < scroll:
            scroll = selected_idx
        elif selected_idx >= scroll + max_vis:
            scroll = selected_idx - max_vis + 1


def confirm(canvas, message: str, *, default: bool = False):
    fd = sys.stdin.fileno()
    while True:
        suffix = "[O/n]" if default else "[o/N]"
        canvas.render(Text(f"{message} {suffix}"))
        key = keys.read_key(fd, timeout=0.15)
        if key is None:
            continue
        if isinstance(key, keys.KeyEnter):
            return default
        if isinstance(key, keys.KeyEsc):
            return ESC
        if isinstance(key, keys.KeyCtrlC):
            raise KeyboardInterrupt()
        if isinstance(key, keys.KeyChar):
            if key.char.lower() in ("o", "y"):
                return True
            if key.char.lower() == "n":
                return False


def text(canvas, message: str):
    return _text_input(canvas, message, mask=None)


def password(canvas, message: str):
    return _text_input(canvas, message, mask="•")


def _text_input(canvas, message: str, mask: str | None = None):
    buf: list[str] = []
    fd = sys.stdin.fileno()
    while True:
        disp = "".join(mask if mask else c for c in buf)
        canvas.render(Text(f"{message}\n> {disp}_"))
        key = keys.read_key(fd, timeout=0.15)
        if key is None:
            continue
        if isinstance(key, keys.KeyEnter):
            return "".join(buf)
        if isinstance(key, keys.KeyEsc):
            return ESC
        if isinstance(key, keys.KeyCtrlC):
            raise KeyboardInterrupt()
        if isinstance(key, keys.KeyBackspace):
            if buf:
                buf.pop()
        elif isinstance(key, keys.KeyChar):
            buf.append(key.char)


def _build_select(message, choices, selected, scroll, max_vis):
    lines = [Text(message, style="bold"), Text("")]
    visible = choices[scroll:scroll + max_vis]
    for i, choice in enumerate(visible):
        idx = scroll + i
        if idx == selected:
            lines.append(Text(f" ▸ {choice}", style="reverse"))
        else:
            lines.append(Text(f"   {choice}"))
    if scroll > 0:
        lines.append(Text("   ↑ plus…", style="dim"))
    if scroll + max_vis < len(choices):
        lines.append(Text("   ↓ plus…", style="dim"))
    return Text("\n").join(lines)
