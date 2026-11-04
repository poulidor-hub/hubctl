# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

from hubctl_cli import animation


def test_steps_have_expected_keys():
    assert len(animation._STEPS) >= 5
    for label, fn in animation._STEPS:
        key, value = fn()
        assert isinstance(key, str)
        assert isinstance(label, str)


def test_preload_with_bar_returns_dict():
    from rich.console import Console
    c = Console(force_terminal=True, width=100)
    state = animation.preload_with_bar(c, animate=False)
    assert isinstance(state, dict)
    assert len(state) > 0


def test_preload_with_bar_populates_state_dict():
    from rich.console import Console
    c = Console(force_terminal=True, width=100)
    existing = {"custom": 42}
    result = animation.preload_with_bar(c, animate=False, state_dict=existing)
    assert result is existing
    assert existing["custom"] == 42
    assert len(existing) > 1
