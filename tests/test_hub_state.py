from unittest import mock
from hubctl_cli import hub_state


def test_check_docker_true_when_returncode_zero():
    with mock.patch("subprocess.run") as run:
        run.return_value = mock.Mock(returncode=0)
        assert hub_state.check_docker() is True


def test_check_docker_false_when_missing():
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        assert hub_state.check_docker() is False


def test_disk_usage_percent_free():
    fake = mock.Mock(total=100, used=80, free=20)
    with mock.patch("shutil.disk_usage", return_value=fake):
        d = hub_state.get_disk_usage()
    assert d.percent_free == 20.0
    assert d.free == 20


def test_get_apps_lists_apps_dir(tmp_path, monkeypatch):
    apps = tmp_path / "APPS"
    (apps / "demo").mkdir(parents=True)
    (apps / "demo" / "VERSION").write_text("1.2.3\n")
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    with mock.patch.object(hub_state, "_git_dirty", return_value=False), \
         mock.patch.object(hub_state, "_dir_size", return_value=4096):
        result = hub_state.get_apps()
    assert len(result) == 1
    assert result[0].name == "demo"
    assert result[0].version == "1.2.3"
