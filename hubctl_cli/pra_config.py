"""Config de l'emplacement PRA secondaire (kit hors disque serveur).

Le fichier pra_location.json vit dans hubctl_cli/vault/ (gitignoré), aux
côtés du coffre. Le marqueur HUB-PRA-KIT.json vit à la racine de
l'emplacement secondaire et permet de retrouver le kit même si son point
de montage change.
"""
from __future__ import annotations

import json
import os
import secrets as _secrets
from pathlib import Path

from hubctl_cli import hub_state

CONFIG_FILENAME = "pra_location.json"
KIT_MARKER = "HUB-PRA-KIT.json"
VAULT_FILENAME = "parc.vault.enc"
DEFAULT_STALENESS_DAYS = 7


def config_path() -> Path:
    return hub_state.PARC_ROOT / "hubctl_cli" / "vault" / CONFIG_FILENAME


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
