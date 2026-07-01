from __future__ import annotations
import os
import re
import tempfile
from pathlib import Path

_KEY_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")


def read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        m = _KEY_RE.match(line)
        if m:
            result[m.group(1)] = m.group(2)
    return result


def backup_env(path: Path) -> Path | None:
    if not path.exists():
        return None
    bak = path.with_suffix(path.suffix + ".bak")
    bak.write_text(path.read_text())
    return bak


def restore_env(path: Path) -> bool:
    bak = path.with_suffix(path.suffix + ".bak")
    if not bak.exists():
        return False
    bak.rename(path)
    return True


def write_env(path: Path, updates: dict[str, str]) -> None:
    if not path.exists():
        lines = [f"{k}={v}" for k, v in updates.items()]
        _atomic_write(path, "\n".join(lines) + "\n")
        return

    remaining = dict(updates)
    new_lines: list[str] = []
    for line in path.read_text().splitlines():
        m = _KEY_RE.match(line)
        if m and m.group(1) in remaining:
            new_lines.append(f"{m.group(1)}={remaining.pop(m.group(1))}")
        else:
            new_lines.append(line)

    for k, v in remaining.items():
        new_lines.append(f"{k}={v}")

    _atomic_write(path, "\n".join(new_lines) + "\n")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, str(path))


def env_defaults(mode: str) -> dict[str, str]:
    if mode == "development":
        return {
            "APP_ENV": "development",
            "DOCKER_SCAN_ENABLED": "true",
            "AUTO_GENERATE_KEYS": "true",
            "ALLOW_PLAINTEXT_SECRETS": "true",
            "HUB_HTTPS_ENABLED": "false",
            "SKIP_PASSWORD_CHECK": "true",
        }
    return {
        "APP_ENV": "production",
        "DOCKER_SCAN_ENABLED": "false",
        "AUTO_GENERATE_KEYS": "false",
        "ALLOW_PLAINTEXT_SECRETS": "false",
        "HUB_HTTPS_ENABLED": "true",
        "SKIP_PASSWORD_CHECK": "false",
    }
