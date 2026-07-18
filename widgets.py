from __future__ import annotations
import sys
from rich.text import Text
from hubctl_cli import keys
from hubctl_cli.canvas import Canvas

ESC = object()

GLYPH_ENTER = "⏎"
GLYPH_ESC = "⎋"
GLYPH_UPDN = "↑↓"
GLYPH_PAGE = "␣"


def key_hints(pairs: list[tuple[str, str]]) -> Text:
    return Text(" · ".join(f"{glyph} {label}" for glyph, label in pairs), style="dim")


def select(canvas, message: str, choices: list[str], *, disabled: set[str] | None = None):
    fd = sys.stdin.fileno()
    disabled = disabled or set()
    per_page = 9
    n = len(choices)
    pages = max(1, (n + per_page - 1) // per_page)

    def enabled(i: int) -> bool:
        item = choices[i]
        if isinstance(item, Text):
            return item.plain not in disabled
        return item not in disabled

    def first_enabled() -> int:
        for i in range(n):
            if enabled(i):
                return i
        return 0

    def step(idx: int, direction: int) -> int:
        for _ in range(n):
            idx = (idx + direction) % n
            if enabled(idx):
                return idx
        return idx

    selected = first_enabled()

    while True:
        page = selected // per_page
        start = page * per_page
        visible = choices[start:start + per_page]
        canvas.render(_build_select(message, visible, selected - start, page, pages, disabled))
        key = keys.read_key(fd, timeout=0.15)
        if key is None:
            continue
        if isinstance(key, keys.KeyUp):
            selected = step(selected, -1)
        elif isinstance(key, keys.KeyDown):
            selected = step(selected, 1)
        elif isinstance(key, keys.KeyEnter):
            if enabled(selected):
                return choices[selected]
        elif isinstance(key, keys.KeyEsc):
            return ESC
        elif isinstance(key, keys.KeyCtrlC):
            raise KeyboardInterrupt()
        elif isinstance(key, keys.KeyChar):
            ch = key.char
            if ch == "0":
                return ESC
            if ch in ("+", " "):
                selected = min(((page + 1) % pages) * per_page, n - 1)
            elif ch == "-":
                selected = min(((page - 1) % pages) * per_page, n - 1)
            elif ch.isdigit():
                pos = int(ch) - 1
                if 0 <= pos < len(visible) and enabled(start + pos):
                    selected = start + pos


def multiselect(canvas, message: str, choices, *, disabled=None):
    fd = sys.stdin.fileno()
    selected_idx = 0
    toggled: set[int] = set()

    def _is_selectable(i: int, label: str) -> bool:
        if label.startswith("──") or label.startswith("ℹ") or label == "← Retour":
            return False
        if disabled and (i in disabled or label in disabled):
            return False
        return True

    while True:
        rendered = _build_multiselect(message, choices, selected_idx, toggled)
        canvas.render(rendered)
        key = keys.read_key(fd, timeout=0.15)
        if key is None:
            continue
        if isinstance(key, keys.KeyEnter):
            return [choices[i] for i in sorted(toggled) if _is_selectable(i, choices[i])]
        if isinstance(key, keys.KeyEsc):
            return ESC
        if isinstance(key, keys.KeyCtrlC):
            raise KeyboardInterrupt()
        if isinstance(key, (keys.KeyUp, keys.KeyDown)):
            step = -1 if isinstance(key, keys.KeyUp) else 1
            for _ in range(len(choices)):
                selected_idx = (selected_idx + step) % len(choices)
                if _is_selectable(selected_idx, choices[selected_idx]):
                    break
        elif isinstance(key, keys.KeyChar):
            if key.char == " " and _is_selectable(selected_idx, choices[selected_idx]):
                if selected_idx in toggled:
                    toggled.discard(selected_idx)
                else:
                    toggled.add(selected_idx)


def _build_multiselect(message, choices, selected_idx, toggled):
    lines = [Text(message, style="bold"), Text()]
    for i, label in enumerate(choices):
        # Déterminer si c'est un séparateur (texte brut ou Text)
        plain = label.plain if isinstance(label, Text) else str(label)
        is_sep = plain.startswith("──") or plain.startswith("ℹ") or plain == "← Retour"
        if is_sep:
            if isinstance(label, Text):
                lines.append(Text.assemble(label, style="dim"))
            else:
                lines.append(Text(label, style="dim"))
            continue
        marker = "[x]" if i in toggled else "[ ]"
        cursor = ">" if i == selected_idx else " "
        rendered = label if isinstance(label, Text) else Text(str(label))
        line = Text.assemble(
            Text(f"{cursor} {marker} "),
            rendered,
        )
        if i == selected_idx:
            line.stylize("bold cyan")
        lines.append(line)
    lines.append(Text())
    lines.append(Text("↑↓ naviguer · Espace sélectionner · Entrée valider · Esc annuler", style="dim"))
    return Text("\n").join(lines)


def confirm(canvas, message: str, *, default: bool = False):
    fd = sys.stdin.fileno()
    choice = default
    while True:
        canvas.render(_build_confirm(message, choice))
        key = keys.read_key(fd, timeout=0.15)
        if key is None:
            continue
        if isinstance(key, keys.KeyEnter):
            return choice
        if isinstance(key, keys.KeyEsc):
            return ESC
        if isinstance(key, keys.KeyCtrlC):
            raise KeyboardInterrupt()
        if isinstance(key, (keys.KeyUp, keys.KeyDown)):
            choice = not choice
        elif isinstance(key, keys.KeyChar):
            c = key.char.lower()
            if c in ("1", "o", "y"):
                choice = True
            elif c in ("2", "n"):
                choice = False


def confirm_by_typing(canvas, message: str, expected: str) -> bool:
    typed = _text_input(canvas, message, mask=None)
    if typed is ESC:
        return False
    return typed == expected


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


def _build_select(message, visible, sel_idx, page, pages, disabled=None):
    disabled = disabled or set()
    lines = [Text(message, style="bold")]
    if pages > 1:
        lines.append(Text(f"page {page + 1}/{pages}", style="dim"))
    lines.append(Text(""))
    for i, choice in enumerate(visible):
        num = i + 1
        # Si le choix est déjà un Text (stylé), on le réutilise tel quel
        if isinstance(choice, Text):
            rendered_choice = choice
        else:
            rendered_choice = Text(str(choice))
        prefix = Text(f"   {num}. ")
        is_disabled = (choice.plain if isinstance(choice, Text) else choice) in disabled
        if is_disabled:
            prefix = Text(f"   {num}. ", style="dim")
            rendered_choice.stylize("dim")
            lines.append(Text.assemble(prefix, rendered_choice))
        elif i == sel_idx:
            prefix = Text(f" ▸ {num}. ")
            lines.append(Text.assemble(prefix, rendered_choice, style="reverse"))
        else:
            lines.append(Text.assemble(prefix, rendered_choice))
    lines.append(Text(""))
    hints = [(GLYPH_UPDN, "naviguer"), (GLYPH_ENTER, "valider"), (GLYPH_ESC, "retour")]
    if pages > 1:
        hints.append((GLYPH_PAGE, "page"))
    lines.append(key_hints(hints))
    return Text("\n").join(lines)


def _build_confirm(message, choice):
    oui = Text(" ▸ 1. Oui", style="reverse") if choice else Text("   1. Oui")
    non = Text("   2. Non") if choice else Text(" ▸ 2. Non", style="reverse")
    return Text("\n").join([
        Text(message, style="bold"),
        Text(""),
        oui,
        non,
        Text(""),
        key_hints([(GLYPH_UPDN, "naviguer"), (GLYPH_ENTER, "valider"), (GLYPH_ESC, "retour")]),
    ])
