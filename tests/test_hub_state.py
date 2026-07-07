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


def _mk_app(apps_dir, name, compat=True):
    d = apps_dir / name
    d.mkdir(parents=True)
    if compat:
        (d / "Makefile").write_text("up:\n")
        (d / "docker-compose.yml").write_text("services: {}\n")
    return d


def test_get_apps_status_maps_docker(tmp_path, monkeypatch):
    apps = tmp_path / "APPS"
    _mk_app(apps, "shop", compat=True)
    _mk_app(apps, "old", compat=False)
    monkeypatch.setattr(hub_state, "APPS_DIR", apps)
    hub_state.set_mode("prod")
    docker_json = (
        '{"Labels":"com.docker.compose.project=shop,com.docker.compose.service=api",'
        '"State":"running","Status":"Up 2 hours"}\n'
    )
    with mock.patch("subprocess.run", return_value=mock.Mock(stdout=docker_json)):
        rows = {r.name: r for r in hub_state.get_apps_status()}
    assert rows["shop"].status == "running"
    assert rows["shop"].uptime == "Up 2 hours"
    assert rows["old"].status == "legacy"
    assert rows["old"].legacy is True
    assert rows["old"].uptime == "—"


def test_get_apps_status_stopped_when_no_container(tmp_path, monkeypatch):
    apps = tmp_path / "APPS"
    _mk_app(apps, "shop", compat=True)
    monkeypatch.setattr(hub_state, "APPS_DIR", apps)
    hub_state.set_mode("prod")
    with mock.patch("subprocess.run", return_value=mock.Mock(stdout="")):
        rows = {r.name: r for r in hub_state.get_apps_status()}
    assert rows["shop"].status == "stopped"
    assert rows["shop"].uptime == "—"


def test_get_apps_status_docker_absent(tmp_path, monkeypatch):
    apps = tmp_path / "APPS"
    _mk_app(apps, "shop", compat=True)
    monkeypatch.setattr(hub_state, "APPS_DIR", apps)
    hub_state.set_mode("prod")
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        rows = {r.name: r for r in hub_state.get_apps_status()}
    assert rows["shop"].status == "stopped"


def test_get_apps_status_no_heavy_calls(tmp_path, monkeypatch):
    apps = tmp_path / "APPS"
    _mk_app(apps, "shop", compat=True)
    monkeypatch.setattr(hub_state, "APPS_DIR", apps)
    hub_state.set_mode("prod")
    with mock.patch.object(hub_state, "_dir_size") as ds, \
         mock.patch.object(hub_state, "_git_dirty") as gd, \
         mock.patch("subprocess.run", return_value=mock.Mock(stdout="")):
        hub_state.get_apps_status()
    ds.assert_not_called()
    gd.assert_not_called()


def test_active_apps_dir_prod_returns_apps_dir():
    hub_state.set_mode("prod")
    assert hub_state.active_apps_dir() == hub_state.APPS_DIR


def test_active_apps_dir_dev_returns_workshop_dir():
    hub_state.set_mode("dev")
    try:
        assert hub_state.active_apps_dir() == hub_state.WORKSHOP_DIR
    finally:
        hub_state.set_mode("prod")


def test_auto_detect_mode_prod_when_workshop_empty(tmp_path, monkeypatch):
    """WORKSHOP_DIR vide ou inexistant → PROD."""
    monkeypatch.setattr(hub_state, "WORKSHOP_DIR", tmp_path / "ws")
    assert hub_state.auto_detect_mode() == "prod"


def test_auto_detect_mode_dev_when_workshop_has_app(tmp_path, monkeypatch):
    """WORKSHOP_DIR avec une app (docker-compose.yml) → DEV."""
    ws = tmp_path / "ws"
    app = ws / "exemple-apps-ws"
    app.mkdir(parents=True)
    (app / "docker-compose.yml").write_text("services:\n  api:\n    image: x\n")
    monkeypatch.setattr(hub_state, "WORKSHOP_DIR", ws)
    assert hub_state.auto_detect_mode() == "dev"


def test_auto_detect_mode_prod_when_workshop_has_only_dirs_without_compose(tmp_path, monkeypatch):
    """WORKSHOP_DIR avec des dossiers mais SANS docker-compose.yml → PROD."""
    ws = tmp_path / "ws"
    (ws / "leftover").mkdir(parents=True)
    monkeypatch.setattr(hub_state, "WORKSHOP_DIR", ws)
    assert hub_state.auto_detect_mode() == "prod"
