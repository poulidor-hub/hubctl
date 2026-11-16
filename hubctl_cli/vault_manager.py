# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

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
            return {"env": 0, "files": 0, "ca": 0, "db": False}
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
        # Backup CA HTTPS (certs/ca) : la CA est importée dans les navigateurs
        # du parc — la perdre ferait régénérer une CA au PRA et toute la
        # flotte repartirait en SEC_ERROR_BAD_SIGNATURE.
        ca_files = _backup_certs_ca(self.vault, on_log=on_log)
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
        return {"env": env_count, "files": files, "ca": ca_files, "db": db_ok, "pra": pra}

    def export_recovery(self) -> "Path":
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = hub_state.recovery_dir() / stamp
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
    """Restaure tous les .env, secrets, la CA HTTPS et la DB depuis le coffre."""
    restored = vault.restore(dry_run=dry_run, force=force)
    # Restaurer la DB si présente
    db_sql = vault.get_file("HUB_base/backup/hub_dump.sql")
    if db_sql:
        if not dry_run:
            if _restore_db(db_sql):
                restored.append("HUB_base/backup/hub_dump.sql → DB restaurée")
            else:
                restored.append("HUB_base/backup/hub_dump.sql → ⚠ DB NON restaurée "
                                "(replay en échec — voir logs, snapshot live rechargé)")
        else:
            restored.append("HUB_base/backup/hub_dump.sql → DB (dry-run : rien rejoué)")
    # CA HTTPS : écrite par le chemin commun vault.restore() (les modes
    # ca.crt 0644 / ca.key 0600 / dossier 0700 sont posés par
    # coffre._apply_restore_modes) ; on la met en évidence — retrouver la CA
    # d'origine est ce qui préserve la confiance des navigateurs post-PRA.
    if vault.get_file("HUB_base/certs/ca/ca.key") is not None:
        restored.append("HUB_base/certs/ca → CA HTTPS restaurée")
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


def _backup_certs_ca(vault, *, on_log=None) -> int:
    """Sauvegarde la CA HTTPS locale (HUB_base/certs/ca : ca.crt + ca.key).

    Sans elle, un PRA sur disque perdu régénérerait une CA et les navigateurs
    du parc rejetteraient le HTTPS (SEC_ERROR_BAD_SIGNATURE). Dégradé
    silencieux si absente (HTTPS non déployé), comme secrets/ inexistant.
    """
    ca_dir = hub_state.HUB_DIR / "certs" / "ca"
    count = 0
    if ca_dir.exists():
        for f in sorted(ca_dir.rglob("*")):
            if not f.is_file() or f.is_symlink():
                continue
            rel = f.relative_to(hub_state.PARC_ROOT).as_posix()
            vault.set_file(rel, f.read_bytes())
            count += 1
            if on_log:
                on_log(f"[vault] backup {rel}")
    return count


# NB : la liste des tables restaurées n'est plus codée en dur — elle est
# parsée du dump lui-même (_dump_tables), de sorte que truncate/snapshot/
# vérification couvrent exactement ce que _backup_db a capturé (issue
# Hubctl#1 : schema_migrations capturée mais jamais tronquée → dup keys en
# replay).


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


def _dump_tables(sql: bytes) -> list[str]:
    """Tables d'un dump pg_dump --inserts, dans l'ordre du dump (marqueurs
    « -- Data for Name: »). Vide si le contenu n'est pas un dump reconnaissable."""
    tables: list[str] = []
    for line in sql.decode(errors="replace").splitlines():
        if line.startswith("-- Data for Name: "):
            name = line.split(":", 1)[1].split(";", 1)[0].strip()
            if name and name not in tables:
                tables.append(name)
    return tables


def _dump_row_counts(sql: bytes) -> dict[str, int]:
    """Lignes attendues par table (en --inserts, un INSERT par enregistrement),
    lues section par section du dump."""
    counts: dict[str, int] = {}
    current: str | None = None
    for line in sql.decode(errors="replace").splitlines():
        if line.startswith("-- Data for Name: "):
            current = line.split(":", 1)[1].split(";", 1)[0].strip()
            counts.setdefault(current, 0)
        elif current and line.startswith("INSERT INTO"):
            counts[current] += 1
    return counts


def _verify_row_counts(expected: dict[str, int], *, on_log=None) -> bool:
    """Compare les counts post-restore aux counts attendus du dump.

    Sans ce contrôle, un psql sorti 0 malgré des INSERTs en échec déclarait
    « DB restaurée » avec des tables vides (issue Hubctl#1)."""
    if not expected:
        return True
    select = " UNION ALL ".join(f"SELECT '{t}', count(*) FROM {t}" for t in expected)
    try:
        r = subprocess.run(
            ["docker", "exec", "hub-db", "psql", "-U", "hubadmin", "-d", "hub_auth",
             "-v", "ON_ERROR_STOP=1", "-t", "-A", "-F", "|", "-c", select],
            capture_output=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        if on_log:
            on_log(f"[vault] vérification post-restore impossible: {e}")
        return False
    if r.returncode != 0:
        if on_log:
            on_log(f"[vault] vérification post-restore échouée: "
                   f"{r.stderr.decode(errors='replace')[:200]}")
        return False
    actual: dict[str, int] = {}
    for line in r.stdout.decode(errors="replace").splitlines():
        t, sep, n = line.partition("|")
        if sep:
            try:
                actual[t.strip()] = int(n.strip())
            except ValueError:
                pass
    mismatch = {t: (exp, actual.get(t))
                for t, exp in expected.items() if actual.get(t) != exp}
    if mismatch:
        if on_log:
            detail = ", ".join(f"{t}: attendu {e}, obtenu {a}"
                               for t, (e, a) in mismatch.items())
            on_log(f"[vault] restore DB INCOMPLET (counts post-restore ≠ dump) — {detail}")
        return False
    return True


def _restore_db(sql: bytes, *, on_log=None) -> bool:
    """Rejoue le dump SQL dans la base, avec détection d'échec explicite.

    Safety net : on snapshot la base live AVANT tout truncate, puis on la
    restaurera si le replay échoue — sinon un dump partiel/corrompu laissait la
    base vidée (TRUNCATE CASCADE) sans recours, perdant users/apps/settings.

    Détections ajoutées (issue Hubctl#1 : restore déclaré « succès » avec des
    apps non rejouées) :
    - psql -v ON_ERROR_STOP=1 : toute erreur SQL (conflit FK, dup key) donne un
      code de sortie non nul au lieu d'un replay partiel silencieux ;
    - TRUNCATE vérifié : un truncate raté garantit un replay en échec (dup
      keys), on abandonne AVANT de vider davantage ;
    - comptage de lignes post-restore comparé aux sections du dump.
    """
    tables = _dump_tables(sql)
    if not tables:
        if on_log:
            on_log("[vault] restore DB annulé : contenu sans section DATA "
                   "(-- Data for Name:) — pas un dump pg_dump --inserts reconnaissable")
        return False
    snapshot = _pg_dump_tables(tables, on_log=on_log)
    if snapshot is None:
        if on_log:
            on_log("[vault] restore DB annulé : snapshot live impossible (hub-db injoignable ?)")
        return False
    expected = _dump_row_counts(sql)
    # Truncate des tables cibles — vérifié : un truncate raté rend le replay
    # dangereux (dup keys sur tables non vidées), on arrête tout.
    for t in tables:
        try:
            r = subprocess.run(
                ["docker", "exec", "hub-db", "psql", "-U", "hubadmin", "-d", "hub_auth",
                 "-v", "ON_ERROR_STOP=1", "-c", f"TRUNCATE {t} CASCADE"],
                capture_output=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            if on_log:
                on_log(f"[vault] restore DB annulé : TRUNCATE {t} impossible: {e}")
            _rollback_db(snapshot, on_log=on_log)
            return False
        if r.returncode != 0:
            if on_log:
                on_log(f"[vault] restore DB annulé : TRUNCATE {t} échoué "
                       f"(code {r.returncode}): {r.stderr.decode(errors='replace')[:200]}")
            _rollback_db(snapshot, on_log=on_log)
            return False
    # Replay du dump du coffre — ON_ERROR_STOP=1 : toute erreur SQL interrompt
    # psql avec un code non nul (sans lui, les conflits FK passaient inaperçus).
    try:
        r = subprocess.run(
            ["docker", "exec", "-i", "hub-db", "psql", "-U", "hubadmin", "-d", "hub_auth",
             "-v", "ON_ERROR_STOP=1"],
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
    # Post-vérification : la base doit contenir exactement les lignes du dump.
    if not _verify_row_counts(expected, on_log=on_log):
        _rollback_db(snapshot, on_log=on_log)
        return False
    return True


def _rollback_db(snapshot: bytes, *, on_log=None) -> None:
    """Restaure le snapshot live pris avant le truncate (best-effort)."""
    try:
        r = subprocess.run(
            ["docker", "exec", "-i", "hub-db", "psql", "-U", "hubadmin", "-d", "hub_auth",
             "-v", "ON_ERROR_STOP=1"],
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
