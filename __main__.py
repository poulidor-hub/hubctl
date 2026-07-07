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
from hubctl_cli import hub_state, keys
from hubctl_cli.park_export import export_park
from hubctl_cli.park_restore import restore_park


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hubctl", description="Menu CLI de pilotage du hub POULIDOR.")
    p.add_argument("--no-anim", action="store_true", help="Désactiver l'intro animée.")
    p.add_argument("--dry-run", action="store_true", help="Imprimer les commandes sans les exécuter.")
    p.add_argument("--anim", default=None, help="Nom de l'animation d'intro (ou 'random').")
    p.add_argument("--init", action="store_true", help="Cloner tous les dépôts listés dans manifest.json.")

    sub = p.add_subparsers(dest="command", help="Commandes")

    # park-export
    export_parser = sub.add_parser("park-export", help="Exporter tout le parc dans une archive tar.gz")
    export_parser.add_argument("--app", default=None, help="Exporter une seule app (slug)")
    export_parser.add_argument("--no-encrypt", action="store_true",
                               help="Produire une archive tar.gz non chiffrée (legacy, secrets en clair)")
    export_parser.add_argument("--passphrase-stdin", action="store_true",
                               help="Lire la passphrase d'archive depuis stdin (CI/DR)")

    # park-restore
    restore_parser = sub.add_parser("park-restore", help="Restaurer le parc depuis une archive (Hub vierge uniquement)")
    restore_parser.add_argument("archive", help="Chemin vers l'archive park-*.tar.gz")
    restore_parser.add_argument("--passphrase-stdin", action="store_true",
                                help="Lire la passphrase d'archive depuis stdin (CI/DR)")

    # debug (agent Granite/Ollama)
    debug_parser = sub.add_parser("debug", help="Agent de debug Granite/Ollama")
    debug_parser.add_argument("app", nargs="?", default=None, help="Application cible")
    debug_parser.add_argument("--rw", action="store_true", help="Mode semi-auto")
    debug_parser.add_argument("--setup", action="store_true", help="Installer Ollama et Granite")
    debug_parser.add_argument("--model", choices=["8b", "2b"], default=None, help="Forcer le modele")
    debug_parser.add_argument("--off", action="store_true", help="Desactiver l'agent")
    debug_parser.add_argument("--status", action="store_true", help="Verifier la disponibilite")

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

    # Commandes non-interactives (pas de TUI)
    if args.command == "park-export":
        try:
            passphrase = None
            if not args.no_encrypt:
                import sys
                if args.passphrase_stdin:
                    passphrase = sys.stdin.readline().rstrip("\n")
                    if not passphrase:
                        print("❌ Passphrase vide reçue sur stdin. Refus de produire "
                              "une archive 'chiffrée' par une passphrase vide.")
                        return 1
                elif sys.stdin.isatty():
                    import getpass
                    passphrase = getpass.getpass("🔐 Passphrase d'archive : ")
                    confirm = getpass.getpass("🔐 Confirmer : ")
                    if passphrase != confirm:
                        print("❌ Les passphrases ne correspondent pas.")
                        return 1
                else:
                    print("❌ Aucune passphrase fournie. Utilisez --passphrase-stdin "
                          "ou --no-encrypt pour une archive en clair.")
                    return 1
            export_park(app_slug=args.app, encrypt=not args.no_encrypt, passphrase=passphrase)
            return 0
        except Exception as e:
            print(f"❌ park-export a échoué : {e}")
            return 1
    if args.command == "park-restore":
        passphrase = None
        if args.passphrase_stdin:
            import sys
            passphrase = sys.stdin.readline().rstrip("\n")
        return 0 if restore_park(args.archive, passphrase=passphrase) else 1
    if args.command == "debug":
        from hubctl_cli.debug_agent import run_debug
        return run_debug(args)

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
        menu._require_vault(canvas, vault_mgr)
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
