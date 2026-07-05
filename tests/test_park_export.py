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

    archive = pe.export_park()
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
