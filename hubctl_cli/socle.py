# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Commandes socle : build local des images de base, statut flotte."""
from __future__ import annotations
import subprocess
from pathlib import Path
from rich.console import Console
from rich.table import Table

from hubctl_cli import utils

_console = Console()

PACKAGES = {"api": "poulidor/socle-api", "web": "poulidor/socle-web"}

# Budget par défaut du build d'UNE image de base (secondes). Le build passe
# par utils.run_shell (sortie streamée dans la console de l'appelant, donc
# aussi dans le TUI) dont la limite par défaut est 120 s : elle tuerait le
# build en cours — même écart que le premier boot du Hub (cf. hub_make).
SOCLE_BUILD_TIMEOUT = 1800


def find_socle_dir() -> Path:
    """HUBsocle : sous-module du méta-repo, sinon sibling du méta-repo."""
    meta = Path(__file__).resolve().parent.parent.parent  # hubctl_src/hubctl_cli/socle.py -> POULIDOR/
    for candidate in (meta / "HUBsocle", meta.parent / "HUBsocle"):
        if (candidate / "VERSION").exists():
            return candidate
    raise FileNotFoundError("HUBsocle introuvable (méta-repo/HUBsocle ou sibling)")


def find_meta_repo() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def socle_version(repo_dir: Path) -> str | None:
    f = repo_dir / "VERSION"
    return f.read_text().strip() if f.exists() else None


def collect_app_socle_versions(meta_dir: Path) -> dict[str, str]:
    """APP_SOCLE_VERSION par app ('-' si absent ; MIGRATION et fichiers cachés exclus)."""
    result: dict[str, str] = {}
    apps_dir = meta_dir / "APPS"
    if not apps_dir.is_dir():
        return result
    for d in sorted(apps_dir.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name == "MIGRATION":
            continue
        env = d / ".env"
        ver = "-"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("APP_SOCLE_VERSION="):
                    ver = line.split("=", 1)[1].strip()
                    break
        result[d.name] = ver
    return result


def _image_exists(tag: str) -> bool:
    out = subprocess.run(["docker", "images", "-q", tag], capture_output=True, text=True)
    return bool(out.stdout.strip())


def socle_build(force: bool = False, dry_run: bool = False, *,
                console: Console | None = None, timeout: int | None = None) -> int:
    """Construit localement les images manquantes ``poulidor/socle-{api,web}:<V>``.

    ``console`` : console de l'appelant — obligatoire pour que la sortie parte
    dans le flux du TUI au lieu de stdout. ``timeout`` : budget par image
    (défaut ``SOCLE_BUILD_TIMEOUT`` ; ``None`` ne désactive PAS la limite).
    Retourne 124 sur timeout (convention de ``utils.run_shell``).
    """
    con = console if console is not None else _console
    budget = timeout if timeout is not None else SOCLE_BUILD_TIMEOUT
    socle = find_socle_dir()
    version = socle_version(socle)
    if not version:
        con.print("[red]VERSION HUBsocle introuvable[/red]")
        return 1
    ctx_api = socle
    ctx_web = socle / "images" / "web"
    for kind, image in PACKAGES.items():
        tag = f"{image}:{version}"
        ctx = ctx_api if kind == "api" else ctx_web
        dockerfile = "images/api/Dockerfile" if kind == "api" else "Dockerfile"
        if _image_exists(tag) and not force:
            con.print(f"[green]✓ {tag} existe déjà (cache)[/green] — --force pour rebâtir")
            continue
        cmd = ["docker", "build", "-f", str(ctx / dockerfile),
               "--build-arg", f"SOCLE_VERSION={version}", "-t", tag, str(ctx)]
        rc = utils.run_shell(cmd, dry_run=dry_run, console=con, timeout=budget)
        if rc == 124:
            con.print(f"[red]⏳ Timeout ({budget}s) sur le build de {tag} — relance `./hubctl socle build` (le cache Docker conserve l'avancement).[/red]")
            return rc
        if rc != 0:
            con.print(f"[red]❌ Build échoué pour {tag}. Vérifier le checkout HUBsocle.[/red]")
            return rc
    con.print(f"[green]✅ Socle {version} prêt (local, offline-first)[/green]")
    return 0


def socle_status() -> int:
    meta = find_meta_repo()
    socle = find_socle_dir()
    current = socle_version(socle)
    table = Table(title=f"Socle HUBsocle {current} — statut flotte")
    table.add_column("App")
    table.add_column("APP_SOCLE_VERSION")
    table.add_column("État")
    rows = collect_app_socle_versions(meta)
    for app, ver in rows.items():
        state = "[green]à jour[/green]" if ver == current else (
            "[yellow]socle non déclaré (fork template)[/yellow]" if ver == "-" else "[red]en retard[/red]")
        table.add_row(app, ver, state)
    _console.print(table)
    return 0
