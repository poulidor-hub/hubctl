"""Animation : un oiseau s'envole et rejoint son flock, puis « HUB » apparaît."""
import time
from rich.align import Align
from rich.text import Text
from rich.live import Live

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


def play(console, state):
    width = min(console.width, 60)
    with Live(console=console, refresh_per_second=8, transient=True) as live:
        for col in range(0, width - 6, 4):
            line = " " * col + _BIRD
            sky = Text(line + "\n\n" + " " * (width - len(_FLOCK)) + _FLOCK, style="cyan")
            live.update(Align.center(sky))
            time.sleep(0.12)
    big = Text("\n".join(_HUB), style="bold magenta")
    console.print(Align.center(big))
    time.sleep(0.4)
