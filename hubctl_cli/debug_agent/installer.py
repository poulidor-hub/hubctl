# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Installer et detecteur de materiel pour l'agent de debug.

Detecte le GPU (nvidia-smi), verifie Ollama et le modele Granite,
et guide l'admin via une interface Rich pour le setup initial.
"""
import re
import subprocess

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.panel import Panel


# ============================================================
# GPU DETECTION
# ============================================================

def detect_gpu() -> dict | None:
    """Detecte le GPU NVIDIA et sa VRAM via nvidia-smi.

    Retourne {"name": str, "vram_mb": int} ou None si pas de GPU NVIDIA.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    # Format: "NVIDIA GeForce RTX 3090, 24576"
    line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    match = re.match(r"^(.+?),\s*(\d+)", line)
    if not match:
        return None

    return {
        "name": match.group(1).strip(),
        "vram_mb": int(match.group(2)),
    }


# ============================================================
# OLLAMA CHECK
# ============================================================

def check_ollama() -> str | None:
    """Retourne la version d'Ollama, ou None si non installe."""
    try:
        result = subprocess.run(
            ["ollama", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    # Format: "ollama version is 0.6.2"
    match = re.search(r"(\d+\.\d+)", result.stdout)
    return match.group(1) if match else result.stdout.strip()


# ============================================================
# MODEL CHECK
# ============================================================

def check_model(model_tag: str) -> bool:
    """Retourne True si le modele est telecharge dans Ollama."""
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

    if result.returncode != 0:
        return False

    return model_tag in result.stdout


# ============================================================
# MODEL RECOMMENDATION
# ============================================================

MODEL_TAGS = {
    "8b": "granite3.2:8b",
    "2b": "granite3.2:2b",
}


def recommend_model() -> str:
    """Recommande 8b si GPU avec >=8 Go VRAM, sinon 2b."""
    gpu = detect_gpu()
    if gpu and gpu["vram_mb"] >= 8192:
        return "8b"
    return "2b"


# ============================================================
# VERIFY READY
# ============================================================

def verify_ready(model_size: str) -> tuple[bool, str]:
    """Verifie que tout est pret pour lancer une session de debug.

    Args:
        model_size: "8b" ou "2b"

    Retourne (ok, message).
    """
    version = check_ollama()
    if version is None:
        return False, (
            "Ollama n'est pas installe. Lancez :\n"
            "  curl -fsSL https://ollama.com/install.sh | sh"
        )

    model_tag = MODEL_TAGS.get(model_size, f"granite3.2:{model_size}")
    if not check_model(model_tag):
        return False, (
            f"Modele {model_tag} non trouve. Lancez :\n"
            f"  hubctl debug --setup"
        )

    return True, f"Ollama {version} + {model_tag} pret."


# ============================================================
# SETUP (interactif, avec Rich)
# ============================================================

def run_setup(console: Console, force_model: str | None = None):
    """Guide interactif pour installer Ollama et Granite.

    Args:
        console: instance rich.console.Console
        force_model: "8b" ou "2b" pour forcer, None pour auto-detection
    """
    console.print(Panel(
        "[bold cyan]Installation de l'agent de debug Granite/Ollama[/bold cyan]",
        border_style="cyan",
    ))

    # 1. Detection GPU
    console.print("\n[bold]Detection du materiel...[/bold]")
    gpu = detect_gpu()
    if gpu:
        console.print(
            f"  [green]OK[/green] GPU detecte : {gpu['name']} "
            f"({gpu['vram_mb']} MiB VRAM)"
        )
    else:
        console.print("  [yellow]![/yellow] Aucun GPU NVIDIA detecte (mode CPU)")

    # 2. Verif Ollama
    console.print("\n[bold]Verification d'Ollama...[/bold]")
    version = check_ollama()
    if version:
        console.print(f"  [green]OK[/green] ollama {version} detecte")
    else:
        console.print("  [red]X[/red] Ollama non installe.")
        console.print("  Installez-le manuellement :")
        console.print("    [dim]curl -fsSL https://ollama.com/install.sh | sh[/dim]")
        return

    # 3. Choix du modele
    if force_model:
        model_size = force_model
    else:
        model_size = recommend_model()
        model_tag = MODEL_TAGS[model_size]
        recommended_label = "recommande" if gpu else "fallback CPU"

        existing_8b = check_model(MODEL_TAGS["8b"])
        existing_2b = check_model(MODEL_TAGS["2b"])

        if not existing_8b and not existing_2b:
            console.print("\n[bold]Aucun modele Granite trouve.[/bold]")
            console.print(
                f"  Modele {model_size} ({model_tag}) [{recommended_label}]"
            )
            choice = Prompt.ask(
                "  Telecharger ?",
                choices=["8b", "2b", "skip"],
                default=model_size,
            )
            if choice == "skip":
                console.print("  [yellow]Installation annulee.[/yellow]")
                return
            model_size = choice
        elif existing_8b:
            model_size = "8b"
            console.print(f"  [green]OK[/green] {MODEL_TAGS['8b']} deja present")
        else:
            model_size = "2b"
            console.print(f"  [green]OK[/green] {MODEL_TAGS['2b']} deja present")

    model_tag = MODEL_TAGS.get(model_size, f"granite3.2:{model_size}")

    # 4. Telechargement si necessaire
    if not check_model(model_tag):
        sizes = {"8b": "~5 Go", "2b": "~1.5 Go"}
        console.print(
            f"\n[bold]Telechargement de {model_tag}...[/bold] "
            f"({sizes.get(model_size, '?')})"
        )
        console.print("  [dim](Ctrl+C pour annuler — reprendra au prochain --setup)[/dim]")

        try:
            process = subprocess.Popen(
                ["ollama", "pull", model_tag],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for line in process.stdout:
                console.print(f"  {line.rstrip()}")
            process.wait()
            if process.returncode == 0:
                console.print(f"\n  [green]OK[/green] {model_tag} telecharge.")
            else:
                console.print(f"\n  [red]X[/red] Erreur lors du telechargement.")
        except KeyboardInterrupt:
            console.print("\n  [yellow]Interrompu.[/yellow]")
            return
    else:
        console.print(f"\n  [green]OK[/green] {model_tag} deja present.")

    # 5. Verification finale
    ok, msg = verify_ready(model_size)
    if ok:
        console.print(f"\n[bold green]Pret ![/bold green] {msg}")
        console.print(
            "\nLancez : [cyan]hubctl debug <APP>[/cyan]"
        )
    else:
        console.print(f"\n[red]X[/red] {msg}")
