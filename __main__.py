from __future__ import annotations
import argparse
import sys
import threading

from rich.console import Console
from rich.text import Text

from hubctl_cli import actions, animation, menu
from hubctl_cli.actions import (
    action_work, action_ship, action_update, action_start_stop, action_status,
    action_logs, action_restart, action_health, action_db_shell,
)
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
    p.add_argument("--dev", action="store_true", help="Forcer le mode DEV")
    p.add_argument("--prod", action="store_true", help="Forcer le mode PROD")

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

    # ── Sous-commandes CLI (non-interactives) ──
    work_parser = sub.add_parser("work", help="Mettre une app en atelier (dev)")
    work_parser.add_argument("slug", help="Slug de l'app dans APPS/")

    ship_parser = sub.add_parser("ship", help="Ramener une app en production")
    ship_parser.add_argument("slug", help="Slug de l'app (sans suffixe -ws)")

    update_cli_parser = sub.add_parser("update", help="Mettre à jour le Hub ou une app")
    update_cli_parser.add_argument("target", help="'hub' ou le slug de l'app")

    start_parser = sub.add_parser("start", help="Démarrer le Hub ou une app")
    start_parser.add_argument("target", help="'hub' ou le slug de l'app")

    stop_parser = sub.add_parser("stop", help="Arrêter le Hub ou une app")
    stop_parser.add_argument("target", help="'hub' ou le slug de l'app")

    sub.add_parser("status", help="Vue d'ensemble du parc (Hub + apps + mode)")

    logs_parser = sub.add_parser("logs", help="Logs d'un service (hub ou app)")
    logs_parser.add_argument("target", help="'hub' ou le slug de l'app")
    mx = logs_parser.add_mutually_exclusive_group()
    mx.add_argument("--api", action="store_true", help="Logs de l'API uniquement")
    mx.add_argument("--db", action="store_true", help="Logs de PostgreSQL uniquement")
    mx.add_argument("--frontend", action="store_true", help="Logs du frontend uniquement")

    restart_parser = sub.add_parser("restart", help="Redémarrer un service (hub ou app)")
    restart_parser.add_argument("target", help="'hub' ou le slug de l'app")
    restart_parser.add_argument("service", nargs="?", choices=["api", "frontend"], default=None,
                                help="Service à redémarrer (défaut : tous)")

    health_parser = sub.add_parser("health", help="Health check d'une cible (hub ou app)")
    health_parser.add_argument("target", help="'hub' ou le slug de l'app")
    health_parser.add_argument("--detailed", action="store_true", help="GET /api/health/detailed")

    dbshell_parser = sub.add_parser("db-shell", help="Shell PostgreSQL d'une app (interactif)")
    dbshell_parser.add_argument("app", help="Slug de l'app")

    return p


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _resolve_mode(args) -> str:
    """Résout le mode : --dev/--prod > HUBCTL_MODE env > auto_detect_mode()."""
    import os
    from hubctl_cli.hub_state import auto_detect_mode, set_mode
    if getattr(args, "dev", False):
        mode = "dev"
    elif getattr(args, "prod", False):
        mode = "prod"
    elif os.environ.get("HUBCTL_MODE", "").lower() in ("dev", "prod"):
        mode = os.environ["HUBCTL_MODE"].lower()
    else:
        mode = auto_detect_mode()
    set_mode(mode)
    return mode


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

    if args.command in ("work", "ship", "update", "start", "stop", "status"):
        from rich.console import Console as RichConsole
        console = RichConsole()
        # Résoudre le mode avant l'action
        _resolve_mode(args)
        try:
            if args.command == "work":
                action_work(console, args.slug)
            elif args.command == "ship":
                action_ship(console, args.slug)
            elif args.command == "update":
                action_update(console, args.target, dry_run=False)
            elif args.command == "start":
                action_start_stop(console, "start", target=args.target)
            elif args.command == "stop":
                action_start_stop(console, "stop", target=args.target)
            elif args.command == "status":
                action_status(console)
            return 0
        except Exception as e:
            print(f"❌ {args.command} a échoué : {e}")
            return 1

    if args.command in ("logs", "restart", "health", "db-shell"):
        from rich.console import Console as RichConsole
        console = RichConsole()
        _resolve_mode(args)
        try:
            if args.command == "logs":
                service = "api" if args.api else "postgres" if args.db else "frontend" if args.frontend else None
                action_logs(console, args.target, service=service)
            elif args.command == "restart":
                action_restart(console, args.target, service=args.service)
            elif args.command == "health":
                return action_health(console, args.target, detailed=args.detailed)
            elif args.command == "db-shell":
                action_db_shell(console, args.app)
                return 0
            return 0
        except Exception as e:
            print(f"❌ {args.command} a échoué : {e}")
            return 1

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
        mode = _resolve_mode(args)
        from rich.console import Console as RichConsole
        if mode == "dev":
            RichConsole().print("[dim]Mode auto-détecté : DEV (APP_Workshop/ actif)[/dim]")
        else:
            RichConsole().print("[dim]Mode auto-détecté : PROD (APPS/)[/dim]")
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
