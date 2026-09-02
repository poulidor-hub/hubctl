from __future__ import annotations
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

def _find_parc_root() -> Path:
    """Trouve la racine du parc POULIDOR en cherchant le ``manifest.json``.

    On remonte depuis le fichier courant (qui peut être dans un sous-module
    via symlink) jusqu'à trouver la vraie racine du parc. Cela évite que
    ``Path(__file__).resolve()`` suive un symlink ``hubctl_cli → hubctl_src/hubctl_cli``
    et donne un PARC_ROOT incorrect.

    On ne vérifie plus ``APPS/`` : sur un fresh clone sans apps déployées,
    le dossier ``APPS/`` n'existe pas encore (Git ne tracke pas les dossiers
    vides), ce qui faisait échouer la détection et retombait sur le fallback
    pointant vers ``hubctl_src/`` au lieu de la racine POULIDOR.
    """
    current = Path(__file__).resolve()
    for _ in range(10):  # sécurité : 10 niveaux max
        if (current / "manifest.json").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    # Fallback : comportement legacy (cassé avec les symlinks mais au moins
    # on ne crashe pas).
    return Path(__file__).resolve().parent.parent


PARC_ROOT = _find_parc_root()
HUB_DIR = PARC_ROOT / "HUB_base"
APPS_DIR = PARC_ROOT / "APPS"

_mode: str = "prod"


def _runtime_dir(name: str, parc_root: Path | None = None, pkg_dir: Path | None = None) -> Path:
    """Dossier de données runtime de hubctl (vault/, backups/, recovery/…).

    Emplacement canonique : ``<paquet>/<name>/`` — données gitignorées du
    submodule. Repli legacy : ``<PARC_ROOT>/hubctl_cli/<name>/`` pour les
    installations où ``hubctl_cli/`` est un vrai dossier racine (le symlink
    racine n'existe plus depuis la restructuration). Si aucun des deux
    n'existe, le dossier du paquet est retourné (créé au premier besoin).
    """
    root = parc_root if parc_root is not None else PARC_ROOT
    base = pkg_dir if pkg_dir is not None else Path(__file__).resolve().parent
    pkg = base / name
    if pkg.is_dir():
        return pkg
    legacy = root / "hubctl_cli" / name
    if legacy.is_dir():
        return legacy
    return pkg


def vault_dir(parc_root: Path | None = None, pkg_dir: Path | None = None) -> Path:
    """Dossier du coffre chiffré (``parc.vault.enc`` + clé ``parc.vault.key``)."""
    return _runtime_dir("vault", parc_root, pkg_dir)


def vault_path() -> Path:
    """Chemin du coffre chiffré, toutes commandes confondues."""
    return vault_dir() / "parc.vault.enc"


def backups_dir(parc_root: Path | None = None, pkg_dir: Path | None = None) -> Path:
    """Dossier des archives tar.gz et snapshots DB avant suppression/update."""
    return _runtime_dir("backups", parc_root, pkg_dir)


def recovery_dir(parc_root: Path | None = None, pkg_dir: Path | None = None) -> Path:
    """Dossier des exports de recovery du coffre."""
    return _runtime_dir("recovery", parc_root, pkg_dir)


def set_mode(mode: str) -> None:
    global _mode
    _mode = mode


def get_mode() -> str:
    return _mode


def auto_detect_mode() -> str:
    """Détecte le mode automatiquement : DEV si au moins une app dans APPS/
    a un fichier ``.workshop.env`` (atelier Docker Compose), PROD sinon.
    """
    apps = APPS_DIR
    if apps.exists():
        for d in apps.iterdir():
            if d.is_dir() and (d / ".workshop.env").exists():
                return "dev"
    return "prod"


def get_active_apps() -> list[AppInfo]:
    if _mode == "dev":
        return get_workshop_apps()
    return get_apps()


def active_apps_dir() -> Path:
    """Répertoire des apps actif selon le mode.
    
    En mode dev comme en prod, les ateliers sont lancés depuis APPS/ via
    docker compose -p X-ws. On retourne donc toujours APPS_DIR.
    """
    return APPS_DIR


@dataclass
class AppInfo:
    name: str
    path: Path
    version: str
    git_dirty: bool
    size_bytes: int
    legacy: bool = False


@dataclass
class ServiceInfo:
    name: str
    state: str
    health: str


@dataclass
class AppStatus:
    name: str
    status: str
    uptime: str
    legacy: bool


@dataclass
class DiskInfo:
    total: int
    used: int
    free: int
    percent_free: float


def check_docker() -> bool:
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10
        ).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _read_version(path: Path) -> str:
    f = path / "VERSION"
    return f.read_text().strip() if f.exists() else "?"


def _git_dirty(path: Path) -> bool:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        return bool(out.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file() and not p.is_symlink():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _is_legacy(path: Path) -> bool:
    return not ((path / "Makefile").exists() and (path / "docker-compose.yml").exists())


def get_apps() -> list[AppInfo]:
    apps_dir = PARC_ROOT / "APPS"
    if not apps_dir.exists():
        return []
    apps = []
    for d in sorted(apps_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        apps.append(AppInfo(
            name=d.name, path=d, version=_read_version(d),
            git_dirty=_git_dirty(d), size_bytes=_dir_size(d),
            legacy=_is_legacy(d),
        ))
    return apps


def get_workshop_apps() -> list[AppInfo]:
    """Liste les apps de prod qui ont un atelier (.workshop.env).

    Les ateliers tournent via ``docker compose -p X-ws`` depuis ``APPS/X/``.
    On détecte leur existence par la présence du fichier ``APPS/X/.workshop.env``.
    Le ``name`` retourné est le slug de base (ex: ``canari``), pas le
    slug atelier (``canari-ws``).
    """
    apps_dir = APPS_DIR
    if not apps_dir.exists():
        return []
    apps = []
    for d in sorted(apps_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if not (d / ".workshop.env").exists():
            continue
        apps.append(AppInfo(
            name=d.name, path=d, version=_read_version(d),
            git_dirty=_git_dirty(d), size_bytes=_dir_size(d),
            legacy=_is_legacy(d),
        ))
    return apps


def _norm(s: str) -> str:
    """Normalise un nom pour la correspondance app↔conteneur, comme Compose v2 :
    minuscules + tout caractère non alphanumérique remplacé par '-'.
    L'ancienne version supprimait les séparateurs → collisions (ex. 'exemple-app' et
    'exemple' donnaient 'exemple' → faux statut app)."""
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower())


def _docker_api_containers() -> dict[str, tuple[str, str]]:
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--format", "json"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return {}
    result: dict[str, tuple[str, str]] = {}
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        labels = item.get("Labels", "")
        # Docker peut renvoyer Labels soit en string comma-separated, soit en dict
        # (selon la version/le format). Gérer les deux formes.
        label_map: dict[str, str] = {}
        if isinstance(labels, dict):
            label_map = {str(k).strip(): str(v).strip() for k, v in labels.items()}
        elif labels:
            for kv in str(labels).split(","):
                if "=" in kv:
                    k, _, v = kv.partition("=")
                    label_map[k.strip()] = v.strip()
        if label_map.get("com.docker.compose.service") != "api":
            continue
        project = label_map.get("com.docker.compose.project", "")
        if project:
            result[_norm(project)] = (item.get("State", ""), item.get("Status", ""))
    return result


def get_apps_status() -> list[AppStatus]:
    """Statut des apps. En mode dev, affiche les ateliers (slug -ws) ;
    en mode prod, affiche les apps de prod."""
    active_dir = APPS_DIR
    if not active_dir.exists():
        return []
    containers = _docker_api_containers()
    rows: list[AppStatus] = []
    for d in sorted(active_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if _is_legacy(d):
            rows.append(AppStatus(d.name, "legacy", "—", True))
            continue
        # En mode dev, on regarde le conteneur atelier ({slug}-ws) ;
        # en mode prod, le conteneur prod ({slug}).
        display_name = d.name
        lookup_name = d.name
        if _mode == "dev":
            if not (d / ".workshop.env").exists():
                continue  # pas d'atelier pour cette app → masquer en dev
            lookup_name = f"{d.name}-ws"
            display_name = f"{d.name}-ws"
        c = containers.get(_norm(lookup_name))
        if c is None:
            rows.append(AppStatus(display_name, "stopped", "—", False))
            continue
        c_state, c_status = c
        if c_state == "running":
            rows.append(AppStatus(display_name, "running", c_status or "Up", False))
        else:
            rows.append(AppStatus(display_name, "stopped", c_status or "—", False))
    return rows


def get_hub_services() -> list[ServiceInfo]:
    compose = PARC_ROOT / "HUB_base" / "docker-compose.yml"
    if not compose.exists():
        return []
    try:
        out = subprocess.run(
            ["docker", "compose", "-f", str(compose), "ps", "--format", "json"],
            capture_output=True, text=True, timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    services = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        services.append(ServiceInfo(
            name=item.get("Service", item.get("Name", "?")),
            state=item.get("State", "?"),
            health=item.get("Health", "") or "—",
        ))
    return services


def get_disk_usage() -> DiskInfo:
    u = shutil.disk_usage(PARC_ROOT)
    percent_free = round(u.free / u.total * 100, 1) if u.total else 0.0
    return DiskInfo(total=u.total, used=u.used, free=u.free, percent_free=percent_free)


def get_hub_version() -> str:
    return _read_version(PARC_ROOT / "HUB_base")
