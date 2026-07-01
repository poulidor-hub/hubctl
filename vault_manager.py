from __future__ import annotations
import hashlib
from pathlib import Path

from hubctl_cli import hub_state, env_manager
from hubctl_cli.coffre import Vault


def _sorted_hash(data: dict) -> str:
    items = sorted(data.items())
    raw = "".join(f"{k}={v}" for k, v in items)
    return hashlib.sha256(raw.encode()).hexdigest()


def _target_path(target: str) -> Path | None:
    if target == "hub":
        return hub_state.HUB_DIR / ".env"
    if target.startswith("app:"):
        name = target[4:]
        return hub_state.APPS_DIR / name / ".env"
    if target.startswith("ws:"):
        name = target[3:]
        return hub_state.WORKSHOP_DIR / name / ".env"
    return None


class VaultManager:
    def __init__(self, vault: Vault):
        self.vault = vault

    @property
    def managed_env(self) -> bool:
        from hubctl_cli import hub_state as hs
        if self.vault.is_locked:
            return False
        data = self.vault._data
        if data is None:
            return False
        meta = data.get("__meta__", {})
        if "managed_env" in meta:
            return bool(meta["managed_env"])
        if hs.get_mode() == "dev":
            return False
        hub_env = data.get("hub", {})
        return hub_env.get("APP_ENV", "").lower() != "development"

    def toggle_managed(self) -> bool:
        if self.vault.is_locked:
            return False
        meta = self.vault._data.setdefault("__meta__", {})
        meta["managed_env"] = not self.managed_env
        self.vault.save()
        return meta["managed_env"]

    def push_env(self, target: str, *, on_log=None) -> bool:
        if self.vault.is_locked or self.vault._data is None:
            return False
        disk_path = _target_path(target)
        if disk_path is None:
            return False
        data = self.vault.get_entry(target)
        if not data:
            return False

        env_manager.backup_env(disk_path)

        disk_path.parent.mkdir(parents=True, exist_ok=True)
        env_manager.write_env(disk_path, dict(data))

        if on_log:
            on_log(f"[vault] push {target} → {disk_path}")
        return True

    def pull_env(self, target: str, *, on_log=None) -> bool:
        if self.vault.is_locked or self.vault._data is None:
            return False
        disk_path = _target_path(target)
        if disk_path is None or not disk_path.exists():
            return False
        disk_data = env_manager.read_env(disk_path)
        if not disk_data:
            return False

        self.vault.set_entry(target, disk_data)

        if on_log:
            on_log(f"[vault] pull {disk_path} → {target}")
        return True

    def auto_import_missing(self, *, on_log=None) -> int:
        if self.vault.is_locked or self.vault._data is None:
            return 0
        count = 0
        for target in self._list_disk_targets():
            if target not in self.vault._data:
                if self.pull_env(target, on_log=on_log):
                    count += 1
        self.vault.save()
        return count

    def check_divergence(self) -> dict[str, str]:
        if self.vault.is_locked or self.vault._data is None:
            return {}
        diverged: dict[str, str] = {}
        for target in self._list_disk_targets():
            if target not in self.vault._data:
                continue
            disk_path = _target_path(target)
            if not disk_path or not disk_path.exists():
                continue
            disk_data = env_manager.read_env(disk_path)
            vault_data = self.vault.get_entry(target)
            if _sorted_hash(disk_data) != _sorted_hash(vault_data):
                diverged[target] = target
        return diverged

    def _list_disk_targets(self) -> list[str]:
        targets: list[str] = []
        hub_env = hub_state.HUB_DIR / ".env"
        if hub_env.exists():
            targets.append("hub")
        if hub_state.APPS_DIR.exists():
            for d in sorted(hub_state.APPS_DIR.iterdir()):
                if d.is_dir() and not d.name.startswith(".") and (d / ".env").exists():
                    targets.append(f"app:{d.name}")
        if hub_state.WORKSHOP_DIR.exists():
            for d in sorted(hub_state.WORKSHOP_DIR.iterdir()):
                if d.is_dir() and not d.name.startswith(".") and (d / ".env").exists():
                    targets.append(f"ws:{d.name}")
        return targets
