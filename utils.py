from __future__ import annotations
import re
import shlex
import subprocess
import threading
from queue import Queue
from typing import Callable

from rich.console import Console

_console = Console()
_stream_callback: Callable[[str], None] | None = None
_current_proc: subprocess.Popen | None = None


def set_stream_callback(cb: Callable[[str], None] | None) -> None:
    global _stream_callback
    _stream_callback = cb


def cancel_current() -> None:
    global _current_proc
    if _current_proc is not None:
        try:
            _current_proc.kill()
        except OSError:
            pass


_NOISE_PATTERNS = [
    re.compile(r"^\s*$"),
    re.compile(r"^WARN\["),
    re.compile(r"^#0 building"),
    re.compile(r"^#\d+ \[internal\]"),
    re.compile(r"^#\d+ transferring"),
    re.compile(r"^#\d+ exporting"),
    re.compile(r"^#\d+ resolving"),
    re.compile(r"^#[0-9]+ (DONE|CACHED)"),
    re.compile(r"^\s*\$ "),  # dry-run echo line
]


def is_noise(line: str) -> bool:
    for pat in _NOISE_PATTERNS:
        if pat.match(line):
            return True
    return False


def run_shell(cmd, *, cwd=None, timeout=120, dry_run=False, stream=True, console=None) -> int:
    if _stream_callback is not None:
        return run_shell_stream(cmd, cwd=cwd, timeout=timeout, dry_run=dry_run, on_line=_stream_callback)

    con = console or _console
    printable = " ".join(shlex.quote(c) for c in cmd)
    if dry_run:
        con.print(f"[dim]$ {printable}[/dim]  [yellow](dry-run)[/yellow]")
        return 0
    con.print(f"[dim]$ {printable}[/dim]")
    try:
        if stream:
            proc = subprocess.Popen(cmd, cwd=cwd)
            proc.communicate(timeout=timeout)
            return proc.returncode
        proc = subprocess.run(cmd, cwd=cwd, timeout=timeout, capture_output=True)
        return proc.returncode
    except subprocess.TimeoutExpired:
        con.print(f"[red]\u2717 Timeout ({timeout}s) sur:[/red] {printable}")
        return 124
    except FileNotFoundError:
        con.print(f"[red]\u2717 Commande introuvable:[/red] {cmd[0]}")
        return 127


def run_shell_stream(cmd, *, cwd=None, timeout=120, dry_run=False, on_line=None, filter_noise=True) -> int:
    printable = " ".join(shlex.quote(c) for c in cmd)
    if dry_run:
        if on_line:
            on_line(f"[dry-run] $ {printable}")
        return 0

    if on_line:
        on_line(f"$ {printable}")

    try:
        global _current_proc
        proc = subprocess.Popen(
            cmd, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        _current_proc = proc

        def _read_loop():
            for line in proc.stdout:
                stripped = line.rstrip("\n\r")
                if on_line:
                    if not filter_noise or not is_noise(stripped):
                        on_line(stripped)

        reader = threading.Thread(target=_read_loop, daemon=True)
        reader.start()

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            _current_proc = None
            if on_line:
                on_line(f"[TIMEOUT] ({timeout}s)")
            return 124
        reader.join(timeout=2)
        _current_proc = None
        return proc.returncode
    except FileNotFoundError:
        if on_line:
            on_line(f"[ERREUR] Commande introuvable : {cmd[0]}")
        return 127


def confirm(message, *, default=False, console=None) -> bool:
    con = console or _console
    suffix = "[O/n]" if default else "[o/N]"
    con.print(f"[bold]{message}[/bold] {suffix} ", end="")
    answer = input().strip().lower()
    if not answer:
        return default
    return answer in ("o", "oui", "y", "yes")


def confirm_phrase(expected, *, console=None) -> bool:
    con = console or _console
    con.print(f"[yellow]Pour confirmer, retape exactement[/yellow] [bold]{expected}[/bold] : ", end="")
    return input().strip() == expected
