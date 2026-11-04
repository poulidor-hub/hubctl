# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Tests park-export : capture COMPLÈTE et récursive des secrets Hub.

Couvre la régression critique où les noms codés en dur (jwt_private.pem,
jwt_public.pem, .fernet_key) ne correspondaient à aucun fichier réel → archives
DR produites sans aucun secret.
"""
from __future__ import annotations
import tarfile
from pathlib import Path

import pytest

import hubctl_cli.park_export as pe


@pytest.fixture
def fake_parc(tmp_path, monkeypatch):
    parc = tmp_path / "parc"
    hub = parc / "HUB_base"
    secrets = hub / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "jwt_private_key.pem").write_bytes(b"-----PRIVATE-----\n")
    (secrets / "jwt_public_key.pem").write_bytes(b"-----PUBLIC-----\n")
    (secrets / "backup_private_key.pem").write_bytes(b"-----BK-PRIV-----\n")
    (secrets / "backup_public_key.pem").write_bytes(b"-----BK-PUB-----\n")
    sa = secrets / "service_accounts" / "sa_demo.env"
    sa.parent.mkdir(parents=True)
    sa.write_text("SA_KEY=value\n")
    (hub / ".env").write_text("SECRETS_ENCRYPTION_KEY=fernetdata\nAPP_ENV=production\n")
    monkeypatch.setattr(pe, "PARC_ROOT", parc)
    monkeypatch.setattr(pe, "HUB_DIR", hub)
    monkeypatch.setattr(pe, "HUB_SECRETS_DIR", secrets)
    monkeypatch.setattr(pe, "APPS_DIR", parc / "APPS")
    monkeypatch.setattr(pe, "EXPORT_DIR", tmp_path / "exports")
    return parc


def test_collect_hub_secrets_captures_all_top_level_files(fake_parc, tmp_path):
    work = tmp_path / "work"
    collected = pe._collect_hub_secrets(work)
    names = sorted(p.name for p in collected)
    assert "jwt_private_key.pem" in names
    assert "jwt_public_key.pem" in names
    assert "backup_private_key.pem" in names
    assert "backup_public_key.pem" in names


def test_collect_hub_sectors_preserves_content(fake_parc, tmp_path):
    work = tmp_path / "work"
    pe._collect_hub_secrets(work)
    assert (work / "secrets" / "jwt_private_key.pem").read_bytes() == b"-----PRIVATE-----\n"


def test_collect_hub_secrets_captures_service_accounts_subtree(fake_parc, tmp_path):
    work = tmp_path / "work"
    collected = pe._collect_hub_secrets(work)
    rels = sorted(p.relative_to(work / "secrets").as_posix() for p in collected)
    assert "service_accounts/sa_demo.env" in rels
    assert (work / "secrets" / "service_accounts" / "sa_demo.env").read_text() == "SA_KEY=value\n"


def test_export_park_archive_contains_all_secrets(fake_parc, tmp_path, monkeypatch):
    """Export end-to-end (docker mocké) : l'archive doit contenir tous les secrets."""
    def fake_pg_dump(container, user, db, output):
        output.write_bytes(b"PGDUMP")
        return True
    monkeypatch.setattr(pe, "_pg_dump", fake_pg_dump)
    monkeypatch.setattr(pe, "_detect_apps", lambda: [])

    archive = pe.export_park(encrypt=False)
    assert archive.exists()
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(extracted)

    sec = extracted / "secrets"
    assert (sec / "jwt_private_key.pem").read_bytes() == b"-----PRIVATE-----\n"
    assert (sec / "jwt_public_key.pem").read_bytes() == b"-----PUBLIC-----\n"
    assert (sec / "backup_private_key.pem").read_bytes() == b"-----BK-PRIV-----\n"
    assert (sec / "backup_public_key.pem").read_bytes() == b"-----BK-PUB-----\n"
    assert (sec / "service_accounts" / "sa_demo.env").read_text() == "SA_KEY=value\n"


from hubctl_cli.park_export import (
    encrypt_archive, decrypt_archive, is_encrypted_archive,
    ARCHIVE_MAGIC, WrongPassphraseError,
)


def test_encrypt_decrypt_roundtrip():
    """encrypt_archive puis decrypt_archive restitue les bytes originaux."""
    original = b"CONTENU TAR.GZ SECRET" * 100
    blob = encrypt_archive(original, "ma-passphrase-2026")
    assert blob[:len(ARCHIVE_MAGIC)] == ARCHIVE_MAGIC
    restored = decrypt_archive(blob, "ma-passphrase-2026")
    assert restored == original


def test_decrypt_wrong_passphrase_raises():
    """Une mauvaise passphrase lève WrongPassphraseError (pas de crash générique)."""
    blob = encrypt_archive(b"data", "bonne-pass")
    import pytest
    with pytest.raises(WrongPassphraseError):
        decrypt_archive(blob, "mauvaise-pass")


def test_is_encrypted_archive_detects_magic_bytes(tmp_path):
    """is_encrypted_archive reconnaît le magic PARKEV1."""
    enc_path = tmp_path / "a.tar.gz.enc"
    enc_path.write_bytes(ARCHIVE_MAGIC + b"\x00" * 16 + b"ciphertext")
    plain_path = tmp_path / "b.tar.gz"
    plain_path.write_bytes(b"\x1f\x8b\x08" + b"plain gzip data")  # gzip magic
    assert is_encrypted_archive(enc_path) is True
    assert is_encrypted_archive(plain_path) is False


def test_encrypt_uses_random_salt():
    """Deux chiffrements de la même donnée produisent des blobs différents (salt aléatoire)."""
    data = b"identique"
    b1 = encrypt_archive(data, "pass")
    b2 = encrypt_archive(data, "pass")
    assert b1 != b2  # salt différent
    assert decrypt_archive(b1, "pass") == decrypt_archive(b2, "pass") == data


def test_export_park_encrypts_by_default(fake_parc, tmp_path, monkeypatch):
    """Par défaut, export_park produit une archive chiffrée (magic PARKEV1)
    dont le contenu n'est pas lisible comme tar.gz en clair."""
    monkeypatch.setattr(pe, "_pg_dump", lambda c, u, d, o: o.write_bytes(b"PGDUMP") or True)
    monkeypatch.setattr(pe, "_detect_apps", lambda: [])

    archive = pe.export_park(passphrase="test-pass-123")
    assert archive.suffix == ".enc", f"l'archive chiffrée doit finir par .enc, eu {archive.name}"
    blob = archive.read_bytes()
    assert blob.startswith(pe.ARCHIVE_MAGIC), "doit avoir le magic PARKEV1"
    # Le contenu chiffré n'est PAS un gzip valide (magic gzip = \x1f\x8b)
    assert blob[:2] != b"\x1f\x8b", "le ciphertext ne doit pas être un gzip en clair"


def test_export_park_no_encrypt_produces_plain_tarball(fake_parc, tmp_path, monkeypatch):
    """--no-encrypt (encrypt=False) produit une archive tar.gz legacy (rétrocompat)."""
    monkeypatch.setattr(pe, "_pg_dump", lambda c, u, d, o: o.write_bytes(b"PGDUMP") or True)
    monkeypatch.setattr(pe, "_detect_apps", lambda: [])

    archive = pe.export_park(encrypt=False)
    assert archive.suffix == ".gz"
    blob = archive.read_bytes()
    assert blob[:2] == b"\x1f\x8b", "doit être un gzip valide (magic \\x1f\\x8b)"
    assert not blob.startswith(pe.ARCHIVE_MAGIC), "ne doit pas avoir le magic chiffré"


def test_export_park_hub_db_null_when_dump_fails(fake_parc, tmp_path, monkeypatch):
    """Si le dump Hub DB échoue, le manifest doit marquer hub_db à null — sinon
    il pointait vers databases/hub.pgdump inexistant → restore abortait plus tard
    sans explication (faux sentiment de backup valide)."""
    import io, tarfile, json
    monkeypatch.setattr(pe, "_pg_dump", lambda c, u, d, o: False)  # dump échoue
    monkeypatch.setattr(pe, "_detect_apps", lambda: [])

    archive = pe.export_park(encrypt=False)
    with tarfile.open(archive, "r:gz") as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read())
    assert manifest["hub_db"] is None


def test_export_park_encrypted_roundtrip_restore(fake_parc, tmp_path, monkeypatch):
    """Une archive chiffrée peut être déchiffrée et extraite (round-trip export→restore)."""
    import io
    import tarfile
    import hubctl_cli.park_restore as pr
    monkeypatch.setattr(pe, "_pg_dump", lambda c, u, d, o: o.write_bytes(b"PGDUMP") or True)
    monkeypatch.setattr(pe, "_detect_apps", lambda: [])

    archive = pe.export_park(passphrase="roundtrip-pass")

    # Déchiffrer manuellement et extraire
    blob = archive.read_bytes()
    tar_bytes = pe.decrypt_archive(blob, "roundtrip-pass")
    extracted = tmp_path / "out"
    extracted.mkdir()
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        tar.extractall(extracted, filter="data")
    assert (extracted / "manifest.json").exists()
    assert (extracted / "secrets" / "jwt_private_key.pem").read_bytes() == b"-----PRIVATE-----\n"
