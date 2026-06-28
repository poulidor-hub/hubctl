from __future__ import annotations
import argparse
import sys

from rich.console import Console

from hubctl_cli import animation, menu
from hubctl_cli.coffre import Vault
from hubctl_cli import hub_state


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hubctl", description="Menu CLI de pilotage du hub POULIDOR.")
    p.add_argument("--no-anim", action="store_true", help="Désactiver l'intro animée.")
    p.add_argument("--dry-run", action="store_true", help="Imprimer les commandes sans les exécuter.")
    p.add_argument("--anim", default=None, help="Nom de l'animation d'intro (ou 'random').")
    return p


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _run(args, console) -> int:
    interactive = _interactive()
    skip = args.no_anim or not interactive

    if interactive:
        console.clear()

    state = animation.preload_with_bar(console, animate=not skip)

    if not skip:
        anims = animation.discover_animations()
        chosen = animation.select_animation(anims, name=args.anim)
        animation.play_intro(console, anim=chosen, state=state, skip=skip)

    if not interactive:
        menu._status_banner(console, state)
        console.print("[yellow]Mode non interactif : menu indisponible (un terminal TTY est requis).[/yellow]")
        return 0

    vault_path = hub_state.PARC_ROOT / "hubctl_cli" / "vault" / "parc.vault.enc"
    vault = Vault(vault_path)
    menu.main_loop(console, state, vault, dry_run=args.dry_run)
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    console = Console()
    try:
        return _run(args, console)
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Interrompu.[/dim]")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
