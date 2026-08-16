"""Tests pra_replicate — réplication coffre + snapshot vers l'emplacement PRA."""
import json
import os
import time

from hubctl_cli import hub_state, pra_config, pra_replicate


def _age(path, seconds_ago=3600):
    """Rend un fichier « ancien » (hors garde fraîcheur de 120 s)."""
    old = time.time() - seconds_ago
    os.utime(path, (old, old))


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(hub_state, "HUB_DIR", tmp_path / "HUB_base")
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path / "APPS")
    # parc : coffre + snapshot
    vault = tmp_path / "hubctl_cli" / "vault" / pra_config.VAULT_FILENAME
    vault.parent.mkdir(parents=True)
    vault.write_bytes(b"COFFRE-V1")
    snaps = tmp_path / "HUB_base" / "storage" / "snapshots"
    snaps.mkdir(parents=True)
    (snaps / "snapshot_aa.tar.zst.enc").write_bytes(b"A" * 10)
    _age(snaps / "snapshot_aa.tar.zst.enc")  # écriture terminée (pas frais)
    # emplacement PRA (simule un autre disque) : config + marqueur
    usb = tmp_path / "usb"
    usb.mkdir()
    (usb / pra_config.KIT_MARKER).write_text('{"kit_id": "K1"}')
    pra_config.save_config({"path": str(usb), "kit_id": "K1"})
    return vault, snaps, usb


def test_replicate_copies_vault_and_snapshot(tmp_path, monkeypatch):
    vault, snaps, usb = _setup(tmp_path, monkeypatch)
    result = pra_replicate.replicate()
    assert result["status"] == "ok"
    assert (usb / pra_config.VAULT_FILENAME).read_bytes() == b"COFFRE-V1"
    assert (usb / "snapshot_aa.tar.zst.enc").exists()
    cfg = pra_config.load_config()
    assert cfg["last_snapshot_id"] == "snapshot_aa.tar.zst.enc"
    assert cfg["last_vault_sha256"]
    assert cfg["last_sync_at"]


def test_replicate_no_copy_when_unchanged(tmp_path, monkeypatch):
    vault, snaps, usb = _setup(tmp_path, monkeypatch)
    pra_replicate.replicate()
    copied_mtime = (usb / "snapshot_aa.tar.zst.enc").stat().st_mtime_ns
    result = pra_replicate.replicate()
    assert result["status"] == "ok" and result["copied"] == []
    assert (usb / "snapshot_aa.tar.zst.enc").stat().st_mtime_ns == copied_mtime


def test_replicate_copies_new_snapshot_and_purges_old(tmp_path, monkeypatch):
    vault, snaps, usb = _setup(tmp_path, monkeypatch)
    pra_replicate.replicate()
    (snaps / "snapshot_bb.tar.zst.enc").write_bytes(b"B" * 10)
    _age(snaps / "snapshot_bb.tar.zst.enc")
    result = pra_replicate.replicate()
    assert "snapshot_bb.tar.zst.enc" in result["copied"]
    assert (usb / "snapshot_bb.tar.zst.enc").exists()
    assert not (usb / "snapshot_aa.tar.zst.enc").exists()  # purge anciens


# ── Garde anti copie partielle ──

def test_replicate_defers_fresh_snapshot(tmp_path, monkeypatch):
    # Snapshot écrit à l'instant (mtime < 120 s) : écriture possiblement en
    # cours côté HUB → ne PAS copier ce cycle, ne PAS poser last_snapshot_id.
    vault, snaps, usb = _setup(tmp_path, monkeypatch)
    fresh = snaps / "snapshot_bb.tar.zst.enc"
    fresh.write_bytes(b"B" * 10)  # mtime = maintenant
    logs = []
    result = pra_replicate.replicate(on_log=logs.append)
    assert result["status"] == "ok"  # cycle normal, pas d'échec
    assert "snapshot_bb.tar.zst.enc" not in result["copied"]
    assert not (usb / "snapshot_bb.tar.zst.enc").exists()
    cfg = pra_config.load_config()
    assert cfg.get("last_snapshot_id") is None  # jamais posé → retry prochain cycle
    assert cfg["last_vault_sha256"]  # le coffre, lui, a bien été répliqué
    assert any("réplication différée" in l for l in logs)


def test_replicate_size_mismatch_returns_error_and_cleans_partial(tmp_path, monkeypatch):
    # Taille divergente après copie → partiel supprimé, config NON sauvée
    # (pas de last_snapshot_id) → le prochain cycle retentera.
    vault, snaps, usb = _setup(tmp_path, monkeypatch)
    real_copy = pra_replicate._copy_stream_atomic

    def partial_copy(src, dst):
        if "snapshot_" in src.name:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes()[:4])  # copie tronquée
        else:
            real_copy(src, dst)

    monkeypatch.setattr(pra_replicate, "_copy_stream_atomic", partial_copy)
    result = pra_replicate.replicate()
    assert result["status"] == "error"
    assert "incomplet" in result["message"]
    assert result["copied"] == []
    assert not (usb / "snapshot_aa.tar.zst.enc").exists()  # partiel supprimé
    cfg = pra_config.load_config()
    assert "last_snapshot_id" not in cfg
    assert "last_sync_at" not in cfg  # aucun save_config sur ce chemin d'échec


def test_replicate_missing_location_non_blocking(tmp_path, monkeypatch):
    import shutil
    vault, snaps, usb = _setup(tmp_path, monkeypatch)
    pra_replicate.replicate()
    cfg_before = pra_config.load_config()
    shutil.rmtree(usb)
    result = pra_replicate.replicate()
    assert result["status"] == "missing"
    # last_sync_at inchangé (le badge staleness se déclenchera)
    assert pra_config.load_config()["last_sync_at"] == cfg_before["last_sync_at"]


def test_replicate_none_without_config(tmp_path, monkeypatch):
    vault, snaps, usb = _setup(tmp_path, monkeypatch)
    pra_config.save_config({})
    assert pra_replicate.replicate()["status"] == "none"


def test_replicate_never_raises_on_error(tmp_path, monkeypatch):
    vault, snaps, usb = _setup(tmp_path, monkeypatch)
    vault.unlink()  # coffre absent → erreur interne, pas d'exception
    result = pra_replicate.replicate()
    assert result["status"] == "error"
    assert "Coffre introuvable" in result["message"]


def test_configure_location_writes_config_and_marker(tmp_path, monkeypatch):
    vault, snaps, usb = _setup(tmp_path, monkeypatch)
    pra_config.save_config({})  # repart d'une config vide
    dest = tmp_path / "nas"
    dest.mkdir()
    monkeypatch.setattr(pra_config, "_st_dev", lambda p: 7 if "nas" in str(p) else 8)
    monkeypatch.setattr(pra_config, "same_physical_disk", lambda a, b: False)
    ok, msg = pra_replicate.configure_location(dest, label="NAS PRA")
    assert ok, msg
    cfg = pra_config.load_config()
    assert cfg["path"] == str(dest) and cfg["kit_id"]
    marker = json.loads((dest / pra_config.KIT_MARKER).read_text())
    assert marker["kit_id"] == cfg["kit_id"]
    assert (dest / pra_config.VAULT_FILENAME).exists()
    assert (dest / "PRA-README.txt").exists()


def test_configure_location_refuses_error(tmp_path, monkeypatch):
    vault, snaps, usb = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(pra_config, "_st_dev", lambda p: 42)  # même fs
    ok, msg = pra_replicate.configure_location(tmp_path)
    assert not ok and "filesystem" in msg.lower()
    assert pra_config.load_config().get("path") != str(tmp_path)


def test_remove_location_clears_path(tmp_path, monkeypatch):
    vault, snaps, usb = _setup(tmp_path, monkeypatch)
    pra_replicate.replicate()
    pra_replicate.remove_location()
    cfg = pra_config.load_config()
    assert "path" not in cfg and "kit_id" not in cfg
    assert pra_config.should_offer_setup() is True
