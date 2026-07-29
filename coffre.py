from __future__ import annotations
import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from hubctl_cli import env_manager, hub_state

MAGIC = b"HUBCTLV1"


def _atomic_write_bytes(path: Path, blob: bytes) -> None:
    """Écrit `blob` dans `path` de façon atomique : temp (mode 0600) → fsync →
    `os.replace`. Un crash pendant l'écriture ne corrompt donc jamais le fichier
    cible (le temp orphelin est nettoyé).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(path))
    except Exception:
        try:
            Path(tmp).unlink(missing_ok=True)
        except OSError:
            pass
        raise


# Schémas Git autorisés pour le clonage d'apps depuis le coffre. On refuse
# explicitement file:// et les chemins locaux : un coffre importé d'une source
# externe pourrait sinon forcer le clonage d'un dépôt arbitraire (vecteur
# d'exécution git : hooks, core.fsmonitor, etc.).
_GIT_URL_OK_PREFIXES = ("http://", "https://", "ssh://", "git@", "git://")


def _validate_git_url(url: str) -> str | None:
    """Valide qu'une URL Git utilise un schéma distant autorisé.
    Retourne un message d'erreur si refusée, None si acceptée.
    """
    u = (url or "").strip()
    if not u:
        return "URL Git vide."
    if u.startswith(("file://", "/", "..")) or u.startswith("./"):
        return "Schéma non autorisé : file:// et chemins locaux interdits."
    if not u.startswith(_GIT_URL_OK_PREFIXES):
        return ("Seuls les schémas http(s)://, ssh (git@host:), git:// sont autorisés.")
    return None


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
            apps_meta = []
            for d in sorted(hub_state.APPS_DIR.iterdir()):
                if d.is_dir() and (d / ".env").exists():
                    git_url = None
                    try:
                        r = subprocess.run(["git", "-C", str(d), "remote", "get-url", "origin"],
                                          capture_output=True, text=True, timeout=5)
                        git_url = r.stdout.strip() if r.returncode == 0 else None
                    except Exception:
                        pass
                    version = None
                    ver_file = d / "VERSION"
                    if ver_file.exists():
                        version = ver_file.read_text().strip()
                    apps_meta.append({"slug": d.name, "git_url": git_url, "version": version})
            self._data["__apps__"] = apps_meta
        # Supprimer les entrees d'apps disparues
        stale = [k for k in self._data if k.startswith(("app:", "ws:")) and k not in current_entries]
        for k in stale:
            self._data.pop(k, None)

    def restore(self, *, dry_run: bool = False, force: bool = False, clone_apps: bool = False) -> list[str]:
        assert self._data is not None, "vault verrouillé"
        written = []
        if "hub" in self._data:
            path = hub_state.HUB_DIR / ".env"
            _write_env_if_needed(path, self._data["hub"], dry_run, force)
            written.append(str(path))
        for app in self._data.get("__apps__", []):
            slug = app if isinstance(app, str) else app.get("slug", "")
            (hub_state.APPS_DIR / slug).mkdir(parents=True, exist_ok=True)
        if clone_apps:
            for app in self._data.get("__apps__", []):
                slug = app if isinstance(app, str) else app.get("slug", "")
                git_url = app.get("git_url") if isinstance(app, dict) else None
                if not git_url:
                    continue
                if _validate_git_url(git_url) is not None:
                    # Schéma interdit (file://, chemin local…) : on ignore, on ne clone pas.
                    continue
                try:
                    subprocess.run(
                        ["git", "clone", git_url, str(hub_state.APPS_DIR / slug)],
                        check=True, timeout=120,
                    )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
                    # Clone best-effort : un échec ne doit pas interrompre le restore.
                    pass
        for key, data in self._data.items():
            if key.startswith("app:"):
                slug = key[4:]
                path = hub_state.APPS_DIR / slug / ".env"
                path.parent.mkdir(parents=True, exist_ok=True)
                _write_env_if_needed(path, data, dry_run, force)
                written.append(str(path))
            # ws:* ignorés : ateliers temporaires, recréés à la demande
        for relpath in self._data.get("__files__", {}):
            raw = self.get_file(relpath)
            if raw is None:
                continue
            target = hub_state.PARC_ROOT / relpath
            # Path traversal : un coffre importé (source externe) peut contenir
            # des relpath malveillants (`../../etc/cron.d/evil` ou chemin absolu).
            # On n'écrit jamais en dehors du parc.
            try:
                inside = target.resolve().is_relative_to(hub_state.PARC_ROOT.resolve())
            except (ValueError, OSError):
                inside = False
            if not inside:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if not dry_run:
                target.write_bytes(raw)
                os.chmod(target, 0o600)
            written.append(str(target))
        return written

    def save(self) -> None:
        assert self._fernet is not None and self._salt is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        blob = MAGIC + self._salt + self._fernet.encrypt(json.dumps(self._data).encode("utf-8"))

        # Backup tolérant du coffre précédent : on préserve l'ancien contenu dans
        # un .bak récupérable. Ne doit JAMAIS faire échouer la sauvegarde courante
        # (d'où le except OSError: pass).
        if self.path.exists():
            try:
                prev = self.path.read_bytes()
                if prev != blob:
                    bak = self.path.with_name(self.path.name + ".bak")
                    _atomic_write_bytes(bak, prev)
            except OSError:
                pass

        # Écriture atomique : temp → fsync → os.replace. Un crash (disque plein,
        # SIGINT, panne) pendant l'écriture ne peut ni corrompre ni tronquer le
        # coffre — c'est la garantie critique pour ne pas perdre tous les secrets.
        _atomic_write_bytes(self.path, blob)

    def list_entries(self) -> list[str]:
        assert self._data is not None
        return sorted(self._data.keys())

    def reveal(self, entry: str) -> dict:
        assert self._data is not None
        return dict(self._data.get(entry, {}))
