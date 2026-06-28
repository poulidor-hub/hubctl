from unittest import mock
from hubctl_cli import utils


def test_run_shell_dry_run_does_not_execute():
    with mock.patch("subprocess.Popen") as popen:
        rc = utils.run_shell(["echo", "hi"], dry_run=True)
    popen.assert_not_called()
    assert rc == 0


def test_run_shell_returns_process_returncode():
    rc = utils.run_shell(["true"], stream=False)
    assert rc == 0
    rc = utils.run_shell(["false"], stream=False)
    assert rc == 1


def test_confirm_phrase_matches_exactly():
    with mock.patch("builtins.input", return_value="exemple-apps"):
        assert utils.confirm_phrase("exemple-apps") is True
    with mock.patch("builtins.input", return_value="wrong"):
        assert utils.confirm_phrase("exemple-apps") is False
