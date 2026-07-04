import sys
from unittest import mock
from hubctl_cli import widgets
from hubctl_cli.keys import KeyEnter, KeyUp, KeyDown, KeyEsc, KeyCtrlC, KeyChar, KeyBackspace


def _fake_canvas():
    c = mock.Mock()
    c.inner_cols = 98
    c.inner_rows = 28
    c.render = mock.Mock()
    return c


def _patch_keys(monkeypatch, keys_list):
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    it = iter(keys_list)

    def fake_read(fd, timeout=None):
        try:
            return next(it)
        except StopIteration:
            return None

    monkeypatch.setattr("hubctl_cli.widgets.keys.read_key", fake_read)


def test_select_highlight_enter_returns_first(monkeypatch):
    c = _fake_canvas()
    _patch_keys(monkeypatch, [KeyEnter()])
    result = widgets.select(c, "Pick:", ["A", "B", "C"])
    assert result == "A"


def test_select_down_enter_returns_second(monkeypatch):
    c = _fake_canvas()
    _patch_keys(monkeypatch, [KeyDown(), KeyEnter()])
    result = widgets.select(c, "Pick:", ["A", "B", "C"])
    assert result == "B"


def test_select_up_wraps_to_last(monkeypatch):
    c = _fake_canvas()
    _patch_keys(monkeypatch, [KeyUp(), KeyEnter()])
    result = widgets.select(c, "Pick:", ["A", "B", "C"])
    assert result == "C"


def test_select_digit_points_then_enter(monkeypatch):
    c = _fake_canvas()
    _patch_keys(monkeypatch, [KeyChar("2"), KeyEnter()])
    result = widgets.select(c, "Pick:", ["A", "B", "C"])
    assert result == "B"


def test_select_zero_returns_sentinel(monkeypatch):
    c = _fake_canvas()
    _patch_keys(monkeypatch, [KeyChar("0")])
    result = widgets.select(c, "Pick:", ["A", "B"])
    assert result is widgets.ESC


def test_select_digit_out_of_range_ignored(monkeypatch):
    c = _fake_canvas()
    _patch_keys(monkeypatch, [KeyChar("5"), KeyEnter()])
    result = widgets.select(c, "Pick:", ["A", "B"])
    assert result == "A"


def test_select_pagination_space_next_page(monkeypatch):
    c = _fake_canvas()
    choices = [f"item{i}" for i in range(12)]
    _patch_keys(monkeypatch, [KeyChar(" "), KeyEnter()])
    result = widgets.select(c, "Pick:", choices)
    assert result == "item9"


def test_select_pagination_minus_prev_wraps(monkeypatch):
    c = _fake_canvas()
    choices = [f"item{i}" for i in range(12)]
    _patch_keys(monkeypatch, [KeyChar("-"), KeyEnter()])
    result = widgets.select(c, "Pick:", choices)
    assert result == "item9"


def test_select_esc_returns_sentinel(monkeypatch):
    c = _fake_canvas()
    _patch_keys(monkeypatch, [KeyEsc()])
    result = widgets.select(c, "Pick:", ["A", "B"])
    assert result is widgets.ESC


def test_select_ctrl_c_raises(monkeypatch):
    import pytest
    c = _fake_canvas()
    _patch_keys(monkeypatch, [KeyCtrlC()])
    with pytest.raises(KeyboardInterrupt):
        widgets.select(c, "Pick:", ["A"])


def test_confirm_enter_default_true(monkeypatch):
    c = _fake_canvas()
    _patch_keys(monkeypatch, [KeyEnter()])
    result = widgets.confirm(c, "OK?", default=True)
    assert result is True


def test_confirm_enter_default_false(monkeypatch):
    c = _fake_canvas()
    _patch_keys(monkeypatch, [KeyEnter()])
    result = widgets.confirm(c, "OK?", default=False)
    assert result is False


def test_confirm_esc_returns_sentinel(monkeypatch):
    c = _fake_canvas()
    _patch_keys(monkeypatch, [KeyEsc()])
    result = widgets.confirm(c, "OK?")
    assert result is widgets.ESC


def test_confirm_digit_one_true(monkeypatch):
    c = _fake_canvas()
    _patch_keys(monkeypatch, [KeyChar("1"), KeyEnter()])
    assert widgets.confirm(c, "OK?", default=False) is True


def test_confirm_digit_two_false(monkeypatch):
    c = _fake_canvas()
    _patch_keys(monkeypatch, [KeyChar("2"), KeyEnter()])
    assert widgets.confirm(c, "OK?", default=True) is False


def test_confirm_o_alias_true(monkeypatch):
    c = _fake_canvas()
    _patch_keys(monkeypatch, [KeyChar("o"), KeyEnter()])
    assert widgets.confirm(c, "OK?", default=False) is True


def test_confirm_arrow_toggles(monkeypatch):
    c = _fake_canvas()
    _patch_keys(monkeypatch, [KeyDown(), KeyEnter()])
    assert widgets.confirm(c, "OK?", default=True) is False


def test_text_input_and_enter(monkeypatch):
    c = _fake_canvas()
    _patch_keys(monkeypatch, [
        KeyChar("h"), KeyChar("e"), KeyChar("l"), KeyChar("l"), KeyChar("o"),
        KeyEnter(),
    ])
    result = widgets.text(c, "Name:")
    assert result == "hello"


def test_text_backspace(monkeypatch):
    c = _fake_canvas()
    _patch_keys(monkeypatch, [
        KeyChar("a"), KeyChar("b"), KeyBackspace(), KeyChar("c"),
        KeyEnter(),
    ])
    result = widgets.text(c, "Name:")
    assert result == "ac"


def test_text_empty_enter(monkeypatch):
    c = _fake_canvas()
    _patch_keys(monkeypatch, [KeyEnter()])
    result = widgets.text(c, "Name:")
    assert result == ""


def test_text_esc_returns_sentinel(monkeypatch):
    c = _fake_canvas()
    _patch_keys(monkeypatch, [KeyEsc()])
    result = widgets.text(c, "Name:")
    assert result is widgets.ESC


def test_password_returns_clear_text(monkeypatch):
    c = _fake_canvas()
    _patch_keys(monkeypatch, [
        KeyChar("s"), KeyChar("e"), KeyChar("c"), KeyChar("r"), KeyChar("e"), KeyChar("t"),
        KeyEnter(),
    ])
    result = widgets.password(c, "Pass:")
    assert result == "secret"


def test_password_esc_returns_sentinel(monkeypatch):
    c = _fake_canvas()
    _patch_keys(monkeypatch, [KeyEsc()])
    result = widgets.password(c, "Pass:")
    assert result is widgets.ESC
