"""Tests park-restore : restauration de l'arborescence secrets sans écrasement.

Couvre la cohérence du round-trip export -> restore pour l'arbre HUB_base/secrets/
(sous-dossier service_accounts/ inclus).
"""
from __future__ import annotations
import subprocess
from pathlib import Path

import hubctl_cli.park_restore as pr


def test_restore_tree_copies_new_files_recursively(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    (src / "service_accounts").mkdir(parents=True)
    (src / "jwt_private_key.pem").write_bytes(b"PRIVATE")
    (src / "service_accounts" / "sa.env").write_text("SA=1\n")

    pr._restore_tree(src, dest)

    assert (dest / "jwt_private_key.pem").read_bytes() == b"PRIVATE"
    assert (dest / "service_accounts" / "sa.env").read_text() == "SA=1\n"


def test_restore_tree_does_not_overwrite_existing(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    (src / "jwt_private_key.pem").write_bytes(b"NEW")
    (dest / "jwt_private_key.pem").write_bytes(b"EXISTING")

    pr._restore_tree(src, dest)

    assert (dest / "jwt_private_key.pem").read_bytes() == b"EXISTING"


def test_restore_tree_creates_missing_dest(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest" / "nested"  # n'existe pas
    src.mkdir()
    (src / "f.pem").write_bytes(b"X")

    pr._restore_tree(src, dest)

    assert (dest / "f.pem").read_bytes() == b"X"


# ── Tests _check_hub_is_empty ──


class _FakeRun:
    """Mock de _run() qui retourne des sorties Docker/psql différentes selon la commande."""

    def __init__(self, container_status: str, db_output: str | None = None,
                 db_errors: bool = False):
        self.container_status = container_status
        self.db_output = db_output          # None = pas d'appel exec attendu
        self.db_errors = db_errors

    def __call__(self, cmd: list[str], **kwargs):
        assert isinstance(cmd, list)
        if "exec" in cmd and "psql" in cmd:
            if self.db_errors:
                raise subprocess.CalledProcessError(1, cmd, output=b"", stderr=b"connection refused")
            return subprocess.CompletedProcess(cmd, 0, stdout=self.db_output or "", stderr="")
        if "ps" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=self.container_status, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def test_check_empty_no_container(monkeypatch):
    """Aucun conteneur hub-db → Hub vierge → True."""
    monkeypatch.setattr(pr, "_run", _FakeRun(container_status=""))
    assert pr._check_hub_is_empty() is True


def test_check_empty_running_db_empty(monkeypatch):
    """Conteneur tourne, table users absente (retourne 0) → vierge → True."""
    monkeypatch.setattr(pr, "_run", _FakeRun(
        container_status="Up 2 hours",
        db_output="0",
    ))
    assert pr._check_hub_is_empty() is True


def test_check_empty_running_db_populated(monkeypatch):
    """Conteneur tourne, table users existe et contient des lignes (≠ 0) → NON vierge → False."""
    monkeypatch.setattr(pr, "_run", _FakeRun(
        container_status="Up 3 days",
        db_output="1",
    ))
    assert pr._check_hub_is_empty() is False


def test_check_empty_container_stopped(monkeypatch):
    """Conteneur existe mais arrêté (Exited) → on refuse par sécurité → False."""
    monkeypatch.setattr(pr, "_run", _FakeRun(
        container_status="Exited (0) 3 days ago",
    ))
    assert pr._check_hub_is_empty() is False


def test_check_empty_psql_fails(monkeypatch):
    """Conteneur tourne mais la requête psql échoue → refus → False."""
    monkeypatch.setattr(pr, "_run", _FakeRun(
        container_status="Up 1 hour",
        db_errors=True,
    ))
    assert pr._check_hub_is_empty() is False


def test_restore_tree_rejects_parent_traversal(tmp_path, monkeypatch):
    """Un membre avec '..' NE doit PAS écrire hors de dest."""
    src = tmp_path / "src"
    dest = tmp_path / "sub" / "dest"

    src.mkdir()
    (src / "legit.txt").write_text("ok")
    (tmp_path / "evil.txt").write_text("EVIL")

    injected = src / ".." / "evil.txt"
    original_rglob = Path.rglob
    def fake_rglob(self, pattern):
        yield from original_rglob(self, pattern)
        if str(self) == str(src):
            yield injected
    monkeypatch.setattr(Path, "rglob", fake_rglob)

    report = pr._restore_tree(src, dest)

    assert (dest / "legit.txt").exists()
    assert (dest / "legit.txt").read_text() == "ok"
    assert not (tmp_path / "sub" / "evil.txt").exists(), \
        "Le fichier evil ne doit pas être copié hors de dest"

    created_files = [r[0] for r in report if r[1]]
    assert "legit.txt" in created_files
    assert "../evil.txt" not in created_files
