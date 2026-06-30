from __future__ import annotations
import argparse
import sys

from rich.console import Console
from rich.text import Text

from hubctl_cli import animation, menu
from hubctl_cli.canvas import Canvas
from hubctl_cli.coffre import Vault
from hubctl_cli import hub_state
from hubctl_cli import keys


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hubctl", description="Menu CLI de pilotage du hub POULIDOR.")
    p.add_argument("--no-anim", action="store_true", help="Désactiver l'intro animée.")
    p.add_argument("--dry-run", action="store_true", help="Imprimer les commandes sans les exécuter.")
    p.add_argument("--anim", default=None, help="Nom de l'animation d'intro (ou 'random').")
    return p


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _poll_quit() -> bool:
    k = keys.read_key(sys.stdin.fileno(), timeout=0.5)
    if k is None:
        return False
    if isinstance(k, (keys.KeyEsc, keys.KeyCtrlC)):
        return True
    return False


def _run(args) -> int:
    interactive = _interactive()
    skip_anim = args.no_anim or not interactive

    state: dict = {}
    canvas = Canvas(title="HUBCTL · POULIDOR", target_cols=100, target_rows=30)

    if not interactive:
        console = Console()
        state = animation.preload_with_bar(console, animate=False)
        console.print(menu._status_banner(state))
        console.print("[yellow]Mode non interactif : menu indisponible (un terminal TTY est requis).[/yellow]")
        return 0

    try:
        canvas.open()

        while canvas.is_too_small:
            canvas.render(None)
            if _poll_quit():
                return 0

        try:
            canvas.render_body(Text("Chargement…", style="dim"))
            state = animation.preload_with_bar(canvas, animate=not skip_anim)
            if not skip_anim:
                try:
                    anims = animation.discover_animations()
                    chosen = animation.select_animation(anims, name=args.anim)
                    animation.play_intro(canvas, anim=chosen, state=state)
                except KeyboardInterrupt:
                    pass
        except KeyboardInterrupt:
            pass  # Ctrl-C pendant preload → on passe au menu

        vault_path = hub_state.PARC_ROOT / "hubctl_cli" / "vault" / "parc.vault.enc"
        vault = Vault(vault_path)
        menu.main_loop(canvas, state, vault, dry_run=args.dry_run)
        return 0

    except (KeyboardInterrupt, EOFError):
        return 130

    finally:
        canvas.close()


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
