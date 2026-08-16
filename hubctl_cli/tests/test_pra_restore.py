"""Tests pra_restore — assistant PRA (étapes unitaires, tout mocké)."""
import pytest
from pathlib import Path

from hubctl_cli import hub_state, pra_config, pra_restore


def _patch_parc(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(hub_state, "HUB_DIR", tmp_path / "HUB_base")
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path / "APPS")


def _kit(tmp_path):
    kit = tmp_path / "usb"
    kit.mkdir(exist_ok=True)
    (kit / pra_config.KIT_MARKER).write_text('{"kit_id": "K1"}')
    (kit / pra_config.VAULT_FILENAME).write_bytes(b"VAULT")
    (kit / "snapshot_abc123.tar.zst.enc").write_bytes(b"S1")
    (kit / "snapshot_def456.tar.zst.enc").write_bytes(b"S2-older")
    import os
    older = 1700000000
    os.utime(kit / "snapshot_def456.tar.zst.enc", (older, older))
    return kit


def test_pick_snapshot_arg_wins(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    snap = pra_restore.pick_snapshot(kit, "snapshot_def456.tar.zst.enc")
    assert snap.name == "snapshot_def456.tar.zst.enc"
    snap2 = pra_restore.pick_snapshot(kit, str(kit / "snapshot_def456.tar.zst.enc"))
    assert snap2.name == "snapshot_def456.tar.zst.enc"


def test_pick_snapshot_latest_by_mtime(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    assert pra_restore.pick_snapshot(kit, None).name == "snapshot_abc123.tar.zst.enc"


def test_pick_snapshot_none_available(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    empty = tmp_path / "emptykit"
    empty.mkdir()
    with pytest.raises(SystemExit):
        pra_restore.pick_snapshot(empty, None)


def test_find_kit_explicit_arg(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    assert pra_restore.find_kit(str(kit)) == kit


def test_find_kit_explicit_arg_without_vault(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    (kit / pra_config.VAULT_FILENAME).unlink()
    with pytest.raises(SystemExit):
        pra_restore.find_kit(str(kit))


def test_find_kit_discover(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    monkeypatch.setattr(pra_restore.pra_config, "load_config", lambda: None)
    monkeypatch.setattr(pra_restore.pra_config, "discover_kits", lambda: [kit])
    assert pra_restore.find_kit(None) == kit


def test_find_kit_none_found(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    monkeypatch.setattr(pra_restore.pra_config, "load_config", lambda: None)
    monkeypatch.setattr(pra_restore.pra_config, "discover_kits", lambda: [])
    with pytest.raises(SystemExit):
        pra_restore.find_kit(None)


def test_check_parc_empty_refuses_without_force(monkeypatch):
    from hubctl_cli import park_restore
    monkeypatch.setattr(park_restore, "_check_hub_is_empty", lambda: False)
    with pytest.raises(SystemExit):
        pra_restore.check_parc_empty(False)


def test_check_parc_empty_passes_empty(monkeypatch):
    from hubctl_cli import park_restore
    monkeypatch.setattr(park_restore, "_check_hub_is_empty", lambda: True)
    pra_restore.check_parc_empty(False)  # ne lève pas


def test_check_parc_empty_force_overrides(monkeypatch, capsys):
    from hubctl_cli import park_restore
    monkeypatch.setattr(park_restore, "_check_hub_is_empty", lambda: False)
    pra_restore.check_parc_empty(True)  # ne lève pas
    assert "force" in capsys.readouterr().out.lower()


def test_env_lookup_and_api_base_url(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    assert pra_restore._api_base_url() == "http://localhost:8000"
    hub_state.HUB_DIR.mkdir(parents=True)
    (hub_state.HUB_DIR / ".env").write_text("HUB_API_PORT=8010\nADMIN_USERNAME=boss\n")
    assert pra_restore._api_base_url() == "http://localhost:8010"
    assert pra_restore._env_lookup("ADMIN_USERNAME") == "boss"
    assert pra_restore._env_lookup("ABSENT") is None


def test_build_stack_missing_compose_exits(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        pra_restore.build_stack(None, dry_run=True)


def test_build_stack_dry_run_no_subprocess(tmp_path, monkeypatch):
    import subprocess as sp
    _patch_parc(tmp_path, monkeypatch)
    hub_state.HUB_DIR.mkdir(parents=True)
    (hub_state.HUB_DIR / "docker-compose.yml").write_text("x: 1\n")
    called = []
    monkeypatch.setattr(sp, "run", lambda *a, **kw: called.append(a) or None)
    pra_restore.build_stack(None, dry_run=True)
    assert called == []  # dry-run : zéro subprocess


def test_wait_healthy_polls_docker_inspect(monkeypatch):
    import subprocess as sp
    from hubctl_cli.pra_restore import wait_healthy
    results = iter([("1", "starting"), ("0", "healthy")])

    class FakeProc:
        def __init__(self, code, out):
            self.returncode = int(code)
            self.stdout = out

    monkeypatch.setattr(sp, "run", lambda *a, **kw: FakeProc(*next(results)))
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert wait_healthy(None, timeout_s=5) is True


def test_wait_healthy_timeout(monkeypatch):
    import subprocess as sp
    from hubctl_cli.pra_restore import wait_healthy

    class FakeProc:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(sp, "run", lambda *a, **kw: FakeProc())
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert wait_healthy(None, timeout_s=0) is False
