# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""hubctl — menu CLI de pilotage du hub POULIDOR."""
from pathlib import Path

_PARENT = Path(__file__).resolve().parent.parent
_VERSION_PATH = _PARENT / "VERSION"
__version__ = _VERSION_PATH.read_text().strip() if _VERSION_PATH.exists() else "0.0.0"
