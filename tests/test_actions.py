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
    with mock.patch.object(actions.hub_state, "get_disk_usage", return_value=fake), \
         mock.patch.object(actions.prompts, "yesno", return_value=False):
        assert actions.disk_guard() is False


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
    mgr.managed_env = False
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
    menu._ui_state["prompt_buf"] = ""
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
    menu._ui_state["prompt_buf"] = ""
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
    menu._ui_state["prompt_buf"] = ""
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
    menu._ui_state["prompt_buf"] = ""
    menu._nav_stack = ["main"]
    canvas = _menu_canvas()
    vault_mgr = _mock_vault_mgr()

    with mock.patch("sys.stdin.fileno", return_value=0), \
         mock.patch.object(menu.keys, "read_key",
                           side_effect=[menu.keys.KeyChar("6"),
                                        menu.keys.KeyEnter(),
                                        menu.keys.KeyChar("0"),
                                        menu.keys.KeyEnter()]), \
         mock.patch.object(menu, "_dispatch",
                           side_effect=[False, True]) as dispatch_mock:
        menu.main_loop(canvas, {"docker": False, "services": [], "apps": []},
                       vault_mgr, dry_run=True)
    assert dispatch_mock.call_count == 2
    assert dispatch_mock.call_args_list[0].args[0] == "6"
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


# ── Port helpers ──

def test_port_in_use_detects_port():
    with mock.patch("subprocess.run") as run:
        run.return_value.stdout = "LISTEN 0 128 0.0.0.0:8000 0.0.0.0:*"
        assert actions._port_in_use(8000) is True


def test_port_in_use_free():
    with mock.patch("subprocess.run") as run:
        run.return_value.stdout = "LISTEN 0 128 0.0.0.0:9000 0.0.0.0:*"
        assert actions._port_in_use(8000) is False


def test_find_free_ports_first_available():
    with mock.patch.object(actions, "_port_in_use", return_value=False):
        api, fe, https = actions._find_free_ports(8000, 80, 443)
    assert api == 8000
    assert fe == 80
    assert https == 443


def test_find_free_ports_skip_occupied():
    calls = [True, True, True, False, False, False]
    with mock.patch.object(actions, "_port_in_use", side_effect=calls):
        api, fe, https = actions._find_free_ports(8000, 80, 443)
    assert api == 8003  # short-circuit: 3 True skip offsets 0-2, then 3 False at offset 3
    assert fe == 83
    assert https == 446


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
