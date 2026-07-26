"""Tests de la nouvelle boucle principale à 5 touches."""
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


def test_main_loop_key_4_dispatches_backup(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    c = _fake_canvas()
    vault = mock.Mock()
    vault.backup_all.return_value = {"env": 1, "files": 1}
    it = iter([KeyChar("4"), KeyCtrlC()])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    called = []
    monkeypatch.setattr(menu, "_run_action", lambda *a, **kw: called.append(a[1]) or True)
    with pytest.raises(KeyboardInterrupt):
        menu.main_loop(c, vault)
    assert called
    assert "Sauvegarde" in called[0]


def test_main_loop_key_5_dispatches_atelier_with_app(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    c = _fake_canvas()
    vault = mock.Mock()
    vault.backup_all.return_value = {"env": 1, "files": 1}
    it = iter([KeyChar("5"), KeyCtrlC()])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    monkeypatch.setattr(menu, "choose_app", lambda c, prompt: "myapp")
    called = []
    monkeypatch.setattr(menu, "_run_action", lambda *a, **kw: called.append(a[1]) or True)
    with pytest.raises(KeyboardInterrupt):
        menu.main_loop(c, vault)
    assert any("Atelier" in x for x in called)


def test_main_loop_key_5_skips_when_no_app(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    c = _fake_canvas()
    vault = mock.Mock()
    vault.backup_all.return_value = {"env": 1, "files": 1}
    it = iter([KeyChar("5"), KeyCtrlC()])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    monkeypatch.setattr(menu, "choose_app", lambda c, prompt: None)
    called = []
    monkeypatch.setattr(menu, "_run_action", lambda *a, **kw: called.append(1))
    with pytest.raises(KeyboardInterrupt):
        menu.main_loop(c, vault)
    assert called == []


def test_main_loop_key_0_quits_when_confirmed(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    c = _fake_canvas()
    vault = mock.Mock()
    vault.backup_all.return_value = {"env": 1, "files": 1}
    it = iter([KeyChar("0")])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    monkeypatch.setattr(menu.widgets, "confirm", lambda c, m: True)
    menu.main_loop(c, vault)


def test_main_loop_key_0_stays_when_not_confirmed(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
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
    c = _fake_canvas()
    vault = mock.Mock()
    vault.backup_all.return_value = {"env": 1, "files": 1}
    it = iter([KeyEsc()])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    monkeypatch.setattr(menu.widgets, "confirm", lambda c, m: True)
    menu.main_loop(c, vault)


def test_main_loop_ctrlc_raises(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    c = _fake_canvas()
    vault = mock.Mock()
    vault.backup_all.return_value = {"env": 1, "files": 1}
    it = iter([KeyCtrlC()])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    with pytest.raises(KeyboardInterrupt):
        menu.main_loop(c, vault)


# ── run_action ──


def test_run_action_returns_true_on_success():
    c = _fake_canvas()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(menu, "pause", lambda c: None)
    result = menu._run_action(c, "test", lambda: None)
    assert result is True


def test_run_action_returns_false_on_failure():
    c = _fake_canvas()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(menu, "pause", lambda c: None)
    result = menu._run_action(c, "test", lambda: 1 / 0)
    assert result is False
