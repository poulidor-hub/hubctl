# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

from __future__ import annotations
import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.text import Text

from hubctl_cli import actions, animation, atelier_serve, menu, update_serve
from hubctl_cli.actions import (
    action_start_all, action_stop_all, action_update_all, action_update,
    action_atelier, action_atelier_close_all, action_bootstrap, action_vpn, action_seed,
    action_seed_export, action_doctor,
)
from hubctl_cli.canvas import Canvas
from hubctl_cli.coffre import Vault
from hubctl_cli.vault_manager import (
    VaultManager, restore_all, export_vault, import_vault,
    app_list, app_set_url,
)
from hubctl_cli import hub_state, keys
from hubctl_cli.park_export import export_park
from hubctl_cli.park_restore import restore_park


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hubctl", description="Menu CLI de pilotage du hub POULIDOR.")
    p.add_argument("--no-anim", action="store_true", help="Desactiver l'animation de chargement.")
    p.add_argument("--dry-run", action="store_true", dest="global_dry_run",
                   help="Imprimer les commandes sans les executer.")

    devprod = p.add_mutually_exclusive_group()
    devprod.add_argument("--dev", action="store_true", dest="dev", help="Forcer le mode DEV")
    devprod.add_argument("--prod", action="store_true", dest="prod", help="Forcer le mode PROD")

    sub = p.add_subparsers(dest="command", help="Commandes")

    # start — demarrer tout
    start_parser = sub.add_parser("start", help="Demarrer le Hub et/ou les apps")
    start_parser.add_argument("target", nargs="?", default="all",
                              choices=["all", "prod", "dev"])

    # stop — arreter tout
    stop_parser = sub.add_parser("stop", help="Arreter toutes les apps et le Hub")
    stop_parser.add_argument("target", nargs="?", default="all",
                             choices=["all", "prod", "dev"])

    # update — mise à jour ciblée
    update_parser = sub.add_parser(
        "update",
        help="Mise a jour ciblee : hub, une ou plusieurs apps, ou tout (git pull + rebuild)")
    update_parser.add_argument("targets", nargs="*", metavar="CIBLE",
                               help='hub | <slug app> | "*" ou all (Hub + toutes les apps)')
    update_parser.add_argument("--serve", action="store_true", dest="update_serve", default=False,
                               help="Lancer le serveur hôte de mise à jour (bind 127.0.0.1 par défaut, cf. --host) : "
                                    "GET /health, GET /update/status, POST /update/run, "
                                    "GET /update/job")
    update_parser.add_argument("--port", type=int, dest="update_port", default=8778, metavar="PORT",
                               help="Avec --serve : port d'écoute (défaut : 8778)")
    update_parser.add_argument("--host", dest="update_host", default="127.0.0.1", metavar="HOST",
                               help="Avec --serve : interface d'écoute (défaut : 127.0.0.1). "
                                    "Ex : la passerelle docker0 (ip -4 addr show docker0 — PAS "
                                    "toujours 172.17.0.1, cf. pools du daemon) pour être joignable "
                                    "des conteneurs via host.docker.internal, sans exposition LAN")
    update_parser.add_argument("--token-file", type=Path, dest="update_token_file", default=None,
                               metavar="CHEMIN",
                               help="Avec --serve : fichier du token d'auth X-Atelier-Token "
                                    "(défaut : ~/.config/hubctl/atelier_token)")

    # doctor — diagnostic des clones du parc
    doctor_parser = sub.add_parser(
        "doctor",
        help="Diagnostic des clones (méta, HUB_base, apps) : divergences, saleté git")
    doctor_parser.add_argument("--fix", dest="doctor_fix", action="store_true",
                               help="Appliquer les remèdes sûrs (réalignement HUB_base, "
                                    "branches mergées)")
    doctor_parser.add_argument("--yes", dest="doctor_yes", action="store_true",
                               help="Appliquer sans confirmation interactive")

    # atelier
    atelier_parser = sub.add_parser(
        "atelier", help="Creer, mettre a jour, deployer en prod ou supprimer l'atelier d'une app")
    atelier_parser.add_argument("app", nargs="?", default=None,
                                help="Slug de l'app (omettre avec --close-all)")
    majprod = atelier_parser.add_mutually_exclusive_group()
    majprod.add_argument("--maj", action="store_true", dest="atelier_maj", default=False,
                         help="Mettre a jour l'atelier : git fetch + checkout + rebuild -ws")
    majprod.add_argument("--prod", action="store_true", dest="atelier_prod", default=False,
                         help="Deployer origin/main en prod puis supprimer l'atelier")
    majprod.add_argument("--close-all", action="store_true", dest="atelier_close_all", default=False,
                         help="Fermer TOUS les ateliers actifs (confirmation globale, ou --yes pour scripter)")
    majprod.add_argument("--serve", action="store_true", dest="atelier_serve", default=False,
                         help="Lancer le mini-serveur hôte atelier (bind 127.0.0.1 par défaut, cf. --host) : "
                              "GET /health, GET /atelier/list, POST /atelier/close-all, "
                              "GET /vault/status, POST /vault/backup")
    atelier_parser.add_argument("--port", type=int, dest="atelier_port", default=8777, metavar="PORT",
                                help="Avec --serve : port d'écoute (défaut : 8777)")
    atelier_parser.add_argument("--host", dest="atelier_host", default="127.0.0.1", metavar="HOST",
                                help="Avec --serve : interface d'écoute (défaut : 127.0.0.1). "
                                     "Ex : la passerelle docker0 (ip -4 addr show docker0 — PAS "
                                     "toujours 172.17.0.1, cf. pools du daemon) pour être joignable "
                                     "des conteneurs via host.docker.internal, sans exposition LAN")
    atelier_parser.add_argument("--token-file", type=Path, dest="atelier_token_file", default=None,
                                metavar="CHEMIN",
                                help="Avec --serve : fichier du token d'auth X-Atelier-Token "
                                     "(défaut : ~/.config/hubctl/atelier_token)")
    atelier_parser.add_argument("--yes", action="store_true", dest="atelier_yes", default=False,
                                help="Avec --close-all : fermer sans confirmation interactive")
    atelier_parser.add_argument("--ref", default=None, metavar="REF",
                                help="Branche ou tag a tester avec --maj (defaut: main)")

    # init — bootstrap
    init_parser = sub.add_parser("init", help="Bootstrap automatique du Hub (genere .env, demarre, verifie la sante)")
    init_parser.add_argument("--force", action="store_true",
                             help="Ecraser le .env existant sans confirmation")

    # export — export parc
    export_parser = sub.add_parser("export", help="Exporter tout le parc dans une archive tar.gz")
    export_parser.add_argument("--app", default=None, help="Exporter une seule app (slug)")
    export_parser.add_argument("--no-encrypt", action="store_true",
                               help="Produire une archive tar.gz non chiffree (legacy, secrets en clair)")
    export_parser.add_argument("--passphrase-stdin", action="store_true",
                               help="Lire la passphrase d'archive depuis stdin (CI/DR)")

    # restore — restore parc
    restore_parser = sub.add_parser("restore", help="Restaurer le parc depuis une archive (Hub vierge uniquement)")
    restore_parser.add_argument("archive", help="Chemin vers l'archive park-*.tar.gz")
    restore_parser.add_argument("--passphrase-stdin", action="store_true",
                                help="Lire la passphrase d'archive depuis stdin (CI/DR)")

    # pra-restore — assistant PRA
    pra_parser = sub.add_parser(
        "pra-restore",
        help="Restauration PRA depuis un kit (coffre + dernier snapshot)")
    pra_parser.add_argument("kit", nargs="?", default=None,
                            help="Chemin du kit PRA (scan auto si omis)")
    pra_parser.add_argument("--snapshot", default=None,
                            help="Nom ou chemin du snapshot à restaurer (défaut: plus récent du kit)")
    pra_parser.add_argument("--apps", default=None,
                            help="all | none | slugs CSV (défaut: interactif). Scriptable : "
                                 "ADMIN_PASSWORD=… ./hubctl pra-restore ~/kit --yes --force --apps all")
    pra_parser.add_argument("--admin-user", default=None,
                            help="Login admin pour la réinjection (défaut: ADMIN_USERNAME/.env/admin)")
    pra_parser.add_argument("--yes", action="store_true", help="Saute les confirmations")
    pra_parser.add_argument("--force", action="store_true",
                            help="Autoriser même sur un parc non vide (re-PRA)")
    pra_parser.add_argument("--dry-run", action="store_true",
                            help="Afficher le plan sans rien exécuter")

    # debug
    debug_parser = sub.add_parser("debug", help="Agent de debug Granite/Ollama")
    debug_parser.add_argument("app", nargs="?", default=None, help="Application cible")
    debug_parser.add_argument("--rw", action="store_true", help="Mode semi-auto")
    debug_parser.add_argument("--setup", action="store_true", help="Installer Ollama et Granite")
    debug_parser.add_argument("--model", choices=["8b", "2b"], default=None, help="Forcer le modele")
    debug_parser.add_argument("--off", action="store_true", help="Desactiver l'agent")
    debug_parser.add_argument("--status", action="store_true", help="Verifier la disponibilite")

    # vpn
    vpn_parser = sub.add_parser("vpn", help="Gestion WireGuard (sync, status, on, off)")
    vpn_parser.add_argument("action", choices=["sync", "status", "on", "off"], help="Action VPN")

    # vault
    vault_parser = sub.add_parser("vault", help="Gestion du coffre chiffre")
    vault_sub = vault_parser.add_subparsers(dest="vault_action")
    vault_sub.add_parser("backup", help="Synchroniser et sauvegarder le coffre")
    vault_sub.add_parser("restore", help="Restaurer les .env depuis le coffre")
    vault_sub.add_parser("status", help="Verifier les divergences")
    vault_export_parser = vault_sub.add_parser("export", help="Exporter le coffre")
    vault_export_parser.add_argument("dest", help="Chemin de destination")
    vault_import_parser = vault_sub.add_parser("import", help="Importer un coffre")
    vault_import_parser.add_argument("src", help="Chemin source")

    vault_app = vault_sub.add_parser("app", help="Apps dans le coffre")
    app_sub = vault_app.add_subparsers(dest="app_action")
    app_sub.add_parser("list", help="Lister les apps avec git_url et version")
    set_url = app_sub.add_parser("set-url", help="Modifier le git URL d'une app")
    set_url.add_argument("slug")
    set_url.add_argument("url")

    # jwt — trousseau de clés JWT
    jwt_parser = sub.add_parser("jwt", help="Trousseau de clés JWT (rotation, révocation)")
    jwt_sub = jwt_parser.add_subparsers(dest="jwt_action")
    jwt_sub.add_parser("keys", help="Lister le trousseau")
    jwt_sub.add_parser("rotate", help="Rotater la clé de signature (overlap 24 h)")
    jwt_revoke = jwt_sub.add_parser("revoke", help="Révoquer une clé (mode compromission)")
    jwt_revoke.add_argument("--kid", required=True)
    jwt_cleanup = jwt_sub.add_parser("cleanup", help="Supprimer les PEM archivés/révoqués anciens")
    jwt_cleanup.add_argument("--older-than-months", type=int, default=12)

    # seed (déploiement batch déclaratif d'apps + export)
    seed_parser = sub.add_parser("seed",
                                 help="Déployer/converger des apps depuis un fichier seed YAML")
    seed_parser.add_argument("fichier", nargs="?", default=None,
                              help="Chemin vers le fichier seed (YAML) — ou 'export' pour exporter le parc")
    seed_parser.add_argument("--prune", action="store_true",
                              help="Supprimer les apps présentes mais non listées (confirmation)")
    seed_parser.add_argument("--dry-run", action="store_true",
                              help="Simulation : afficher les actions sans les exécuter")
    seed_parser.add_argument("-o", "--output", default=None,
                              help="Chemin de sortie (défaut: seed-export-YYYY-MM-DD.yaml)")
    seed_parser.add_argument("--force", action="store_true",
                              help="Écraser le fichier s'il existe déjà")
    seed_parser.set_defaults(seed_action=None)

    # socle — build local des images de base, statut flotte
    socle_parser = sub.add_parser("socle", help="Socle HUBsocle : build des images de base, statut flotte")
    socle_sub = socle_parser.add_subparsers(dest="socle_cmd")
    socle_build_p = socle_sub.add_parser("build", help="Construire les images poulidor/socle-{api,web} (local)")
    socle_build_p.add_argument("--force", action="store_true", help="Reconstruire même si le tag existe")
    socle_sub.add_parser("status", help="Version socle par app (drift flotte)")

    # docker — daemon : pools d'adresses (F14, anti-saturation networks)
    docker_parser = sub.add_parser(
        "docker", help="Daemon Docker : pools d'adresses (anti-saturation networks)")
    docker_sub = docker_parser.add_subparsers(dest="docker_cmd")
    docker_setup_p = docker_sub.add_parser(
        "setup-pools", help="Configurer default-address-pools dans /etc/docker/daemon.json")
    docker_setup_p.add_argument("--yes", action="store_true", help="Appliquer sans confirmation")
    docker_sub.add_parser("check", help="Vérifier si les pools recommandés sont en place")

    return p


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _update_mode(args) -> str:
    """Mode pour `update` : EXPLICITE uniquement (plan Task 4 + revue I4).

    Jamais l'auto-détection atelier : un `.workshop.env` résiduel ne doit
    pas basculer un Hub de prod en `up-dev`. Défaut : prod.
    """
    import os
    if getattr(args, "dev", False):
        return "dev"
    if getattr(args, "prod", False):
        return "prod"
    env = os.environ.get("HUBCTL_MODE", "").lower()
    if env in ("dev", "prod"):
        return env
    return "prod"


def _resolve_mode(args) -> str:
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


def _print_update_usage(console) -> None:
    """`hubctl update` sans cible : affiche l'usage, n'exécute rien."""
    console.print("[yellow]hubctl update requiert au moins une cible — rien n'a été exécuté.[/yellow]")
    console.print("[bold]Usage :[/bold]")
    console.print("  hubctl update hub             # Hub seul")
    console.print("  hubctl update app-exemple     # une app (slug)")
    console.print("  hubctl update hub app-exemple # plusieurs cibles")
    console.print('  hubctl update "*"             # Hub + toutes les apps (alias : all)')


def _poll_quit() -> bool:
    k = keys.read_key(sys.stdin.fileno(), timeout=0.5)
    if k is None:
        return False
    if isinstance(k, (keys.KeyEsc, keys.KeyCtrlC)):
        return True
    return False


def _unlock_vault_cli(vault, console) -> bool:
    """Déverrouille le coffre pour `hubctl vault …` : clé locale d'abord,
    puis saisie manuelle (3 essais, migration de la clé au succès)."""
    from getpass import getpass
    if not vault.is_locked:
        return True
    if vault.unlock_stored():
        return True
    for attempt in range(3):
        pw = getpass("🔐 Passphrase du coffre : ")
        if vault.unlock(pw):
            try:
                vault.store_passphrase(pw)
            except OSError:
                pass  # non bloquant
            return True
        console.print("[red]Passphrase incorrecte.[/red]" if attempt < 2 else "")
    console.print("[red]❌ 3 tentatives echouees. Abandon.[/red]")
    return False


def _run(args) -> int:
    interactive = _interactive()
    global_dry_run = getattr(args, "global_dry_run", False)

    # ── Commandes non-interactives ──

    if args.command == "export":
        try:
            passphrase = None
            if not args.no_encrypt:
                if args.passphrase_stdin:
                    passphrase = sys.stdin.readline().rstrip("\n")
                    if not passphrase:
                        print("❌ Passphrase vide recue sur stdin. Refus de produire "
                              "une archive 'chiffree' par une passphrase vide.")
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
            print(f"❌ export a echoue : {e}")
            return 1

    if args.command == "restore":
        try:
            passphrase = None
            if args.passphrase_stdin:
                passphrase = sys.stdin.readline().rstrip("\n")
            return 0 if restore_park(args.archive, passphrase=passphrase) else 1
        except Exception as e:
            print(f"❌ restore a échoué : {e}")
            return 1

    if args.command == "pra-restore":
        from hubctl_cli import pra_restore
        try:
            return pra_restore.run_pra_restore(
                kit=args.kit, snapshot=args.snapshot, apps=args.apps,
                admin_user=args.admin_user, yes=args.yes,
                force=args.force, dry_run=args.dry_run or global_dry_run)
        except SystemExit as e:
            if isinstance(e.code, str):
                print(e.code)
            return e.code if isinstance(e.code, int) else 1
        except KeyboardInterrupt:
            print("\n❌ Interrompu.")
            return 130
        except Exception as e:
            print(f"❌ pra-restore a échoué : {e}")
            return 1

    if args.command == "debug":
        from hubctl_cli.debug_agent import run_debug
        return run_debug(args)

    if args.command == "init":
        from rich.console import Console as RichConsole
        console = RichConsole()
        try:
            return action_bootstrap(console, force=args.force, dry_run=global_dry_run)
        except Exception as e:
            print(f"❌ init a echoue : {e}")
            return 1

    if args.command == "start":
        from rich.console import Console as RichConsole
        console = RichConsole()
        _resolve_mode(args)
        try:
            action_start_all(console=console, target=args.target, dry_run=global_dry_run)
            return 0
        except Exception as e:
            print(f"❌ start a echoue : {e}")
            return 1

    if args.command == "stop":
        from rich.console import Console as RichConsole
        console = RichConsole()
        _resolve_mode(args)
        try:
            action_stop_all(console=console, target=args.target, dry_run=global_dry_run)
            return 0
        except Exception as e:
            print(f"❌ stop a echoue : {e}")
            return 1

    if args.command == "update":
        from rich.console import Console as RichConsole
        console = RichConsole()
        if args.update_serve:
            # Serveur hôte « Centre de mises à jour » (spec §4.1) : gère le
            # parc entier — pas de cible, pas de _update_mode (la collecte
            # et le job n'ont pas besoin du mode).
            if args.targets:
                # Même ruling que `atelier --serve` sans slug d'app : la
                # cible se passe à POST /update/run, pas au lancement.
                print("❌ --serve ne prend pas de cible "
                      "(l'agent met a jour le parc entier via POST /update/run).")
                return 2
            try:
                return update_serve.serve(port=args.update_port,
                                          token_file=args.update_token_file,
                                          host=args.update_host)
            except Exception as e:
                print(f"❌ update --serve a echoue : {e}")
                return 1
        # Plan unification updates (revue I4) : mode explicite uniquement,
        # jamais l'auto-détection atelier pour une mise à jour.
        hub_state.set_mode(_update_mode(args))
        try:
            if not args.targets:
                _print_update_usage(console)
                return 1
            return action_update(console=console, targets=args.targets,
                                 dry_run=global_dry_run)
        except Exception as e:
            print(f"❌ update a echoue : {e}")
            return 1

    if args.command == "doctor":
        from rich.console import Console as RichConsole
        console = RichConsole()
        try:
            return action_doctor(console=console, fix=args.doctor_fix,
                                 assume_yes=args.doctor_yes)
        except Exception as e:
            print(f"❌ doctor a échoué : {e}")
            return 1

    if args.command == "atelier":
        from rich.console import Console as RichConsole
        console = RichConsole()
        if args.atelier_serve:
            # Le serveur gère TOUS les ateliers : pas de slug, pas de
            # _resolve_mode (collect_ateliers n'a pas besoin du mode).
            if args.app is not None:
                print("❌ --serve ne prend pas de slug d'app "
                      "(le serveur liste et ferme tous les ateliers).")
                return 2
            try:
                return atelier_serve.serve(port=args.atelier_port,
                                           token_file=args.atelier_token_file,
                                           host=args.atelier_host)
            except Exception as e:
                print(f"❌ atelier --serve a echoue : {e}")
                return 1
        _resolve_mode(args)
        if args.atelier_close_all:
            if args.app is not None:
                print("❌ --close-all ne prend pas de slug d'app (elle agit sur tous les ateliers).")
                return 2
            try:
                return action_atelier_close_all(console=console, dry_run=global_dry_run,
                                                oui=args.atelier_yes)
            except Exception as e:
                print(f"❌ atelier --close-all a echoue : {e}")
                return 1
        if args.app is None:
            print("❌ precisez un slug d'app, ou --close-all pour fermer tous les ateliers actifs.")
            return 2
        try:
            action_atelier(console=console, app_slug=args.app, dry_run=global_dry_run,
                           maj=args.atelier_maj, prod=args.atelier_prod, ref=args.ref)
            return 0
        except Exception as e:
            print(f"❌ atelier a echoue : {e}")
            return 1

    if args.command == "vpn":
        from rich.console import Console as RichConsole
        console = RichConsole()
        try:
            action_vpn(console=console, subcmd=args.action, dry_run=global_dry_run)
            return 0
        except Exception as e:
            print(f"❌ vpn a echoue : {e}")
            return 1

    if args.command == "vault":
        from rich.console import Console as RichConsole
        console = RichConsole()
        try:
            vault_path = hub_state.vault_path()
            vault = Vault(vault_path)
            vault_mgr = VaultManager(vault)

            # Déverrouiller le coffre si nécessaire (clé locale d'abord)
            if not _unlock_vault_cli(vault, console):
                return 1

            if args.vault_action == "backup":
                summary = vault_mgr.backup_all(on_log=lambda msg: console.print(msg))
                msg = f"Coffre sauvegarde : {summary['env']} .env, {summary['files']} fichiers"
                if summary.get("ca"):
                    msg += f", CA HTTPS incluse ({summary['ca']} fichiers)"
                console.print(f"[green]{msg}[/green]")
                return 0

            elif args.vault_action == "restore":
                restored = restore_all(vault, dry_run=global_dry_run)
                if restored:
                    for r in restored:
                        console.print(f"[green]Restaure : {r}[/green]")
                else:
                    console.print("[dim]Aucune divergence a restaurer.[/dim]")
                return 0

            elif args.vault_action == "status":
                diverged = vault_mgr.check_divergence()
                if diverged:
                    console.print("[yellow]Divergences detectees :[/yellow]")
                    for target in diverged:
                        console.print(f"  • {target}")
                else:
                    console.print("[green]Aucune divergence.[/green]")
                return 0

            elif args.vault_action == "export":
                dest = export_vault(vault, Path(args.dest))
                console.print(f"[green]Coffre exporte vers {dest}[/green]")
                return 0

            elif args.vault_action == "import":
                src = Path(args.src)
                ok = import_vault(vault, src)
                if ok:
                    console.print(f"[green]Coffre importe depuis {src}[/green]")
                    return 0
                else:
                    console.print(f"[red]Fichier source introuvable : {src}[/red]")
                    return 1

            elif args.vault_action == "app":
                if args.app_action == "list":
                    for app in app_list(vault):
                        print(f"{app['slug']:30s} {app.get('git_url','-') or '-':50s} {app.get('version','-') or '-'}")
                elif args.app_action == "set-url":
                    ok = app_set_url(vault, args.slug, args.url)
                    print("✓ URL mise à jour" if ok else f"✗ App '{args.slug}' non trouvée")
                else:
                    console.print("[red]Action app inconnue.[/red]")
                    return 1
                return 0

            else:
                console.print("[red]Action coffre inconnue.[/red]")
                return 1

        except Exception as e:
            print(f"❌ vault a echoue : {e}")
            return 1

    if args.command == "jwt":
        from hubctl_cli import jwt_keys
        if args.jwt_action == "keys":
            return jwt_keys.run_keys()
        if args.jwt_action == "rotate":
            return jwt_keys.run_rotate()
        if args.jwt_action == "revoke":
            return jwt_keys.run_revoke(args.kid)
        if args.jwt_action == "cleanup":
            return jwt_keys.run_cleanup(args.older_than_months)
        return 0

    if args.command == "seed":
        from rich.console import Console as RichConsole
        console = RichConsole()
        _resolve_mode(args)

        # Export mode : -o flag ou fichier == "export"
        if args.output is not None or args.fichier == "export":
            try:
                return action_seed_export(console,
                                          output_path=args.output,
                                          force=args.force)
            except Exception as e:
                print(f"❌ seed export a échoué : {e}")
                return 1

        # Legacy : seed <fichier>
        if not args.fichier:
            console.print("[red]✗ Fichier seed requis. Usage : hubctl seed <fichier.yaml>[/red]")
            console.print("[dim]Nouveau : hubctl seed export [-o <chemin>] pour exporter le parc actuel.[/dim]")
            return 1
        try:
            return action_seed(console, args.fichier,
                               prune=args.prune,
                               dry_run=global_dry_run or args.dry_run)
        except Exception as e:
            print(f"❌ seed a échoué : {e}")
            return 1

    if args.command == "socle":
        from hubctl_cli.socle import socle_build, socle_status
        if args.socle_cmd == "build":
            return socle_build(force=getattr(args, "force", False), dry_run=global_dry_run)
        return socle_status()

    if args.command == "docker":
        from hubctl_cli.docker_pools import check_pools, setup_pools
        from rich.console import Console as _Console
        con = _Console()
        if args.docker_cmd == "check":
            ok = check_pools()
            con.print(
                "[green]✓ Pools d'adresses Docker configurés[/green]"
                if ok
                else "[red]✗ Pools non configurés — hubctl docker setup-pools[/red]"
            )
            return 0 if ok else 1
        return setup_pools(con, yes=getattr(args, "yes", False))

    # ── TUI (pas de sous-commande ou flags seuls) ──

    skip_anim = args.no_anim or not interactive

    canvas = Canvas(title="HUBCTL · POULIDOR", target_cols=100, target_rows=30)

    if not interactive:
        console = Console()
        animation.preload_with_bar(console, animate=False)
        console.print(menu._status_banner())
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
            state: dict = {}
            animation.preload_with_bar(canvas, animate=not skip_anim, state_dict=state)
        except KeyboardInterrupt:
            pass

        vault_path = hub_state.vault_path()
        vault = Vault(vault_path)
        vault_mgr = VaultManager(vault)
        menu.install_layout(canvas)
        mode = _resolve_mode(args)
        from rich.console import Console as RichConsole
        if mode == "dev":
            RichConsole().print("[dim]Mode auto-detecte : DEV (ateliers .workshop.env actifs)[/dim]")
        else:
            RichConsole().print("[dim]Mode auto-detecte : PROD (APPS/)[/dim]")
        menu._require_vault(canvas, vault_mgr)
        menu.main_loop(canvas, vault_mgr, initial_state=state)
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
