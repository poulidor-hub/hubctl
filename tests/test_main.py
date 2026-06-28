from unittest import mock
from hubctl_cli.__main__ import build_parser
from hubctl_cli import __main__ as m


def test_parser_flags():
    p = build_parser()
    args = p.parse_args(["--no-anim", "--dry-run", "--anim", "fly-to-flock"])
    assert args.no_anim is True
    assert args.dry_run is True
    assert args.anim == "fly-to-flock"


def test_parser_defaults():
    p = build_parser()
    args = p.parse_args([])
    assert args.no_anim is False
    assert args.dry_run is False
    assert args.anim is None


def test_non_interactive_exits_cleanly_without_menu():
    with mock.patch.object(m, "_interactive", return_value=False), \
         mock.patch.object(m.animation, "preload_with_bar",
                           return_value={"docker": False, "services": []}), \
         mock.patch.object(m.menu, "main_loop") as ml:
        rc = m.main([])
    assert rc == 0
    ml.assert_not_called()


def test_keyboard_interrupt_during_intro_goes_to_menu():
    with mock.patch.object(m, "_interactive", return_value=True), \
         mock.patch.object(m.animation, "preload_with_bar", side_effect=KeyboardInterrupt), \
         mock.patch.object(m, "Vault"), \
         mock.patch.object(m.menu, "main_loop") as ml:
        rc = m.main([])
    assert rc == 0
    ml.assert_called_once()
