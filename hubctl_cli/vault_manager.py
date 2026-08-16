from __future__ import annotations
import base64
import datetime
import hashlib
import os
import subprocess
from pathlib import Path

from hubctl_cli import __version__, hub_state, env_manager
from hubctl_cli import coffre
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
        # Les ateliers partagent le .env de la prod (APPS/X/.env) avec des overrides.
        # Le .workshop.env ne contient que des configs (slug, ports), pas de secrets.
        return hub_state.APPS_DIR / name / ".workshop.env"
    return None


class VaultManager:
    def __init__(self, vault: Vault):
        self.vault = vault

    def backup_all(self, *, on_log=None) -> dict:
        if self.vault.is_locked or self.vault._data is None:
            return {"env": 0, "files": 0, "db": False}
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
        # Backup DB : utilisateurs, apps, service accounts, settings
        db_ok = _backup_db(self.vault, on_log=on_log)
        self.vault.save()
        # Réplication PRA (couche non-bloquante : un échec n'empêche jamais
        # la sauvegarde de réussir — cf. pra_replicate.replicate, ne lève jamais,
        # double filet try/except par prudence).
        pra = None
        try:
            from hubctl_cli import pra_replicate
            pra = pra_replicate.replicate(on_log=on_log)
        except Exception as e:
            if on_log:
                on_log(f"[pra] réplication ignorée : {e}")
        env_count = sum(1 for k in self.vault.list_entries() if not k.startswith("__"))
        return {"env": env_count, "files": files, "db": db_ok, "pra": pra}

    def export_recovery(self) -> "Path":
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = hub_state.PARC_ROOT / "hubctl_cli" / "recovery" / stamp
        dest.mkdir(parents=True, exist_ok=True)
        os.chmod(dest, 0o700)
        old_umask = os.umask(0o077)
        try:
            for entry in self.vault.list_entries():
                if entry.startswith("__"):
                    continue
                data = self.vault.get_entry(entry)
                if entry == "hub":
                    rel = Path("HUB_base") / ".env"
                elif entry.startswith("app:"):
                    rel = Path("APPS") / entry[4:] / ".env"
                else:
                    # Les entrées legacy "ws:NAME" (schéma atelier historique abandonné
                    # au profit de APPS/X/.workshop.env) ne sont plus restaurées.
                    continue
                out = (dest / rel).resolve()
                if not out.is_relative_to(dest.resolve()):
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text("".join(f"{k}={v}\n" for k, v in data.items()))
                os.chmod(out, 0o600)
            for rel in self.vault.list_files():
                raw = self.vault.get_file(rel)
                if raw is None:
                    continue
                out = (dest / rel).resolve()
                if not out.is_relative_to(dest.resolve()):
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(raw)
                os.chmod(out, 0o600)
        finally:
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
        return targets


def restore_all(vault, *, dry_run: bool = False, force: bool = False) -> list[str]:
    """Restaure tous les .env, secrets, et la DB depuis le coffre."""
    restored = vault.restore(dry_run=dry_run, force=force)
    # Restaurer la DB si présente
    db_sql = vault.get_file("HUB_base/backup/hub_dump.sql")
    if db_sql:
        if not dry_run:
            _restore_db(db_sql)
        restored.append("HUB_base/backup/hub_dump.sql → DB restaurée")
    return restored


def _backup_db(vault, *, on_log=None) -> bool:
    """Sauvegarde les tables critiques dans le coffre."""
    tables = ["users", "applications", "service_accounts", "service_account_app_access",
              "app_settings", "user_app_roles", "schema_migrations"]
    cmd = ["docker", "exec", "hub-db", "pg_dump", "-U", "hubadmin", "-d", "hub_auth",
           "--data-only", "--inserts"]
    for t in tables:
        cmd.extend(["-t", t])
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        if r.returncode == 0 and r.stdout:
            vault.set_file("HUB_base/backup/hub_dump.sql", r.stdout)
            if on_log:
                on_log(f"[vault] backup DB ({len(r.stdout)} bytes)")
            return True
    except Exception as e:
        if on_log:
            on_log(f"[vault] backup DB échoué: {e}")
    return False


_RESTORE_TABLES = ["user_app_roles", "service_account_app_access", "service_accounts",
                   "applications", "app_settings", "users"]


def _pg_dump_tables(tables, *, on_log=None) -> bytes | None:
    """Snapshot live des tables (filet de sécurité avant truncate). None si injoignable."""
    cmd = ["docker", "exec", "hub-db", "pg_dump", "-U", "hubadmin", "-d", "hub_auth",
           "--data-only", "--inserts"]
    for t in tables:
        cmd.extend(["-t", t])
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        if r.returncode == 0 and r.stdout:
            return r.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        if on_log:
            on_log(f"[vault] snapshot live DB échoué: {e}")
    return None


def _restore_db(sql: bytes, *, on_log=None) -> bool:
    """Rejoue le dump SQL dans la base.

    Safety net : on snapshot la base live AVANT tout truncate, puis on la
    restaurera si le replay échoue — sinon un dump partiel/corrompu laissait la
    base vidée (TRUNCATE CASCADE) sans recours, perdant users/apps/settings.
    """
    snapshot = _pg_dump_tables(_RESTORE_TABLES, on_log=on_log)
    if snapshot is None:
        if on_log:
            on_log("[vault] restore DB annulé : snapshot live impossible (hub-db injoignable ?)")
        return False
    # Truncate des tables cibles.
    for t in _RESTORE_TABLES:
        try:
            subprocess.run(
                ["docker", "exec", "hub-db", "psql", "-U", "hubadmin", "-d", "hub_auth",
                 "-c", f"TRUNCATE {t} CASCADE"],
                capture_output=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
    # Replay du dump du coffre.
    try:
        r = subprocess.run(
            ["docker", "exec", "-i", "hub-db", "psql", "-U", "hubadmin", "-d", "hub_auth"],
            input=sql, capture_output=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        if on_log:
            on_log(f"[vault] restore DB échoué (psql): {e}")
        _rollback_db(snapshot, on_log=on_log)
        return False
    if r.returncode != 0:
        if on_log:
            on_log(f"[vault] restore DB échoué (code {r.returncode}): "
                   f"{r.stderr.decode(errors='replace')[:200]}")
        _rollback_db(snapshot, on_log=on_log)
        return False
    return True


def _rollback_db(snapshot: bytes, *, on_log=None) -> None:
    """Restaure le snapshot live pris avant le truncate (best-effort)."""
    try:
        r = subprocess.run(
            ["docker", "exec", "-i", "hub-db", "psql", "-U", "hubadmin", "-d", "hub_auth"],
            input=snapshot, capture_output=True, timeout=30,
        )
        if r.returncode == 0 and on_log:
            on_log("[vault] rollback DB effectué (base restaurée à l'état d'avant truncate)")
        elif on_log:
            on_log("[vault] rollback DB échoué ⚠ — base laissée à l'état post-truncate")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        if on_log:
            on_log("[vault] rollback DB échoué ⚠ — base laissée à l'état post-truncate")


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
        "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }, indent=2))
    return dest


def import_vault(vault, src: Path) -> bool:
    """Importe un coffre depuis un fichier externe.

    Valide le magic HUBCTLV1 (refuse tout binaire arbitraire — sinon un coffre
    malveillant pouvait activer le path traversal de restore() et le git clone)
    et préserve l'ancien coffre dans un .bak récupérable avant écrasement.
    """
    if not src.exists():
        return False
    blob = src.read_bytes()
    if not blob.startswith(coffre.MAGIC):
        return False
    # Backup tolérant du coffre courant avant écrasement.
    if vault.path.exists():
        try:
            prev = vault.path.read_bytes()
            if prev != blob:
                bak = vault.path.with_name(vault.path.name + ".bak")
                coffre._atomic_write_bytes(bak, prev)
        except OSError:
            pass
    coffre._atomic_write_bytes(vault.path, blob)
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
