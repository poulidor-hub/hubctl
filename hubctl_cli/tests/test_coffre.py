# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

from pathlib import Path
import pytest
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


# ── Atomicité de save() (perte de secrets au crash) ──


def test_save_keeps_backup_on_overwrite(tmp_path):
    """Une 2e save() doit préserver l'ancien coffre dans un .bak récupérable."""
    from hubctl_cli.coffre import Vault
    p = tmp_path / "c.vault.enc"
    v = Vault(p)
    v.unlock("pw")
    v.set_entry("hub", {"K": "V1"})
    v.save()

    v.set_entry("hub", {"K": "V2"})
    v.save()

    bak = tmp_path / "c.vault.enc.bak"
    assert bak.exists(), "le backup .bak doit être créé lors d'une réécriture"
    # Le backup doit contenir l'ancien état (V1), pas le courant (V2).
    vb = Vault(bak)
    assert vb.unlock("pw") is True
    assert vb.reveal("hub") == {"K": "V1"}
    # Et le courant contient bien V2.
    assert v.reveal("hub") == {"K": "V2"}


def test_save_atomic_crash_preserves_original(tmp_path, monkeypatch):
    """Si l'écriture échoue (ex: disque plein pendant os.replace), le coffre
    original doit rester INTACT et re-déchiffrable — sinon on perd tous les secrets.
    """
    from hubctl_cli import coffre
    from hubctl_cli.coffre import Vault

    p = tmp_path / "c.vault.enc"
    v = Vault(p)
    v.unlock("pw")
    v.set_entry("hub", {"SECRET": "ORIGINAL"})
    v.save()
    original_bytes = p.read_bytes()

    # On tente une 2e save avec un os.replace qui échoue (simule disque plein).
    v.set_entry("hub", {"SECRET": "CORROMPU"})

    def boom(*a, **k):
        raise OSError("No space left on device")
    monkeypatch.setattr(coffre.os, "replace", boom)

    with pytest.raises(OSError):
        v.save()

    # Le coffre sur disque n'a pas été touché.
    assert p.read_bytes() == original_bytes
    v2 = Vault(p)
    assert v2.unlock("pw") is True
    assert v2.reveal("hub") == {"SECRET": "ORIGINAL"}

    # Aucun fichier temporaire traînant.
    leftovers = [x.name for x in tmp_path.iterdir() if x.name.endswith(".tmp")]
    assert leftovers == [], f"fichiers temporaires non nettoyés : {leftovers}"


def test_save_no_backup_when_first_write(tmp_path):
    """À la 1re création du coffre, il n'y a rien à backuper → pas de .bak."""
    from hubctl_cli.coffre import Vault
    p = tmp_path / "c.vault.enc"
    v = Vault(p)
    v.unlock("pw")
    v.set_entry("hub", {"K": "V"})
    v.save()
    assert p.exists()
    assert not (tmp_path / "c.vault.enc.bak").exists()


def test_restore_blocks_path_traversal(tmp_path, monkeypatch):
    """Un coffre importé (source externe) peut contenir des relpath malveillants
    dans __files__ (`../../etc/cron.d/evil`). restore() ne doit JAMAIS écrire
    hors du parc — sinon écriture arbitraire sur le disque.
    """
    from hubctl_cli.coffre import Vault
    parc = tmp_path / "parc"
    parc.mkdir()
    monkeypatch.setattr(hub_state, "PARC_ROOT", parc)

    v = Vault(tmp_path / "v.enc")
    v.unlock("pw")
    v.set_file("../evil_outside.txt", b"PWNED")   # résout hors du parc
    v.set_file("legit/inside.txt", b"OK")          # reste dans le parc

    written = v.restore()

    # Le fichier légitime est bien écrit.
    assert (parc / "legit" / "inside.txt").read_bytes() == b"OK"
    # Le fichier malveillant n'a PAS été écrit hors du parc.
    assert not (tmp_path / "evil_outside.txt").exists()
    # Et il n'est pas annoncé dans la liste des fichiers restaurés.
    assert all("evil_outside" not in w for w in written)


# ── Validation des git_url avant clone (anti file:// / hooks locaux) ──


def test_validate_git_url_accepts_remote_schemes():
    from hubctl_cli.coffre import _validate_git_url
    for ok in [
        "https://github.com/x/y.git",
        "http://git.internal/x/y",
        "git@github.com:x/y.git",
        "ssh://git@gitlab.com/x/y.git",
        "git://host/x/y",
    ]:
        assert _validate_git_url(ok) is None, f"{ok} devrait être accepté"


def test_validate_git_url_rejects_file_and_local_paths():
    from hubctl_cli.coffre import _validate_git_url
    for bad in [
        "file:///etc/passwd",
        "/etc/passwd",
        "./relative/repo",
        "../escape/repo",
        "relative/path",
        "",
    ]:
        assert _validate_git_url(bad) is not None, f"{bad!r} devrait être rejeté"


# ── Passphrase auto-générée (parc.vault.key) ──


def test_generate_passphrase_strong_and_unique():
    from hubctl_cli import coffre
    a = coffre.generate_passphrase()
    b = coffre.generate_passphrase()
    assert len(a) >= 32
    assert a != b


def test_key_path_sibling_of_vault(tmp_path):
    from hubctl_cli.coffre import Vault
    v = Vault(tmp_path / "parc.vault.enc")
    assert v.key_path == tmp_path / "parc.vault.key"


def test_store_load_passphrase_roundtrip(tmp_path):
    import os
    from hubctl_cli.coffre import Vault
    v = Vault(tmp_path / "parc.vault.enc")
    v.store_passphrase("s3cr3t-key")
    assert v.load_passphrase() == "s3cr3t-key"
    assert os.stat(v.key_path).st_mode & 0o777 == 0o600


def test_load_passphrase_absent_returns_none(tmp_path):
    from hubctl_cli.coffre import Vault
    v = Vault(tmp_path / "parc.vault.enc")
    assert v.load_passphrase() is None


def test_load_passphrase_empty_file_returns_none(tmp_path):
    from hubctl_cli.coffre import Vault
    v = Vault(tmp_path / "parc.vault.enc")
    v.key_path.write_text("\n")
    assert v.load_passphrase() is None


def test_unlock_stored_roundtrip(tmp_path):
    from hubctl_cli.coffre import Vault
    p = tmp_path / "parc.vault.enc"
    v = Vault(p)
    v.unlock("master")
    v.save()
    v.store_passphrase("master")
    v2 = Vault(p)
    assert v2.is_locked is True
    assert v2.unlock_stored() is True
    assert v2.is_locked is False


def test_unlock_stored_wrong_key_returns_false(tmp_path):
    from hubctl_cli.coffre import Vault
    p = tmp_path / "parc.vault.enc"
    v = Vault(p)
    v.unlock("master")
    v.save()
    v.store_passphrase("autre-chose")
    v2 = Vault(p)
    assert v2.unlock_stored() is False
    assert v2.is_locked is True
