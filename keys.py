from __future__ import annotations
import os
import select
from dataclasses import dataclass


class KeyUp:
    pass


class KeyDown:
    pass


class KeyEsc:
    pass


class KeyEnter:
    pass


class KeyBackspace:
    pass


class KeyCtrlC:
    pass


@dataclass
class KeyChar:
    char: str


Key = KeyUp | KeyDown | KeyEsc | KeyEnter | KeyBackspace | KeyCtrlC | KeyChar


def read_key(fd: int, timeout: float | None = None) -> Key | None:
    ready, _, _ = select.select([fd], [], [], timeout)
    if not ready:
        return None
    data = os.read(fd, 64)
    if not data:
        raise EOFError()
    return _decode(data)


def _decode(data: bytes) -> Key:
    if not data:
        raise ValueError("empty key data")
    if data == b"\x1b":
        return KeyEsc()
    if data in (b"\r", b"\n"):
        return KeyEnter()
    if data in (b"\x7f", b"\x08"):
        return KeyBackspace()
    if data == b"\x03":
        return KeyCtrlC()
    if data == b"\x1b[A":
        return KeyUp()
    if data == b"\x1b[B":
        return KeyDown()
    return KeyChar(data.decode("utf-8"))
