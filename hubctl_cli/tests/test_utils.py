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


# ── run_shell : pas de fuite de processus sur timeout (#14) ──


def test_run_shell_kills_proc_on_timeout(monkeypatch):
    """run_shell (stream=True) doit tuer le processus fils en cas de timeout.
    `communicate(timeout=)` ne le tue pas — l'ancien code laissait des fils
    orphelins (docker/compose) s'accumuler."""
    import subprocess
    proc = mock.Mock()
    proc.communicate.side_effect = subprocess.TimeoutExpired(cmd=["x"], timeout=1)
    proc.returncode = 124
    monkeypatch.setattr(utils.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(utils, "_stream_callback", None)  # forcer la branche non-stream-callback
    rc = utils.run_shell(["sleep", "1000"], stream=True, timeout=1)
    assert rc == 124
    proc.kill.assert_called(), "le proc fils doit être tué sur timeout (pas d'orphelin)"


# ── cancel_current : robustesse (#15) ──


def test_cancel_current_kills_active_proc(monkeypatch):
    proc = mock.Mock()
    monkeypatch.setattr(utils, "_current_proc", proc)
    utils.cancel_current()
    proc.kill.assert_called_once()


def test_cancel_current_safe_when_none(monkeypatch):
    monkeypatch.setattr(utils, "_current_proc", None)
    utils.cancel_current()  # ne doit pas lever
