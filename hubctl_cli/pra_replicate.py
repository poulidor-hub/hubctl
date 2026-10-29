"""Réplication du kit PRA (coffre + dernier snapshot) vers l'emplacement secondaire.

Couche AU-DESSUS du coffre existant : le disque reste la source de vérité,
la réplication ne fait que copier. Un échec de réplication ne fait JAMAIS
échouer la sauvegarde (statut != 'ok', pas d'exception).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from hubctl_cli import coffre, hub_state, pra_config

SNAPSHOT_GLOB = "snapshot_*.tar.zst.enc"

KIT_README = """KIT PRA — POULIDOR
==================

Ce kit contient le coffre chiffré du parc et le dernier snapshot du HUB
(tous deux chiffrés — aucune donnée en clair).

Restauration sur un serveur neuf :
1. git clone <repo POULIDOR> && cd POULIDOR
2. pip install -r hubctl_cli/requirements.txt
3. python -m hubctl_cli pra-restore

L'assistant détecte ce kit automatiquement (garder HUB-PRA-KIT.json à côté
du coffre).
"""


def vault_path() -> Path:
    return hub_state.vault_path()


def snapshots_dir() -> Path:
    return hub_state.HUB_DIR / "storage" / "snapshots"


def _db_latest_snapshot_id() -> str | None:
    """snapshot_id du dernier snapshot success en base.

    La DB fait foi : le mtime des fichiers snapshot du hub peut être
    trompeur (copies, montages, restaurations — observé en réel, le hub
    préserve un mtime ancien à l'écriture). None si DB injoignable.
    """
    try:
        r = subprocess.run(
            ["docker", "compose", "exec", "-T", "db", "psql", "-U", "hubadmin",
             "-d", "hub_auth", "-t", "-A", "-c",
             "SELECT snapshot_id FROM hub_snapshots WHERE status = 'success' "
             "ORDER BY created_at DESC LIMIT 1"],
            cwd=str(hub_state.HUB_DIR), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def latest_snapshot() -> Path | None:
    d = snapshots_dir()
    if not d.is_dir():
        return None
    snap_id = _db_latest_snapshot_id()
    if snap_id:
        cand = d / f"snapshot_{snap_id}.tar.zst.enc"
        if cand.exists():
            return cand
        # Ligne en base mais fichier absent (purge hub locale) → mtime.
    snaps = sorted(d.glob(SNAPSHOT_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
    return snaps[0] if snaps else None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_stream_atomic(src: Path, dst: Path) -> None:
    """Copie en streaming (fichiers Go possibles) : tmp → fsync → os.replace.
    Un support arraché en cours de copie ne corrompt jamais l'ancienne version."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name("." + dst.name + ".tmp")
    try:
        with open(src, "rb") as fs, open(tmp, "wb") as fd:
            shutil.copyfileobj(fs, fd, length=1024 * 1024)
            fd.flush()
            os.fsync(fd.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, dst)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _write_marker(loc: Path, marker: dict) -> None:
    p = loc / pra_config.KIT_MARKER
    tmp = p.with_name("." + p.name + ".tmp")
    tmp.write_text(json.dumps(marker, indent=2))
    os.replace(tmp, p)
    readme = loc / "PRA-README.txt"
    tmp2 = readme.with_name("." + readme.name + ".tmp")
    tmp2.write_text(KIT_README)
    os.replace(tmp2, readme)


def replicate(*, on_log: Callable[[str], None] | None = None) -> dict:
    """Réplique coffre + dernier snapshot. Ne lève jamais."""
    try:
        return _replicate(on_log=on_log)
    except Exception as e:  # filet final — garantie « jamais d'exception »
        return {"status": "error", "copied": [], "message": str(e)}


def _replicate(*, on_log=None) -> dict:
    cfg = pra_config.load_config()
    if not cfg or not cfg.get("path") or cfg.get("declined"):
        return {"status": "none", "copied": [], "message": "Aucun emplacement PRA configuré"}

    loc = pra_config.find_kit_location(cfg.get("kit_id", ""), cfg.get("path"))
    if loc is None:
        if on_log:
            on_log("[pra] Emplacement PRA introuvable — réplication ignorée")
        return {"status": "missing", "copied": [],
                "message": "Emplacement PRA introuvable (brancher le support ?)"}

    # Coffre : diff par checksum — le .bak suit le principal.
    vp = vault_path()
    if not vp.exists():
        return {"status": "error", "copied": [], "message": f"Coffre introuvable : {vp}"}
    copied: list[str] = []
    digest = _sha256_file(vp)
    if digest != cfg.get("last_vault_sha256"):
        _copy_stream_atomic(vp, loc / pra_config.VAULT_FILENAME)
        bak = vp.with_name(pra_config.VAULT_FILENAME + ".bak")
        if bak.exists():
            _copy_stream_atomic(bak, loc / (pra_config.VAULT_FILENAME + ".bak"))
        # La clé suit le coffre : sans elle, le kit est indéchiffrable
        # (passphrase auto-générée inconnue de l'utilisateur).
        key = vp.with_suffix(".key")
        if key.exists():
            _copy_stream_atomic(key, loc / coffre.VAULT_KEY_FILENAME)
            copied.append(coffre.VAULT_KEY_FILENAME)
        copied.append(pra_config.VAULT_FILENAME)
        cfg["last_vault_sha256"] = digest

    # Snapshot : diff par nom + purge des anciens sur le kit.
    # Le HUB écrit ses snapshots en copie NON atomique : différencier par le
    # nom seul peut copier un fichier en cours d'écriture → kit corrompu et
    # last_snapshot_id posé (jamais re-copié). D'où les 2 gardes ci-dessous.
    snap = latest_snapshot()
    if snap is not None and snap.name != cfg.get("last_snapshot_id"):
        if time.time() - snap.stat().st_mtime < 120:
            # Frais → écriture possiblement en cours : différer SANS poser
            # last_snapshot_id (le cycle reste « ok », coffre déjà répliqué).
            if on_log:
                on_log(f"[pra] snapshot {snap.name} très récent "
                       "(écriture en cours ?) — réplication différée au prochain cycle")
        else:
            dst = loc / snap.name
            _copy_stream_atomic(snap, dst)
            if snap.stat().st_size != dst.stat().st_size:
                # Copie partielle (source modifiée pendant la copie) : on
                # supprime le partiel et on NE sauve pas la config → le
                # prochain cycle retentera la copie.
                dst.unlink(missing_ok=True)
                return {"status": "error", "copied": [],
                        "message": "Snapshot copié incomplet (taille divergente) — réessayer"}
            for old in loc.glob(SNAPSHOT_GLOB):
                if old.name != snap.name:
                    try:
                        old.unlink()
                    except OSError:
                        pass
            copied.append(snap.name)
            cfg["last_snapshot_id"] = snap.name

    # Marqueur + README toujours rafraîchis (copie pas chère).
    _write_marker(loc, {
        "kit_id": cfg.get("kit_id"),
        "label": cfg.get("label"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })

    # Mise à jour de la config SEULEMENT en cas de succès complet.
    cfg["last_sync_at"] = datetime.now(timezone.utc).isoformat()
    pra_config.save_config(cfg)
    if on_log:
        on_log(f"[pra] Kit PRA synchronisé ({', '.join(copied) if copied else 'rien à copier'})")
    return {"status": "ok", "copied": copied, "message": ""}


def configure_location(path: Path, label: str = "", *, on_log=None) -> tuple[bool, str]:
    """Configure un nouvel emplacement PRA : validation, marqueur, config,
    première réplication immédiate."""
    status, msg = pra_config.validate_location(path)
    if status == "error":
        return False, msg
    kit_id = pra_config.new_kit_id()
    cfg = pra_config.load_config() or {}
    cfg.update({
        "path": str(path),
        "label": label or str(path),
        "kit_id": kit_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "staleness_days": pra_config.DEFAULT_STALENESS_DAYS,
        "declined": False,
    })
    pra_config.save_config(cfg)
    _write_marker(path, {"kit_id": kit_id, "label": cfg["label"],
                         "updated_at": cfg["created_at"]})
    result = replicate(on_log=on_log)
    ok = result["status"] == "ok"
    return ok, result.get("message") or ("Kit PRA initialisé" if ok
                                         else "Réplication initiale échouée — voir logs")


def remove_location() -> None:
    """Retire la config d'emplacement (le contenu du kit reste en place)."""
    cfg = pra_config.load_config()
    if cfg:
        cfg.pop("path", None)
        cfg.pop("kit_id", None)
        pra_config.save_config(cfg)
