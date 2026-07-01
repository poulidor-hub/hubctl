from __future__ import annotations
import base64
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from hubctl_cli import env_manager, hub_state

MAGIC = b"HUBCTLV1"


def derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=2 ** 14, r=8, p=1)
    raw = kdf.derive(passphrase.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


class Vault:
    def __init__(self, vault_path: Path):
        self.path = Path(vault_path)
        self._salt: bytes | None = None
        self._fernet: Fernet | None = None
        self._data: dict | None = None

    @property
    def is_locked(self) -> bool:
        return self._fernet is None

    def unlock(self, passphrase: str) -> bool:
        if self.path.exists():
            blob = self.path.read_bytes()
            if not blob.startswith(MAGIC):
                return False
            self._salt = blob[len(MAGIC):len(MAGIC) + 16]
            token = blob[len(MAGIC) + 16:]
            fernet = Fernet(derive_key(passphrase, self._salt))
            try:
                self._data = json.loads(fernet.decrypt(token))
            except (InvalidToken, ValueError):
                return False
            self._fernet = fernet
        else:
            self._salt = os.urandom(16)
            self._fernet = Fernet(derive_key(passphrase, self._salt))
            self._data = {}
        return True

    def lock(self) -> None:
        self._fernet = None
        self._data = None

    def get_entry(self, key: str) -> dict:
        assert self._data is not None, "vault verrouillé"
        return dict(self._data.get(key, {}))

    def set_entry(self, key: str, data: dict) -> None:
        assert self._data is not None, "vault verrouillé"
        self._data[key] = dict(data)

    def update_entry(self, key: str, data: dict) -> None:
        assert self._data is not None, "vault verrouillé"
        merged = dict(self._data.get(key, {}))
        merged.update(data)
        self._data[key] = merged

    def remove_entry(self, key: str) -> None:
        assert self._data is not None, "vault verrouillé"
        self._data.pop(key, None)

    def sync(self, vault_mgr=None) -> None:
        assert self._data is not None, "vault verrouillé"
        if vault_mgr is not None:
            vault_mgr.pull_env("hub")
            for d in sorted(hub_state.APPS_DIR.iterdir()) if hub_state.APPS_DIR.exists() else ():
                if d.is_dir() and not d.name.startswith("."):
                    vault_mgr.pull_env(f"app:{d.name}")
            for d in sorted(hub_state.WORKSHOP_DIR.iterdir()) if hub_state.WORKSHOP_DIR.exists() else ():
                if d.is_dir() and not d.name.startswith("."):
                    vault_mgr.pull_env(f"ws:{d.name}")
        else:
            hub = env_manager.read_env(hub_state.HUB_DIR / ".env")
            if hub:
                self._data["hub"] = hub
            if hub_state.APPS_DIR.exists():
                for d in sorted(hub_state.APPS_DIR.iterdir()):
                    if d.is_dir() and not d.name.startswith("."):
                        env = env_manager.read_env(d / ".env")
                        if env:
                            self._data[f"app:{d.name}"] = env
            if hub_state.WORKSHOP_DIR.exists():
                for d in sorted(hub_state.WORKSHOP_DIR.iterdir()):
                    if d.is_dir() and not d.name.startswith("."):
                        env = env_manager.read_env(d / ".env")
                        if env:
                            self._data[f"ws:{d.name}"] = env

    def save(self) -> None:
        assert self._fernet is not None and self._salt is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        token = self._fernet.encrypt(json.dumps(self._data).encode("utf-8"))
        self.path.write_bytes(MAGIC + self._salt + token)
        os.chmod(self.path, 0o600)

    def list_entries(self) -> list[str]:
        assert self._data is not None
        return sorted(self._data.keys())

    def reveal(self, entry: str) -> dict:
        assert self._data is not None
        return dict(self._data.get(entry, {}))
