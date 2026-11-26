# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Tests de la nouvelle boucle principale a 6 touches (prod/dev separation)."""
import sys
from unittest import mock

import pytest

from hubctl_cli import menu
from hubctl_cli.keys import KeyChar, KeyCtrlC, KeyEsc


@pytest.fixture(autouse=True)
def _stop_poll_worker():
    """Hubctl#2 : chaque main_loop démarre un poll worker daemon — sans arrêt
    en teardown, ces threads résiduels sous-traitent docker/git en boucle et
    leurs subprocess.run polluent les fenêtres des tests suivants de la
    suite (échecs intermittents de test_park_checks)."""
    yield
    menu.stop_poll_worker()


def _fake_canvas():
    c = mock.Mock()
    c.inner_rows = 28
    c.inner_cols = 98

    def _stream_command(runner, label=""):
        retcode = runner()
        return (retcode, False)

    c.stream_command = _stream_command
    return c


# ── Poll worker : arrêt propre (flake Hubctl#2) ──


def test_poll_worker_stops_on_stop_api(monkeypatch):
    """Hubctl#2 : main_loop démarre un poll worker daemon dont la boucle était
    « while True: sleep(3) » — inarrêtable. Chaque test passant par main_loop
    accumulait un thread vivant qui sous-traite docker/git en boucle pour
    toujours : 14 _state_poll_worker étaient encore actifs pendant
    test_park_checks, leurs subprocess.run écrasaient le spy global des tests
    de timeout → échecs intermittents en suite complète. Contrat : les workers
    sont enregistrés (_start_poll_worker) et stop_poll_worker() les arrête."""
    import threading

    monkeypatch.setattr(menu, "_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(menu, "_refresh_state_snapshot", lambda: None)
    monkeypatch.setattr(menu, "_refresh_apps_status", lambda: None)

    t = menu._start_poll_worker(None)  # canvas inutilisé par les refreshs

    assert isinstance(t, threading.Thread) and t.is_alive()
    menu.stop_poll_worker()
    t.join(timeout=2)
    assert not t.is_alive(), "le poll worker doit s'arrêter sur stop_poll_worker()"


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


def test_run_action_returns_true_on_success(monkeypatch):
    """Fixture monkeypatch (auto-undo) — Hubctl#3 : l'ancien
    pytest.MonkeyPatch() manuel sans undo laissait menu.pause no-op pour tout
    le reste du process (flake test_pause_waits_for_enter)."""
    c = _fake_canvas()
    monkeypatch.setattr(menu, "pause", lambda *a, **kw: None)
    result = menu._run_action(c, "test", lambda: None)
    assert result is True


def test_run_action_returns_false_on_failure(monkeypatch):
    c = _fake_canvas()
    monkeypatch.setattr(menu, "pause", lambda *a, **kw: None)
    result = menu._run_action(c, "test", lambda: 1 / 0)
    assert result is False


def test_menu_pause_not_polluted():
    """Hubctl#3 : les tests run_action ci-dessus créaient pytest.MonkeyPatch()
    à la main, SANS undo — menu.pause restait remplacé par un no-op pour tout
    le reste du process : test_pause_waits_for_enter (test_actions) voyait
    alors render.call_count == 0 alors que pause() rend inconditionnellement.
    Garde : après la suite, menu.pause doit être la fonction réelle."""
    assert getattr(menu.pause, "__name__", "") == "pause", \
        "menu.pause a été remplacé et jamais restauré (MonkeyPatch manuel sans undo)"


# ── Installer une application (URL git) — touche 5 du menu PROD ──


def test_build_key_hints_prod_mentionne_l_installation_d_app(monkeypatch):
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "prod")
    assert "Installer une application" in menu._build_key_hints().plain


def test_main_loop_prod_key_5_dispatches_install(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "prod")
    c = _fake_canvas()
    vault = mock.Mock()
    it = iter([KeyChar("5"), KeyCtrlC()])
    monkeypatch.setattr(menu.keys, "read_key", lambda fd, timeout=None: next(it, None))
    called = []
    monkeypatch.setattr(menu, "view_install_app", lambda canvas: called.append(canvas))
    with pytest.raises(KeyboardInterrupt):
        menu.main_loop(c, vault)
    assert called == [c]


def _menu_install(monkeypatch, *, url="  git@github.com:exemple/Mon-App.git ", mdp="admin-saisi",
                  confirmation=True):
    c = _fake_canvas()
    monkeypatch.setattr(menu, "pause", lambda *a, **kw: None)
    monkeypatch.setattr(menu.widgets, "text", lambda canvas, msg: url)
    monkeypatch.setattr(menu.widgets, "password", lambda canvas, msg: mdp)
    confirm = mock.Mock(return_value=confirmation)
    monkeypatch.setattr(menu.widgets, "confirm", confirm)
    return c, confirm


def test_view_install_app_demande_url_mdp_confirmation_puis_meme_chemin(monkeypatch):
    """URL → mot de passe admin (masqué, facultatif) → confirmation → le même
    chemin que la CLI (`actions.action_app_install`), sans re-confirmation."""
    c, confirm = _menu_install(monkeypatch)
    with mock.patch.object(menu.actions, "action_app_install", return_value=0) as run:
        assert menu.view_install_app(c) is True
    run.assert_called_once()
    args, kwargs = run.call_args.args, run.call_args.kwargs
    assert args[1] == ["git@github.com:exemple/Mon-App.git"], "l'URL est nettoyée"
    assert kwargs["yes"] is True, "le menu a déjà confirmé (widget)"
    assert kwargs["prompt_password"]() == "admin-saisi"
    assert "Mon-App" in confirm.call_args.args[1]


def test_view_install_app_mdp_vide_laisse_en_attente(monkeypatch):
    c, _confirm = _menu_install(monkeypatch, mdp="")
    with mock.patch.object(menu.actions, "action_app_install", return_value=0) as run:
        menu.view_install_app(c)
    assert run.call_args.kwargs["prompt_password"] is None


def test_view_install_app_esc_sur_url_n_installe_rien(monkeypatch):
    c, _confirm = _menu_install(monkeypatch, url=menu.widgets.ESC)
    with mock.patch.object(menu.actions, "action_app_install") as run:
        menu.view_install_app(c)
    run.assert_not_called()


def test_view_install_app_url_vide_n_installe_rien(monkeypatch):
    c, _confirm = _menu_install(monkeypatch, url="   ")
    with mock.patch.object(menu.actions, "action_app_install") as run:
        menu.view_install_app(c)
    run.assert_not_called()


def test_view_install_app_confirmation_refusee_n_installe_rien(monkeypatch):
    c, _confirm = _menu_install(monkeypatch, confirmation=False)
    with mock.patch.object(menu.actions, "action_app_install") as run:
        menu.view_install_app(c)
    run.assert_not_called()


def test_view_install_app_rc_ko_signale_l_echec(monkeypatch):
    c, _confirm = _menu_install(monkeypatch)
    pauses = []
    monkeypatch.setattr(menu, "pause", lambda canvas, msg=None: pauses.append(msg))
    with mock.patch.object(menu.actions, "action_app_install", return_value=1):
        assert menu.view_install_app(c) is False
    assert pauses and "✗" in pauses[-1].plain


def test_view_install_app_exception_signale_l_echec(monkeypatch, capsys):
    c, _confirm = _menu_install(monkeypatch)
    with mock.patch.object(menu.actions, "action_app_install", side_effect=RuntimeError("docker down")):
        assert menu.view_install_app(c) is False
    assert "docker down" in capsys.readouterr().err
