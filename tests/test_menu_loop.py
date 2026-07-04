import sys
from unittest import mock
import pytest
from hubctl_cli import menu
from hubctl_cli.keys import KeyChar, KeyEnter, KeyCtrlC


def _fake_canvas():
    c = mock.Mock()
    c.inner_rows = 28
    c.inner_cols = 98
    return c


def test_main_loop_enter_dispatches_highlighted(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    c = _fake_canvas()
    it = iter([KeyChar("3"), KeyEnter(), KeyCtrlC()])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    dispatched = []
    monkeypatch.setattr(menu, "_dispatch",
                        lambda buf, *a, **k: dispatched.append(buf) or False)
    with pytest.raises(KeyboardInterrupt):
        menu.main_loop(c, {}, None, dry_run=True)
    assert dispatched == ["3"]


def test_main_loop_digit_only_does_not_dispatch(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    c = _fake_canvas()
    it = iter([KeyChar("3"), KeyCtrlC()])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    dispatched = []
    monkeypatch.setattr(menu, "_dispatch",
                        lambda buf, *a, **k: dispatched.append(buf) or False)
    with pytest.raises(KeyboardInterrupt):
        menu.main_loop(c, {}, None, dry_run=True)
    assert dispatched == []


def test_install_layout_sets_four_builders():
    c = mock.Mock()
    menu.install_layout(c, {"apps": []})
    c.set_layout.assert_called_once()
    _, kwargs = c.set_layout.call_args
    assert {"z3_builder", "z1_builder", "z4_builder", "z2_builder"} <= set(kwargs)


def test_build_resources_shows_legacy_count():
    from hubctl_cli.hub_state import AppInfo
    from pathlib import Path
    apps = [
        AppInfo("a", Path("."), "1", False, 0, legacy=False),
        AppInfo("b", Path("."), "1", False, 0, legacy=True),
    ]
    text = menu._build_resources({"apps": apps}).plain
    assert "Apps : 2 (1 legacy)" in text


def test_build_resources_no_legacy_suffix_when_none():
    from hubctl_cli.hub_state import AppInfo
    from pathlib import Path
    apps = [AppInfo("a", Path("."), "1", False, 0, legacy=False)]
    text = menu._build_resources({"apps": apps}).plain
    assert "Apps : 1" in text
    assert "legacy" not in text


def test_build_services_lists_apps_with_api_status(monkeypatch):
    monkeypatch.setattr(menu.hub_state, "get_apps_api_status",
                        lambda: [("shop", "running"), ("old", "legacy")])
    text = menu._build_services({"docker": True, "services": []}).plain
    assert "APPS" in text
    assert "shop" in text and "RUNNING" in text
    assert "old" in text and "LEGACY" in text
