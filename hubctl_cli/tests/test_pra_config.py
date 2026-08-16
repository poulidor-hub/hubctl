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


# ── Validation « ailleurs » + détection disque ──

import pytest
from pathlib import Path


def _write_mountinfo(tmp_path, lines):
    f = tmp_path / "mountinfo"
    f.write_text("\n".join(lines) + "\n")
    return str(f)


def _make_sys_block(tmp_path, entries):
    """entries: dict majmin -> 'sda/sda1' (chemin relatif devices)."""
    sysroot = tmp_path / "sys"
    for majmin, rel in entries.items():
        target = sysroot / "devices" / rel
        target.mkdir(parents=True, exist_ok=True)
        link = sysroot / "dev" / "block" / majmin
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(f"../../devices/{rel}")
    return str(sysroot / "dev" / "block")


MI = [
    "1 0 8:1 / / rw,relatime - ext4 /dev/sda1 rw",
    "2 0 8:2 / /media/usb rw - ext4 /dev/sda2 rw",
    "3 0 8:16 / /mnt/usb2 rw - vfat /dev/sdb1 rw",
    "4 0 0:33 / /mnt/nas nfs rw - nfs srv:/export rw",
]


def test_same_physical_disk_two_partitions_same_disk(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    monkeypatch.setattr(pra_config, "_MOUNTINFO", _write_mountinfo(tmp_path, MI))
    monkeypatch.setattr(pra_config, "_SYS_BLOCK", _make_sys_block(
        tmp_path, {"8:1": "pci/ata1/sda/sda1", "8:2": "pci/ata1/sda/sda2", "8:16": "pci/ata2/sdb/sdb1"}))
    # /media/usb = sda2, / = sda1 → même disque sda
    assert pra_config.same_physical_disk(Path("/media/usb"), Path("/")) is True


def test_same_physical_disk_different_disks(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    monkeypatch.setattr(pra_config, "_MOUNTINFO", _write_mountinfo(tmp_path, MI))
    monkeypatch.setattr(pra_config, "_SYS_BLOCK", _make_sys_block(
        tmp_path, {"8:1": "pci/ata1/sda/sda1", "8:16": "pci/ata2/sdb/sdb1"}))
    assert pra_config.same_physical_disk(Path("/mnt/usb2"), Path("/")) is False


def test_same_physical_disk_network_always_different(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    monkeypatch.setattr(pra_config, "_MOUNTINFO", _write_mountinfo(tmp_path, MI))
    monkeypatch.setattr(pra_config, "_SYS_BLOCK", _make_sys_block(tmp_path, {"8:1": "pci/sda/sda1"}))
    assert pra_config.same_physical_disk(Path("/mnt/nas"), Path("/")) is False


def test_validate_location_error_same_filesystem(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    monkeypatch.setattr(pra_config, "_st_dev", lambda p: 42)
    d = tmp_path / "usb"
    d.mkdir()
    status, msg = pra_config.validate_location(d)
    assert status == "error" and "filesystem" in msg.lower()


def test_validate_location_ok(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    monkeypatch.setattr(pra_config, "_st_dev", lambda p: 1 if str(p).endswith("usb") else 2)
    monkeypatch.setattr(pra_config, "same_physical_disk", lambda a, b: False)
    d = tmp_path / "usb"
    d.mkdir()
    status, _ = pra_config.validate_location(d)
    assert status == "ok"


def test_validate_location_warn_same_physical_disk(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    monkeypatch.setattr(pra_config, "_st_dev", lambda p: 1 if str(p).endswith("usb") else 2)
    monkeypatch.setattr(pra_config, "same_physical_disk", lambda a, b: True)
    d = tmp_path / "usb"
    d.mkdir()
    status, msg = pra_config.validate_location(d)
    assert status == "warn" and "disque" in msg.lower()


def test_validate_location_error_missing(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    status, _ = pra_config.validate_location(Path(tmp_path / "inexistant"))
    assert status == "error"


# ── Marqueur / détection kit ──

def test_read_kit_marker_roundtrip(tmp_path):
    d = tmp_path / "usb"
    d.mkdir()
    assert pra_config.read_kit_marker(d) is None
    (d / pra_config.KIT_MARKER).write_text('{"kit_id": "xyz"}')
    assert pra_config.read_kit_marker(d) == {"kit_id": "xyz"}
    (d / pra_config.KIT_MARKER).write_text("{corrupt")
    assert pra_config.read_kit_marker(d) is None


def test_find_kit_location_configured_path_first(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    usb = tmp_path / "usb"
    usb.mkdir()
    (usb / pra_config.KIT_MARKER).write_text('{"kit_id": "K1"}')
    found = pra_config.find_kit_location("K1", str(usb))
    assert found == usb


def test_find_kit_location_scan_fallback(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    usb = tmp_path / "usb"
    usb.mkdir()
    (usb / pra_config.KIT_MARKER).write_text('{"kit_id": "K2"}')
    monkeypatch.setattr(pra_config, "_scan_roots", lambda: [usb])
    found = pra_config.find_kit_location("K2", "/chemin/plus/la")
    assert found == usb


def test_find_kit_location_wrong_kit_id(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    usb = tmp_path / "usb"
    usb.mkdir()
    (usb / pra_config.KIT_MARKER).write_text('{"kit_id": "K2"}')
    monkeypatch.setattr(pra_config, "_scan_roots", lambda: [usb])
    assert pra_config.find_kit_location("AUTRE", None) is None


def test_discover_kits_finds_marker_and_vault(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    usb = tmp_path / "usb"
    usb.mkdir()
    (usb / pra_config.KIT_MARKER).write_text('{"kit_id": "K1"}')
    (usb / pra_config.VAULT_FILENAME).write_bytes(b"MAGIC")
    vide = tmp_path / "vide"
    vide.mkdir()  # marqueur sans coffre → exclu
    (vide / pra_config.KIT_MARKER).write_text('{"kit_id": "K2"}')
    monkeypatch.setattr(pra_config, "_scan_roots", lambda: [usb, vide])
    assert pra_config.discover_kits() == [usb]


# ── Staleness + phase setup ──

def _kit_ok(tmp_path):
    usb = tmp_path / "usb"
    usb.mkdir(exist_ok=True)
    (usb / pra_config.KIT_MARKER).write_text('{"kit_id": "K1"}')
    return usb


def test_staleness_badge_none_without_config(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    assert pra_config.staleness_badge() is None


def test_staleness_badge_missing_location(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    pra_config.save_config({"path": "/pas/la", "kit_id": "K1"})
    assert pra_config.staleness_badge() == "⚠ PRA : emplacement secondaire introuvable"


def test_staleness_badge_never_synced(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    usb = _kit_ok(tmp_path)
    pra_config.save_config({"path": str(usb), "kit_id": "K1"})
    assert pra_config.staleness_badge() == "⚠ PRA : jamais synchronisé"


def test_staleness_badge_stale_8_days(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    usb = _kit_ok(tmp_path)
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    pra_config.save_config({"path": str(usb), "kit_id": "K1", "last_sync_at": old})
    assert pra_config.staleness_badge() == "⚠ PRA : dernière synchro il y a 8 j"


def test_staleness_badge_fresh_is_none(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    usb = _kit_ok(tmp_path)
    from datetime import datetime, timezone
    pra_config.save_config({"path": str(usb), "kit_id": "K1",
                            "last_sync_at": datetime.now(timezone.utc).isoformat()})
    assert pra_config.staleness_badge() is None


def test_staleness_badge_silent_when_declined(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    pra_config.save_config({"declined": True})
    assert pra_config.staleness_badge() is None


def test_should_offer_setup_matrix(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    assert pra_config.should_offer_setup() is True                     # pas de config
    pra_config.save_config({"declined": True})
    assert pra_config.should_offer_setup() is False                    # refus définitif
    pra_config.save_config({"path": "/media/usb", "kit_id": "K"})
    assert pra_config.should_offer_setup() is False                    # configuré
    pra_config.save_config({})
    assert pra_config.should_offer_setup() is True                     # config vide
