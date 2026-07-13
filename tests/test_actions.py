from unittest import mock
from hubctl_cli import actions, env_manager


def test_valid_app_name():
    assert actions.valid_app_name("exemple-apps")
    assert actions.valid_app_name("app_1")
    assert not actions.valid_app_name("Bad")
    assert not actions.valid_app_name("1abc")
    assert not actions.valid_app_name("a b")


def test_hub_make_builds_make_command():
    with mock.patch.object(actions.utils, "run_shell", return_value=0) as rs:
        actions.hub_make("down", dry_run=True)
    args = rs.call_args.args[0]
    assert args[0] == "make"
    assert "-C" in args
    assert args[-1] == "down"


def test_app_make_targets_app_dir():
    with mock.patch.object(actions.utils, "run_shell", return_value=0) as rs:
        actions.app_make("exemple-apps", "logs", dry_run=True)
    args = rs.call_args.args[0]
    assert str(actions.hub_state.APPS_DIR / "exemple-apps") in args
    assert args[-1] == "logs"


def test_disk_guard_blocks_when_critical():
    fake = mock.Mock(percent_free=3.0, free=1, total=100)
    with mock.patch.object(actions.hub_state, "get_disk_usage", return_value=fake):
        assert actions.disk_guard() is False


def test_disk_guard_ok_when_low_but_not_critical():
    fake = mock.Mock(percent_free=10.0, free=1, total=100)
    with mock.patch.object(actions.hub_state, "get_disk_usage", return_value=fake):
        assert actions.disk_guard() is True


def test_disk_guard_ok_when_plenty():
    fake = mock.Mock(percent_free=50.0, free=1, total=100)
    with mock.patch.object(actions.hub_state, "get_disk_usage", return_value=fake):
        assert actions.disk_guard() is True


def test_remove_app_non_interactive(tmp_path, monkeypatch):
    apps = tmp_path / "APPS"
    (apps / "shop").mkdir(parents=True)
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", apps)
    monkeypatch.setattr(actions.hub_state, "PARC_ROOT", tmp_path)
    console = mock.Mock()
    with mock.patch.object(actions, "app_make") as am, \
         mock.patch("tarfile.open"), \
         mock.patch("shutil.move") as mv:
        actions.action_remove_app(console, "shop", None, dry_run=False)
    am.assert_called_once()
    mv.assert_called_once()
    assert not hasattr(actions, "prompts")


def test_repo_name_from_url():
    assert actions.repo_name_from_url("git@github.com:Poutchouli/exemple-app-2.git") == "exemple-app-2"
    assert actions.repo_name_from_url("https://github.com/Poutchouli/exemple-app-2.git") == "exemple-app-2"
    assert actions.repo_name_from_url("git@github.com:Org/my-app") == "my-app"
    assert actions.repo_name_from_url("") is None
    assert actions.repo_name_from_url("nom avec espaces/") is None


def test_menu_tree_contains_all_actions():
    from hubctl_cli import menu, hub_state
    hub_state.set_mode("dev")
    all_labels = []
    for ctx_name, ctx in menu.MENU_TREE.items():
        for _digit, (label, _target) in ctx.items():
            all_labels.append(label.lower())
    for builder in (menu._build_main_menu, menu._build_config_menu):
        for _digit, (label, _target) in builder().items():
            all_labels.append(label.lower())
    joined = " ".join(all_labels)
    for kw in ["apps", "hub", "configuration", "quitter", "update", "ajouter", "supprimer",
                "logs", "start", "stop", "coffre", "setup", "réseau", "admin", "workshop",
                "scénarios", "passer en"]:
        assert kw in joined


def _menu_canvas():
    c = mock.MagicMock()
    c.inner_cols = 98
    c.inner_rows = 28
    c.set_layout = mock.MagicMock()
    return c


def _mock_vault_mgr():
    mgr = mock.MagicMock()
    mgr.vault = mock.MagicMock()
    mgr.vault.is_locked = True
    return mgr


def test_pause_waits_for_enter():
    from hubctl_cli import menu
    canvas = _menu_canvas()
    with mock.patch("sys.stdin.fileno", return_value=0), \
         mock.patch.object(menu.keys, "read_key", side_effect=[menu.keys.KeyEnter()]):
        menu.pause(canvas)
    assert canvas.render.call_count >= 1


def test_main_loop_quits_on_zero_key():
    from hubctl_cli import menu
    menu._nav_stack = ["main"]
    canvas = _menu_canvas()
    vault_mgr = _mock_vault_mgr()
    with mock.patch("sys.stdin.fileno", return_value=0), \
         mock.patch.object(menu.keys, "read_key",
                           side_effect=[menu.keys.KeyChar("0"),
                                        menu.keys.KeyEnter()]):
        menu.main_loop(canvas, {"docker": False, "services": [], "hub_version": "v0.1", "apps": []},
                       vault_mgr, dry_run=True)
    canvas.render.assert_called()


def test_main_loop_escape_confirms_then_quits():
    from hubctl_cli import menu
    menu._nav_stack = ["main"]
    canvas = _menu_canvas()
    vault_mgr = _mock_vault_mgr()
    with mock.patch("sys.stdin.fileno", return_value=0), \
         mock.patch.object(menu.keys, "read_key",
                           side_effect=[menu.keys.KeyEsc()]), \
         mock.patch.object(menu.widgets, "confirm", return_value=True):
        menu.main_loop(canvas, {"docker": False, "services": [], "apps": []},
                       vault_mgr, dry_run=True)
    canvas.render.assert_called()


def test_main_loop_ctrlc_at_menu_exits():
    from hubctl_cli import menu
    menu._nav_stack = ["main"]
    canvas = _menu_canvas()
    vault_mgr = _mock_vault_mgr()
    import pytest
    with mock.patch("sys.stdin.fileno", return_value=0), \
         mock.patch.object(menu.keys, "read_key",
                           side_effect=[menu.keys.KeyCtrlC()]):
        with pytest.raises(KeyboardInterrupt):
            menu.main_loop(canvas, {"docker": False, "services": [], "apps": []},
                           vault_mgr, dry_run=True)


def test_main_loop_action_returns_to_menu_after_action():
    from hubctl_cli import menu
    menu._nav_stack = ["main"]
    canvas = _menu_canvas()
    vault_mgr = _mock_vault_mgr()

    with mock.patch("sys.stdin.fileno", return_value=0), \
         mock.patch.object(menu.keys, "read_key",
                           side_effect=[menu.keys.KeyChar("3"),
                                        menu.keys.KeyEnter(),
                                        menu.keys.KeyChar("0"),
                                        menu.keys.KeyEnter()]), \
         mock.patch.object(menu, "_dispatch",
                           side_effect=[False, True]) as dispatch_mock:
        menu.main_loop(canvas, {"docker": False, "services": [], "apps": []},
                       vault_mgr, dry_run=True)
    assert dispatch_mock.call_count == 2
    assert dispatch_mock.call_args_list[0].args[0] == "3"
    assert dispatch_mock.call_args_list[1].args[0] == "0"


# ── action_init tests ──

def _mock_console():
    c = mock.MagicMock()
    return c


def test_action_init_missing_manifest():
    console = _mock_console()
    with mock.patch.object(actions.hub_state, "PARC_ROOT",
                           mock.MagicMock(__truediv__=mock.Mock(return_value=mock.MagicMock(exists=mock.Mock(return_value=False))))):
        actions.action_init(console, dry_run=False)
    console.print.assert_called()


def test_action_init_invalid_json():
    console = _mock_console()
    fake_path = mock.MagicMock(exists=mock.Mock(return_value=True))
    fake_path.read_text.return_value = "not json"
    with mock.patch.object(actions.hub_state, "PARC_ROOT",
                           mock.MagicMock(__truediv__=mock.Mock(return_value=fake_path))):
        actions.action_init(console, dry_run=False)
    console.print.assert_called()


def test_action_init_empty_manifest():
    console = _mock_console()
    fake_path = mock.MagicMock(exists=mock.Mock(return_value=True))
    fake_path.read_text.return_value = "{}"
    with mock.patch.object(actions.hub_state, "PARC_ROOT",
                           mock.MagicMock(__truediv__=mock.Mock(return_value=fake_path))):
        actions.action_init(console, dry_run=False)
    console.print.assert_called()


def _fake_path(name):
    p = mock.MagicMock()
    p.configure_mock(**{"parent.mkdir": mock.Mock(), "__str__.return_value": f"/fake/parc/{name}"})
    return p


def test_action_init_dry_run():
    console = _mock_console()
    manifest_data = '{"test-app": "git@github.com:org/test-app.git", "other": "url2"}'
    fake_manifest = mock.MagicMock(exists=mock.Mock(return_value=True))
    fake_manifest.read_text.return_value = manifest_data
    dest_mock = mock.MagicMock(exists=mock.Mock(return_value=False))
    dest_mock.configure_mock(**{"parent.mkdir": mock.Mock(), "__str__.return_value": "/fake/parc/test-app"})
    dest_mock2 = mock.MagicMock(exists=mock.Mock(return_value=False))
    dest_mock2.configure_mock(**{"parent.mkdir": mock.Mock(), "__str__.return_value": "/fake/parc/other"})

    parc_root_mock = mock.MagicMock()
    parc_root_mock.__truediv__.side_effect = lambda x: {
        "manifest.json": fake_manifest,
        "test-app": dest_mock,
        "other": dest_mock2,
    }[x]

    with mock.patch.object(actions.hub_state, "PARC_ROOT", parc_root_mock), \
         mock.patch.object(actions, "disk_guard", return_value=True), \
         mock.patch.object(actions.utils, "run_shell", return_value=0) as rs:
        actions.action_init(console, dry_run=True)
    assert rs.call_count == 2
    first_args = rs.call_args_list[0].args[0]
    assert first_args[0] == "git"
    assert first_args[1] == "clone"


# ── Workshop move tests ──

from pathlib import Path
from rich.console import Console


def _fake_console():
    return Console(force_terminal=True, width=120)


def test_action_workshop_move_bad_direction():
    with mock.patch.object(actions, "_console", _fake_console()):
        try:
            actions.action_workshop_move(_fake_console(), "invalid", "testapp", dry_run=True)
            assert False, "should have raised ValueError"
        except ValueError as e:
            assert "direction" in str(e)


def test_workshop_move_missing_dev_mode(tmp_path):
    """Should abort when HUB_base/.dev-mode is missing."""
    hub_dir = tmp_path / "HUB_base"
    hub_dir.mkdir()
    apps_dir = tmp_path / "APPS"
    apps_dir.mkdir()
    ws_dir = tmp_path / "APP_Workshop"
    ws_dir.mkdir()

    app_dir = apps_dir / "testapp"
    app_dir.mkdir()
    (app_dir / "docker-compose.yml").write_text("services: {}\n")

    with mock.patch.object(actions.hub_state, "HUB_DIR", hub_dir):
        with mock.patch.object(actions.hub_state, "APPS_DIR", apps_dir):
            with mock.patch.object(actions.hub_state, "WORKSHOP_DIR", ws_dir):
                with mock.patch.object(actions.hub_state, "PARC_ROOT", tmp_path):
                    actions.action_workshop_move(
                        _fake_console(), "to_workshop", "testapp", dry_run=True,
                    )
    # Should return without error (early exit on missing .dev-mode)
    assert not (ws_dir / "testapp-ws").exists()


def test_workshop_move_dry_run_to_workshop(tmp_path):
    """Full dry-run flow: APPS → Workshop."""
    hub_dir = tmp_path / "HUB_base"
    hub_dir.mkdir()
    (hub_dir / ".dev-mode").touch()
    apps_dir = tmp_path / "APPS"
    apps_dir.mkdir()
    ws_dir = tmp_path / "APP_Workshop"
    ws_dir.mkdir()
    (tmp_path / "hubctl_cli" / "backups").mkdir(parents=True)
    (tmp_path / "hubctl_cli" / "_trash").mkdir(parents=True)

    app_dir = apps_dir / "testapp"
    app_dir.mkdir()
    (app_dir / "docker-compose.yml").write_text("services: {}\n")
    (app_dir / ".env").write_text("APP_SLUG=testapp\nAPI_PORT=9001\n")

    with mock.patch.object(actions.hub_state, "HUB_DIR", hub_dir):
        with mock.patch.object(actions.hub_state, "APPS_DIR", apps_dir):
            with mock.patch.object(actions.hub_state, "WORKSHOP_DIR", ws_dir):
                with mock.patch.object(actions.hub_state, "PARC_ROOT", tmp_path):
                    actions.action_workshop_move(
                        _fake_console(), "to_workshop", "testapp", dry_run=True,
                    )

    # Dry-run: destination should NOT exist
    assert not (ws_dir / "testapp-ws").exists()
    # Source should still exist (dry-run doesn't delete)
    assert (apps_dir / "testapp").exists()


def test_workshop_move_dry_run_to_production(tmp_path):
    """Full dry-run flow: APP_Workshop → APPS (strip -ws suffix)."""
    hub_dir = tmp_path / "HUB_base"
    hub_dir.mkdir()
    (hub_dir / ".dev-mode").touch()
    apps_dir = tmp_path / "APPS"
    apps_dir.mkdir()
    ws_dir = tmp_path / "APP_Workshop"
    ws_dir.mkdir()
    (tmp_path / "hubctl_cli" / "backups").mkdir(parents=True)
    (tmp_path / "hubctl_cli" / "_trash").mkdir(parents=True)

    app_dir = ws_dir / "testapp-ws"
    app_dir.mkdir()
    (app_dir / "docker-compose.yml").write_text("services: {}\n")
    (app_dir / ".env").write_text("APP_SLUG=testapp-ws\nAPI_PORT=9002\n")

    with mock.patch.object(actions.hub_state, "HUB_DIR", hub_dir):
        with mock.patch.object(actions.hub_state, "APPS_DIR", apps_dir):
            with mock.patch.object(actions.hub_state, "WORKSHOP_DIR", ws_dir):
                with mock.patch.object(actions.hub_state, "PARC_ROOT", tmp_path):
                    actions.action_workshop_move(
                        _fake_console(), "to_production", "testapp-ws", dry_run=True,
                    )

    # Dry-run: destination should NOT exist
    assert not (apps_dir / "testapp").exists()
    # Source should still exist
    assert (ws_dir / "testapp-ws").exists()


def test_workshop_move_source_missing(tmp_path):
    """Should exit early when source directory does not exist."""
    hub_dir = tmp_path / "HUB_base"
    hub_dir.mkdir()
    (hub_dir / ".dev-mode").touch()
    apps_dir = tmp_path / "APPS"
    apps_dir.mkdir()

    with mock.patch.object(actions.hub_state, "HUB_DIR", hub_dir):
        with mock.patch.object(actions.hub_state, "APPS_DIR", apps_dir):
            with mock.patch.object(actions.hub_state, "PARC_ROOT", tmp_path):
                actions.action_workshop_move(
                    _fake_console(), "to_workshop", "nonexistent", dry_run=True,
                )
    # Should not raise


def test_workshop_move_dest_exists(tmp_path):
    """Should exit early when destination already exists."""
    hub_dir = tmp_path / "HUB_base"
    hub_dir.mkdir()
    (hub_dir / ".dev-mode").touch()
    apps_dir = tmp_path / "APPS"
    apps_dir.mkdir()
    ws_dir = tmp_path / "APP_Workshop"
    ws_dir.mkdir()

    (apps_dir / "testapp").mkdir()
    (ws_dir / "testapp-ws").mkdir()  # destination already exists

    with mock.patch.object(actions.hub_state, "HUB_DIR", hub_dir):
        with mock.patch.object(actions.hub_state, "APPS_DIR", apps_dir):
            with mock.patch.object(actions.hub_state, "WORKSHOP_DIR", ws_dir):
                with mock.patch.object(actions.hub_state, "PARC_ROOT", tmp_path):
                    actions.action_workshop_move(
                        _fake_console(), "to_workshop", "testapp", dry_run=True,
                    )
    # Should exit early, no crash


def test_workshop_move_slug_from_env(tmp_path):
    """When APP_SLUG differs from folder name, the .env value is used for rename."""
    hub_dir = tmp_path / "HUB_base"
    hub_dir.mkdir()
    (hub_dir / ".dev-mode").touch()
    apps_dir = tmp_path / "APPS"
    apps_dir.mkdir()
    ws_dir = tmp_path / "APP_Workshop"
    ws_dir.mkdir()
    (tmp_path / "hubctl_cli" / "backups").mkdir(parents=True)
    (tmp_path / "hubctl_cli" / "_trash").mkdir(parents=True)

    app_dir = apps_dir / "exemple-app"
    app_dir.mkdir()
    (app_dir / "docker-compose.yml").write_text("services: {}\n")
    (app_dir / ".env").write_text("APP_SLUG=citrondorge\nAPI_PORT=9003\n")

    with mock.patch.object(actions.hub_state, "HUB_DIR", hub_dir):
        with mock.patch.object(actions.hub_state, "APPS_DIR", apps_dir):
            with mock.patch.object(actions.hub_state, "WORKSHOP_DIR", ws_dir):
                with mock.patch.object(actions.hub_state, "PARC_ROOT", tmp_path):
                    actions.action_workshop_move(
                        _fake_console(), "to_workshop", "exemple-app", dry_run=True,
                    )

    # Dry-run doesn't write files, so we only verify no crash
    assert (apps_dir / "exemple-app").exists()


def test_attach_app_dev_targets_workshop(tmp_path, monkeypatch):
    ws = tmp_path / "APP_Workshop"
    (ws / "shop").mkdir(parents=True)
    (ws / "shop" / "docker-compose.yml").write_text("services: {}\n")
    monkeypatch.setattr(actions.hub_state, "WORKSHOP_DIR", ws)
    monkeypatch.setattr(actions.hub_state, "get_mode", lambda: "dev")
    monkeypatch.setattr(actions, "disk_guard", lambda con=None: True)
    console = mock.Mock()
    with mock.patch.object(actions.utils, "run_shell", return_value=0) as rs:
        actions.action_attach_app(console, "shop", dry_run=False)
    cmds = [c.args[0] for c in rs.call_args_list]
    assert ["docker", "compose", "-f", str(ws / "shop" / "docker-compose.yml"), "build"] in cmds
    assert ["docker", "compose", "-f", str(ws / "shop" / "docker-compose.yml"), "up", "-d"] in cmds


def test_attach_app_prod_targets_apps(tmp_path, monkeypatch):
    apps = tmp_path / "APPS"
    (apps / "shop").mkdir(parents=True)
    (apps / "shop" / "docker-compose.yml").write_text("services: {}\n")
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", apps)
    monkeypatch.setattr(actions.hub_state, "get_mode", lambda: "prod")
    monkeypatch.setattr(actions, "disk_guard", lambda con=None: True)
    console = mock.Mock()
    with mock.patch.object(actions.utils, "run_shell", return_value=0) as rs:
        actions.action_attach_app(console, "shop", dry_run=False)
    composes = [c.args[0][3] for c in rs.call_args_list if c.args[0][:2] == ["docker", "compose"]]
    assert all(str(apps / "shop") in c for c in composes)


def test_attach_app_aborts_without_compose(tmp_path, monkeypatch):
    apps = tmp_path / "APPS"
    (apps / "shop").mkdir(parents=True)
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", apps)
    monkeypatch.setattr(actions.hub_state, "get_mode", lambda: "prod")
    console = mock.Mock()
    with mock.patch.object(actions.utils, "run_shell", return_value=0) as rs:
        actions.action_attach_app(console, "shop", dry_run=False)
    rs.assert_not_called()


def test_attach_app_runs_initialise_when_present(tmp_path, monkeypatch):
    apps = tmp_path / "APPS"
    (apps / "shop").mkdir(parents=True)
    (apps / "shop" / "docker-compose.yml").write_text("services: {}\n")
    (apps / "shop" / "INITIALISE.py").write_text("print('init')\n")
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", apps)
    monkeypatch.setattr(actions.hub_state, "get_mode", lambda: "prod")
    monkeypatch.setattr(actions, "disk_guard", lambda con=None: True)
    console = mock.Mock()
    with mock.patch.object(actions.utils, "run_shell", return_value=0) as rs:
        actions.action_attach_app(console, "shop", dry_run=False)
    cmds = [c.args[0] for c in rs.call_args_list]
    assert ["python", "INITIALISE.py"] in cmds


import json
from unittest import mock


def test_ensure_jwt_keys_does_not_write_to_dotenv(tmp_path, monkeypatch):
    """H10 régression : ne PAS écrire AUTO_GENERATE_KEYS dans le .env de prod."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    hub_dir = tmp_path
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", hub_dir)

    dotenv = tmp_path / ".env"
    dotenv.write_text("APP_ENV=production\n")  # prod, pas de AUTO_GENERATE_KEYS

    actions._ensure_jwt_keys()

    content = dotenv.read_text()
    assert "AUTO_GENERATE_KEYS" not in content, (
        "H10: _ensure_jwt_keys ne doit PAS persister AUTO_GENERATE_KEYS dans le .env de prod"
    )
    assert "APP_ENV=production" in content  # .env non modifié


def test_cleanup_orphan_tests_only_targets_hubctl_projects(monkeypatch):
    """Seuls les projets -test correspondant au hub ou à une app réelle sont nettoyés.
    Les projets -test étrangers (load-test, integration-test) doivent être préservés."""
    projects = [
        {"Name": "exemple-apps-test"},   # hubctl-owned (app réelle)
        {"Name": "hub-test"},              # hubctl-owned (hub)
        {"Name": "load-test"},             # ÉTRANGER — ne doit PAS être nettoyé
        {"Name": "integration-test"},      # ÉTRANGER — ne doit PAS être nettoyé
    ]

    cleaned: list[str] = []

    class _FakeRun:
        def __call__(self, cmd, **kwargs):
            if "ls" in cmd:
                return mock.Mock(stdout=json.dumps(projects), returncode=0)
            if "down" in cmd:
                # Capture le nom du projet nettoyé
                idx = cmd.index("-p")
                cleaned.append(cmd[idx + 1])
                return mock.Mock(stdout="", returncode=0)
            return mock.Mock(stdout="", returncode=0)

    monkeypatch.setattr(actions.subprocess, "run", _FakeRun())

    # Simuler get_apps : une seule app "exemple-apps"
    fake_app = mock.Mock()
    fake_app.name = "exemple-apps"
    monkeypatch.setattr(actions.hub_state, "get_apps", lambda: [fake_app])
    monkeypatch.setattr(actions.hub_state, "get_workshop_apps", lambda: [])

    actions.cleanup_orphan_tests()

    assert "exemple-apps-test" in cleaned, "L'app hubctl doit être nettoyée"
    assert "hub-test" in cleaned, "Le hub-test doit être nettoyé"
    assert "load-test" not in cleaned, "Le projet étranger load-test ne doit PAS être nettoyé"
    assert "integration-test" not in cleaned, "Le projet étranger integration-test ne doit PAS être nettoyé"


# ── action_update (Lot 2 : flux séquentiel + rollback) ──


def test_action_update_sequential_flow(tmp_path, monkeypatch):
    """action_update doit faire : snapshot/git → down → pull → build → up → health.
    Pas de blue/green (cassé), pas d'instance test."""
    calls = []

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        return 0

    monkeypatch.setattr(actions.utils, "run_shell", fake_run_shell)
    monkeypatch.setattr(actions, "_git_previous_commit", lambda p, console=None: "abc1234")
    monkeypatch.setattr(actions, "_db_snapshot", lambda console=None: tmp_path / "snap.sql")
    monkeypatch.setattr(actions, "_health_check", lambda target, console=None, timeout=60: True)
    monkeypatch.setattr(actions, "disk_guard", lambda console=None: True)
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", tmp_path)
    monkeypatch.setattr(actions.hub_state, "active_apps_dir", lambda: tmp_path)
    (tmp_path / "docker-compose.yml").write_text("services:\n  db:\n    image: postgres\n")

    actions.action_update(None, "hub", dry_run=False)

    # Verify the sequence: git pull, make down, make build, make up (or up-prod)
    flat = [" ".join(c) for c in calls]
    assert any("git" in c and "pull" in c for c in flat), "git pull manquant"
    assert any("make" in c and "down" in c for c in flat), "make down manquant"
    assert any("make" in c and "build" in c for c in flat), "make build manquant"
    assert any("make" in c and ("up" in c) for c in flat), "make up manquant"
    # NO test instance / blue-green
    assert not any("-test" in c for c in flat), "blue/green test instance encore présent"


def test_action_update_rollback_on_health_failure(tmp_path, monkeypatch):
    """Si le health check échoue après l'update, rollback doit être appelé."""
    calls = []

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        return 0

    monkeypatch.setattr(actions.utils, "run_shell", fake_run_shell)
    monkeypatch.setattr(actions, "_git_previous_commit", lambda p, console=None: "abc1234")
    monkeypatch.setattr(actions, "_db_snapshot", lambda console=None: tmp_path / "snap.sql")
    monkeypatch.setattr(actions, "_health_check", lambda target, console=None, timeout=60: False)
    monkeypatch.setattr(actions, "disk_guard", lambda console=None: True)
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", tmp_path)
    monkeypatch.setattr(actions.hub_state, "active_apps_dir", lambda: tmp_path)
    (tmp_path / "docker-compose.yml").write_text("services:\n  db:\n    image: postgres\n")

    actions.action_update(None, "hub", dry_run=False)

    flat = [" ".join(c) for c in calls]
    # Rollback: git reset + make up (restore prod)
    assert any("git" in c and "reset" in c for c in flat), "git reset (rollback) manquant"


def test_health_check_reads_app_port(tmp_path, monkeypatch):
    """H3: _health_check doit lire API_PORT du .env de l'app cible."""
    app_dir = tmp_path / "myapp"
    app_dir.mkdir()
    (app_dir / ".env").write_text("API_PORT=9999\n")
    monkeypatch.setattr(actions.hub_state, "active_apps_dir", lambda: tmp_path)

    curl_result = mock.Mock(stdout='{"status":"healthy"}', returncode=0)

    def fake_run(cmd, **kwargs):
        # Capture the URL to verify the port
        if "curl" in cmd:
            test_health_check_reads_app_port.curl_url = cmd[-1]
        return curl_result

    test_health_check_reads_app_port.curl_url = None
    monkeypatch.setattr(actions.subprocess, "run", fake_run)
    monkeypatch.setattr(actions.time, "sleep", lambda x: None)

    actions._health_check("myapp", timeout=1)

    assert "9999" in test_health_check_reads_app_port.curl_url, (
        f"health_check doit utiliser API_PORT=9999, URL vue: {test_health_check_reads_app_port.curl_url}"
    )


def test_action_status_prints_hub_and_apps(monkeypatch):
    """action_status doit afficher le statut du Hub et des apps."""
    from io import StringIO
    from rich.console import Console
    output = StringIO()
    console = Console(file=output, width=120, force_terminal=False)

    # Mock hub services
    fake_service = mock.Mock()
    fake_service.name = "hub-api"
    fake_service.state = "running"
    fake_service.health = "healthy"
    monkeypatch.setattr(actions.hub_state, "get_hub_services", lambda: [fake_service])

    # Mock apps status
    fake_app = mock.Mock()
    fake_app.name = "exemple-apps"
    fake_app.status = "running"
    fake_app.uptime = "2h"
    fake_app.legacy = False
    monkeypatch.setattr(actions.hub_state, "get_apps_status", lambda: [fake_app])

    monkeypatch.setattr(actions.hub_state, "get_hub_version", lambda: "3.9.7")
    monkeypatch.setattr(actions.hub_state, "get_mode", lambda: "dev")

    actions.action_status(console)

    text = output.getvalue()
    assert "hub-api" in text
    assert "exemple-apps" in text
    assert "DEV" in text
    assert "3.9.7" in text


def test_action_work_calls_workshop_move_to_workshop(monkeypatch):
    """action_work doit appeler action_workshop_move avec direction='to_workshop'."""
    calls = []
    monkeypatch.setattr(actions, "action_workshop_move",
                        lambda console, direction, app_name, *, dry_run=False:
                        calls.append((direction, app_name)))
    actions.action_work(None, "exemple-apps")
    assert ("to_workshop", "exemple-apps") in calls


def test_action_ship_calls_workshop_move_to_production(monkeypatch):
    """action_ship doit appeler action_workshop_move avec direction='to_production'.
    Utilise dry_run=True pour éviter le prompt interactif de confirmation."""
    calls = []
    monkeypatch.setattr(actions, "action_workshop_move",
                        lambda console, direction, app_name, *, dry_run=False:
                        calls.append((direction, app_name)))
    actions.action_ship(None, "exemple-apps", dry_run=True)
    assert ("to_production", "exemple-apps") in calls


def test_action_ship_aborts_on_wrong_confirmation(monkeypatch):
    """action_ship NE doit PAS appeler workshop_move si la confirmation est incorrecte."""
    calls = []
    monkeypatch.setattr(actions, "action_workshop_move",
                        lambda console, direction, app_name, *, dry_run=False:
                        calls.append((direction, app_name)))
    monkeypatch.setattr("builtins.input", lambda prompt="": "mauvais-slug")
    actions.action_ship(None, "exemple-apps")
    assert ("to_production", "exemple-apps") not in calls, (
        "workshop_move ne doit pas être appelé si la confirmation échoue"
    )


def test_action_start_stop_app_calls_app_make(monkeypatch):
    """start/stop d'une app doit appeler app_make (pas hub_make)."""
    calls = []
    monkeypatch.setattr(actions, "app_make",
                        lambda app_name, target, *, dry_run=False, console=None:
                        calls.append((app_name, target)))
    monkeypatch.setattr(actions, "_ensure_jwt_keys", lambda console, *, dry_run=False: None)
    actions.action_start_stop(None, "start", target="exemple-apps")
    assert ("exemple-apps", "up") in calls


def test_action_start_stop_hub_default_unchanged(monkeypatch):
    """Sans target, action_start_stop garde le comportement hub (compatibilité)."""
    calls = []
    monkeypatch.setattr(actions, "hub_make",
                        lambda target, *, dry_run=False, console=None: calls.append(target))
    monkeypatch.setattr(actions, "_ensure_jwt_keys", lambda console, *, dry_run=False: None)
    actions.action_start_stop(None, "stop")
    assert "down" in calls


# ── Task 1: action_logs + action_restart + _compose_file ──

def test_compose_file_hub():
    from hubctl_cli import actions, hub_state
    with mock.patch.object(actions.hub_state, "HUB_DIR", hub_state.PARC_ROOT / "HUB_base"):
        p = actions._compose_file("hub")
        assert p is not None
        assert p.name == "docker-compose.yml"


def test_compose_file_unknown_app_returns_none(tmp_path, monkeypatch):
    from hubctl_cli import actions, hub_state
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", tmp_path)
    assert actions._compose_file("inexistante") is None


def test_action_logs_all_services(tmp_path, monkeypatch):
    from hubctl_cli import actions, hub_state
    monkeypatch.setattr(actions.hub_state, "_mode", "prod")
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", tmp_path)
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "docker-compose.yml").write_text("")
    with mock.patch.object(actions.utils, "run_shell", return_value=0) as rs:
        actions.action_logs(mock.Mock(), "shop")
    cmd = rs.call_args.args[0]
    assert cmd[:5] == ["docker", "compose", "-f", str(tmp_path / "shop" / "docker-compose.yml"), "logs"]
    assert "api" not in cmd and "postgres" not in cmd


def test_action_logs_filter_api(tmp_path, monkeypatch):
    from hubctl_cli import actions, hub_state
    monkeypatch.setattr(actions.hub_state, "_mode", "prod")
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", tmp_path)
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "docker-compose.yml").write_text("")
    with mock.patch.object(actions.utils, "run_shell", return_value=0) as rs:
        actions.action_logs(mock.Mock(), "shop", service="api")
    assert rs.call_args.args[0][-1] == "api"


def test_action_logs_unknown_target_no_run():
    from hubctl_cli import actions
    with mock.patch.object(actions.utils, "run_shell") as rs:
        actions.action_logs(mock.Mock(), "inexistante")
    rs.assert_not_called()


def test_action_restart_with_service(tmp_path, monkeypatch):
    from hubctl_cli import actions, hub_state
    monkeypatch.setattr(actions.hub_state, "_mode", "prod")
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", tmp_path)
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "docker-compose.yml").write_text("")
    with mock.patch.object(actions.utils, "run_shell", return_value=0) as rs:
        actions.action_restart(mock.Mock(), "shop", service="frontend")
    cmd = rs.call_args.args[0]
    assert "restart" in cmd
    assert cmd[-1] == "frontend"


# ── Task 2: health + db-shell ──

def test_resolve_api_port_hub(tmp_path, monkeypatch):
    from hubctl_cli import actions, hub_state
    fake_hub = tmp_path / "HUB_base"
    fake_hub.mkdir()
    (fake_hub / ".env").write_text("HUB_API_PORT=8005\n")
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", fake_hub)
    assert actions._resolve_api_port("hub") == "8005"


def test_resolve_api_port_app(tmp_path, monkeypatch):
    from hubctl_cli import actions, hub_state
    monkeypatch.setattr(actions.hub_state, "_mode", "prod")
    apps = tmp_path / "APPS"; (apps / "shop").mkdir(parents=True)
    (apps / "shop" / ".env").write_text("API_PORT=9010\n")
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", apps)
    assert actions._resolve_api_port("shop") == "9010"


def test_action_health_healthy(monkeypatch):
    from hubctl_cli import actions
    monkeypatch.setattr(actions, "_resolve_api_port", lambda t: "8000")
    fake = mock.Mock(returncode=0, stdout='{"status":"healthy","version":"1.0"}')
    with mock.patch.object(actions.subprocess, "run", return_value=fake):
        rc = actions.action_health(mock.Mock(), "hub")
    assert rc == 0


def test_action_health_unhealthy(monkeypatch):
    from hubctl_cli import actions
    monkeypatch.setattr(actions, "_resolve_api_port", lambda t: "8000")
    fake = mock.Mock(returncode=0, stdout='{"status":"unhealthy"}')
    with mock.patch.object(actions.subprocess, "run", return_value=fake):
        rc = actions.action_health(mock.Mock(), "hub")
    assert rc == 1


def test_action_health_detailed_url(monkeypatch):
    from hubctl_cli import actions
    monkeypatch.setattr(actions, "_resolve_api_port", lambda t: "8000")
    fake = mock.Mock(returncode=0, stdout='{"status":"healthy"}')
    with mock.patch.object(actions.subprocess, "run", return_value=fake) as rs:
        actions.action_health(mock.Mock(), "hub", detailed=True)
    assert "/api/health/detailed" in rs.call_args.args[0][-1]


def test_app_db_creds(tmp_path, monkeypatch):
    from hubctl_cli import actions, hub_state
    monkeypatch.setattr(actions.hub_state, "_mode", "prod")
    apps = tmp_path / "APPS"; (apps / "shop").mkdir(parents=True)
    (apps / "shop" / ".env").write_text("POSTGRES_USER=zoe\nPOSTGRES_DB=shopdb\n")
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", apps)
    assert actions._app_db_creds("shop") == ("zoe", "shopdb")


def test_action_db_shell_invokes_psql(tmp_path, monkeypatch):
    from hubctl_cli import actions, hub_state
    monkeypatch.setattr(actions.hub_state, "_mode", "prod")
    apps = tmp_path / "APPS"; (apps / "shop").mkdir(parents=True)
    (apps / "shop" / "docker-compose.yml").write_text("")
    (apps / "shop" / ".env").write_text("POSTGRES_USER=zoe\nPOSTGRES_DB=shopdb\n")
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", apps)
    with mock.patch.object(actions.utils, "run_shell", return_value=0) as rs:
        actions.action_db_shell(mock.Mock(), "shop")
    cmd = rs.call_args.args[0]
    assert "exec" in cmd and "postgres" in cmd
    assert "zoe" in cmd and "shopdb" in cmd


def test_action_db_shell_unknown_app():
    from hubctl_cli import actions
    with mock.patch.object(actions.utils, "run_shell") as rs:
        actions.action_db_shell(mock.Mock(), "nope")
    rs.assert_not_called()


def test_ensure_jwt_keys_skips_when_present(tmp_path, monkeypatch):
    from hubctl_cli import actions, hub_state
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "jwt_private_key.pem").write_text("x")
    (secrets / "jwt_public_key.pem").write_text("x")
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", tmp_path)
    with mock.patch.object(actions.utils, "run_shell") as rs:
        actions._ensure_jwt_keys(dry_run=False)
    rs.assert_not_called()


def test_ensure_jwt_keys_generates_when_missing(tmp_path, monkeypatch):
    from hubctl_cli import actions, hub_state
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", tmp_path)
    with mock.patch.object(actions.utils, "run_shell", return_value=0) as rs:
        actions._ensure_jwt_keys(mock.Mock(), dry_run=False)
    cmds = [c.args[0] for c in rs.call_args_list]
    assert any(c[:2] == ["openssl", "genrsa"] for c in cmds)
    assert any(c[:2] == ["openssl", "rsa"] for c in cmds)
    assert any(c[:2] == ["chmod", "600"] for c in cmds)


def test_ensure_jwt_keys_dry_run_no_openssl(tmp_path, monkeypatch):
    from hubctl_cli import actions, hub_state
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", tmp_path)
    with mock.patch.object(actions.utils, "run_shell", return_value=0) as rs:
        actions._ensure_jwt_keys(mock.Mock(), dry_run=True)
    cmds = [c.args[0] for c in rs.call_args_list]
    assert not any(c[:2] == ["openssl", "genrsa"] for c in cmds)


# ── Task 5: Blue-green helpers ──

def test_port_in_use_true():
    from hubctl_cli import actions
    fake = mock.Mock(stdout="0.0.0.0:8000 users:((sshd))")
    with mock.patch.object(actions.subprocess, "run", return_value=fake):
        assert actions._port_in_use(8000) is True


def test_port_in_use_false():
    from hubctl_cli import actions
    fake = mock.Mock(stdout="0.0.0.0:9000 users:((sshd))")
    with mock.patch.object(actions.subprocess, "run", return_value=fake):
        assert actions._port_in_use(8000) is False


def test_find_free_ports_first_try():
    from hubctl_cli import actions
    with mock.patch.object(actions, "_port_in_use", return_value=False):
        assert actions._find_free_ports(8000, 80, 443) == (8000, 80, 443)


def test_find_free_ports_increments():
    from hubctl_cli import actions
    side = {8000: True, 80: False, 443: False}
    with mock.patch.object(actions, "_port_in_use", side_effect=lambda p: side.get(p, False)):
        api, fe, https = actions._find_free_ports(8000, 80, 443)
        assert api == 8001 and fe == 81 and https == 444


def test_find_free_ports_no_port_raises():
    from hubctl_cli import actions
    with mock.patch.object(actions, "_port_in_use", return_value=True):
        try:
            actions._find_free_ports(8000, 80, 443)
            assert False, "devrait lever RuntimeError"
        except RuntimeError:
            pass


def test_db_snapshot_app_success(tmp_path, monkeypatch):
    from hubctl_cli import actions, hub_state
    apps = tmp_path / "APPS"; (apps / "shop").mkdir(parents=True)
    (apps / "shop" / "docker-compose.yml").write_text("")
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", apps)
    monkeypatch.setattr(actions.hub_state, "PARC_ROOT", tmp_path)
    with mock.patch.object(actions.subprocess, "run", return_value=mock.Mock(returncode=0)):
        path = actions._db_snapshot_app("shop")
    assert path is not None and path.exists()


def test_db_snapshot_app_failure_returns_none(tmp_path, monkeypatch):
    from hubctl_cli import actions, hub_state
    apps = tmp_path / "APPS"; (apps / "shop").mkdir(parents=True)
    (apps / "shop" / "docker-compose.yml").write_text("")
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", apps)
    monkeypatch.setattr(actions.hub_state, "PARC_ROOT", tmp_path)
    with mock.patch.object(actions.subprocess, "run", side_effect=actions.subprocess.CalledProcessError(1, "docker")):
        assert actions._db_snapshot_app("shop") is None


# ── Task 6: Blue-green deploy ──

def test_read_prod_ports_hub(tmp_path, monkeypatch):
    from hubctl_cli import actions
    fake_hub = tmp_path / "HUB_base"; fake_hub.mkdir()
    (fake_hub / ".env").write_text("HUB_API_PORT=8000\nHUB_FRONTEND_PORT=80\nHUB_HTTPS_PORT=443\n")
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", fake_hub)
    assert actions._read_prod_ports("hub") == (8000, 80, 443)


def test_deploy_success_flow(tmp_path, monkeypatch):
    from hubctl_cli import actions, hub_state
    # préparer cible hub factice
    fake_hub = tmp_path / "HUB_base"; fake_hub.mkdir()
    (fake_hub / "docker-compose.yml").write_text("")
    (fake_hub / ".env").write_text("HUB_API_PORT=8000\nHUB_FRONTEND_PORT=80\nHUB_HTTPS_PORT=443\n")
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", fake_hub)
    monkeypatch.setattr(actions.hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(actions, "disk_guard", lambda *a, **k: True)
    monkeypatch.setattr(actions, "_git_previous_commit", lambda *a, **k: "abc1234")
    monkeypatch.setattr(actions, "_db_snapshot", lambda *a, **k: tmp_path / "snap.sql")
    monkeypatch.setattr(actions, "_find_free_ports", lambda *a: (8001, 81, 444))
    monkeypatch.setattr(actions, "_health_check", mock.Mock(return_value=True))
    monkeypatch.setattr(actions, "_rollback", mock.Mock())
    with mock.patch.object(actions.utils, "run_shell", return_value=0) as rs:
        actions.action_deploy(mock.Mock(), "hub")
    cmds = [c.args[0] for c in rs.call_args_list]
    # séquence attendue : git pull, make build, compose -p hub-test up, compose -p hub down, compose -p hub up, compose -p hub-test down -v
    joined = [" ".join(c) for c in cmds]
    assert any("git" in j and "pull" in j for j in joined)
    assert any("make" in j and "build" in j for j in joined)
    assert any("-p" in j and "hub-test" in j and "up" in j for j in joined)
    assert any("-p" in j and j.count("hub") >= 1 and "down" in j for j in joined)
    assert any("-p" in j and "hub-test" in j and "down" in j for j in joined)
    # C1: verify env= is passed to the green instance compose up
    green_up_call = [c for c in rs.call_args_list if "-p" in c.args[0] and "hub-test" in c.args[0] and "up" in c.args[0]]
    assert len(green_up_call) == 1
    assert "env" in green_up_call[0].kwargs, "C1: env must be passed to green instance compose up"
    # C2: verify health_check gets port_override for green instance
    all_hc_args = [call for call in actions._health_check.call_args_list if call.kwargs.get("port_override") is not None]
    assert len(all_hc_args) >= 1, "C2: _health_check must receive port_override"


def test_deploy_git_pull_fails_rollback(tmp_path, monkeypatch):
    from hubctl_cli import actions
    fake_hub = tmp_path / "HUB_base"; fake_hub.mkdir()
    (fake_hub / "docker-compose.yml").write_text("")
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", fake_hub)
    monkeypatch.setattr(actions, "disk_guard", lambda *a, **k: True)
    monkeypatch.setattr(actions, "_git_previous_commit", lambda *a, **k: "abc1234")
    monkeypatch.setattr(actions, "_db_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(actions, "_rollback", mock.Mock())
    with mock.patch.object(actions.utils, "run_shell", return_value=1):
        actions.action_deploy(mock.Mock(), "hub")
    actions._rollback.assert_called_once()


def test_deploy_health_fail_rollback(tmp_path, monkeypatch):
    from hubctl_cli import actions
    fake_hub = tmp_path / "HUB_base"; fake_hub.mkdir()
    (fake_hub / "docker-compose.yml").write_text("")
    (fake_hub / ".env").write_text("HUB_API_PORT=8000\nHUB_FRONTEND_PORT=80\nHUB_HTTPS_PORT=443\n")
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", fake_hub)
    monkeypatch.setattr(actions.hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(actions, "disk_guard", lambda *a, **k: True)
    monkeypatch.setattr(actions, "_git_previous_commit", lambda *a, **k: "abc")
    monkeypatch.setattr(actions, "_db_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(actions, "_find_free_ports", lambda *a: (8001, 81, 444))
    monkeypatch.setattr(actions, "_health_check", lambda *a, **k: False)
    monkeypatch.setattr(actions, "_rollback", mock.Mock())
    with mock.patch.object(actions.utils, "run_shell", return_value=0):
        actions.action_deploy(mock.Mock(), "hub")
    actions._rollback.assert_called_once()
    # C3: for hub, db_container/db_name should be None (defaults)
    call_kwargs = actions._rollback.call_args.kwargs
    assert call_kwargs.get("db_container") is None, "C3: hub uses default db_container"
    assert call_kwargs.get("db_name") is None, "C3: hub uses default db_name"


def test_deploy_dry_run_no_real_docker(tmp_path, monkeypatch):
    from hubctl_cli import actions
    fake_hub = tmp_path / "HUB_base"; fake_hub.mkdir()
    (fake_hub / "docker-compose.yml").write_text("")
    (fake_hub / ".env").write_text("HUB_API_PORT=8000\nHUB_FRONTEND_PORT=80\nHUB_HTTPS_PORT=443\n")
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", fake_hub)
    monkeypatch.setattr(actions.hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(actions, "disk_guard", lambda *a, **k: True)
    monkeypatch.setattr(actions, "_git_previous_commit", lambda *a, **k: "abc")
    monkeypatch.setattr(actions, "_db_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(actions, "_find_free_ports", lambda *a: (8001, 81, 444))
    monkeypatch.setattr(actions, "_health_check", lambda *a, **k: True)
    with mock.patch.object(actions.utils, "run_shell", return_value=0) as rs:
        actions.action_deploy(mock.Mock(), "hub", dry_run=True)
    # toutes les invocations ont dry_run=True
    for c in rs.call_args_list:
        assert c.kwargs.get("dry_run") is True


def test_deploy_dry_run_skips_real_actions(tmp_path, monkeypatch):
    """C6+C7+C8 : deploy --dry-run ne doit lancer AUCUNE action réelle.

    - C6 : _rollback reçoit dry_run=True
    - C7 : _health_check n'est jamais appelée (simulée en dry-run)
    - C8 : _db_snapshot jamais appelée + aucun pg_dump via subprocess
    - aucun run_shell sans dry_run=True
    """
    from hubctl_cli import actions
    fake_hub = tmp_path / "HUB_base"; fake_hub.mkdir()
    (fake_hub / "docker-compose.yml").write_text("")
    (fake_hub / ".env").write_text("HUB_API_PORT=8000\nHUB_FRONTEND_PORT=80\nHUB_HTTPS_PORT=443\n")
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", fake_hub)
    monkeypatch.setattr(actions.hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(actions, "disk_guard", lambda *a, **k: True)
    monkeypatch.setattr(actions, "_git_previous_commit", lambda *a, **k: "abc1234")
    monkeypatch.setattr(actions, "_find_free_ports", lambda *a: (8001, 81, 444))

    rollback_mock = mock.Mock()
    monkeypatch.setattr(actions, "_rollback", rollback_mock)
    health_mock = mock.Mock(return_value=True)
    monkeypatch.setattr(actions, "_health_check", health_mock)
    db_snap_mock = mock.Mock(return_value=tmp_path / "snap.sql")
    monkeypatch.setattr(actions, "_db_snapshot", db_snap_mock)

    subproc_cmds: list[list[str]] = []

    def spy_subprocess(cmd, **kwargs):
        subproc_cmds.append(cmd)
        return mock.Mock(returncode=0)

    monkeypatch.setattr(actions.subprocess, "run", spy_subprocess)

    # git pull échoue → déclenche le chemin rollback en dry-run
    with mock.patch.object(actions.utils, "run_shell", return_value=1) as rs:
        actions.action_deploy(mock.Mock(), "hub", dry_run=True)

    # C6 : rollback appelé avec dry_run=True
    rollback_mock.assert_called_once()
    assert rollback_mock.call_args.kwargs.get("dry_run") is True, \
        "C6: _rollback doit recevoir dry_run=True"

    # C7 : _health_check jamais appelée (simulée en dry-run)
    health_mock.assert_not_called()

    # C8 : _db_snapshot jamais appelée (snapshot skippé en dry-run)
    db_snap_mock.assert_not_called()
    assert not any("pg_dump" in " ".join(c) for c in subproc_cmds), \
        "C8: pg_dump ne doit pas être exécuté en dry-run"

    # Aucun run_shell sans dry_run=True
    for c in rs.call_args_list:
        assert c.kwargs.get("dry_run") is True, \
            f"run_shell appelé sans dry_run=True : {c.args[0]}"


# ── Workshop create tests ([→] sans suppression de prod) ──

def test_workshop_create_dry_run_no_dest_no_delete(tmp_path, monkeypatch):
    """[→] dry-run : ne crée pas l'atelier, ne supprime pas la prod, ne backup pas."""
    hub_dir = tmp_path / "HUB_base"; hub_dir.mkdir(); (hub_dir / ".dev-mode").touch()
    apps_dir = tmp_path / "APPS"; apps_dir.mkdir()
    ws_dir = tmp_path / "APP_Workshop"; ws_dir.mkdir()
    (tmp_path / "hubctl_cli" / "backups").mkdir(parents=True)
    (tmp_path / "hubctl_cli" / "_trash").mkdir(parents=True)
    app_dir = apps_dir / "testapp"; app_dir.mkdir()
    (app_dir / "docker-compose.yml").write_text("services: {}\n")
    (app_dir / ".env").write_text("APP_SLUG=testapp\nAPI_PORT=9001\n")

    monkeypatch.setattr(actions.hub_state, "check_docker", lambda: True)
    with mock.patch.object(actions.hub_state, "HUB_DIR", hub_dir), \
         mock.patch.object(actions.hub_state, "APPS_DIR", apps_dir), \
         mock.patch.object(actions.hub_state, "WORKSHOP_DIR", ws_dir), \
         mock.patch.object(actions.hub_state, "PARC_ROOT", tmp_path):
        actions.action_workshop_create(_fake_console(), "testapp", dry_run=True)

    assert not (ws_dir / "testapp-ws").exists()   # dry-run : rien créé
    assert (apps_dir / "testapp").exists()         # prod JAMAIS supprimée
    assert not (tmp_path / "hubctl_cli" / "_trash" / "testapp-ws").exists()  # pas de trash


def test_workshop_create_refuses_if_dest_exists(tmp_path, monkeypatch):
    """[→] refuse si l'atelier X-ws existe déjà (early-return sans effet de bord)."""
    hub_dir = tmp_path / "HUB_base"; hub_dir.mkdir(); (hub_dir / ".dev-mode").touch()
    apps_dir = tmp_path / "APPS"; apps_dir.mkdir()
    ws_dir = tmp_path / "APP_Workshop"; ws_dir.mkdir()
    (apps_dir / "testapp").mkdir()
    (apps_dir / "testapp" / "docker-compose.yml").write_text("services: {}\n")
    (apps_dir / "testapp" / ".env").write_text("APP_SLUG=testapp\nSENTINEL=keep\n")
    (ws_dir / "testapp-ws").mkdir()   # déjà en atelier

    monkeypatch.setattr(actions.hub_state, "check_docker", lambda: True)
    calls = []
    monkeypatch.setattr(actions.utils, "run_shell", lambda cmd, **kw: calls.append(cmd) or 0)
    with mock.patch.object(actions.hub_state, "HUB_DIR", hub_dir), \
         mock.patch.object(actions.hub_state, "APPS_DIR", apps_dir), \
         mock.patch.object(actions.hub_state, "WORKSHOP_DIR", ws_dir):
        actions.action_workshop_create(_fake_console(), "testapp", dry_run=False)
    # Aucune commande shell (pas de stop, pas de copy) : early-return avant tout effet.
    assert calls == [], "Ne doit rien exécuter si l'atelier existe déjà"
    # Atelier existant préservé :
    assert (ws_dir / "testapp-ws").exists()
    # Source intacte (aucune modification de .env) :
    assert (apps_dir / "testapp" / ".env").read_text().find("SENTINEL=keep") >= 0


def test_workshop_create_real_run_leaves_prod_in_place(tmp_path, monkeypatch):
    """[→] run réel : crée l'atelier (clone DB mocké), prod laissée en place, PAS de backup/trash."""
    hub_dir = tmp_path / "HUB_base"; hub_dir.mkdir(); (hub_dir / ".dev-mode").touch()
    apps_dir = tmp_path / "APPS"; apps_dir.mkdir()
    ws_dir = tmp_path / "APP_Workshop"; ws_dir.mkdir()
    (tmp_path / "hubctl_cli" / "backups").mkdir(parents=True)
    app_dir = apps_dir / "testapp"; app_dir.mkdir()
    (app_dir / "docker-compose.yml").write_text("services: {}\n")
    (app_dir / ".env").write_text("APP_SLUG=testapp\nAPI_PORT=9001\n")

    monkeypatch.setattr(actions.hub_state, "check_docker", lambda: True)
    # Stub toutes les commandes shell (stop, volume create/cp, build, up) + curl health OK.
    monkeypatch.setattr(actions.utils, "run_shell", lambda cmd, **kw: 0)
    monkeypatch.setattr(actions.subprocess, "run", lambda *a, **kw: mock.Mock(returncode=0, stdout='{"status":"healthy"}'))
    monkeypatch.setattr(actions.time, "sleep", lambda *_: None)

    with mock.patch.object(actions.hub_state, "HUB_DIR", hub_dir), \
         mock.patch.object(actions.hub_state, "APPS_DIR", apps_dir), \
         mock.patch.object(actions.hub_state, "WORKSHOP_DIR", ws_dir), \
         mock.patch.object(actions.hub_state, "PARC_ROOT", tmp_path):
        actions.action_workshop_create(_fake_console(), "testapp", dry_run=False)

    assert (ws_dir / "testapp-ws").exists()        # atelier créé
    assert (apps_dir / "testapp").exists()          # prod TOUJOURS là
    # PAS de backup tar.gz créé pour cette app :
    assert not list((tmp_path / "hubctl_cli" / "backups").glob("testapp-*.tar.gz"))
    # Slug renommé dans l'atelier :
    assert env_manager.read_env(ws_dir / "testapp-ws" / ".env").get("APP_SLUG") == "testapp-ws"


def test_workshop_create_never_uses_down_with_v(tmp_path, monkeypatch):
    """[→] action_workshop_create ne doit JAMAIS invoquer `docker compose down -v`."""
    hub_dir = tmp_path / "HUB_base"; hub_dir.mkdir(); (hub_dir / ".dev-mode").touch()
    apps_dir = tmp_path / "APPS"; apps_dir.mkdir()
    ws_dir = tmp_path / "APP_Workshop"; ws_dir.mkdir()
    (tmp_path / "hubctl_cli" / "backups").mkdir(parents=True)
    app_dir = apps_dir / "testapp"; app_dir.mkdir()
    (app_dir / "docker-compose.yml").write_text("services: {}\n")
    (app_dir / ".env").write_text("APP_SLUG=testapp\nAPI_PORT=9001\n")

    monkeypatch.setattr(actions.hub_state, "check_docker", lambda: True)
    rs_calls, sub_calls = [], []
    monkeypatch.setattr(actions.utils, "run_shell", lambda cmd, **kw: rs_calls.append(cmd) or 0)
    monkeypatch.setattr(actions.subprocess, "run", lambda *a, **kw: sub_calls.append(a) or mock.Mock(returncode=0, stdout='{"status":"healthy"}'))
    monkeypatch.setattr(actions.time, "sleep", lambda *_: None)

    with mock.patch.object(actions.hub_state, "HUB_DIR", hub_dir), \
         mock.patch.object(actions.hub_state, "APPS_DIR", apps_dir), \
         mock.patch.object(actions.hub_state, "WORKSHOP_DIR", ws_dir), \
         mock.patch.object(actions.hub_state, "PARC_ROOT", tmp_path):
        actions.action_workshop_create(_fake_console(), "testapp", dry_run=False)

    all_cmds = [" ".join(c) for c in rs_calls] + [" ".join(str(x) for x in a) for a in sub_calls]
    for cmd in all_cmds:
        assert not ("down" in cmd and "-v" in cmd), f"down -v interdit trouvé : {cmd}"


def test_prod_deploy_code_only_does_git_pull_no_volume_copy(tmp_path, monkeypatch):
    """[←] défaut (copy_db=False) : git pull + build + up, PAS de alpine cp sur volume."""
    calls = []
    def fake_run_shell(cmd, **kw):
        calls.append(cmd)
        return 0
    monkeypatch.setattr(actions.utils, "run_shell", fake_run_shell)
    monkeypatch.setattr(actions, "_git_previous_commit", lambda p, console=None: "abc1234")
    monkeypatch.setattr(actions, "_health_check", lambda target, console=None, timeout=60: True)
    monkeypatch.setattr(actions, "disk_guard", lambda console=None: True)

    hub_dir = tmp_path / "HUB_base"; hub_dir.mkdir(); (hub_dir / ".dev-mode").touch()
    apps_dir = tmp_path / "APPS"; apps_dir.mkdir()
    ws_dir = tmp_path / "APP_Workshop"; ws_dir.mkdir()
    prod = apps_dir / "myapp"; prod.mkdir()
    (prod / "docker-compose.yml").write_text("services: {}\n")
    (prod / ".env").write_text("APP_SLUG=myapp\nAPI_PORT=9005\n")
    (ws_dir / "myapp-ws").mkdir()  # atelier présent

    with mock.patch.object(actions.hub_state, "HUB_DIR", hub_dir), \
         mock.patch.object(actions.hub_state, "APPS_DIR", apps_dir), \
         mock.patch.object(actions.hub_state, "WORKSHOP_DIR", ws_dir):
        actions.action_prod_deploy(_fake_console(), "myapp", copy_db=False, dry_run=False)

    flat = [" ".join(c) for c in calls]
    assert any("git" in c and "pull" in c for c in flat), "git pull manquant"
    assert any("make" in c and "build" in c for c in flat), "make build manquant"
    assert any("make" in c and c.split()[-1] == "up" for c in flat), "make up manquant"
    # PAS de copie de volume :
    assert not any("alpine" in c for c in flat), "alpine cp ne doit pas apparaître en code-only"
    # PAS de docker volume rm (destructif) :
    assert not any("volume" in c and "rm" in c for c in flat)


def test_prod_deploy_code_only_rollback_on_health_fail(tmp_path, monkeypatch):
    """[←] code-only : health fail → git reset --hard {prev} (via _rollback)."""
    calls = []
    monkeypatch.setattr(actions.utils, "run_shell", lambda cmd, **kw: calls.append(cmd) or 0)
    monkeypatch.setattr(actions, "_git_previous_commit", lambda p, console=None: "deadbee")
    monkeypatch.setattr(actions, "_health_check", lambda target, console=None, timeout=60: False)
    monkeypatch.setattr(actions, "disk_guard", lambda console=None: True)

    hub_dir = tmp_path / "HUB_base"; hub_dir.mkdir(); (hub_dir / ".dev-mode").touch()
    apps_dir = tmp_path / "APPS"; apps_dir.mkdir()
    ws_dir = tmp_path / "APP_Workshop"; ws_dir.mkdir()
    prod = apps_dir / "myapp"; prod.mkdir()
    (prod / "docker-compose.yml").write_text("services: {}\n")
    (ws_dir / "myapp-ws").mkdir()

    with mock.patch.object(actions.hub_state, "HUB_DIR", hub_dir), \
         mock.patch.object(actions.hub_state, "APPS_DIR", apps_dir), \
         mock.patch.object(actions.hub_state, "WORKSHOP_DIR", ws_dir):
        actions.action_prod_deploy(_fake_console(), "myapp", copy_db=False, dry_run=False)

    flat = [" ".join(c) for c in calls]
    assert any("git" in c and "reset" in c and "deadbee" in c for c in flat), "git reset rollback manquant"


def test_prod_deploy_aborts_if_prod_missing(tmp_path, monkeypatch):
    """[←] si APPS/X absent → message, pas de crash (vieille app pré-refonte)."""
    hub_dir = tmp_path / "HUB_base"; hub_dir.mkdir(); (hub_dir / ".dev-mode").touch()
    apps_dir = tmp_path / "APPS"; apps_dir.mkdir()   # vide
    ws_dir = tmp_path / "APP_Workshop"; ws_dir.mkdir()
    (ws_dir / "myapp-ws").mkdir()
    monkeypatch.setattr(actions.hub_state, "check_docker", lambda: True)

    with mock.patch.object(actions.hub_state, "HUB_DIR", hub_dir), \
         mock.patch.object(actions.hub_state, "APPS_DIR", apps_dir), \
         mock.patch.object(actions.hub_state, "WORKSHOP_DIR", ws_dir):
        actions.action_prod_deploy(_fake_console(), "myapp", copy_db=False, dry_run=False)
    # Aucun effet secondaire : on ne doit pas avoir tenté de git pull.
    # (test passe si pas d'exception levée — le code retourne tôt avec un message.)


# ── Task 3: action_prod_deploy --copy-db (destructif, CLI-only) ──

def test_prod_deploy_copy_db_overwrites_volume_and_pulls(tmp_path, monkeypatch):
    """[←] --copy-db : backup volume + overwrite + git pull + build + up."""
    calls = []
    monkeypatch.setattr(actions.utils, "run_shell", lambda cmd, **kw: calls.append(cmd) or 0)
    sub_calls = []
    monkeypatch.setattr(actions.subprocess, "run", lambda *a, **kw: sub_calls.append(a) or mock.Mock(returncode=0, stdout=b''))
    monkeypatch.setattr(actions, "_git_previous_commit", lambda p, console=None: "abc1234")
    monkeypatch.setattr(actions, "_health_check", lambda target, console=None, timeout=60: True)
    monkeypatch.setattr(actions, "disk_guard", lambda console=None: True)
    monkeypatch.setattr("builtins.input", lambda *_: "myapp")  # gate : slug exact requis

    hub_dir = tmp_path / "HUB_base"; hub_dir.mkdir(); (hub_dir / ".dev-mode").touch()
    apps_dir = tmp_path / "APPS"; apps_dir.mkdir()
    ws_dir = tmp_path / "APP_Workshop"; ws_dir.mkdir()
    prod = apps_dir / "myapp"; prod.mkdir()
    (prod / "docker-compose.yml").write_text("services: {}\n")
    (ws_dir / "myapp-ws").mkdir()
    monkeypatch.setattr(actions.hub_state, "check_docker", lambda: True)

    with mock.patch.object(actions.hub_state, "HUB_DIR", hub_dir), \
         mock.patch.object(actions.hub_state, "APPS_DIR", apps_dir), \
         mock.patch.object(actions.hub_state, "WORKSHOP_DIR", ws_dir), \
         mock.patch.object(actions.hub_state, "PARC_ROOT", tmp_path):
        actions.action_prod_deploy(_fake_console(), "myapp", copy_db=True, dry_run=False)

    flat = [" ".join(c) for c in calls]
    # Overwrite de volume :
    assert any("alpine" in c and "cp" in c for c in flat), "alpine cp (overwrite DB) manquant"
    # git pull aussi (code cohérent avec la DB) :
    assert any("git" in c and "pull" in c for c in flat), "git pull manquant"


def test_prod_deploy_copy_db_aborts_on_wrong_confirmation(tmp_path, monkeypatch):
    """La gate input() : refus → aucun effet."""
    monkeypatch.setattr("builtins.input", lambda *_: "non-non-non")
    monkeypatch.setattr(actions, "disk_guard", lambda console=None: True)
    monkeypatch.setattr(actions.hub_state, "check_docker", lambda: True)

    hub_dir = tmp_path / "HUB_base"; hub_dir.mkdir(); (hub_dir / ".dev-mode").touch()
    apps_dir = tmp_path / "APPS"; apps_dir.mkdir()
    ws_dir = tmp_path / "APP_Workshop"; ws_dir.mkdir()
    prod = apps_dir / "myapp"; prod.mkdir()
    (prod / "docker-compose.yml").write_text("services: {}\n")
    (ws_dir / "myapp-ws").mkdir()

    calls = []
    monkeypatch.setattr(actions.utils, "run_shell", lambda cmd, **kw: calls.append(cmd) or 0)

    with mock.patch.object(actions.hub_state, "HUB_DIR", hub_dir), \
         mock.patch.object(actions.hub_state, "APPS_DIR", apps_dir), \
         mock.patch.object(actions.hub_state, "WORKSHOP_DIR", ws_dir):
        actions.action_prod_deploy(_fake_console(), "myapp", copy_db=True, dry_run=False)

    assert calls == [], "Aucune commande ne doit être exécutée si la confirmation échoue"


def test_prod_deploy_copy_db_rollback_restores_volume_to_data_mount(tmp_path, monkeypatch):
    """[←] --copy-db rollback : si health échoue, restore le volume depuis le backup
    avec un montage SYMÉTRIQUE au backup (dst_vol:/data, -C /) — pas /to."""
    rs_calls = []
    sub_calls = []
    monkeypatch.setattr(actions.utils, "run_shell", lambda cmd, **kw: rs_calls.append(cmd) or 0)
    def fake_sub(*a, **kw):
        sub_calls.append(a)
        # Backup tar retourne un rc sain ; les autres subprocess aussi.
        return mock.Mock(returncode=0, stdout=b'')
    monkeypatch.setattr(actions.subprocess, "run", fake_sub)
    monkeypatch.setattr(actions, "_git_previous_commit", lambda p, console=None: "abc1234")
    monkeypatch.setattr(actions, "_health_check", lambda target, console=None, timeout=60: False)  # force rollback
    monkeypatch.setattr(actions, "disk_guard", lambda console=None: True)
    monkeypatch.setattr(actions.hub_state, "check_docker", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: "myapp")  # gate OK

    hub_dir = tmp_path / "HUB_base"; hub_dir.mkdir(); (hub_dir / ".dev-mode").touch()
    apps_dir = tmp_path / "APPS"; apps_dir.mkdir()
    ws_dir = tmp_path / "APP_Workshop"; ws_dir.mkdir()
    prod = apps_dir / "myapp"; prod.mkdir()
    (prod / "docker-compose.yml").write_text("services: {}\n")
    (ws_dir / "myapp-ws").mkdir()

    with mock.patch.object(actions.hub_state, "HUB_DIR", hub_dir), \
         mock.patch.object(actions.hub_state, "APPS_DIR", apps_dir), \
         mock.patch.object(actions.hub_state, "WORKSHOP_DIR", ws_dir), \
         mock.patch.object(actions.hub_state, "PARC_ROOT", tmp_path):
        actions.action_prod_deploy(_fake_console(), "myapp", copy_db=True, dry_run=False)

    # Le rollback doit contenir une commande alpine tar xzf avec montage dst_vol:/data (pas /to)
    sub_flat = [" ".join(str(x) for x in a) for a in sub_calls]
    restore_cmds = [c for c in sub_flat if "tar" in c and "xzf" in c]
    assert restore_cmds, "rollback restore (tar xzf) manquant"
    assert any("myapp-postgres-data:/data" in c for c in restore_cmds), \
        f"restore doit monter dst_vol à /data (symétrique au backup), vu : {restore_cmds}"
    assert not any(":/to" in c for c in restore_cmds), "le montage /to asymétrique ne doit plus exister"


# ── Task 4: action_workshop_remove ([×] suppression atelier) ──

def test_workshop_remove_dry_run_keeps_atelier(tmp_path, monkeypatch):
    """[×] dry-run : ne supprime rien, ne restart rien."""
    hub_dir = tmp_path / "HUB_base"; hub_dir.mkdir(); (hub_dir / ".dev-mode").touch()
    apps_dir = tmp_path / "APPS"; apps_dir.mkdir()
    ws_dir = tmp_path / "APP_Workshop"; ws_dir.mkdir()
    (apps_dir / "myapp").mkdir()  # prod présente
    ws = ws_dir / "myapp-ws"; ws.mkdir()
    (ws / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "hubctl_cli" / "backups").mkdir(parents=True)
    (tmp_path / "hubctl_cli" / "_trash").mkdir(parents=True)
    monkeypatch.setattr(actions.hub_state, "check_docker", lambda: True)

    with mock.patch.object(actions.hub_state, "HUB_DIR", hub_dir), \
         mock.patch.object(actions.hub_state, "APPS_DIR", apps_dir), \
         mock.patch.object(actions.hub_state, "WORKSHOP_DIR", ws_dir), \
         mock.patch.object(actions.hub_state, "PARC_ROOT", tmp_path):
        actions.action_workshop_remove(_fake_console(), "myapp-ws", dry_run=True)

    assert ws.exists(), "dry-run ne doit pas supprimer l'atelier"


def test_workshop_remove_real_run_trashes_and_restarts_prod(tmp_path, monkeypatch):
    """[×] run réel : down -v + trash + restart prod."""
    hub_dir = tmp_path / "HUB_base"; hub_dir.mkdir(); (hub_dir / ".dev-mode").touch()
    apps_dir = tmp_path / "APPS"; apps_dir.mkdir()
    ws_dir = tmp_path / "APP_Workshop"; ws_dir.mkdir()
    prod = apps_dir / "myapp"; prod.mkdir()
    (prod / "docker-compose.yml").write_text("services: {}\n")
    ws = ws_dir / "myapp-ws"; ws.mkdir()
    (ws / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "hubctl_cli" / "backups").mkdir(parents=True)
    (tmp_path / "hubctl_cli" / "_trash").mkdir(parents=True)

    calls = []
    monkeypatch.setattr(actions.utils, "run_shell", lambda cmd, **kw: calls.append(cmd) or 0)
    monkeypatch.setattr(actions.hub_state, "check_docker", lambda: True)

    with mock.patch.object(actions.hub_state, "HUB_DIR", hub_dir), \
         mock.patch.object(actions.hub_state, "APPS_DIR", apps_dir), \
         mock.patch.object(actions.hub_state, "WORKSHOP_DIR", ws_dir), \
         mock.patch.object(actions.hub_state, "PARC_ROOT", tmp_path):
        actions.action_workshop_remove(_fake_console(), "myapp-ws", dry_run=False)

    assert not ws.exists(), "atelier doit être supprimé"
    flat = [" ".join(c) for c in calls]
    assert any("down" in c and "-v" in c for c in flat), "docker compose down -v manquant"
    assert any("make" in c and "up" in c for c in flat), "restart prod (make up) manquant"


def test_workshop_remove_skips_prod_restart_if_absent(tmp_path, monkeypatch):
    """[×] si prod absente : supprime l'atelier, ne tente pas de restart (warning)."""
    hub_dir = tmp_path / "HUB_base"; hub_dir.mkdir(); (hub_dir / ".dev-mode").touch()
    apps_dir = tmp_path / "APPS"; apps_dir.mkdir()   # vide
    ws_dir = tmp_path / "APP_Workshop"; ws_dir.mkdir()
    ws = ws_dir / "myapp-ws"; ws.mkdir()
    (ws / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "hubctl_cli" / "backups").mkdir(parents=True)
    (tmp_path / "hubctl_cli" / "_trash").mkdir(parents=True)

    calls = []
    monkeypatch.setattr(actions.utils, "run_shell", lambda cmd, **kw: calls.append(cmd) or 0)
    monkeypatch.setattr(actions.hub_state, "check_docker", lambda: True)

    with mock.patch.object(actions.hub_state, "HUB_DIR", hub_dir), \
         mock.patch.object(actions.hub_state, "APPS_DIR", apps_dir), \
         mock.patch.object(actions.hub_state, "WORKSHOP_DIR", ws_dir), \
         mock.patch.object(actions.hub_state, "PARC_ROOT", tmp_path):
        actions.action_workshop_remove(_fake_console(), "myapp-ws", dry_run=False)

    assert not ws.exists()
    flat = [" ".join(c) for c in calls]
    assert not any("make" in c and "up" in c for c in flat), "ne doit pas tenter make up si prod absente"
