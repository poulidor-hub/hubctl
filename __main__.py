from __future__ import annotations
import argparse
import sys
import threading

from rich.console import Console
from rich.text import Text

from hubctl_cli import actions, animation, menu
from hubctl_cli.canvas import Canvas
from hubctl_cli.coffre import Vault
from hubctl_cli.vault_manager import VaultManager
from hubctl_cli import hub_state
from hubctl_cli import keys


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hubctl", description="Menu CLI de pilotage du hub POULIDOR.")
    p.add_argument("--no-anim", action="store_true", help="Désactiver l'intro animée.")
    p.add_argument("--dry-run", action="store_true", help="Imprimer les commandes sans les exécuter.")
    p.add_argument("--anim", default=None, help="Nom de l'animation d'intro (ou 'random').")
    p.add_argument("--init", action="store_true", help="Cloner tous les dépôts listés dans manifest.json.")
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

    if args.init:
        console = Console()
        actions.action_init(console, dry_run=args.dry_run)
        return 0

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

        from hubctl_cli.actions import cleanup_orphan_tests
        cleanup_orphan_tests()

        while canvas.is_too_small:
            canvas.render(None)
            if _poll_quit():
                return 0

        try:
            state: dict = {"_loading": True}
            preload_done = threading.Event()

            def _do_preload():
                animation.preload_with_bar(canvas, animate=not skip_anim, state_dict=state)
                state["_loading"] = False
                preload_done.set()

            if not skip_anim:
                t = threading.Thread(target=_do_preload, daemon=True)
                t.start()

            try:
                if not skip_anim:
                    anims = animation.discover_animations()
                    chosen = animation.select_animation(anims, name=args.anim)
                    animation.play_intro(canvas, anim=chosen, state=state)
                else:
                    animation.preload_with_bar(canvas, animate=False, state_dict=state)
            except KeyboardInterrupt:
                pass
            finally:
                if not skip_anim:
                    preload_done.wait(timeout=30)
        except KeyboardInterrupt:
            pass

        vault_path = hub_state.PARC_ROOT / "hubctl_cli" / "vault" / "parc.vault.enc"
        vault = Vault(vault_path)
        vault_mgr = VaultManager(vault)
        if not hub_state.HUB_DIR.exists():
            menu._offer_init_fresh(canvas, dry_run=args.dry_run)
        menu.install_layout(canvas, state)
        mode = menu._choose_mode(canvas, vault_mgr)
        hub_state.set_mode(mode)
        if mode == "dev" and hub_state.APPS_DIR.exists():
            menu._offer_snapshot(canvas, vault_mgr, dry_run=args.dry_run)
        menu.main_loop(canvas, state, vault_mgr, dry_run=args.dry_run)
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
