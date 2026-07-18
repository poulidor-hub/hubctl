"""Test exhaustif de tous les chemins de dispatch du menu TUI.

Vérifie que chaque option de menu a un handler accessible,
que les handlers ne crashent pas sur appel, et que les menus
dynamiques (DEV/PROD, workshop) sont corrects.
"""
import sys
from unittest import mock

import pytest

from hubctl_cli import menu, hub_state


# ── Helpers ──


def _plain(obj) -> str:
    """Retourne la représentation texte d'un objet (str ou Text)."""
    return obj.plain if hasattr(obj, 'plain') else str(obj)


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


def _mock_all_deps(monkeypatch):
    """Mocke toutes les dépendances externes pour que les handlers
    ne tentent pas d'appeler Docker, git, ou le réseau."""
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(menu.widgets, "select", lambda c, m, choices, **kw: menu.widgets.ESC)
    monkeypatch.setattr(menu.widgets, "confirm", lambda c, m, **kw: True)
    monkeypatch.setattr(menu.widgets, "confirm_by_typing", lambda c, m, e: True)
    monkeypatch.setattr(menu.widgets, "multiselect", lambda c, m, choices, **kw: menu.widgets.ESC)
    monkeypatch.setattr(menu, "pause", lambda c: None)
    monkeypatch.setattr(menu, "choose_app", lambda c: None)
    monkeypatch.setattr(menu.actions.hub_state, "check_docker", lambda: True)
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "dev")
    monkeypatch.setattr(menu.dependencies, "build_graph", lambda: {})


# ── Test : chaque handler de ACTION_HANDLERS est appelable ──


HANDLERS_TO_SKIP = {
    "quick_setup",     # setup wizard complexe
    "network",         # configuration réseau interactive
    "admin",           # configuration admin interactive
    "advanced",        # config avancée interactive
    "sc_diag_quick",   # scénario (appelle plusieurs handlers)
    "sc_update_full",  # scénario
    "sc_app_rescue",   # scénario
    "sc_health",       # scénario
    "logs_hub",        # docker compose logs (bloquant)
    "coffre",          # nécessite vault
    "workshop",        # boucle interactive (while True, testé séparément)
}


@pytest.mark.parametrize("target", list(menu.ACTION_HANDLERS.keys()))
def test_every_action_handler_is_callable(target, monkeypatch):
    """Chaque handler dans ACTION_HANDLERS doit accepter (canvas, state, vault_mgr, dry_run)."""
    if target in HANDLERS_TO_SKIP:
        pytest.skip(f"Handler '{target}' nécessite un setup interactif")

    _mock_all_deps(monkeypatch)
    handler = menu.ACTION_HANDLERS[target]
    canvas = _fake_canvas()

    try:
        result = handler(canvas, {}, None, dry_run=True)
        assert isinstance(result, bool)
    except Exception as e:
        pytest.fail(f"Handler '{target}' a levé : {type(e).__name__}: {e}")


# ── Test : les menus dynamiques sont cohérents ──


def test_build_apps_menu_dev_has_workshop(monkeypatch):
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "dev")
    ctx = menu._build_apps_menu()
    assert "5" in ctx
    assert ctx["5"][1] == "workshop"
    assert "1" in ctx and ctx["1"][1] == "update_app"
    assert "2" in ctx and ctx["2"][1] == "add_app"
    assert "3" in ctx and ctx["3"][1] == "remove_app"
    assert "4" in ctx and ctx["4"][1] == "logs_app"
    assert "0" in ctx and ctx["0"][1] == "__back__"


def test_build_apps_menu_prod_no_workshop(monkeypatch):
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "prod")
    ctx = menu._build_apps_menu()
    assert "5" not in ctx


def test_build_config_menu_dev_has_workshop(monkeypatch):
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "dev")
    ctx = menu._build_config_menu()
    assert "6" in ctx
    assert ctx["6"][1] == "workshop"


def test_build_config_menu_prod_no_workshop(monkeypatch):
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "prod")
    ctx = menu._build_config_menu()
    assert "6" not in ctx


def test_build_main_menu_has_all_sections():
    ctx = menu._build_main_menu()
    assert ctx["1"][1] == "apps"
    assert ctx["2"][1] == "hub"
    assert ctx["3"][1] == "config"
    assert ctx["4"][1] == "scenarios"
    assert "5" in ctx  # toggle mode
    assert ctx["0"][0] == "Quitter"


# ── Test : _current_context route correctement ──


def test_current_context_routes_main_to_build_main_menu(monkeypatch):
    monkeypatch.setattr(menu, "_nav_stack", ["main"])
    ctx = menu._current_context()
    assert ctx["1"][1] == "apps"


def test_current_context_routes_apps_to_build_apps_menu(monkeypatch):
    monkeypatch.setattr(menu, "_nav_stack", ["main", "apps"])
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "dev")
    ctx = menu._current_context()
    assert "5" in ctx  # workshop en DEV


def test_current_context_routes_config_to_build_config_menu(monkeypatch):
    monkeypatch.setattr(menu, "_nav_stack", ["main", "config"])
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "dev")
    ctx = menu._current_context()
    assert "6" in ctx  # workshop en DEV


def test_current_context_routes_hub_to_menutree(monkeypatch):
    monkeypatch.setattr(menu, "_nav_stack", ["main", "hub"])
    ctx = menu._current_context()
    assert ctx["1"][1] == "update_hub"


def test_current_context_routes_scenarios_to_menutree(monkeypatch):
    monkeypatch.setattr(menu, "_nav_stack", ["main", "scenarios"])
    ctx = menu._current_context()
    assert ctx["1"][1] == "sc_diag_quick"


# ── Test : workshop menu complet ──


def test_workshop_menu_empty_shows_no_apps(monkeypatch):
    from hubctl_cli.hub_state import AppInfo
    from pathlib import Path

    monkeypatch.setattr(menu.hub_state, "get_apps", lambda: [])
    monkeypatch.setattr(menu.hub_state, "get_workshop_apps", lambda: [])

    captured = []
    monkeypatch.setattr(menu.widgets, "select",
                        lambda c, m, choices, **kw: captured.append(list(choices)) or menu.widgets.ESC)

    menu._workshop_menu(_fake_canvas(), dry_run=True)

    flat = [c for choices in captured for c in choices]
    assert any("Aucune app" in str(c) for c in flat), "doit afficher 'Aucune app trouvée' quand vide"


def test_workshop_menu_has_all_five_option_types(monkeypatch):
    """Vérifie que les 5 types d'options sont présents dans le menu atelier."""
    from hubctl_cli.hub_state import AppInfo
    from pathlib import Path

    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(menu, "pause", lambda c: None)

    # 1 app en atelier + 3 apps en prod (dont l'app en atelier)
    ws_app = AppInfo("exemple-apps-ws", Path("."), "1", False, 0, legacy=False)
    prod_app1 = AppInfo("exemple-apps", Path("."), "1", False, 0, legacy=False)
    prod_app2 = AppInfo("exemple-app-2", Path("."), "1", False, 0, legacy=False)
    prod_app3 = AppInfo("exemple-app", Path("."), "1", False, 0, legacy=False)

    monkeypatch.setattr(menu.hub_state, "get_apps", lambda: [prod_app1, prod_app2, prod_app3])
    monkeypatch.setattr(menu.hub_state, "get_workshop_apps", lambda: [ws_app])
    monkeypatch.setattr(menu.dependencies, "build_graph",
                        lambda: {"exemple-apps-ws": [], "exemple-app-2": [], "exemple-app": []})

    captured = []
    monkeypatch.setattr(menu.widgets, "select",
                        lambda c, m, choices, **kw: captured.append(list(choices)) or menu.widgets.ESC)

    menu._workshop_menu(_fake_canvas(), dry_run=True)

    flat = [_plain(c) for choices in captured for c in choices]

    assert any("APP_Workshop" in c for c in flat), "section APP_Workshop absente"
    assert any("APPS" in c for c in flat), "section APPS absente"
    assert any("[←]" in c for c in flat), "option [←] absente"
    assert any("[×]" in c for c in flat), "option [×] absente"
    assert any("[→→]" in c for c in flat), "option [→→] absente"
    assert any("[→+]" in c for c in flat), "option [→+] absente"
    assert any("[→]" in c and "exemple-app-2" in c for c in flat), "option [→] exemple-app-2 absente"
    assert any("exemple-apps" in c and "en atelier" in c for c in flat), \
        "option ⏸ exemple-apps absente"


# ── Test : dispatch invalide ──


def test_dispatch_invalid_key_shows_error(monkeypatch):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(menu, "pause", lambda c: None)
    monkeypatch.setattr(menu, "_current_context", lambda: {"1": ("Test", "test_target")})
    result = menu._dispatch("99", _fake_canvas(), {}, None, dry_run=True)
    assert result is False
