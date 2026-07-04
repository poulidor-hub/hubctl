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


def test_streamwriter_forwards_lines_to_callback():
    from hubctl_cli import utils
    got = []
    utils.set_stream_callback(got.append)
    try:
        w = utils.StreamWriter()
        w.write("hello\nworld\n")
    finally:
        utils.set_stream_callback(None)
    assert got == ["hello", "world"]


def test_streamwriter_buffers_partial_line():
    from hubctl_cli import utils
    got = []
    utils.set_stream_callback(got.append)
    try:
        w = utils.StreamWriter()
        w.write("par")
        assert got == []
        w.write("tial\n")
    finally:
        utils.set_stream_callback(None)
    assert got == ["partial"]


def test_streamwriter_flush_emits_remainder():
    from hubctl_cli import utils
    got = []
    utils.set_stream_callback(got.append)
    try:
        w = utils.StreamWriter()
        w.write("no newline")
        w.flush()
    finally:
        utils.set_stream_callback(None)
    assert got == ["no newline"]


def test_streamwriter_falls_back_to_stdout(monkeypatch):
    import io
    from hubctl_cli import utils
    utils.set_stream_callback(None)
    fake = io.StringIO()
    monkeypatch.setattr(utils.sys, "__stdout__", fake)
    w = utils.StreamWriter()
    w.write("line1\n")
    assert "line1" in fake.getvalue()
