from __future__ import annotations
import contextlib
import shutil
import signal
import sys
import termios
import threading
import tty
from typing import Any

from rich.align import Align
from rich.console import RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.text import Text


def canvas_geometry(cols: int, rows: int, target_cols: int, target_rows: int) -> dict[str, Any]:
    is_too_small = cols < target_cols or rows < target_rows
    origin_col = max(0, (cols - target_cols) // 2)
    origin_row = max(0, (rows - target_rows) // 2)
    return {
        "origin_col": origin_col,
        "origin_row": origin_row,
        "is_too_small": is_too_small,
        "inner_cols": target_cols - 2,
        "inner_rows": target_rows - 2,
    }


def _guard_screen(cols: int, rows: int, target_cols: int, target_rows: int) -> Panel:
    msg = f"Agrandis ton terminal à {target_cols}×{target_rows}\n(actuel : {cols}×{rows})"
    return Panel(Text(msg, style="bold yellow"), title="Fenêtre trop petite")


_BORDER_TOP = "▛" + "▀" * 96 + "▜"
_BORDER_BOT = "▙" + "▄" * 96 + "▟"

_COL_W1 = 23
_COL_W2 = 27
_COL_W3 = 44


def _make_col_sep(w1: int, w2: int, w3: int, left: str, tee: str, right: str) -> str:
    return left + "─" * w1 + tee + "─" * w2 + tee + "─" * w3 + right


class Canvas:
    def __init__(self, *, title: str = "HUBCTL · POULIDOR", target_cols: int = 100, target_rows: int = 30):
        self.title = title
        self.target_cols = target_cols
        self.target_rows = target_rows
        self._live: Live | None = None
        self._raw_fd: int | None = None
        self._old_termios: list[Any] | None = None
        self._resized = False
        self._last_inner: RenderableType | None = None
        self._z3_builder: Any = None
        self._z1_builder: Any = None
        self._z2_builder: Any = None
        self._z4_builder: Any = None
        self._use_layout = False

    @property
    def inner_cols(self) -> int:
        return self.target_cols - 2

    @property
    def inner_rows(self) -> int:
        return self.target_rows - 2

    @property
    def is_too_small(self) -> bool:
        cols, rows = shutil.get_terminal_size()
        return cols < self.target_cols or rows < self.target_rows

    def _on_resize(self, signum: int, frame: object) -> None:
        self._resized = True

    def set_layout(self, z3_builder: Any, z1_builder: Any, z4_builder: Any, *, z2_builder: Any = None) -> None:
        self._z3_builder = z3_builder
        self._z1_builder = z1_builder
        self._z2_builder = z2_builder
        self._z4_builder = z4_builder
        self._use_layout = True

    def open(self) -> None:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError("Canvas requires a TTY")
        self._raw_fd = sys.stdin.fileno()
        self._old_termios = termios.tcgetattr(self._raw_fd)
        tty.setcbreak(self._raw_fd)
        self._live = Live(screen=True, refresh_per_second=30, transient=False)
        self._live.__enter__()
        signal.signal(signal.SIGWINCH, self._on_resize)

    def close(self) -> None:
        if self._live is not None:
            self._live.__exit__(None, None, None)
            self._live = None
        if self._old_termios is not None and self._raw_fd is not None:
            try:
                signal.signal(signal.SIGWINCH, signal.SIG_DFL)
            except (TypeError, ValueError, OSError):
                pass
            termios.tcsetattr(self._raw_fd, termios.TCSADRAIN, self._old_termios)
            self._old_termios = None

    def render(self, inner: RenderableType) -> None:
        if self._live is None:
            return
        self._last_inner = inner
        cols, rows = shutil.get_terminal_size()
        if self.is_too_small:
            content = _guard_screen(cols, rows, self.target_cols, self.target_rows)
        elif self._use_layout:
            content = self._render_layout(inner)
        else:
            panel = Panel(
                inner,
                width=self.target_cols,
                height=self.target_rows,
                border_style="cyan",
                padding=0,
            )
            content = Align.center(panel, vertical="middle")
        self._live.update(content)

    @staticmethod
    def _split_text(t: RenderableType) -> list[Text]:
        if isinstance(t, Text):
            return t.split()
        plain = str(t) if hasattr(t, 'plain') else ""
        return [Text(line) for line in plain.split("\n")]

    def _render_layout(self, z2_menu: RenderableType) -> RenderableType:
        welcome = (self._z3_builder() if callable(self._z3_builder) else None) or Text("")
        resources = (self._z1_builder() if callable(self._z1_builder) else None) or Text("")
        services = (self._z2_builder() if callable(self._z2_builder) else None) or Text("")
        logs = (self._z4_builder() if callable(self._z4_builder) else None) or Text("")

        w1, w2, w3 = _COL_W1, _COL_W2, _COL_W3

        top = Text(_BORDER_TOP + "\n", style="bold magenta")
        bot = Text(_BORDER_BOT, style="bold magenta")
        sep = Text("▌" + "─" * (self.inner_cols - 2) + "▐\n", style="bold bright_black")

        col_sep_top = Text(_make_col_sep(w1, w2, w3, "▌", "┬", "▐") + "\n", style="bold bright_black")
        col_sep_bot = Text(_make_col_sep(w1, w2, w3, "▌", "┴", "▐") + "\n", style="bold bright_black")

        res_texts = self._split_text(resources)
        svc_texts = self._split_text(services)
        menu_texts = self._split_text(z2_menu)

        max_lines = max(len(res_texts), len(svc_texts), len(menu_texts), 1)

        middle = Text()
        for i in range(max_lines):
            l_t = res_texts[i] if i < len(res_texts) else Text("")
            c_t = svc_texts[i] if i < len(svc_texts) else Text("")
            r_t = menu_texts[i] if i < len(menu_texts) else Text("")

            l_plain = l_t.plain
            c_plain = c_t.plain
            r_plain = r_t.plain

            line = Text("│", style="bold bright_black")
            line.append(l_t)
            if len(l_plain) < w1:
                line.append(" " * (w1 - len(l_plain)))
            line.append("│", style="bold bright_black")
            line.append(c_t)
            if len(c_plain) < w2:
                line.append(" " * (w2 - len(c_plain)))
            line.append("│", style="bold bright_black")
            line.append(r_t)
            if len(r_plain) < w3:
                line.append(" " * (w3 - len(r_plain)))
            line.append("│\n", style="bold bright_black")
            middle.append(line)

        logs_texts = self._split_text(logs)

        inner = Text.assemble(top, welcome, Text("\n"), col_sep_top, middle, col_sep_bot)
        for lt in logs_texts:
            inner.append(lt)
            inner.append("\n")
        inner.append(bot)

        panel = Panel(inner, width=self.target_cols, height=self.target_rows, border_style="cyan", padding=0)
        return Align.center(panel, vertical="middle")

    @contextlib.contextmanager
    def paused(self):
        if self._live is not None:
            self._live.__exit__(None, None, None)
            self._live = None
        if self._old_termios is not None and self._raw_fd is not None:
            termios.tcsetattr(self._raw_fd, termios.TCSADRAIN, self._old_termios)
        try:
            yield
        finally:
            if self._raw_fd is not None:
                tty.setcbreak(self._raw_fd)
            self._live = Live(screen=True, refresh_per_second=30, transient=False)
            self._live.__enter__()
            if self._last_inner is not None:
                self.render(self._last_inner)

    def stream_command(self, fn, *, label: str = "") -> tuple[int, bool]:
        from hubctl_cli import keys
        from hubctl_cli import utils

        old_layout = self._use_layout
        self._use_layout = False

        lines_buf: list[str] = []
        done = threading.Event()
        retcode = [-1]
        cancelled = [False]

        def on_line(line: str) -> None:
            lines_buf.append(line)

        utils.set_stream_callback(on_line)
        try:
            def _runner():
                retcode[0] = fn()
                done.set()

            t = threading.Thread(target=_runner, daemon=True)
            t.start()

            fd = self._raw_fd if self._raw_fd is not None else sys.stdin.fileno()
            tail = 30

            while not done.is_set():
                visible = lines_buf[-tail:]
                log_text = Text()
                log_text.append(f"▸ {label}\n", style="bold yellow")
                log_text.append("─" * 60 + "\n", style="dim")
                for line in visible:
                    ll = line.strip()
                    if not ll:
                        continue
                    if any(w in ll.lower() for w in ("✗", "error", "fail", "traceback")):
                        log_text.append(f"  {ll}\n", style="red")
                    elif any(w in ll.lower() for w in ("✓", "success", "ok")):
                        log_text.append(f"  {ll}\n", style="green")
                    elif any(w in ll for w in ("WARN", "warning")):
                        log_text.append(f"  {ll}\n", style="yellow")
                    else:
                        log_text.append(f"  {ll}\n", style="dim")
                log_text.append("\n[Ctrl-C pour annuler]", style="dim")

                self.render(log_text)

                k = keys.read_key(fd, timeout=0.15)
                if isinstance(k, (keys.KeyCtrlC, keys.KeyEsc)):
                    cancelled[0] = True
                    utils.cancel_current()
                    break

            if not done.is_set():
                t.join(timeout=3)
                return retcode[0], True

            visible = lines_buf[-tail:]
            log_text = Text()
            if retcode[0] == 0:
                log_text.append(f"✓ {label}\n", style="bold green")
            else:
                log_text.append(f"✗ {label} (code {retcode[0]})\n", style="bold red")
            log_text.append("─" * 60 + "\n", style="dim")
            for line in visible:
                ll = line.strip()
                if not ll:
                    continue
                log_text.append(f"  {ll}\n", style="dim")
            self.render(log_text)

            return retcode[0], False
        finally:
            utils.set_stream_callback(None)
            self._use_layout = old_layout
