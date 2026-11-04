# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

from __future__ import annotations
import os
import re
import tempfile
from pathlib import Path

# Accepte : `KEY=val`, `export KEY=val` (convention shell), `KEY = val` (espaces
# autour de '='). Sans cela, ces lignes étaient ignorées silencieusement → le
# secret correspondant n'était PAS sauvegardé dans le coffre.
_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$")


def read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        m = _KEY_RE.match(line)
        if m:
            result[m.group(1)] = m.group(2)
    return result


def write_env(path: Path, updates: dict[str, str]) -> None:
    # Valider les valeurs : un '\n' dans une valeur casse le fichier .env
    # (la ligne suivante serait interprétée comme une nouvelle variable).
    for k, v in updates.items():
        if "\n" in v:
            raise ValueError(
                f"La valeur de {k} contient un saut de ligne — "
                f"les valeurs multilignes ne sont pas supportées dans les .env"
            )
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
        os.close(fd)
        os.replace(tmp, str(path))
        # Durabilité : forcer le fsync du répertoire parent pour garantir que
        # le rename est persisté en cas de crash système (atomicité préservée).
        try:
            dirfd = os.open(str(path.parent), os.O_RDONLY)
            os.fsync(dirfd)
            os.close(dirfd)
        except OSError:
            pass
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        Path(tmp).unlink(missing_ok=True)
        raise


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
