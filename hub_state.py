from __future__ import annotations
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

PARC_ROOT = Path(__file__).resolve().parent.parent
HUB_DIR = PARC_ROOT / "HUB_base"
APPS_DIR = PARC_ROOT / "APPS"


@dataclass
class AppInfo:
    name: str
    path: Path
    version: str
    git_dirty: bool
    size_bytes: int


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
        ))
    return apps


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
