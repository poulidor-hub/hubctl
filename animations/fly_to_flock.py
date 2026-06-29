"""Animation : un oiseau s'envole et rejoint son flock, puis « HUB » apparaît."""
import time
from rich.align import Align
from rich.text import Text

NAME = "fly-to-flock"
DESCRIPTION = "Oiseau qui s'envole pour rejoindre son flock"

_BIRD = r"\_/\_"
_FLOCK = r"v  v  v   v  v"
_HUB = [
    " ██   ██ ██  ██ ██████ ",
    " ██   ██ ██  ██ ██   ██",
    " ███████ ██  ██ ██████ ",
    " ██   ██ ██  ██ ██   ██",
    " ██   ██ ██████ ██████ ",
]


def _fallback_nap(secs):
    time.sleep(secs)
    return False


def play(viewport, state):
    """viewport is a Canvas instance providing .render(renderable)."""
    nap = state.get("sleep", _fallback_nap)
    width = min(viewport.inner_cols, 60)

    for col in range(0, width - 6, 4):
        line = " " * col + _BIRD
        sky = Text(line + "\n\n" + " " * (width - len(_FLOCK)) + _FLOCK, style="cyan")
        viewport.render(Align.center(sky))
        if nap(0.12):
            return

    big = Text("\n".join(_HUB), style="bold magenta")
    viewport.render(Align.center(big))
    nap(0.4)
