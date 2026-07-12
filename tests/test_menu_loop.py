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


def test_build_services_reads_app_status_from_state():
    from hubctl_cli.hub_state import AppStatus
    rows = [
        AppStatus("shop", "running", "Up 2h", False),
        AppStatus("old", "legacy", "—", True),
    ]
    text = menu._build_services({"docker": True, "services": [], "app_status": rows}).plain
    assert "APPS" in text
    assert "shop" in text and "Up 2h" in text
    assert "old" in text and "legacy" in text


def test_build_services_does_not_probe(monkeypatch):
    def boom():
        raise AssertionError("get_apps_status must not be called from render")
    monkeypatch.setattr(menu.hub_state, "get_apps_status", boom)
    menu._build_services({"docker": True, "services": [], "app_status": []})


def test_build_services_scrolls_window(monkeypatch):
    from hubctl_cli.hub_state import AppStatus
    rows = [AppStatus(f"a{i}", "running", "Up", False) for i in range(6)]
    monkeypatch.setattr(menu.time, "monotonic", lambda: 4.0)
    text = menu._build_services({"docker": True, "services": [], "app_status": rows}).plain
    for name in ("a2", "a3", "a4", "a5"):
        assert name in text
    assert "a0" not in text
    assert "a1" not in text


def test_refresh_apps_status_updates_state(monkeypatch):
    from hubctl_cli.hub_state import AppStatus
    monkeypatch.setattr(menu.hub_state, "get_apps_status",
                        lambda: [AppStatus("x", "running", "Up", False)])
    state = {}
    menu._refresh_apps_status(state)
    assert state["app_status"][0].name == "x"


def test_refresh_apps_status_swallows_errors(monkeypatch):
    def boom():
        raise RuntimeError("docker down")
    monkeypatch.setattr(menu.hub_state, "get_apps_status", boom)
    state = {"app_status": "keep"}
    menu._refresh_apps_status(state)
    assert state["app_status"] == "keep"


def test_exec_remove_app_confirms_before_run(monkeypatch):
    monkeypatch.setattr(menu, "choose_app", lambda c: "shop")
    monkeypatch.setattr(menu.widgets, "confirm_by_typing", lambda *a, **k: True)
    monkeypatch.setattr(menu, "pause", lambda c: None)
    called = []
    monkeypatch.setattr(menu, "_run_action", lambda *a, **k: called.append(a[1]) or True)
    menu._exec_remove_app(mock.Mock(), {}, None, dry_run=False)
    assert called  # _run_action a été appelé


def test_exec_remove_app_aborts_when_declined(monkeypatch):
    monkeypatch.setattr(menu, "choose_app", lambda c: "shop")
    monkeypatch.setattr(menu.widgets, "confirm_by_typing", lambda *a, **k: False)
    monkeypatch.setattr(menu, "pause", lambda c: None)
    called = []
    monkeypatch.setattr(menu, "_run_action", lambda *a, **k: called.append(1))
    result = menu._exec_remove_app(mock.Mock(), {}, None, dry_run=False)
    assert called == []
    assert result is False


def test_exec_attach_app_lists_compatible_and_runs(monkeypatch):
    from hubctl_cli.hub_state import AppInfo
    from pathlib import Path
    apps = [
        AppInfo("shop", Path("."), "1", False, 0, legacy=False),
        AppInfo("old", Path("."), "1", False, 0, legacy=True),
    ]
    monkeypatch.setattr(menu.hub_state, "get_active_apps", lambda: apps)
    seen = {}

    def fake_select(canvas, message, choices, **kw):
        seen["choices"] = choices
        return "shop"
    monkeypatch.setattr(menu.widgets, "select", fake_select)
    called = []
    monkeypatch.setattr(menu, "_run_action", lambda *a, **k: called.append(a[1]) or True)
    menu._exec_attach_app(mock.Mock(), {}, None, dry_run=False)
    assert "shop" in seen["choices"]
    assert "old" not in seen["choices"]
    assert called


def test_exec_attach_app_back_does_not_run(monkeypatch):
    from hubctl_cli.hub_state import AppInfo
    from pathlib import Path
    monkeypatch.setattr(menu.hub_state, "get_active_apps",
                        lambda: [AppInfo("shop", Path("."), "1", False, 0, legacy=False)])
    monkeypatch.setattr(menu.widgets, "select", lambda *a, **k: "← Retour")
    called = []
    monkeypatch.setattr(menu, "_run_action", lambda *a, **k: called.append(1))
    result = menu._exec_attach_app(mock.Mock(), {}, None, dry_run=False)
    assert called == []
    assert result is False


def test_exec_add_app_attach_branch_dispatches(monkeypatch):
    monkeypatch.setattr(menu.widgets, "select", lambda *a, **k: "Rattacher une app existante")
    hit = []
    monkeypatch.setattr(menu, "_exec_attach_app", lambda *a, **k: hit.append(1) or True)
    menu._exec_add_app(mock.Mock(), {}, None, dry_run=False)
    assert hit == [1]


def test_view_coffre_backup_now(monkeypatch):
    from unittest import mock
    vault = mock.Mock()
    vault.is_locked = False
    vault.list_entries.return_value = ["hub"]
    vault_mgr = mock.Mock(vault=vault)
    vault_mgr.check_divergence.return_value = {}
    vault_mgr.backup_all.return_value = {"env": 1, "files": 1}
    seq = iter(["💾 Sauvegarder maintenant", "← Retour"])
    monkeypatch.setattr(menu.widgets, "select", lambda *a, **k: next(seq))
    monkeypatch.setattr(menu, "pause", lambda c: None)
    menu.view_coffre(mock.Mock(), vault_mgr)
    vault_mgr.backup_all.assert_called_once()


def test_view_coffre_export_recovery(monkeypatch):
    from unittest import mock
    from pathlib import Path
    vault = mock.Mock()
    vault.is_locked = False
    vault.list_entries.return_value = ["hub"]
    vault_mgr = mock.Mock(vault=vault)
    vault_mgr.check_divergence.return_value = {}
    vault_mgr.export_recovery.return_value = Path("/tmp/recovery/x")
    seq = iter(["⬇ Récupérer (export)", "← Retour"])
    monkeypatch.setattr(menu.widgets, "select", lambda *a, **k: next(seq))
    monkeypatch.setattr(menu, "pause", lambda c: None)
    menu.view_coffre(mock.Mock(), vault_mgr)
    vault_mgr.export_recovery.assert_called_once()


def test_view_coffre_change_passphrase(monkeypatch):
    from unittest import mock
    vault = mock.Mock()
    vault.is_locked = False
    vault.list_entries.return_value = ["hub"]
    vault_mgr = mock.Mock(vault=vault)
    vault_mgr.check_divergence.return_value = {}
    seq = iter(["🔑 Changer la passphrase", "← Retour"])
    monkeypatch.setattr(menu.widgets, "select", lambda *a, **k: next(seq))
    monkeypatch.setattr(menu.widgets, "password", lambda *a, **k: "newpass")
    monkeypatch.setattr(menu, "pause", lambda c: None)
    menu.view_coffre(mock.Mock(), vault_mgr)
    vault.change_passphrase.assert_called_once_with("newpass")
    vault.save.assert_called()
