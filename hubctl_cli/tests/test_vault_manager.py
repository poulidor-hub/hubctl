# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

from hubctl_cli import hub_state
from hubctl_cli.coffre import Vault
from hubctl_cli.vault_manager import VaultManager, restore_all


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(hub_state, "HUB_DIR", tmp_path / "HUB_base")
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path / "APPS")
    # recovery_dir hermétique : isolé du paquet réel
    monkeypatch.setattr(
        hub_state, "recovery_dir",
        lambda *a, **k: tmp_path / "hubctl_cli" / "recovery")
    (tmp_path / "HUB_base" / "secrets").mkdir(parents=True)
    (tmp_path / "HUB_base" / ".env").write_text("POSTGRES_PASSWORD=sec\n")
    (tmp_path / "HUB_base" / "secrets" / "jwt_private_key.pem").write_bytes(b"KEYDATA")
    v = Vault(tmp_path / "vault" / "v.enc")
    v.unlock("pw")
    return VaultManager(v)


def test_backup_all_captures_env_and_files(tmp_path, monkeypatch):
    mgr = _setup(tmp_path, monkeypatch)
    summary = mgr.backup_all()
    assert mgr.vault.get_entry("hub") == {"POSTGRES_PASSWORD": "sec"}
    assert mgr.vault.get_file("HUB_base/secrets/jwt_private_key.pem") == b"KEYDATA"
    assert summary["env"] >= 1 and summary["files"] >= 1


def test_backup_all_does_not_modify_source(tmp_path, monkeypatch):
    mgr = _setup(tmp_path, monkeypatch)
    env_path = tmp_path / "HUB_base" / ".env"
    key_path = tmp_path / "HUB_base" / "secrets" / "jwt_private_key.pem"
    before = (env_path.read_text(), key_path.read_bytes())
    mgr.backup_all()
    assert (env_path.read_text(), key_path.read_bytes()) == before


def test_export_recovery_writes_tree_only_under_recovery(tmp_path, monkeypatch):
    mgr = _setup(tmp_path, monkeypatch)
    mgr.backup_all()
    (tmp_path / "HUB_base" / ".env").unlink()
    dest = mgr.export_recovery()
    assert dest.parent == tmp_path / "hubctl_cli" / "recovery"
    assert (dest / "HUB_base" / ".env").read_text().strip() == "POSTGRES_PASSWORD=sec"
    assert (dest / "HUB_base" / "secrets" / "jwt_private_key.pem").read_bytes() == b"KEYDATA"
    assert not (tmp_path / "HUB_base" / ".env").exists()


def test_backup_all_captures_service_accounts_subtree(tmp_path, monkeypatch):
    """Le coffre doit capturer récursément HUB_base/secrets/, y compris
    service_accounts/ (credentials SA par app)."""
    mgr = _setup(tmp_path, monkeypatch)
    sa = tmp_path / "HUB_base" / "secrets" / "service_accounts" / "sa_demo.env"
    sa.parent.mkdir(parents=True)
    sa.write_text("SA_TOKEN=xyz\n")
    summary = mgr.backup_all()
    assert mgr.vault.get_file("HUB_base/secrets/service_accounts/sa_demo.env") == b"SA_TOKEN=xyz\n"
    assert summary["files"] >= 2


def test_export_recovery_reproduces_service_accounts_subtree(tmp_path, monkeypatch):
    mgr = _setup(tmp_path, monkeypatch)
    sa = tmp_path / "HUB_base" / "secrets" / "service_accounts" / "sa_demo.env"
    sa.parent.mkdir(parents=True)
    sa.write_text("SA_TOKEN=xyz\n")
    mgr.backup_all()
    dest = mgr.export_recovery()
    assert (dest / "HUB_base" / "secrets" / "service_accounts" / "sa_demo.env").read_text() == "SA_TOKEN=xyz\n"


def test_export_recovery_files_are_not_world_readable(tmp_path, monkeypatch):
    """Les secrets déchiffrés dans recovery/ ne doivent pas être world-readable."""
    mgr = _setup(tmp_path, monkeypatch)
    mgr.backup_all()
    dest = mgr.export_recovery()
    for f in dest.rglob("*"):
        if f.is_file():
            mode = f.stat().st_mode
            assert (mode & 0o004) == 0, f"{f.relative_to(dest)} est world-readable ({oct(mode)})"
    assert (dest.stat().st_mode & 0o007) == 0, "recovery/ ne doit pas être world-accessible"


def test_managed_api_removed():
    for attr in ("push_env", "pull_env", "auto_import_missing", "toggle_managed", "managed_env"):
        assert not hasattr(VaultManager, attr)


def test_backup_all_denies_symlinked_app(tmp_path, monkeypatch):
    """backup_all ne doit PAS sauvegarder les .env de dossiers symlinks."""
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(hub_state, "HUB_DIR", tmp_path / "HUB_base")
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path / "APPS")
    (tmp_path / "HUB_base" / "secrets").mkdir(parents=True)
    (tmp_path / "HUB_base" / ".env").write_text("POSTGRES_PASSWORD=sec\n")
    external = tmp_path / "external"; external.mkdir()
    (external / ".env").write_text("OUTSIDE=1\n")
    (tmp_path / "APPS").mkdir()
    (tmp_path / "APPS" / "linked").symlink_to(external, target_is_directory=True)
    v = Vault(tmp_path / "vault" / "v.enc")
    v.unlock("pw")
    mgr = VaultManager(v)
    mgr.backup_all()
    assert "app:linked" not in v.list_entries()


def test_backup_all_cleans_stale_app_entries(tmp_path, monkeypatch):
    """backup_all doit supprimer les entrées d'apps disparues du disque."""
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(hub_state, "HUB_DIR", tmp_path / "HUB_base")
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path / "APPS")
    (tmp_path / "HUB_base" / "secrets").mkdir(parents=True)
    (tmp_path / "HUB_base" / ".env").write_text("POSTGRES_PASSWORD=sec\n")
    app = tmp_path / "APPS" / "gone"; app.mkdir(parents=True)
    (app / ".env").write_text("DEAD=1\n")
    v = Vault(tmp_path / "vault" / "v.enc")
    v.unlock("pw")
    mgr = VaultManager(v)
    mgr.backup_all()
    assert "app:gone" in v.list_entries()
    # Supprimer l'app physique puis re-sauvegarder
    (app / ".env").unlink(); app.rmdir()
    mgr.backup_all()
    assert "app:gone" not in v.list_entries()


# ── CA HTTPS (HUB_base/certs/ca) : sauvegarde + restauration ──


def _write_ca(tmp_path):
    """Pose une CA factice sur le disque (comme HUB_base/certs/ca en prod)."""
    ca_dir = tmp_path / "HUB_base" / "certs" / "ca"
    ca_dir.mkdir(parents=True)
    (ca_dir / "ca.crt").write_bytes(b"-----BEGIN CERTIFICATE-----")
    (ca_dir / "ca.key").write_bytes(b"-----BEGIN PRIVATE KEY-----")
    return ca_dir


def _mock_backup_db(monkeypatch):
    """Hermetise backup_all : sans ce mock, lancé sur un serveur HUB (docker
    + hub-db vivants), le coffre de test embarquerait un VRAI dump — et un
    restore_all rejouerait un TRUNCATE sur la base réelle."""
    from hubctl_cli import vault_manager as vm
    monkeypatch.setattr(vm, "_backup_db", lambda vault, on_log=None: False)


def test_backup_all_captures_certs_ca(tmp_path, monkeypatch):
    """backup_all doit capturer ca.crt + ca.key sous leur relpath PARC_ROOT :
    la CA est importée dans les navigateurs — un PRA sans elle rejette toute
    la flotte (SEC_ERROR_BAD_SIGNATURE)."""
    mgr = _setup(tmp_path, monkeypatch)
    _write_ca(tmp_path)
    _mock_backup_db(monkeypatch)
    logs = []
    summary = mgr.backup_all(on_log=logs.append)
    assert mgr.vault.get_file("HUB_base/certs/ca/ca.crt") == b"-----BEGIN CERTIFICATE-----"
    assert mgr.vault.get_file("HUB_base/certs/ca/ca.key") == b"-----BEGIN PRIVATE KEY-----"
    assert summary["ca"] == 2
    assert any("HUB_base/certs/ca/ca.crt" in l for l in logs)


def test_backup_all_without_certs_ca_is_graceful(tmp_path, monkeypatch):
    """HUB_base sans certs/ca (HTTPS non déployé) → pas de crash, compteur 0,
    rien d'inventé dans le coffre."""
    mgr = _setup(tmp_path, monkeypatch)
    _mock_backup_db(monkeypatch)
    summary = mgr.backup_all()
    assert summary["ca"] == 0
    assert not any(f.startswith("HUB_base/certs/ca/") for f in mgr.vault.list_files())


def test_restore_all_restores_certs_ca_with_modes(tmp_path, monkeypatch):
    """restore_all doit remettre EXACTEMENT la CA d'origine : ca.key 0600,
    ca.crt 0644, dossier 0700 — c'est ce qui préserve la confiance des
    navigateurs après une restauration PRA."""
    mgr = _setup(tmp_path, monkeypatch)
    ca_dir = _write_ca(tmp_path)
    _mock_backup_db(monkeypatch)
    mgr.backup_all()
    # Simule la perte du disque : la CA disparaît, le coffre seul la connaît.
    for f in ca_dir.iterdir():
        f.unlink()
    ca_dir.rmdir()

    restored = restore_all(mgr.vault)

    crt = ca_dir / "ca.crt"
    key = ca_dir / "ca.key"
    assert crt.read_bytes() == b"-----BEGIN CERTIFICATE-----"
    assert key.read_bytes() == b"-----BEGIN PRIVATE KEY-----"
    assert key.stat().st_mode & 0o777 == 0o600
    assert crt.stat().st_mode & 0o777 == 0o644
    assert ca_dir.stat().st_mode & 0o777 == 0o700
    assert any("certs/ca" in r for r in restored)
    assert any("CA HTTPS" in r for r in restored)


def test_restore_all_old_vault_without_certs_ca(tmp_path, monkeypatch):
    """Coffre ancien sans certs/ca → pas de crash, rien d'écrit sous certs/
    (comportement dégradé attendu sur les coffres d'avant la feature)."""
    mgr = _setup(tmp_path, monkeypatch)
    _mock_backup_db(monkeypatch)
    mgr.backup_all()
    restored = restore_all(mgr.vault)
    assert not (tmp_path / "HUB_base" / "certs").exists()
    assert not any("certs/ca" in r for r in restored)


# ── import_vault : validation + backup de l'ancien coffre ──


def test_import_vault_rejects_invalid_magic(tmp_path):
    """Un fichier externe sans magic HUBCTLV1 ne doit pas remplacer le coffre
    courant (n'importe quel binaire était auparavant accepté)."""
    from hubctl_cli.vault_manager import import_vault
    cur = tmp_path / "v.enc"
    v = Vault(cur)
    v.unlock("pw")
    v.set_entry("hub", {"K": "ORIGINAL"})
    v.save()
    original = cur.read_bytes()

    bad = tmp_path / "bad.enc"
    bad.write_bytes(b"NOT_A_VAULT_garbage")

    assert import_vault(v, bad) is False
    assert cur.read_bytes() == original, "le coffre courant ne doit pas être touché"
    assert not (tmp_path / "v.enc.bak").exists(), "rien n'a été importé → pas de backup"


def test_import_vault_valid_backs_up_current(tmp_path):
    """Un import valide préserve l'ancien coffre dans un .bak récupérable."""
    from hubctl_cli.vault_manager import import_vault
    cur = tmp_path / "v.enc"
    v = Vault(cur)
    v.unlock("pw")
    v.set_entry("hub", {"K": "OLD"})
    v.save()

    src_path = tmp_path / "new.enc"
    vsrc = Vault(src_path)
    vsrc.unlock("other")
    vsrc.set_entry("hub", {"K": "NEW"})
    vsrc.save()

    assert import_vault(v, src_path) is True

    bak = tmp_path / "v.enc.bak"
    assert bak.exists(), "un backup de l'ancien coffre doit être créé"
    vb = Vault(bak)
    assert vb.unlock("pw") is True
    assert vb.reveal("hub") == {"K": "OLD"}, "le .bak doit contenir l'ancien état"
    assert cur.read_bytes() == src_path.read_bytes(), "le courant = le nouveau"


def test_import_vault_missing_src(tmp_path):
    from hubctl_cli.vault_manager import import_vault
    v = Vault(tmp_path / "v.enc")
    v.unlock("pw")
    assert import_vault(v, tmp_path / "absent.enc") is False


# ── _restore_db : snapshot avant truncate + rollback sur échec ──


_SNAPSHOT = b"-- snapshot live --\nINSERT INTO users VALUES (1);\n"


def _db_dispatch(cmd, success_replay):
    """Fake subprocess.run : simule pg_dump (snapshot), TRUNCATE, et psql replay."""
    from unittest import mock
    joined = " ".join(str(c) for c in cmd)
    inp = cmd  # placeholder
    if "pg_dump" in joined:
        return mock.Mock(returncode=0, stdout=_SNAPSHOT, stderr=b"")
    if "TRUNCATE" in joined:
        return mock.Mock(returncode=0, stdout=b"", stderr=b"")
    return mock.Mock(returncode=0 if success_replay else 1,
                     stdout=b"", stderr=b"ERROR: bad dump")


def test_restore_db_rolls_back_on_failure(monkeypatch):
    """Si le replay du dump échoue, _restore_db doit restaurer le snapshot live
    (pris AVANT truncate) pour ne pas laisser la DB vidée sans recours."""
    import subprocess
    from hubctl_cli import vault_manager

    calls = []

    def fake_run(cmd, **kw):
        calls.append({"cmd": list(cmd), "input": kw.get("input")})
        return _db_dispatch(cmd, success_replay=False)

    monkeypatch.setattr(subprocess, "run", fake_run)
    logs = []
    ok = vault_manager._restore_db(b"DUMP_COFFRE", on_log=logs.append)

    assert ok is False
    # 1) un snapshot live est pris AVANT le 1er truncate
    def joined(c):
        return " ".join(str(x) for x in c["cmd"])
    pgdump_idx = next(i for i, c in enumerate(calls) if "pg_dump" in joined(c))
    truncate_idx = next(i for i, c in enumerate(calls) if "TRUNCATE" in joined(c))
    assert pgdump_idx < truncate_idx, "le snapshot doit précéder le truncate"
    # 2) le rollback rejoue le snapshot (psql avec input=snapshot)
    assert any(c["input"] == _SNAPSHOT for c in calls), \
        "le snapshot live doit être rejoué en rollback après échec"
    # 3) un log d'erreur est émis (plus d'échec silencieux)
    assert logs, "un log doit être émis en cas d'échec"


def test_restore_db_success_takes_snapshot(monkeypatch):
    """Même en succès, un snapshot live est pris (filet de sécurité), sans rollback."""
    import subprocess
    from hubctl_cli import vault_manager

    calls = []

    def fake_run(cmd, **kw):
        calls.append({"cmd": list(cmd), "input": kw.get("input")})
        return _db_dispatch(cmd, success_replay=True)

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok = vault_manager._restore_db(b"DUMP_COFFRE")

    assert ok is True
    # snapshot pris
    assert any("pg_dump" in " ".join(str(x) for x in c["cmd"]) for c in calls)
    # pas de rollback : le snapshot n'est jamais rejoué en psql
    assert not any(c["input"] == _SNAPSHOT for c in calls)


# ── Hook réplication PRA ──


def test_backup_all_triggers_pra_replicate(tmp_path, monkeypatch):
    mgr = _setup(tmp_path, monkeypatch)
    from hubctl_cli import pra_replicate
    calls = []
    monkeypatch.setattr(pra_replicate, "replicate",
                        lambda **kw: calls.append(kw) or {"status": "ok", "copied": [], "message": ""})
    summary = mgr.backup_all()
    assert calls, "backup_all doit appeler pra_replicate.replicate"
    assert summary["pra"]["status"] == "ok"


def test_backup_all_survives_replicate_crash(tmp_path, monkeypatch):
    mgr = _setup(tmp_path, monkeypatch)
    from hubctl_cli import pra_replicate

    def boom(**kw):
        raise RuntimeError("usb arrachée")

    monkeypatch.setattr(pra_replicate, "replicate", boom)
    summary = mgr.backup_all()
    assert summary["env"] >= 1          # la sauvegarde a réussi quand même
    assert summary["pra"] is None       # la réplication a échoué silencieusement
