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


def test_is_legacy_true_when_makefile_missing(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    assert hub_state._is_legacy(tmp_path) is True


def test_is_legacy_true_when_compose_missing(tmp_path):
    (tmp_path / "Makefile").write_text("up:\n")
    assert hub_state._is_legacy(tmp_path) is True


def test_is_legacy_false_when_both_present(tmp_path):
    (tmp_path / "Makefile").write_text("up:\n")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    assert hub_state._is_legacy(tmp_path) is False


def test_get_apps_sets_legacy_flag(tmp_path, monkeypatch):
    apps = tmp_path / "APPS"
    (apps / "compat").mkdir(parents=True)
    (apps / "compat" / "Makefile").write_text("up:\n")
    (apps / "compat" / "docker-compose.yml").write_text("services: {}\n")
    (apps / "old").mkdir(parents=True)
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    with mock.patch.object(hub_state, "_git_dirty", return_value=False), \
         mock.patch.object(hub_state, "_dir_size", return_value=0):
        result = {a.name: a.legacy for a in hub_state.get_apps()}
    assert result == {"compat": False, "old": True}


from pathlib import Path


def _app(tmp_path, name, legacy=False):
    from hubctl_cli.hub_state import AppInfo
    return AppInfo(name=name, path=tmp_path, version="1", git_dirty=False,
                   size_bytes=0, legacy=legacy)


def test_app_api_status_legacy_skips_probe(tmp_path):
    hub_state._api_status_cache.clear()
    with mock.patch("subprocess.run") as run:
        status = hub_state.app_api_status(_app(tmp_path, "old", legacy=True))
    assert status == "legacy"
    run.assert_not_called()


def test_app_api_status_running_on_2xx(tmp_path, monkeypatch):
    hub_state._api_status_cache.clear()
    monkeypatch.setattr(hub_state.env_manager, "read_env", lambda p: {"API_PORT": "8100"})
    with mock.patch("subprocess.run",
                    return_value=mock.Mock(returncode=0, stdout="200")):
        status = hub_state.app_api_status(_app(tmp_path, "compat"))
    assert status == "running"


def test_app_api_status_stopped_when_unreachable(tmp_path, monkeypatch):
    hub_state._api_status_cache.clear()
    monkeypatch.setattr(hub_state.env_manager, "read_env", lambda p: {})
    with mock.patch("subprocess.run",
                    return_value=mock.Mock(returncode=0, stdout="000")):
        status = hub_state.app_api_status(_app(tmp_path, "compat"))
    assert status == "stopped"


def test_app_api_status_uses_cache_within_ttl(tmp_path, monkeypatch):
    hub_state._api_status_cache.clear()
    monkeypatch.setattr(hub_state.env_manager, "read_env", lambda p: {})
    with mock.patch("subprocess.run",
                    return_value=mock.Mock(returncode=0, stdout="200")) as run:
        hub_state.app_api_status(_app(tmp_path, "compat"))
        hub_state.app_api_status(_app(tmp_path, "compat"))
    assert run.call_count == 1
