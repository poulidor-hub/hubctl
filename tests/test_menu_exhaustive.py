"""Test des fonctions preservées du nouveau menu direct 5 touches."""
import sys
from unittest import mock

import pytest

from hubctl_cli import menu, hub_state


def _plain(obj) -> str:
    return obj.plain if hasattr(obj, 'plain') else str(obj)


def _snap_with(monkeypatch, **overrides):
    """Injecte un cache d'état (_snap) déterministe pour un test de builder.

    Les builders du dashboard consomment désormais le cache _snap (rafraîchi par
    le poll worker) au lieu d'appeler directement hub_state. On remplace donc la
    référence _snap pour ce test uniquement (restaurée par monkeypatch à la fin).
    """
    base = {
        "docker_ok": None,
        "services": None,
        "apps_count": None,
    }
    base.update(overrides)
    monkeypatch.setattr(menu, "_snap", base)
    return base


def _fake_canvas():
    c = mock.Mock()
    c.inner_rows = 28
    c.inner_cols = 98
    c._use_layout = False

    def _stream_command(runner, label=""):
        retcode = runner()
        return (retcode, False)

    c.stream_command = _stream_command
    return c


# ── Dashboard builders ──


def test_build_welcome_shows_mode(monkeypatch):
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "dev")
    monkeypatch.setattr(menu.hub_state, "get_hub_version", lambda: "v2.0")
    text = menu._build_welcome().plain
    assert "DEV" in text
    assert "v2.0" in text


def test_build_welcome_prod_mode(monkeypatch):
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "prod")
    monkeypatch.setattr(menu.hub_state, "get_hub_version", lambda: "v1.0")
    text = menu._build_welcome().plain
    assert "PROD" in text


def test_build_resources_shows_legacy_count(monkeypatch):
    # Le builder lit apps_count dans le cache _snap (plus d'appel direct à
    # hub_state.get_active_apps, qui lançait git status × N à chaque frame).
    _snap_with(monkeypatch, apps_count=(2, 1))
    text = menu._build_resources().plain
    assert "Apps : 2 (1 legacy)" in text


def test_build_resources_no_legacy_suffix_when_none(monkeypatch):
    _snap_with(monkeypatch, apps_count=(1, 0))
    text = menu._build_resources().plain
    assert "Apps : 1" in text
    assert "legacy" not in text


def test_build_services_reads_app_status(monkeypatch):
    from hubctl_cli.hub_state import AppStatus
    rows = [
        AppStatus("shop", "running", "Up 2h", False),
        AppStatus("old", "legacy", "—", True),
    ]
    menu._app_status_rows = rows
    _snap_with(monkeypatch, docker_ok=True, services=[])
    text = menu._build_services().plain
    assert "APPS" in text
    assert "shop" in text and "Up 2h" in text
    assert "old" in text and "legacy" in text


def test_build_services_docker_down(monkeypatch):
    _snap_with(monkeypatch, docker_ok=False, services=[])
    menu._app_status_rows = None
    text = menu._build_services().plain
    assert "Docker injoignable" in text


def test_build_services_scrolls_window(monkeypatch):
    from hubctl_cli.hub_state import AppStatus
    rows = [AppStatus(f"a{i}", "running", "Up", False) for i in range(6)]
    menu._app_status_rows = rows
    monkeypatch.setattr(menu.time, "monotonic", lambda: 4.0)
    _snap_with(monkeypatch, docker_ok=True, services=[])
    text = menu._build_services().plain
    for name in ("a2", "a3", "a4", "a5"):
        assert name in text
    assert "a0" not in text
    assert "a1" not in text


# ── Key hints ──


def test_build_key_hints(monkeypatch):
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "prod")
    text = menu._build_key_hints().plain
    assert "Démarrer tout" in text
    assert "Arrêter tout" in text
    assert "Mettre à jour" in text
    assert "Coffre" in text
    assert "Quitter" in text

    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "dev")
    text = menu._build_key_hints().plain
    assert "Démarrer prod" in text
    assert "Arrêter prod" in text
    assert "Démarrer dev" in text
    assert "Arrêter dev" in text
    assert "Mettre à jour" in text
    assert "Coffre" in text
    assert "Quitter" in text


# ── Layout ──


def test_install_layout_sets_four_builders():
    c = mock.Mock()
    menu.install_layout(c)
    c.set_layout.assert_called_once()
    _, kwargs = c.set_layout.call_args
    assert {"z3_builder", "z1_builder", "z4_builder", "z2_builder"} <= set(kwargs)


# ── Polling ──


def test_refresh_apps_status_updates_module_var():
    from hubctl_cli.hub_state import AppStatus
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(menu.hub_state, "get_apps_status",
                        lambda: [AppStatus("x", "running", "Up", False)])
    menu._refresh_apps_status()
    assert menu._app_status_rows[0].name == "x"


def test_refresh_apps_status_swallows_errors(monkeypatch):
    def boom():
        raise RuntimeError("docker down")
    monkeypatch.setattr(menu.hub_state, "get_apps_status", boom)
    menu._app_status_rows = "keep"
    menu._refresh_apps_status()
    assert menu._app_status_rows == "keep"


# ── Views ──


def test_status_banner(monkeypatch):
    from hubctl_cli.hub_state import ServiceInfo
    monkeypatch.setattr(menu.hub_state, "check_docker", lambda: True)
    monkeypatch.setattr(menu.hub_state, "get_hub_services",
                        lambda: [ServiceInfo("hub-api", "running", "healthy")])
    text = menu._status_banner().plain
    assert "HUB STATUS" in text
    assert "hub-api" in text


def test_status_banner_docker_down(monkeypatch):
    monkeypatch.setattr(menu.hub_state, "check_docker", lambda: False)
    monkeypatch.setattr(menu.hub_state, "get_hub_services", lambda: [])
    text = menu._status_banner().plain
    assert "Docker injoignable" in text


# ── choose_app ──


def test_choose_app_no_apps(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(menu.hub_state, "get_active_apps", lambda: [])
    monkeypatch.setattr(menu, "pause", lambda c: None)
    result = menu.choose_app(_fake_canvas(), "Test prompt")
    assert result is None


def test_choose_app_returns_on_esc(monkeypatch):
    from hubctl_cli.hub_state import AppInfo
    from pathlib import Path
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(menu.hub_state, "get_active_apps",
                        lambda: [AppInfo("shop", Path("."), "1", False, 0)])
    monkeypatch.setattr(menu.widgets, "select", lambda *a, **k: menu.widgets.ESC)
    result = menu.choose_app(_fake_canvas())
    assert result is None


def test_choose_app_returns_name(monkeypatch):
    from hubctl_cli.hub_state import AppInfo
    from pathlib import Path
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(menu.hub_state, "get_active_apps",
                        lambda: [AppInfo("shop", Path("."), "1", False, 0)])
    monkeypatch.setattr(menu.widgets, "select", lambda *a, **k: "shop")
    result = menu.choose_app(_fake_canvas())
    assert result == "shop"


# ── view_coffre ──


def test_view_coffre_backup_now(monkeypatch):
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
