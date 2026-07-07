from __future__ import annotations
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

PARC_ROOT = Path(__file__).resolve().parent.parent
HUB_DIR = PARC_ROOT / "HUB_base"
APPS_DIR = PARC_ROOT / "APPS"
WORKSHOP_DIR = PARC_ROOT / "APP_Workshop"

_mode: str = "prod"


def set_mode(mode: str) -> None:
    global _mode
    _mode = mode


def get_mode() -> str:
    return _mode


def auto_detect_mode() -> str:
    """Détecte le mode automatiquement : DEV si APP_Workshop/ contient au
    moins une app avec docker-compose.yml, PROD sinon.
    """
    ws = WORKSHOP_DIR
    if ws.exists():
        for d in ws.iterdir():
            if d.is_dir() and (d / "docker-compose.yml").exists():
                return "dev"
    return "prod"


def get_active_apps() -> list[AppInfo]:
    if _mode == "dev":
        return get_workshop_apps()
    return get_apps()


def active_apps_dir() -> Path:
    """Répertoire des apps actif selon le mode : APP_Workshop en dev, APPS en prod."""
    return WORKSHOP_DIR if _mode == "dev" else APPS_DIR


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
    ws_dir = PARC_ROOT / "APP_Workshop"
    if not ws_dir.exists():
        return []
    apps = []
    for d in sorted(ws_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        apps.append(AppInfo(
            name=d.name, path=d, version=_read_version(d),
            git_dirty=_git_dirty(d), size_bytes=_dir_size(d),
            legacy=_is_legacy(d),
        ))
    return apps


def _norm(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


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
        labels = item.get("Labels", "") or ""
        label_map: dict[str, str] = {}
        for kv in labels.split(","):
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
    active_dir = WORKSHOP_DIR if _mode == "dev" else APPS_DIR
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
        c = containers.get(_norm(d.name))
        if c is None:
            rows.append(AppStatus(d.name, "stopped", "—", False))
            continue
        c_state, c_status = c
        if c_state == "running":
            rows.append(AppStatus(d.name, "running", c_status or "Up", False))
        else:
            rows.append(AppStatus(d.name, "stopped", c_status or "—", False))
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
