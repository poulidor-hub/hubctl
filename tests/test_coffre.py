from pathlib import Path
from hubctl_cli import coffre, hub_state


def test_derive_key_is_deterministic():
    salt = b"0123456789abcdef"
    k1 = coffre.derive_key("secret", salt)
    k2 = coffre.derive_key("secret", salt)
    assert k1 == k2
    assert coffre.derive_key("other", salt) != k1


def test_vault_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(hub_state, "HUB_DIR", tmp_path / "HUB_base")
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path / "APPS")
    (tmp_path / "HUB_base").mkdir()
    (tmp_path / "HUB_base" / ".env").write_text("POSTGRES_PASSWORD=topsecret\n")

    vpath = tmp_path / "vault" / "parc.vault.enc"
    v = coffre.Vault(vpath)
    assert v.unlock("master-pass") is True
    v.sync()
    v.save()
    v.lock()
    assert v.is_locked is True

    v2 = coffre.Vault(vpath)
    assert v2.unlock("master-pass") is True
    assert "hub" in v2.list_entries()
    assert v2.reveal("hub")["POSTGRES_PASSWORD"] == "topsecret"


def test_vault_wrong_passphrase(tmp_path):
    vpath = tmp_path / "vault" / "parc.vault.enc"
    v = coffre.Vault(vpath)
    v.unlock("right")
    v.save()
    v.lock()
    v2 = coffre.Vault(vpath)
    assert v2.unlock("wrong") is False


def test_set_get_list_file(tmp_path):
    from hubctl_cli.coffre import Vault
    v = Vault(tmp_path / "v.enc")
    v.unlock("pw")
    v.set_file("HUB_base/secrets/jwt_private_key.pem", b"\x00binary\xff")
    assert v.list_files() == ["HUB_base/secrets/jwt_private_key.pem"]
    assert v.get_file("HUB_base/secrets/jwt_private_key.pem") == b"\x00binary\xff"
    assert v.get_file("absent") is None


def test_change_passphrase(tmp_path):
    from hubctl_cli.coffre import Vault
    p = tmp_path / "v.enc"
    v = Vault(p)
    v.unlock("old")
    v.set_entry("hub", {"K": "V"})
    v.change_passphrase("new")
    v.save()
    v2 = Vault(p)
    assert v2.unlock("new") is True
    assert v2.get_entry("hub") == {"K": "V"}
    v3 = Vault(p)
    assert v3.unlock("old") is False


def test_sync_reads_env_from_disk(tmp_path, monkeypatch):
    from hubctl_cli import coffre, hub_state
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(hub_state, "HUB_DIR", tmp_path / "HUB_base")
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path / "APPS")
    monkeypatch.setattr(hub_state, "WORKSHOP_DIR", tmp_path / "APP_Workshop")
    (tmp_path / "HUB_base").mkdir()
    (tmp_path / "HUB_base" / ".env").write_text("POSTGRES_PASSWORD=topsecret\n")
    v = coffre.Vault(tmp_path / "v.enc")
    v.unlock("pw")
    v.sync()
    assert v.get_entry("hub") == {"POSTGRES_PASSWORD": "topsecret"}


# ── regressions de securite coffre ──

def test_sync_removes_stale_entries(tmp_path, monkeypatch):
    """sync() doit supprimer les entrées d'apps qui n'existent plus sur le disque."""
    from hubctl_cli import coffre, hub_state
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(hub_state, "HUB_DIR", tmp_path / "HUB_base")
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path / "APPS")
    monkeypatch.setattr(hub_state, "WORKSHOP_DIR", tmp_path / "APP_Workshop")
    (tmp_path / "HUB_base").mkdir()
    (tmp_path / "HUB_base" / ".env").write_text("POSTGRES_PASSWORD=topsecret\n")
    app = tmp_path / "APPS" / "oldapp"; app.mkdir(parents=True)
    (app / ".env").write_text("OLD_SECRET=xyz\n")
    v = coffre.Vault(tmp_path / "v.enc")
    v.unlock("pw")
    v.sync()
    assert "app:oldapp" in v.list_entries()
    # Supprimer l'app
    (app / ".env").unlink(); app.rmdir()
    v.sync()
    assert "app:oldapp" not in v.list_entries()


def test_unlock_wrong_does_not_corrupt_state(tmp_path):
    """Un mauvais unlock ne doit pas laisser _salt corrompu ni _fernet utilisable."""
    from hubctl_cli.coffre import Vault
    v = Vault(tmp_path / "v.enc")
    v.unlock("right")
    v.set_entry("hub", {"K": "V"})
    v.save()
    v.lock()
    v2 = Vault(tmp_path / "v.enc")
    ok = v2.unlock("wrong")
    assert ok is False
    assert v2.is_locked is True
    assert v2._data is None


def test_sync_does_not_follow_symlink_dirs(tmp_path, monkeypatch):
    """sync() ne doit PAS ingérer les .env de répertoires symlinkés."""
    from hubctl_cli import coffre, hub_state
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(hub_state, "HUB_DIR", tmp_path / "HUB_base")
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path / "APPS")
    monkeypatch.setattr(hub_state, "WORKSHOP_DIR", tmp_path / "APP_Workshop")
    (tmp_path / "HUB_base").mkdir()
    (tmp_path / "HUB_base" / ".env").write_text("POSTGRES_PASSWORD=topsecret\n")
    external = tmp_path / "external"; external.mkdir()
    (external / ".env").write_text("OUTSIDE=1\n")
    (tmp_path / "APPS").mkdir()
    (tmp_path / "APPS" / "linked").symlink_to(external, target_is_directory=True)
    v = coffre.Vault(tmp_path / "v.enc")
    v.unlock("pw")
    v.sync()
    assert "app:linked" not in v.list_entries()
