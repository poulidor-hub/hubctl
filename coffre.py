from __future__ import annotations
import base64
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from hubctl_cli import env_manager, hub_state

MAGIC = b"HUBCTLV1"


def _write_env_if_needed(path: Path, data: dict, dry_run: bool, force: bool) -> None:
    if not force and path.exists():
        return
    if not dry_run:
        env_manager.write_env(path, data)


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
            salt = blob[len(MAGIC):len(MAGIC) + 16]
            token = blob[len(MAGIC) + 16:]
            fernet = Fernet(derive_key(passphrase, salt))
            try:
                data = json.loads(fernet.decrypt(token))
            except (InvalidToken, ValueError):
                # Ne pas corrompre l'état existant : réinitialiser
                self._salt = None
                self._fernet = None
                self._data = None
                return False
            self._salt = salt
            self._fernet = fernet
            self._data = data
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

    def set_file(self, relpath: str, raw: bytes) -> None:
        assert self._data is not None, "vault verrouillé"
        files = self._data.setdefault("__files__", {})
        files[relpath] = base64.b64encode(raw).decode("ascii")

    def get_file(self, relpath: str) -> bytes | None:
        assert self._data is not None, "vault verrouillé"
        b64 = self._data.get("__files__", {}).get(relpath)
        return base64.b64decode(b64) if b64 is not None else None

    def list_files(self) -> list[str]:
        assert self._data is not None, "vault verrouillé"
        return sorted(self._data.get("__files__", {}).keys())

    def change_passphrase(self, new_pw: str) -> None:
        assert self._data is not None, "vault verrouillé"
        self._salt = os.urandom(16)
        self._fernet = Fernet(derive_key(new_pw, self._salt))

    def sync(self) -> None:
        assert self._data is not None, "vault verrouillé"
        # Nettoyer les entrées obsoletes (apps/ws qui n'existent plus sur le disque)
        current_entries: set[str] = set()
        hub = env_manager.read_env(hub_state.HUB_DIR / ".env")
        if hub:
            self._data["hub"] = hub
            current_entries.add("hub")
        if hub_state.APPS_DIR.exists():
            for d in sorted(hub_state.APPS_DIR.iterdir()):
                if d.is_dir() and not d.is_symlink() and not d.name.startswith("."):
                    env = env_manager.read_env(d / ".env")
                    if env:
                        key = f"app:{d.name}"
                        self._data[key] = env
                        current_entries.add(key)
        if hub_state.WORKSHOP_DIR.exists():
            for d in sorted(hub_state.WORKSHOP_DIR.iterdir()):
                if d.is_dir() and not d.is_symlink() and not d.name.startswith("."):
                    env = env_manager.read_env(d / ".env")
                    if env:
                        key = f"ws:{d.name}"
                        self._data[key] = env
                        current_entries.add(key)
        self._data["__apps__"] = []
        if hub_state.APPS_DIR.exists():
            self._data["__apps__"] = [
                d.name for d in sorted(hub_state.APPS_DIR.iterdir())
                if d.is_dir() and (d / ".env").exists()
            ]
        # Supprimer les entrees d'apps disparues
        stale = [k for k in self._data if k.startswith(("app:", "ws:")) and k not in current_entries]
        for k in stale:
            self._data.pop(k, None)

    def restore(self, *, dry_run: bool = False, force: bool = False) -> list[str]:
        assert self._data is not None, "vault verrouillé"
        written = []
        if "hub" in self._data:
            path = hub_state.HUB_DIR / ".env"
            _write_env_if_needed(path, self._data["hub"], dry_run, force)
            written.append(str(path))
        for slug in self._data.get("__apps__", []):
            (hub_state.APPS_DIR / slug).mkdir(parents=True, exist_ok=True)
        for key, data in self._data.items():
            if key.startswith("app:"):
                slug = key[4:]
                path = hub_state.APPS_DIR / slug / ".env"
                path.parent.mkdir(parents=True, exist_ok=True)
                _write_env_if_needed(path, data, dry_run, force)
                written.append(str(path))
            elif key.startswith("ws:"):
                slug = key[3:]
                path = hub_state.WORKSHOP_DIR / slug / ".env"
                path.parent.mkdir(parents=True, exist_ok=True)
                _write_env_if_needed(path, data, dry_run, force)
                written.append(str(path))
        for relpath in self._data.get("__files__", {}):
            raw = self.get_file(relpath)
            if raw is None:
                continue
            target = hub_state.PARC_ROOT / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            if not dry_run:
                target.write_bytes(raw)
                os.chmod(target, 0o600)
            written.append(str(target))
        return written

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
