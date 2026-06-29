from __future__ import annotations
import contextlib
import shutil
import signal
import sys
import termios
import tty
from typing import Any

from rich.align import Align
from rich.console import RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
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
        self._header_renderable: RenderableType | None = None
        self._body_renderable: RenderableType | None = None

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
        """Full-frame render (backward compat). Clears header/body state."""
        if self._live is None:
            return
        self._last_inner = inner
        self._header_renderable = None
        self._body_renderable = None
        cols, rows = shutil.get_terminal_size()
        if self.is_too_small:
            content = _guard_screen(cols, rows, self.target_cols, self.target_rows)
        else:
            panel = Panel(
                inner,
                title=self.title,
                width=self.target_cols,
                height=self.target_rows,
                border_style="cyan",
            )
            content = Align.center(panel, vertical="middle")
        self._live.update(content)

    def render_header(self, inner: RenderableType) -> None:
        self._header_renderable = inner
        self._render_composite()

    def render_body(self, inner: RenderableType) -> None:
        self._body_renderable = inner
        self._render_composite()

    def _render_composite(self) -> None:
        if self._live is None:
            return
        cols, rows = shutil.get_terminal_size()
        if self.is_too_small:
            content = _guard_screen(cols, rows, self.target_cols, self.target_rows)
            self._live.update(content)
            return
        header = self._header_renderable or Text("")
        body = self._body_renderable or Text("")
        sep = Text("─" * self.inner_cols, style="dim")
        inner = Table.grid()
        inner.add_row(header)
        inner.add_row(sep)
        inner.add_row(body)
        panel = Panel(
            inner,
            title=self.title,
            width=self.target_cols,
            height=self.target_rows,
            border_style="cyan",
        )
        content = Align.center(panel, vertical="middle")
        self._live.update(content)
        self._last_inner = inner

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


class ViewportHeader:
    """Expose uniquement la zone header aux animations."""
    def __init__(self, canvas: Canvas, cols: int = 98, rows: int = 10):
        self._canvas = canvas
        self.cols = cols
        self.rows = rows

    def render(self, inner: RenderableType) -> None:
        self._canvas.render_header(inner)
