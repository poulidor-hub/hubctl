# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Tests de la nouvelle boucle principale a 6 touches (prod/dev separation)."""
import sys
from unittest import mock

import pytest

from hubctl_cli import menu
from hubctl_cli.keys import KeyChar, KeyCtrlC, KeyEsc


def _fake_canvas():
    c = mock.Mock()
    c.inner_rows = 28
    c.inner_cols = 98

    def _stream_command(runner, label=""):
        retcode = runner()
        return (retcode, False)

    c.stream_command = _stream_command
    return c


# ── main_loop tests ──


def test_main_loop_key_1_dispatches_start_all(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "prod")
    c = _fake_canvas()
    vault = mock.Mock()
    vault.backup_all.return_value = {"env": 1, "files": 1}
    it = iter([KeyChar("1"), KeyCtrlC()])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    called = []
    monkeypatch.setattr(menu, "_run_action", lambda *a, **kw: called.append(a[1]) or True)
    with pytest.raises(KeyboardInterrupt):
        menu.main_loop(c, vault)
    assert "Démarrage de toutes les apps" in called


def test_main_loop_key_2_dispatches_stop_all(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "prod")
    c = _fake_canvas()
    vault = mock.Mock()
    vault.backup_all.return_value = {"env": 1, "files": 1}
    it = iter([KeyChar("2"), KeyCtrlC()])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    called = []
    monkeypatch.setattr(menu, "_run_action", lambda *a, **kw: called.append(a[1]) or True)
    with pytest.raises(KeyboardInterrupt):
        menu.main_loop(c, vault)
    assert "Arrêt de toutes les apps" in called


def test_main_loop_key_3_dispatches_update_all(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "prod")
    c = _fake_canvas()
    vault = mock.Mock()
    vault.backup_all.return_value = {"env": 1, "files": 1}
    it = iter([KeyChar("3"), KeyCtrlC()])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    called = []
    monkeypatch.setattr(menu, "_run_action", lambda *a, **kw: called.append(a[1]) or True)
    with pytest.raises(KeyboardInterrupt):
        menu.main_loop(c, vault)
    assert "Mise à jour complète" in called


def test_main_loop_key_4_dispatches_coffre_view(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "prod")
    c = _fake_canvas()
    vault = mock.Mock()
    vault.backup_all.return_value = {"env": 1, "files": 1}
    it = iter([KeyChar("4"), KeyCtrlC()])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    called = []
    monkeypatch.setattr(menu, "view_coffre", lambda *a, **kw: called.append("coffre"))
    with pytest.raises(KeyboardInterrupt):
        menu.main_loop(c, vault)
    assert "coffre" in called


def test_main_loop_dev_key_1_dispatches_start_prod(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "dev")
    c = _fake_canvas()
    vault = mock.Mock()
    vault.backup_all.return_value = {"env": 1, "files": 1}
    it = iter([KeyChar("1"), KeyCtrlC()])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    called = []
    monkeypatch.setattr(menu, "_run_action", lambda *a, **kw: called.append(a[1]) or True)
    with pytest.raises(KeyboardInterrupt):
        menu.main_loop(c, vault)
    assert "Démarrage prod" in called


def test_main_loop_dev_key_2_dispatches_stop_prod(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "dev")
    c = _fake_canvas()
    vault = mock.Mock()
    vault.backup_all.return_value = {"env": 1, "files": 1}
    it = iter([KeyChar("2"), KeyCtrlC()])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    called = []
    monkeypatch.setattr(menu, "_run_action", lambda *a, **kw: called.append(a[1]) or True)
    with pytest.raises(KeyboardInterrupt):
        menu.main_loop(c, vault)
    assert "Arrêt prod" in called


def test_main_loop_dev_key_3_dispatches_start_dev(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "dev")
    c = _fake_canvas()
    vault = mock.Mock()
    vault.backup_all.return_value = {"env": 1, "files": 1}
    it = iter([KeyChar("3"), KeyCtrlC()])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    called = []
    monkeypatch.setattr(menu, "_run_action", lambda *a, **kw: called.append(a[1]) or True)
    with pytest.raises(KeyboardInterrupt):
        menu.main_loop(c, vault)
    assert "Démarrage dev" in called


def test_main_loop_dev_key_4_dispatches_stop_dev(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "dev")
    c = _fake_canvas()
    vault = mock.Mock()
    vault.backup_all.return_value = {"env": 1, "files": 1}
    it = iter([KeyChar("4"), KeyCtrlC()])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    called = []
    monkeypatch.setattr(menu, "_run_action", lambda *a, **kw: called.append(a[1]) or True)
    with pytest.raises(KeyboardInterrupt):
        menu.main_loop(c, vault)
    assert "Arrêt dev" in called


def test_main_loop_dev_key_5_dispatches_update(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "dev")
    c = _fake_canvas()
    vault = mock.Mock()
    vault.backup_all.return_value = {"env": 1, "files": 1}
    it = iter([KeyChar("5"), KeyCtrlC()])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    called = []
    monkeypatch.setattr(menu, "_run_action", lambda *a, **kw: called.append(a[1]) or True)
    with pytest.raises(KeyboardInterrupt):
        menu.main_loop(c, vault)
    assert "Mise à jour complète" in called


def test_main_loop_dev_key_6_dispatches_coffre_view(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "dev")
    c = _fake_canvas()
    vault = mock.Mock()
    vault.backup_all.return_value = {"env": 1, "files": 1}
    it = iter([KeyChar("6"), KeyCtrlC()])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    called = []
    monkeypatch.setattr(menu, "view_coffre", lambda *a, **kw: called.append("coffre"))
    with pytest.raises(KeyboardInterrupt):
        menu.main_loop(c, vault)
    assert "coffre" in called


def test_main_loop_key_0_quits_when_confirmed(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "prod")
    c = _fake_canvas()
    vault = mock.Mock()
    vault.backup_all.return_value = {"env": 1, "files": 1}
    it = iter([KeyChar("0")])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    monkeypatch.setattr(menu.widgets, "confirm", lambda c, m: True)
    menu.main_loop(c, vault)


def test_main_loop_key_0_stays_when_not_confirmed(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "prod")
    c = _fake_canvas()
    vault = mock.Mock()
    vault.backup_all.return_value = {"env": 1, "files": 1}
    it = iter([KeyChar("0"), KeyCtrlC()])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    monkeypatch.setattr(menu.widgets, "confirm", lambda c, m: False)
    with pytest.raises(KeyboardInterrupt):
        menu.main_loop(c, vault)


def test_main_loop_esc_quits_when_confirmed(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "prod")
    c = _fake_canvas()
    vault = mock.Mock()
    vault.backup_all.return_value = {"env": 1, "files": 1}
    it = iter([KeyEsc()])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    monkeypatch.setattr(menu.widgets, "confirm", lambda c, m: True)
    menu.main_loop(c, vault)


def test_main_loop_ctrlc_raises(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "prod")
    c = _fake_canvas()
    vault = mock.Mock()
    vault.backup_all.return_value = {"env": 1, "files": 1}
    it = iter([KeyCtrlC()])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    with pytest.raises(KeyboardInterrupt):
        menu.main_loop(c, vault)


# ── Coffre submenu tests ──


def test_coffre_submenu_backup(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    c = _fake_canvas()
    vault_mgr = mock.Mock()
    vault_mgr.backup_all.return_value = {"env": 1, "files": 1}
    it = iter(["Sauvegarder (backup)"])
    monkeypatch.setattr(menu.widgets, "select", lambda *a, **kw: next(it))
    called = []
    monkeypatch.setattr(menu, "_run_action", lambda *a, **kw: called.append(a[1]) or True)
    menu._coffre_submenu(c, vault_mgr)
    assert "Sauvegarde du coffre" in called


def test_coffre_submenu_restore(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    c = _fake_canvas()
    vault_mgr = mock.Mock()
    vault_mgr.vault = mock.Mock()
    it = iter(["Restaurer (restore)"])
    monkeypatch.setattr(menu.widgets, "select", lambda *a, **kw: next(it))
    called = []
    monkeypatch.setattr(menu, "_run_action", lambda *a, **kw: called.append(a[1]) or True)
    menu._coffre_submenu(c, vault_mgr)
    assert "Restauration depuis le coffre" in called


def test_coffre_submenu_status_diverged(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    c = _fake_canvas()
    vault_mgr = mock.Mock()
    vault_mgr.check_divergence.return_value = {"app:foo": "differs"}
    it = iter(["Statut (status)"])
    monkeypatch.setattr(menu.widgets, "select", lambda *a, **kw: next(it))
    with mock.patch.object(menu, "pause") as mock_pause:
        menu._coffre_submenu(c, vault_mgr)
    assert c.render.called


def test_coffre_submenu_status_clean(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    c = _fake_canvas()
    vault_mgr = mock.Mock()
    vault_mgr.check_divergence.return_value = {}
    it = iter(["Statut (status)"])
    monkeypatch.setattr(menu.widgets, "select", lambda *a, **kw: next(it))
    with mock.patch.object(menu, "pause") as mock_pause:
        menu._coffre_submenu(c, vault_mgr)
    assert c.render.called


def test_coffre_submenu_retour(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    c = _fake_canvas()
    vault_mgr = mock.Mock()
    it = iter(["← Retour"])
    monkeypatch.setattr(menu.widgets, "select", lambda *a, **kw: next(it))
    called = []
    monkeypatch.setattr(menu, "_run_action", lambda *a, **kw: called.append(1))
    menu._coffre_submenu(c, vault_mgr)
    assert called == []


def test_coffre_submenu_esc(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    c = _fake_canvas()
    vault_mgr = mock.Mock()
    it = iter([menu.widgets.ESC])
    monkeypatch.setattr(menu.widgets, "select", lambda *a, **kw: next(it))
    called = []
    monkeypatch.setattr(menu, "_run_action", lambda *a, **kw: called.append(1))
    menu._coffre_submenu(c, vault_mgr)
    assert called == []


# ── run_action ──


def test_run_action_returns_true_on_success():
    c = _fake_canvas()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(menu, "pause", lambda *a, **kw: None)
    result = menu._run_action(c, "test", lambda: None)
    assert result is True


def test_run_action_returns_false_on_failure():
    c = _fake_canvas()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(menu, "pause", lambda *a, **kw: None)
    result = menu._run_action(c, "test", lambda: 1 / 0)
    assert result is False
