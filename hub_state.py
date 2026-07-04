from __future__ import annotations
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from hubctl_cli import env_manager

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


def get_active_apps() -> list[AppInfo]:
    if _mode == "dev":
        return get_workshop_apps()
    return get_apps()


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


_api_status_cache: dict[str, tuple[str, float]] = {}
_API_STATUS_TTL = 5.0


def _probe_api(port: str) -> str:
    url = f"http://localhost:{port}/api/health"
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "1", "-o", "/dev/null", "-w", "%{http_code}", url],
            capture_output=True, text=True, timeout=3,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
        return "stopped"
    code = (r.stdout or "").strip()
    if code.startswith("2"):
        return "running"
    if code == "000" or not code:
        return "stopped"
    return "warning"


def app_api_status(app: AppInfo) -> str:
    if app.legacy:
        return "legacy"
    now = time.monotonic()
    cached = _api_status_cache.get(app.name)
    if cached and now - cached[1] < _API_STATUS_TTL:
        return cached[0]
    env_path = app.path / ".env"
    env = env_manager.read_env(env_path) if env_path.exists() else {}
    port = env.get("API_PORT", "8000")
    status = _probe_api(port)
    _api_status_cache[app.name] = (status, now)
    return status


def get_apps_api_status() -> list[tuple[str, str]]:
    return [(a.name, app_api_status(a)) for a in get_active_apps()]


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
