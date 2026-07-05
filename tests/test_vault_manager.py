from hubctl_cli import hub_state
from hubctl_cli.coffre import Vault
from hubctl_cli.vault_manager import VaultManager


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(hub_state, "HUB_DIR", tmp_path / "HUB_base")
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path / "APPS")
    monkeypatch.setattr(hub_state, "WORKSHOP_DIR", tmp_path / "APP_Workshop")
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


def test_managed_api_removed():
    for attr in ("push_env", "pull_env", "auto_import_missing", "toggle_managed", "managed_env"):
        assert not hasattr(VaultManager, attr)
