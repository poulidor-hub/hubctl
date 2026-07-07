from unittest import mock
from hubctl_cli import actions


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
