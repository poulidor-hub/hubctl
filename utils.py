from __future__ import annotations
import shlex
import subprocess
from rich.console import Console

_console = Console()


def run_shell(cmd, *, cwd=None, timeout=120, dry_run=False, stream=True, console=None) -> int:
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
        con.print(f"[red]✗ Timeout ({timeout}s) sur:[/red] {printable}")
        return 124
    except FileNotFoundError:
        con.print(f"[red]✗ Commande introuvable:[/red] {cmd[0]}")
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
