"""Animation : word art ASCII « Welcome to HUBctl » (~2 s)."""
import time
from rich.align import Align
from rich.text import Text

NAME = "welcome"
DESCRIPTION = "Welcome to HUBctl (ASCII art, ~2 s)"

_WELCOME = [
    r"__      __   _                            _         _   _   _   _   _     ___  _ ",
    r"\ \    / /  | |                          | |       | | | | | | | | | |   |__ \| |",
    r" \ \  / /___| | ___ ___  _ __ ___   ___  | |_ ___  | | | | | |_| | | |      ) | |",
    r"  \ \/ // _ \ |/ __/ _ \| '_ ` _ \ / _ \ | __/ _ \ | | | | |  _  | | |     / / | |",
    r"   \  /|  __/ | (_| (_) | | | | | |  __/ | || (_) || |_| | | | | |_| |_   / /_ |_|",
    r"    \/  \___|_|\___\___/|_| |_| |_|\___|  \__\___/  \___/  \_| |_/\___/  |____|(_)",
]


def _fallback_nap(secs):
    time.sleep(secs)
    return False


def play(viewport, state):
    nap = state.get("sleep", _fallback_nap)
    big = Text("\n".join(_WELCOME), style="bold cyan")
    viewport.render(Align.center(big))
    nap(2.0)
