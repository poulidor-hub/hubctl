from hubctl_cli import keys


def test_decode_printable():
    assert isinstance(keys._decode(b"a"), keys.KeyChar)
    assert keys._decode(b"a").char == "a"
    assert isinstance(keys._decode(b"Z"), keys.KeyChar)
    assert keys._decode(b"Z").char == "Z"
    assert isinstance(keys._decode(b" "), keys.KeyChar)
    assert keys._decode(b" ").char == " "
    assert isinstance(keys._decode(b"\xc3\xa9"), keys.KeyChar)


def test_decode_enter():
    assert isinstance(keys._decode(b"\r"), keys.KeyEnter)
    assert isinstance(keys._decode(b"\n"), keys.KeyEnter)


def test_decode_backspace():
    assert isinstance(keys._decode(b"\x7f"), keys.KeyBackspace)
    assert isinstance(keys._decode(b"\x08"), keys.KeyBackspace)


def test_decode_ctrl_c():
    assert isinstance(keys._decode(b"\x03"), keys.KeyCtrlC)


def test_decode_escape():
    assert isinstance(keys._decode(b"\x1b"), keys.KeyEsc)


def test_decode_arrow_up():
    assert isinstance(keys._decode(b"\x1b[A"), keys.KeyUp)


def test_decode_arrow_down():
    assert isinstance(keys._decode(b"\x1b[B"), keys.KeyDown)


def test_decode_empty_raises():
    import pytest
    with pytest.raises(ValueError):
        keys._decode(b"")


def test_read_key_timeout_returns_none():
    import os
    r, w = os.pipe()
    try:
        result = keys.read_key(r, timeout=0.001)
        assert result is None
    finally:
        os.close(r)
        os.close(w)


def test_read_key_eof_raises():
    import pytest
    import os
    r, w = os.pipe()
    os.close(w)
    try:
        with pytest.raises(EOFError):
            keys.read_key(r, timeout=None)
    finally:
        os.close(r)
