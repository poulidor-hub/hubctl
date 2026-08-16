"""Tests pra_config — persistance de la config emplacement PRA."""
from hubctl_cli import hub_state, pra_config


def _patch_parc(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(hub_state, "HUB_DIR", tmp_path / "HUB_base")
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path / "APPS")


def test_load_config_absent_returns_none(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    assert pra_config.load_config() is None


def test_save_then_load_roundtrip(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    cfg = {"path": "/media/usb", "kit_id": "abc", "declined": False}
    pra_config.save_config(cfg)
    assert pra_config.load_config() == cfg


def test_save_config_writes_0600_and_json(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    import json, os
    pra_config.save_config({"path": "/x"})
    p = pra_config.config_path()
    assert p == tmp_path / "hubctl_cli" / "vault" / "pra_location.json"
    assert json.loads(p.read_text())["path"] == "/x"
    assert (p.stat().st_mode & 0o777) == 0o600


def test_load_config_corrupt_returns_none(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    pra_config.config_path().parent.mkdir(parents=True)
    pra_config.config_path().write_text("{not json")
    assert pra_config.load_config() is None


def test_new_kit_id_unique_hex():
    a, b = pra_config.new_kit_id(), pra_config.new_kit_id()
    assert a != b and len(a) == 16
    int(a, 16)  # hex valide
