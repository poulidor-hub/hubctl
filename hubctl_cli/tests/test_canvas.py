# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

from hubctl_cli import canvas
from rich.text import Text


def test_clip_cell_truncates_long_line():
    t = Text("X" * 60)
    out = canvas.clip_cell(t, 44)
    assert out.cell_len <= 44


def test_clip_cell_keeps_short_line():
    t = Text("hello")
    out = canvas.clip_cell(t, 44)
    assert out.plain == "hello"


def test_clip_cell_does_not_mutate_input():
    t = Text("Y" * 60)
    canvas.clip_cell(t, 10)
    assert len(t.plain) == 60
