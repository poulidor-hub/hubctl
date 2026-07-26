from __future__ import annotations
import base64
import datetime
import hashlib
import os
from pathlib import Path

from hubctl_cli import __version__, hub_state, env_manager
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

    def backup_all(self, *, on_log=None) -> dict:
        if self.vault.is_locked or self.vault._data is None:
            return {"env": 0, "files": 0}
        self.vault.sync()
        files = 0
        secrets_dir = hub_state.HUB_DIR / "secrets"
        if secrets_dir.exists():
            for f in sorted(secrets_dir.rglob("*")):
                if not f.is_file() or f.is_symlink():
                    continue
                rel = f.relative_to(hub_state.PARC_ROOT).as_posix()
                self.vault.set_file(rel, f.read_bytes())
                files += 1
                if on_log:
                    on_log(f"[vault] backup {rel}")
        self.vault.save()
        env_count = sum(1 for k in self.vault.list_entries() if not k.startswith("__"))
        return {"env": env_count, "files": files}

    def export_recovery(self) -> "Path":
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = hub_state.PARC_ROOT / "hubctl_cli" / "recovery" / stamp
        dest.mkdir(parents=True, exist_ok=True)
        os.chmod(dest, 0o700)
        old_umask = os.umask(0o077)
        for entry in self.vault.list_entries():
            if entry.startswith("__"):
                continue
            data = self.vault.get_entry(entry)
            if entry == "hub":
                rel = Path("HUB_base") / ".env"
            elif entry.startswith("app:"):
                rel = Path("APPS") / entry[4:] / ".env"
            elif entry.startswith("ws:"):
                rel = Path("APP_Workshop") / entry[3:] / ".env"
            else:
                continue
            out = (dest / rel).resolve()
            if not out.is_relative_to(dest.resolve()):
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("".join(f"{k}={v}\n" for k, v in data.items()))
        for rel in self.vault.list_files():
            raw = self.vault.get_file(rel)
            if raw is None:
                continue
            out = (dest / rel).resolve()
            if not out.is_relative_to(dest.resolve()):
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(raw)
        os.umask(old_umask)
        return dest

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
                if d.is_dir() and not d.is_symlink() and not d.name.startswith(".") and (d / ".env").exists():
                    targets.append(f"app:{d.name}")
        if hub_state.WORKSHOP_DIR.exists():
            for d in sorted(hub_state.WORKSHOP_DIR.iterdir()):
                if d.is_dir() and not d.is_symlink() and not d.name.startswith(".") and (d / ".env").exists():
                    targets.append(f"ws:{d.name}")
        return targets


def restore_all(vault, *, dry_run: bool = False, force: bool = False) -> list[str]:
    """Restaure tous les .env et secrets depuis le coffre."""
    return vault.restore(dry_run=dry_run, force=force)


def export_vault(vault, dest: Path) -> Path:
    """Exporte le coffre vers un fichier (sync + copie)."""
    vault.sync()
    vault.save()
    import shutil, json, datetime
    shutil.copy2(vault.path, dest)
    manifest = dest.with_suffix(".manifest.json")
    manifest.write_text(json.dumps({
        "apps": vault._data.get("__apps__", []),
        "version": __version__,
        "exported_at": datetime.datetime.utcnow().isoformat()
    }, indent=2))
    return dest


def import_vault(vault, src: Path) -> bool:
    """Importe un coffre depuis un fichier externe."""
    if not src.exists():
        return False
    vault.path.write_bytes(src.read_bytes())
    return True


def app_list(vault) -> list[dict]:
    """Retourne la liste des apps avec leurs métadonnées."""
    return vault._data.get("__apps__", [])


def app_set_url(vault, slug: str, url: str) -> bool:
    """Modifie le git_url d'une app dans le coffre."""
    apps = vault._data.get("__apps__", [])
    for app in apps:
        if app["slug"] == slug:
            app["git_url"] = url
            vault.save()
            return True
    return False
