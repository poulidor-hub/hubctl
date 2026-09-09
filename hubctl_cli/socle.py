"""Commandes socle : build local des images de base, statut flotte."""
from __future__ import annotations
import subprocess
from pathlib import Path
from rich.console import Console
from rich.table import Table

console = Console()

PACKAGES = {"api": "poulidor/socle-api", "web": "poulidor/socle-web"}


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


def socle_build(force: bool = False, dry_run: bool = False) -> int:
    socle = find_socle_dir()
    version = socle_version(socle)
    if not version:
        console.print("[red]VERSION HUBsocle introuvable[/red]")
        return 1
    ctx_api = socle
    ctx_web = socle / "images" / "web"
    for kind, image in PACKAGES.items():
        tag = f"{image}:{version}"
        ctx = ctx_api if kind == "api" else ctx_web
        dockerfile = "images/api/Dockerfile" if kind == "api" else "Dockerfile"
        if _image_exists(tag) and not force:
            console.print(f"[green]✓ {tag} existe déjà (cache)[/green] — --force pour rebâtir")
            continue
        cmd = ["docker", "build", "-f", str(ctx / dockerfile),
               "--build-arg", f"SOCLE_VERSION={version}", "-t", tag, str(ctx)]
        console.print(f"[cyan]$ {' '.join(cmd)}[/cyan]")
        if dry_run:
            continue
        r = subprocess.run(cmd)
        if r.returncode != 0:
            console.print(f"[red]❌ Build échoué pour {tag}. Vérifier le checkout HUBsocle.[/red]")
            return r.returncode
    console.print(f"[green]✅ Socle {version} prêt (local, offline-first)[/green]")
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
    console.print(table)
    return 0
