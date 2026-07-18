import sys
from unittest import mock
import pytest
from hubctl_cli import menu
from hubctl_cli.keys import KeyChar, KeyEnter, KeyCtrlC


def _fake_canvas():
    c = mock.Mock()
    c.inner_rows = 28
    c.inner_cols = 98
    def _stream_command(runner, label=""):
        retcode = runner()
        return (retcode, False)
    c.stream_command = _stream_command
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


def test_workshop_menu_hides_arrow_if_already_in_workshop(monkeypatch):
    """Une app X dont l'atelier X-ws existe n'affiche pas [→] mais une ligne ⏸."""
    from hubctl_cli.hub_state import AppInfo
    from pathlib import Path

    prod_apps = [AppInfo("myapp", Path("."), "1", False, 0, legacy=False)]
    ws_apps = [AppInfo("myapp-ws", Path("."), "1", False, 0, legacy=False)]
    monkeypatch.setattr(menu.hub_state, "get_apps", lambda: prod_apps)
    monkeypatch.setattr(menu.hub_state, "get_workshop_apps", lambda: ws_apps)

    captured = []

    def fake_select(canvas, message, choices, **kw):
        captured.append(choices)
        return menu.widgets.ESC

    monkeypatch.setattr(menu.widgets, "select", fake_select)

    menu._workshop_menu(_fake_canvas(), dry_run=True)

    flat = [c for choices in captured for c in choices]
    assert not any("[→]" in c and "myapp" in c for c in flat), \
        "[→] ne doit pas s'afficher pour une app déjà en atelier"
    assert any("en atelier" in c.lower() and "myapp" in c for c in flat), \
        "ligne ⏸ (en atelier) attendue pour myapp"


def test_workshop_menu_shows_deploy_and_remove_for_workshop_app(monkeypatch):
    """X-ws affiche à la fois [←] déployer et [×] supprimer."""
    from hubctl_cli.hub_state import AppInfo
    from pathlib import Path

    prod_apps = [AppInfo("myapp", Path("."), "1", False, 0, legacy=False)]
    ws_apps = [AppInfo("myapp-ws", Path("."), "1", False, 0, legacy=False)]
    monkeypatch.setattr(menu.hub_state, "get_apps", lambda: prod_apps)
    monkeypatch.setattr(menu.hub_state, "get_workshop_apps", lambda: ws_apps)

    captured = []

    def fake_select(canvas, message, choices, **kw):
        captured.append(choices)
        return menu.widgets.ESC

    monkeypatch.setattr(menu.widgets, "select", fake_select)

    menu._workshop_menu(_fake_canvas(), dry_run=True)

    flat = [c for choices in captured for c in choices]
    assert any("[←]" in c and "myapp-ws" in c for c in flat), \
        "entrée [←] (déployer vers la prod) attendue pour myapp-ws"
    assert any("[×]" in c and "myapp-ws" in c for c in flat), \
        "entrée [×] (supprimer l'atelier) attendue pour myapp-ws"


def test_workshop_menu_deploy_calls_prod_deploy_with_base_slug(monkeypatch):
    """[←] déploie vers la prod en passant le slug BASE (sans -ws) à action_prod_deploy."""
    from hubctl_cli.hub_state import AppInfo
    from pathlib import Path

    ws_apps = [AppInfo("myapp-ws", Path("."), "1", False, 0, legacy=False)]
    monkeypatch.setattr(menu.hub_state, "get_apps", lambda: [])
    monkeypatch.setattr(menu.hub_state, "get_workshop_apps", lambda: ws_apps)

    monkeypatch.setattr(menu.widgets, "select", lambda c, m, choices, **kw:
                        "  [←] myapp-ws  → déployer vers la prod (git pull)")
    monkeypatch.setattr(menu.widgets, "confirm", lambda *a, **k: True)

    called = []
    monkeypatch.setattr(menu, "_run_action",
                        lambda *a, **k: called.append(a) or True)

    menu._workshop_menu(_fake_canvas(), dry_run=True)

    assert called, "_run_action doit être appelé pour [←]"
    # _run_action(canvas, label, fn, _ACTION_CONSOLE, slug, dry_run=...)
    args = called[0]
    assert args[2] is menu.actions.action_prod_deploy
    assert args[4] == "myapp", \
        "le slug passé à action_prod_deploy doit être le BASE (sans -ws)"


def test_workshop_arrow_proposes_deps_when_missing(monkeypatch):
    """[→] X où X dépend de Y (pas en atelier) → propose d'emmener Y aussi."""
    from hubctl_cli.hub_state import AppInfo
    from pathlib import Path

    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)

    prod_apps = [
        AppInfo("exemple-apps", Path("."), "1", False, 0, legacy=False),
        AppInfo("exemple-app-2", Path("."), "1", False, 0, legacy=False),
    ]
    monkeypatch.setattr(menu.hub_state, "get_apps", lambda: prod_apps)
    monkeypatch.setattr(menu.hub_state, "get_workshop_apps", lambda: [])

    # Graphe : exemple-apps dépend de exemple-app-2
    monkeypatch.setattr(
        menu.dependencies, "build_graph",
        lambda: {"exemple-apps": ["exemple-app-2"], "exemple-app-2": []},
    )

    # Séquence d'interactions :
    # 1. select initial → choix "[→] exemple-apps"
    # 2. select suggestion → "Oui (recommandé)"
    # 3. confirm final → True
    interactions = iter([
        "  [→] exemple-apps  → mettre en atelier",
        "Oui (recommandé)",
    ])
    monkeypatch.setattr(menu.widgets, "select", lambda c, m, choices, **kw: next(interactions))
    monkeypatch.setattr(menu.widgets, "confirm", lambda c, m, **kw: True)
    monkeypatch.setattr(menu, "pause", lambda c: None)

    created = []
    monkeypatch.setattr(
        menu.actions,
        "action_workshop_create",
        lambda con, slug, *, dry_run=False: created.append(slug),
    )

    menu._workshop_menu(_fake_canvas(), dry_run=True)

    # Les deux apps ont été emmenées
    assert sorted(created) == ["exemple-app-2", "exemple-apps"]


def test_workshop_arrow_no_deps_no_suggestion(monkeypatch):
    """[→] X où X est autonome → pas de suggestion, action directe."""
    from hubctl_cli.hub_state import AppInfo
    from pathlib import Path

    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)

    prod_apps = [AppInfo("alone", Path("."), "1", False, 0, legacy=False)]
    monkeypatch.setattr(menu.hub_state, "get_apps", lambda: prod_apps)
    monkeypatch.setattr(menu.hub_state, "get_workshop_apps", lambda: [])
    monkeypatch.setattr(
        menu.dependencies, "build_graph", lambda: {"alone": []},
    )

    select_calls = []
    def fake_select(c, m, choices, **kw):
        select_calls.append(list(choices))
        return "  [→] alone  → mettre en atelier"
    monkeypatch.setattr(menu.widgets, "select", fake_select)
    monkeypatch.setattr(menu.widgets, "confirm", lambda c, m, **kw: True)
    monkeypatch.setattr(menu, "pause", lambda c: None)

    created = []
    monkeypatch.setattr(
        menu.actions,
        "action_workshop_create",
        lambda con, slug, *, dry_run=False: created.append(slug),
    )

    menu._workshop_menu(_fake_canvas(), dry_run=True)

    assert created == ["alone"]
    # Un seul appel à select (pas de menu de suggestion)
    assert len(select_calls) == 1


def test_workshop_multiselect_option_present(monkeypatch):
    """Le menu workshop contient une entrée [→→] Multi-sélection."""
    from hubctl_cli.hub_state import AppInfo
    from pathlib import Path

    monkeypatch.setattr(
        menu.hub_state, "get_apps",
        lambda: [AppInfo("alpha", Path("."), "1", False, 0)],
    )
    monkeypatch.setattr(menu.hub_state, "get_workshop_apps", lambda: [])

    captured = []
    def fake_select(c, m, choices, **kw):
        captured.append(list(choices))
        return menu.widgets.ESC
    monkeypatch.setattr(menu.widgets, "select", fake_select)

    menu._workshop_menu(_fake_canvas(), dry_run=True)

    flat = [c for choices in captured for c in choices]
    assert any("[→→]" in c and "Multi-sélection" in c for c in flat)


def test_workshop_multiselect_dispatches_to_multiselect_widget(monkeypatch):
    """Choisir [→→] invoque widgets.multiselect, puis action_workshop_create pour chaque sélection."""
    from hubctl_cli.hub_state import AppInfo
    from pathlib import Path

    monkeypatch.setattr(
        menu.hub_state, "get_apps",
        lambda: [AppInfo("alpha", Path("."), "1", False, 0),
                 AppInfo("beta", Path("."), "1", False, 0)],
    )
    monkeypatch.setattr(menu.hub_state, "get_workshop_apps", lambda: [])
    monkeypatch.setattr(menu.dependencies, "build_graph",
                        lambda: {"alpha": [], "beta": []})

    # select initial → [→→] ; multiselect → ["alpha", "beta"] ; confirm → True
    select_returns = iter(["  [→→] Multi-sélection (mettre plusieurs apps en atelier)"])
    monkeypatch.setattr(menu.widgets, "select",
                        lambda c, m, choices, **kw: next(select_returns))
    monkeypatch.setattr(menu.widgets, "multiselect",
                        lambda c, m, choices, **kw: ["  alpha", "  beta"])
    monkeypatch.setattr(menu.widgets, "confirm", lambda c, m, **kw: True)
    monkeypatch.setattr(menu, "pause", lambda c: None)

    created = []
    monkeypatch.setattr(
        menu.actions,
        "action_workshop_create",
        lambda con, slug, *, dry_run=False: created.append(slug),
    )

    menu._workshop_menu(_fake_canvas(), dry_run=True)

    assert sorted(created) == ["alpha", "beta"]
