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


def test_menu_main_choices_contains_all_actions():
    from hubctl_cli import menu
    choices = menu.build_main_choices()
    joined = " ".join(choices).lower()
    for kw in ["update", "ajouter", "supprimer", "réparer", "logs", "start", "apps", "coffre", "quitter", "init"]:
        assert kw in joined


def _menu_canvas():
    c = mock.MagicMock()
    c.inner_cols = 98
    c.inner_rows = 28
    return c


def test_pause_waits_for_enter():
    from hubctl_cli import menu
    canvas = _menu_canvas()
    with mock.patch("sys.stdin.fileno", return_value=0), \
         mock.patch.object(menu.keys, "read_key", side_effect=[menu.keys.KeyEnter()]):
        menu.pause(canvas)
    assert canvas.render.call_count >= 1


def test_main_loop_clears_screen_then_quits():
    from hubctl_cli import menu
    canvas = _menu_canvas()
    with mock.patch.object(menu, "_retro_select", return_value="0. Quitter"), \
         mock.patch.object(menu.widgets, "select", return_value="0. Quitter"):
        menu.main_loop(canvas, {"docker": False, "services": []}, vault=None, dry_run=True)
    canvas.render.assert_called()


def test_main_loop_escape_quits():
    from hubctl_cli import menu
    canvas = _menu_canvas()
    with mock.patch.object(menu, "_retro_select", return_value=menu.widgets.ESC), \
         mock.patch.object(menu.widgets, "select", return_value=menu.widgets.ESC):
        menu.main_loop(canvas, {"docker": False}, vault=None, dry_run=True)
    canvas.render.assert_called()


def test_main_loop_ctrlc_at_menu_stays_then_quits():
    from hubctl_cli import menu
    canvas = _menu_canvas()
    with mock.patch.object(menu, "_retro_select",
                           side_effect=[KeyboardInterrupt(), "0. Quitter"]) as sel, \
         mock.patch.object(menu.widgets, "select", return_value="0. Quitter"):
        menu.main_loop(canvas, {"docker": False}, vault=None, dry_run=True)
    assert sel.call_count == 2


def test_main_loop_action_keyboardinterrupt_returns_to_menu():
    from hubctl_cli import menu
    canvas = _menu_canvas()
    with mock.patch.object(menu, "_retro_select",
                           side_effect=["4. Réparer / Diagnostiquer", menu.widgets.ESC]), \
         mock.patch.object(menu.widgets, "select", return_value=menu.widgets.ESC), \
         mock.patch.object(menu.actions, "action_repair",
                           side_effect=KeyboardInterrupt) as rep, \
         mock.patch.object(menu, "pause"):
        menu.main_loop(canvas, {"docker": False}, vault=None, dry_run=True)
    rep.assert_called_once()


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
