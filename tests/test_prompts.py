from unittest import mock
from hubctl_cli import prompts


def test_with_escape_uses_add_when_available():
    kb = mock.Mock()
    fake_app = mock.Mock()
    fake_app.key_bindings = kb
    fake_q = mock.Mock(application=fake_app)
    fake_q.ask.return_value = "ok"
    result = prompts._with_escape(fake_q)
    assert result == "ok"
    kb.add.assert_called_once_with("escape")


def test_with_escape_handles_merged_keybindings():
    fake_app = mock.Mock()
    fake_app.key_bindings = mock.Mock(spec=[])  # _MergedKeyBindings : pas de .add
    fake_q = mock.Mock(application=fake_app)
    fake_q.ask.return_value = "ok"
    with mock.patch("prompt_toolkit.key_binding.merge_key_bindings") as merge_kb:
        result = prompts._with_escape(fake_q)
    assert result == "ok"
    fake_q.ask.assert_called_once()
    merge_kb.assert_called_once()


def test_yesno_maps_choices():
    with mock.patch.object(prompts, "select", return_value="Oui"):
        assert prompts.yesno("?") is True
    with mock.patch.object(prompts, "select", return_value="Non"):
        assert prompts.yesno("?") is False
    with mock.patch.object(prompts, "select", return_value=prompts.ESC):
        assert prompts.yesno("?") is prompts.ESC
