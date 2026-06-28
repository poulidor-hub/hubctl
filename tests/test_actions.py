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
         mock.patch.object(actions.utils, "confirm", return_value=False):
        assert actions.disk_guard() is False


def test_menu_main_choices_contains_all_actions():
    from hubctl_cli import menu
    choices = menu.build_main_choices()
    joined = " ".join(choices).lower()
    for kw in ["update", "ajouter", "supprimer", "réparer", "logs", "start", "apps", "coffre", "quitter"]:
        assert kw in joined


def test_pause_waits_for_enter():
    from hubctl_cli import menu
    console = mock.Mock()
    with mock.patch("builtins.input", return_value=""):
        menu.pause(console)
    console.print.assert_called()


def test_main_loop_clears_screen_then_quits():
    from hubctl_cli import menu
    console = mock.Mock()
    with mock.patch.object(menu.prompts, "select", return_value="0. Quitter"):
        menu.main_loop(console, {"docker": False, "services": []}, vault=None, dry_run=True)
    console.clear.assert_called()


def test_main_loop_escape_quits():
    from hubctl_cli import menu
    console = mock.Mock()
    with mock.patch.object(menu.prompts, "select", return_value=menu.prompts.ESC):
        menu.main_loop(console, {"docker": False}, vault=None, dry_run=True)
    console.clear.assert_called()


def test_main_loop_ctrlc_at_menu_stays_then_quits():
    from hubctl_cli import menu
    console = mock.Mock()
    with mock.patch.object(menu.prompts, "select", side_effect=[None, "0. Quitter"]) as sel:
        menu.main_loop(console, {"docker": False}, vault=None, dry_run=True)
    assert sel.call_count == 2


def test_main_loop_action_keyboardinterrupt_returns_to_menu():
    from hubctl_cli import menu
    console = mock.Mock()
    with mock.patch.object(menu.prompts, "select",
                           side_effect=["4. Réparer / Diagnostiquer", menu.prompts.ESC]), \
         mock.patch.object(menu.actions, "action_repair", side_effect=KeyboardInterrupt) as rep, \
         mock.patch.object(menu, "pause"):
        menu.main_loop(console, {"docker": False}, vault=None, dry_run=True)
    rep.assert_called_once()
