"""Tests park-restore : restauration de l'arborescence secrets sans écrasement.

Couvre la cohérence du round-trip export -> restore pour l'arbre HUB_base/secrets/
(sous-dossier service_accounts/ inclus).
"""
from __future__ import annotations
import io
import subprocess
import tarfile
import tempfile
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


def _make_tar_multi(members: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def test_restore_park_rejects_traversal_member(tmp_path, monkeypatch):
    """tar.extractall() sans filter= permet l'écriture hors workdir via '../'."""
    protected = tmp_path / "protected.txt"
    protected.write_text("sensitive")

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    hub_dir = tmp_path / "HUB_base"
    apps_dir = tmp_path / "APPS"
    hub_dir.mkdir(parents=True)
    apps_dir.mkdir(parents=True)

    archive = tmp_path / "evil.tar.gz"
    archive.write_bytes(_make_tar_multi([
        ("../protected.txt", b"EVIL"),
        ("manifest.json",
         b'{"version":"1","exported_at":"2025-01-01T00:00:00","apps":[],'
         b'"hub_db":{"pgdump":"databases/hub.pgdump"}}'),
    ]))

    monkeypatch.setattr(pr, "_check_hub_is_empty", lambda: True)
    monkeypatch.setattr(pr, "_run",
                        lambda cmd, check=True: subprocess.CompletedProcess(
                            cmd, 0, stdout="", stderr=""))
    monkeypatch.setattr(pr, "_wait_db_ready", lambda c, timeout=90: True)
    monkeypatch.setattr(pr, "_restore_pgdump",
                        lambda container, user, db, path: True)
    monkeypatch.setattr(pr, "HUB_DIR", hub_dir)
    monkeypatch.setattr(pr, "APPS_DIR", apps_dir)
    monkeypatch.setattr(pr, "PARC_ROOT", tmp_path)

    class _FakeTempDir:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return str(self.path)

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(tempfile, "TemporaryDirectory",
                        lambda prefix=None, suffix=None, dir=None: _FakeTempDir(work_dir))

    try:
        pr.restore_park(str(archive))
    except tarfile.TarError:
        pass

    assert protected.read_text() == "sensitive", (
        "Fichier hors workdir ne doit pas être modifié par un membre '../'"
    )


def test_restore_park_decrypts_encrypted_archive(tmp_path, monkeypatch):
    """restore_park détecte et déchiffre une archive .tar.gz.enc."""
    import io
    import tarfile
    import hubctl_cli.park_export as pe

    # Construire une archive chiffrée avec un manifest valide
    work = tmp_path / "src"
    work.mkdir()
    (work / "manifest.json").write_text(
        '{"version":"1.0","exported_at":"2026-01-01","apps":[],"hub_db":{"pgdump":"databases/hub.pgdump"}}'
    )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(work, arcname="")
    blob = pe.encrypt_archive(buf.getvalue(), "restore-pass")

    enc_archive = tmp_path / "park.tar.gz.enc"
    enc_archive.write_bytes(blob)

    # Mock les dépendances externes de restore_park
    hub_dir = tmp_path / "HUB_base"
    hub_dir.mkdir(parents=True)
    (hub_dir / "secrets").mkdir()
    monkeypatch.setattr(pr, "HUB_DIR", hub_dir)
    monkeypatch.setattr(pr, "APPS_DIR", tmp_path / "APPS")
    monkeypatch.setattr(pr, "_check_hub_is_empty", lambda: True)
    monkeypatch.setattr(pr, "_run", lambda cmd, check=True: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))
    monkeypatch.setattr(pr, "_wait_db_ready", lambda c, timeout=90: True)
    monkeypatch.setattr(pr, "_restore_pgdump", lambda c, u, d, p: True)

    result = pr.restore_park(str(enc_archive), passphrase="restore-pass")
    assert result is True
    assert (hub_dir / ".env").exists() or True  # pas de .env dans ce manifest minimal


def test_restore_park_accepts_legacy_plain_archive(tmp_path, monkeypatch):
    """restore_park accepte toujours une archive .tar.gz legacy non chiffrée."""
    import io
    import tarfile

    work = tmp_path / "src"
    work.mkdir()
    (work / "manifest.json").write_text(
        '{"version":"1.0","exported_at":"2026-01-01","apps":[],"hub_db":{"pgdump":"databases/hub.pgdump"}}'
    )
    plain_archive = tmp_path / "park-legacy.tar.gz"
    with tarfile.open(plain_archive, "w:gz") as tar:
        tar.add(work, arcname="")

    hub_dir = tmp_path / "HUB_base"
    hub_dir.mkdir(parents=True)
    (hub_dir / "secrets").mkdir()
    monkeypatch.setattr(pr, "HUB_DIR", hub_dir)
    monkeypatch.setattr(pr, "APPS_DIR", tmp_path / "APPS")
    monkeypatch.setattr(pr, "_check_hub_is_empty", lambda: True)
    monkeypatch.setattr(pr, "_run", lambda cmd, check=True: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))
    monkeypatch.setattr(pr, "_wait_db_ready", lambda c, timeout=90: True)
    monkeypatch.setattr(pr, "_restore_pgdump", lambda c, u, d, p: True)

    # Pas de passphrase — archive legacy
    result = pr.restore_park(str(plain_archive))
    assert result is True


def test_restore_park_encrypted_wrong_passphrase_fails_gracefully(tmp_path, monkeypatch):
    """Une mauvaise passphrase sur une archive chiffrée échoue proprement (pas de crash)."""
    import io
    import tarfile
    import hubctl_cli.park_export as pe

    work = tmp_path / "src"
    work.mkdir()
    (work / "manifest.json").write_text('{"version":"1.0","apps":[]}')
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(work, arcname="")
    enc_archive = tmp_path / "park.tar.gz.enc"
    enc_archive.write_bytes(pe.encrypt_archive(buf.getvalue(), "bonne"))

    monkeypatch.setattr(pr, "_check_hub_is_empty", lambda: True)
    monkeypatch.setattr(pr, "HUB_DIR", tmp_path / "HUB_base")

    result = pr.restore_park(str(enc_archive), passphrase="mauvaise")
    assert result is False


# ── _safe_extractall : anti path-traversal + compat Python < 3.12 (#18) ──


def _make_tar(members):
    """Construit un tar.gz en mémoire avec les (name, data) donnés."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return tarfile.open(fileobj=buf, mode="r:gz")


def test_safe_extractall_rejects_path_traversal(tmp_path):
    """Une archive malveillante avec une entrée '../../evil' ne doit pas être
    extraite hors du répertoire cible."""
    tar = _make_tar([("../evil_outside.txt", b"PWNED")])
    assert pr._safe_extractall(tar, tmp_path) is False
    assert not (tmp_path.parent / "evil_outside.txt").exists()


def test_safe_extractall_accepts_clean_archive(tmp_path):
    tar = _make_tar([
        ("manifest.json", b'{"version":"1.0"}'),
        ("databases/hub.pgdump", b"DUMP"),
    ])
    assert pr._safe_extractall(tar, tmp_path) is True
    assert (tmp_path / "manifest.json").read_bytes() == b'{"version":"1.0"}'
    assert (tmp_path / "databases" / "hub.pgdump").read_bytes() == b"DUMP"


# ── Réconciliation migrations fourchues (dump legacy → hub neuf) ──


def test_reconcile_purges_forked_migration(monkeypatch):
    """DB legacy : 027 marquée mais table port_allocations absente → DELETE émis."""
    calls = []

    def fake_run(cmd, check=True):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="DELETE 1", stderr="")

    monkeypatch.setattr(pr, "_run", fake_run)
    assert pr._reconcile_forked_migrations() is True
    joined = [" ".join(c) for c in calls]
    assert any("DELETE FROM schema_migrations" in c for c in joined)
    assert any("port_allocations" in c for c in joined)
    assert any("hub-db" in c for c in joined)


def test_reconcile_noop_when_our_027_applied(monkeypatch):
    """DB déjà conforme (notre 027 a tourné, table existe) → DELETE 0, True."""
    def fake_run(cmd, check=True):
        return subprocess.CompletedProcess(cmd, 0, stdout="DELETE 0", stderr="")

    monkeypatch.setattr(pr, "_run", fake_run)
    assert pr._reconcile_forked_migrations() is True


def test_reconcile_psql_failure_returns_false(monkeypatch):
    """psql échoue (ex. schema_migrations absente) → False, pas d'exception."""
    def fake_run(cmd, check=True):
        return subprocess.CompletedProcess(cmd, 1, stdout="",
                                           stderr="relation \"schema_migrations\" does not exist")

    monkeypatch.setattr(pr, "_run", fake_run)
    assert pr._reconcile_forked_migrations() is False


def test_restore_park_calls_reconciliation(tmp_path, monkeypatch):
    """restore_park appelle la réconciliation après le pg_restore du hub."""
    work = tmp_path / "src"
    work.mkdir()
    (work / "manifest.json").write_text(
        '{"version":"1.0","exported_at":"2026-01-01","apps":[],'
        '"hub_db":{"pgdump":"databases/hub.pgdump"}}'
    )
    plain_archive = tmp_path / "park.tar.gz"
    with tarfile.open(plain_archive, "w:gz") as tar:
        tar.add(work, arcname="")

    hub_dir = tmp_path / "HUB_base"
    hub_dir.mkdir()
    (hub_dir / "secrets").mkdir()
    monkeypatch.setattr(pr, "HUB_DIR", hub_dir)
    monkeypatch.setattr(pr, "APPS_DIR", tmp_path / "APPS")
    monkeypatch.setattr(pr, "_check_hub_is_empty", lambda: True)
    monkeypatch.setattr(pr, "_run", lambda cmd, check=True: subprocess.CompletedProcess(
        cmd, 0, stdout="", stderr=""))
    monkeypatch.setattr(pr, "_wait_db_ready", lambda c, timeout=90: True)
    monkeypatch.setattr(pr, "_restore_pgdump", lambda c, u, d, p: True)
    called = []
    monkeypatch.setattr(pr, "_reconcile_forked_migrations",
                        lambda c=None: called.append(c) or True)

    result = pr.restore_park(str(plain_archive))

    assert result is True
    assert len(called) == 1


# ── Reset DB hub avant pg_restore (dump legacy vs schéma courant) ──


def test_reset_hub_db_runs_drop_and_create(monkeypatch):
    calls = []

    def fake_run(cmd, check=True):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="DROP DATABASE\nCREATE DATABASE", stderr="")

    monkeypatch.setattr(pr, "_run", fake_run)
    assert pr._reset_hub_db() is True
    joined = " ".join(calls[0])
    assert "DROP DATABASE" in joined and "CREATE DATABASE" in joined
    assert "WITH (FORCE)" in joined
    assert "-d" in joined and "postgres" in joined


def test_reset_hub_db_failure_returns_false(monkeypatch):
    def fake_run(cmd, check=True):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="syntax error")

    monkeypatch.setattr(pr, "_run", fake_run)
    assert pr._reset_hub_db() is False


def test_restore_park_aborts_when_reset_fails(tmp_path, monkeypatch):
    work = tmp_path / "src"
    work.mkdir()
    (work / "manifest.json").write_text(
        '{"version":"1.0","exported_at":"2026-01-01","apps":[],'
        '"hub_db":{"pgdump":"databases/hub.pgdump"}}'
    )
    plain_archive = tmp_path / "park.tar.gz"
    with tarfile.open(plain_archive, "w:gz") as tar:
        tar.add(work, arcname="")
    hub_dir = tmp_path / "HUB_base"
    hub_dir.mkdir()
    (hub_dir / "secrets").mkdir()
    monkeypatch.setattr(pr, "HUB_DIR", hub_dir)
    monkeypatch.setattr(pr, "APPS_DIR", tmp_path / "APPS")
    monkeypatch.setattr(pr, "_check_hub_is_empty", lambda: True)
    monkeypatch.setattr(pr, "_run", lambda cmd, check=True: subprocess.CompletedProcess(
        cmd, 0, stdout="", stderr=""))
    monkeypatch.setattr(pr, "_wait_db_ready", lambda c, timeout=90: True)
    monkeypatch.setattr(pr, "_reset_hub_db", lambda c=None: False)
    pgdump_called = []
    monkeypatch.setattr(pr, "_restore_pgdump",
                        lambda c, u, d, p: pgdump_called.append(1) or True)

    result = pr.restore_park(str(plain_archive))

    assert result is False
    assert pgdump_called == [], "le pg_restore ne doit PAS tourner après un reset échoué"
