"""hubctl — menu CLI de pilotage du hub POULIDOR."""
from pathlib import Path

_PARENT = Path(__file__).resolve().parent.parent
_VERSION_PATH = _PARENT / "VERSION"
__version__ = _VERSION_PATH.read_text().strip() if _VERSION_PATH.exists() else "0.0.0"
