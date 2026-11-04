# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

from __future__ import annotations
import re
import shlex
import subprocess
import sys
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


class StreamWriter:
    def __init__(self) -> None:
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if _stream_callback is not None:
                _stream_callback(line)
            else:
                sys.__stdout__.write(line + "\n")
        return len(s)

    def flush(self) -> None:
        if self._buf:
            if _stream_callback is not None:
                _stream_callback(self._buf)
            else:
                sys.__stdout__.write(self._buf)
            self._buf = ""

    def isatty(self) -> bool:
        return False


def cancel_current() -> None:
    # Capturer dans une var locale évite la TOCTOU : le worker peut mettre
    # `_current_proc = None` entre le test `is not None` et `.kill()`, ce qui
    # ferait `None.kill()` → AttributeError (non catché par OSError) → crash UI.
    proc = _current_proc
    if proc is not None:
        try:
            proc.kill()
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


def run_shell(cmd, *, cwd=None, timeout=120, dry_run=False, stream=True, console=None, env=None) -> int:
    if _stream_callback is not None:
        return run_shell_stream(cmd, cwd=cwd, timeout=timeout, dry_run=dry_run, on_line=_stream_callback, env=env)

    con = console or _console
    printable = " ".join(shlex.quote(c) for c in cmd)
    if dry_run:
        con.print(f"[dim]$ {printable}[/dim]  [yellow](dry-run)[/yellow]")
        return 0
    con.print(f"[dim]$ {printable}[/dim]")
    proc = None
    try:
        if stream:
            proc = subprocess.Popen(cmd, cwd=cwd, env=env)
            proc.communicate(timeout=timeout)
            return proc.returncode
        proc = subprocess.run(cmd, cwd=cwd, timeout=timeout, capture_output=True, env=env)
        return proc.returncode
    except subprocess.TimeoutExpired:
        # `communicate(timeout=)` ne tue PAS le fils : il faut le faire explicitement,
        # sinon des processus orphelins (docker/compose) s'accumulent après timeout.
        # (proc n'est assigné que pour la branche Popen ; subprocess.run tue déjà
        # lui-même son fils avant de lever TimeoutExpired.)
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.communicate(timeout=5)  # reaper le zombie
            except Exception:
                pass
        con.print(f"[red]\u2717 Timeout ({timeout}s) sur:[/red] {printable}")
        return 124
    except FileNotFoundError:
        con.print(f"[red]\u2717 Commande introuvable:[/red] {cmd[0]}")
        return 127


def run_shell_stream(cmd, *, cwd=None, timeout=120, dry_run=False, on_line=None, filter_noise=True, env=None) -> int:
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
            text=True, bufsize=1, env=env,
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
