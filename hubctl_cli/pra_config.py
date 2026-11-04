# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Config de l'emplacement PRA secondaire (kit hors disque serveur).

Le fichier pra_location.json vit dans hubctl_cli/vault/ (gitignoré), aux
côtés du coffre. Le marqueur HUB-PRA-KIT.json vit à la racine de
l'emplacement secondaire et permet de retrouver le kit même si son point
de montage change.
"""
from __future__ import annotations

import json
import os
import re
import secrets as _secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

from hubctl_cli import hub_state

CONFIG_FILENAME = "pra_location.json"
KIT_MARKER = "HUB-PRA-KIT.json"
VAULT_FILENAME = "parc.vault.enc"
DEFAULT_STALENESS_DAYS = 7


def config_path() -> Path:
    return hub_state.vault_dir() / CONFIG_FILENAME


def load_config() -> dict | None:
    p = config_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def save_config(cfg: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name("." + CONFIG_FILENAME + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def new_kit_id() -> str:
    return _secrets.token_hex(8)


# ── Détection disque / validation « ailleurs » ──
# Sources injectables pour les tests.
_MOUNTINFO = "/proc/self/mountinfo"
_SYS_BLOCK = "/sys/dev/block"

# Filesystems réseau : toujours considérés comme « ailleurs » (machine distincte).
NETWORK_FSTYPES = {"nfs", "nfs4", "cifs", "smbfs", "sshfs", "fuse.sshfs",
                   "fuse.rclone", "fuse.rclonefs", "9p", "afp", "glusterfs"}

_DISK_RE = re.compile(r"^(sd[a-z]+|vd[a-z]+|nvme\d+n\d+|mmcblk\d+|hd[a-z]+|xvd[a-z]+)$")


def _st_dev(path: Path) -> int:
    return os.stat(path).st_dev


def _mount_info_for(path: Path) -> tuple[str, str] | None:
    """(major:minor, fstype) du montage le plus spécifique contenant `path`."""
    resolved = path.resolve()
    best: tuple[int, str, str] | None = None  # (len(mountpoint), majmin, fstype)
    try:
        with open(_MOUNTINFO) as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if len(fields) < 7 or "-" not in fields:
            continue
        mountpoint = fields[4]
        try:
            mp = Path(mountpoint)
        except ValueError:
            continue
        if resolved != mp and mp not in resolved.parents:
            continue
        sep = fields.index("-")
        fstype = fields[sep + 1] if len(fields) > sep + 1 else ""
        if best is None or len(mountpoint) > best[0]:
            best = (len(mountpoint), fields[2], fstype)
    if best is None:
        return None
    return best[1], best[2]


def _physical_disk(majmin: str) -> str | None:
    """Identifiant du disque physique ('sda', 'nvme0n1'…) ou None (dm/loop)."""
    syslink = Path(_SYS_BLOCK) / majmin
    if not syslink.exists():
        return None
    real = Path(os.path.realpath(syslink))
    for part in reversed(real.parts):
        if _DISK_RE.match(part):
            return part
    return str(real)


def same_physical_disk(path_a: Path, path_b: Path) -> bool:
    """True si les deux chemins vivent sur le même disque physique (ex: deux
    partitions d'un même disque). False si montage réseau, info manquante ou
    disques distincts — conservateur par défaut."""
    ia = _mount_info_for(path_a)
    ib = _mount_info_for(path_b)
    if ia is None or ib is None:
        return False
    (ma, fa), (mb, fb) = ia, ib
    if fa in NETWORK_FSTYPES or fb in NETWORK_FSTYPES:
        return False
    if ma == mb:  # même filesystem → même disque
        return True
    da, db = _physical_disk(ma), _physical_disk(mb)
    return da is not None and db is not None and da == db


def validate_location(path: Path) -> tuple[str, str]:
    """Valide un emplacement PRA. Retourne (statut, message) :
    'ok' | 'warn' (même disque physique, confirmable) | 'error' (refusé)."""
    if not path.exists() or not path.is_dir():
        return "error", f"{path} n'existe pas ou n'est pas un dossier"
    try:
        if _st_dev(path) == _st_dev(hub_state.PARC_ROOT):
            return "error", ("Même filesystem que le parc — une copie sur le disque "
                             "du serveur n'apporte aucune protection.")
    except OSError as e:
        return "error", f"Stat impossible : {e}"
    if same_physical_disk(path, hub_state.PARC_ROOT):
        return "warn", ("Même disque physique que le parc — un crash de ce disque "
                        "détruirait les deux copies (déconseillé).")
    return "ok", ""


# ── Scan des emplacements candidats ──

def _scan_roots() -> list[Path]:
    """Sous-dossiers de /media, /mnt, /run/media (points de montage usuels)."""
    roots: list[Path] = []
    for base in ("/media", "/mnt", "/run/media"):
        b = Path(base)
        if not b.is_dir():
            continue
        for child in sorted(b.iterdir()):
            if child.is_dir() and not child.is_symlink():
                roots.append(child)
    return roots


def scan_candidate_locations() -> list[dict]:
    """Candidats : sous-dossiers de /media, /mnt, /run/media + montages réseau
    de /proc/mounts. Chaque entrée : path, type, total_bytes, free_bytes, status."""
    cands: list[Path] = list(_scan_roots())
    try:
        with open("/proc/mounts") as f:
            for line in f.read().splitlines():
                fields = line.split()
                if len(fields) < 3 or fields[2] not in NETWORK_FSTYPES:
                    continue
                cands.append(Path(fields[1]))
    except OSError:
        pass
    out: list[dict] = []
    for d in cands:
        try:
            usage = shutil.disk_usage(d)
        except OSError:
            continue
        status, _msg = validate_location(d)
        out.append({
            "path": str(d),
            "type": (_mount_info_for(d) or ("", "?"))[1],
            "total_bytes": usage.total,
            "free_bytes": usage.free,
            "status": status,
        })
    return out


# ── Marqueur kit ──

def read_kit_marker(location: Path) -> dict | None:
    marker = location / KIT_MARKER
    if not marker.exists():
        return None
    try:
        return json.loads(marker.read_text())
    except (OSError, ValueError):
        return None


def find_kit_location(expected_kit_id: str, configured_path: str | None = None) -> Path | None:
    """Retrouve l'emplacement du kit par son marqueur : chemin configuré
    d'abord, puis scan des points de montage usuels."""
    candidates: list[Path] = []
    if configured_path:
        candidates.append(Path(configured_path))
    candidates.extend(_scan_roots())
    for d in candidates:
        marker = read_kit_marker(d)
        if marker and marker.get("kit_id") == expected_kit_id:
            return d
    return None


def discover_kits() -> list[Path]:
    """Emplacements contenant un marqueur HUB-PRA-KIT.json ET un coffre.
    Utilisé sur machine neuve (aucune config locale)."""
    kits: list[Path] = []
    for d in _scan_roots():
        if read_kit_marker(d) and (d / VAULT_FILENAME).exists():
            kits.append(d)
    return kits


# ── Badge staleness + phase setup ──

def staleness_badge() -> str | None:
    """Texte du badge dashboard si l'emplacement PRA est absent/périmé."""
    cfg = load_config()
    if not cfg or cfg.get("declined"):
        return None
    loc = find_kit_location(cfg.get("kit_id", ""), cfg.get("path"))
    if loc is None:
        return "⚠ PRA : emplacement secondaire introuvable"
    last = cfg.get("last_sync_at")
    if not last:
        return "⚠ PRA : jamais synchronisé"
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return "⚠ PRA : jamais synchronisé"
    days = (datetime.now(timezone.utc) - last_dt).days
    threshold = int(cfg.get("staleness_days", DEFAULT_STALENESS_DAYS))
    if days >= threshold:
        return f"⚠ PRA : dernière synchro il y a {days} j"
    return None


def should_offer_setup() -> bool:
    """True si la phase « emplacement PRA » doit être proposée au démarrage :
    aucune config, ou config sans chemin ni refus définitif."""
    cfg = load_config()
    if cfg is None:
        return True
    return not cfg.get("path") and not cfg.get("declined")
