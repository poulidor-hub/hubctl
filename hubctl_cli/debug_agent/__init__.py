"""Module debug_agent : agent de debug Granite/Ollama pour POULIDOR.

Chargeable paresseusement depuis hubctl_cli/__main__.py uniquement quand
la sous-commande 'debug' est invoquee. Ne depend d'aucun autre module hubctl_cli.

Orchestrateur principal : run_debug() — point d'entree appele par __main__.py.
"""
import json
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from . import installer, prompt
from .installer import MODEL_TAGS


def _is_disabled() -> bool:
    return os.environ.get("POULIDOR_AI_ENABLED", "true").lower() in (
        "false", "0", "no",
    )


def _status(console: Console):
    """Affiche le statut de l'agent (materiel, ollama, modele)."""
    console.print(Panel(
        "[bold cyan]Statut de l'agent de debug[/bold cyan]",
        border_style="cyan",
    ))

    gpu = installer.detect_gpu()
    if gpu:
        console.print(f"  GPU : {gpu['name']} ({gpu['vram_mb']} MiB VRAM)")
    else:
        console.print("  GPU : [yellow]aucun (mode CPU)[/yellow]")

    version = installer.check_ollama()
    if version:
        console.print(f"  Ollama : {version}")
    else:
        console.print("  Ollama : [red]non installe[/red]")

    for size, tag in MODEL_TAGS.items():
        present = installer.check_model(tag)
        status = "[green]OK[/green]" if present else "[dim]absent[/dim]"
        console.print(f"  {tag} : {status}")

    recommended = installer.recommend_model()
    console.print(f"\n  Modele recommande : {recommended}")


def run_debug(args) -> int:
    """Point d'entree principal de la sous-commande 'debug'.

    Args:
        args: namespace argparse avec les attributs :
            - app (str|None)
            - rw (bool)
            - setup (bool)
            - model (str|None) : "8b" ou "2b"
            - off (bool)
            - status (bool)

    Retourne un code de sortie (0 = succes).
    """
    console = Console()

    # Desactivation globale
    if _is_disabled() and not args.off:
        console.print(
            "[yellow]L'agent de debug est desactive "
            "(POULIDOR_AI_ENABLED=false).[/yellow]"
        )
        return 1

    # Desactivation explicite
    if args.off:
        console.print(
            "[yellow]Pour desactiver l'agent :[/yellow]\n"
            "  export POULIDOR_AI_ENABLED=false"
        )
        return 0

    # Statut
    if args.status:
        _status(console)
        return 0

    # Setup
    if args.setup:
        installer.run_setup(console, force_model=args.model)
        return 0

    # Lancement d'une session de debug
    if not args.app:
        console.print("[red]Erreur : nom d'application requis.[/red]")
        console.print("Usage : hubctl debug <APP> [--rw] [--model 8b|2b]")
        return 1

    # Determiner le modele
    model_size = args.model or installer.recommend_model()
    model_tag = MODEL_TAGS.get(model_size, f"granite3.2:{model_size}")

    # Verifier que tout est pret
    ok, msg = installer.verify_ready(model_size)
    if not ok:
        console.print(f"[red]X[/red] {msg}")
        return 1

    # Avertissement si 8b en CPU
    gpu = installer.detect_gpu()
    mode = "rw" if args.rw else "ro"

    if model_size == "8b" and not gpu:
        from rich.prompt import Confirm
        console.print(
            "[yellow]![/yellow] Aucun GPU detecte. "
            f"{model_tag} en CPU sera lent (~5-15s par reponse)."
        )
        if not Confirm.ask("  Continuer ?", default=False):
            console.print("  [yellow]Annule.[/yellow]")
            return 0

    # Afficher le banner de session
    gpu_label = f"{gpu['name']} ({gpu['vram_mb']} MiB VRAM)" if gpu else "CPU (aucun GPU)"
    mode_label = "semi-auto" if mode == "rw" else "lecture seule"

    console.print(Panel(
        f"[bold cyan]Agent de debug POULIDOR[/bold cyan]\n"
        f"  Materiel : {gpu_label}\n"
        f"  Modele   : {model_tag}\n"
        f"  Cible    : {args.app}\n"
        f"  Mode     : {mode_label}\n"
        f"  Outils   : docker_logs, docker_ps, db_query_select, "
        f"docker_stats, read_file\n"
        f"           + hub-mcp (audit, apps, erreurs)",
        border_style="cyan",
    ))

    # Construire le prompt systeme
    console.print("\nConstruction du contexte...")
    system_prompt = prompt.build_system_prompt(args.app, mode)
    console.print(f"  [green]OK[/green] Contexte charge "
                  f"({len(system_prompt)} caracteres)")

    # Verifier que opencode est installe
    import shutil
    if not shutil.which("opencode"):
        console.print(
            "[red]X[/red] OpenCode n'est pas installe.\n"
            "  Installez-le : [dim]npm install -g opencode[/dim]"
        )
        return 1

    # Generer la config OpenCode temporaire
    park_root = os.environ.get(
        "POULIDOR_ROOT",
        str(Path(__file__).resolve().parents[4]),  # debug_agent/ → hubctl_cli/ → hubctl_src/ → POULIDOR/
    )

    config = {
        "model": {
            "provider": "ollama",
            "name": model_tag,
            "baseUrl": "http://localhost:11434/v1",
            "temperature": 0.1,
            "maxTokens": 4096,
        },
        "mcpServers": {
            "hub-debug": {
                "command": sys.executable,
                "args": [
                    "-m", "hubctl_cli.debug_agent.debug_mcp",
                    "--app", args.app,
                    "--mode", mode,
                ],
                "env": {
                    "POULIDOR_ROOT": park_root,
                },
            },
            "hub-mcp": {
                "command": sys.executable,
                "args": ["-m", "hub_mcp.server"],
                "env": {
                    "POULIDOR_ROOT": park_root,
                },
            },
        },
        "systemPrompt": system_prompt,
    }

    # Ecrire dans un fichier temporaire (dans le park root, ignore par git)
    config_path = Path(park_root) / "opencode.debug.json"
    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    console.print(f"\n[cyan]Lancement d'OpenCode...[/cyan]")
    console.print(f"  Config : {config_path}\n")

    # Lancer OpenCode
    import subprocess
    try:
        result = subprocess.run(
            ["opencode", "--config", str(config_path)],
        )
        return result.returncode
    except KeyboardInterrupt:
        console.print("\n[yellow]Session interrompue.[/yellow]")
        return 130
    finally:
        # Nettoyer le fichier temporaire
        if config_path.exists():
            config_path.unlink()
