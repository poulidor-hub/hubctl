"""Lecture disque des dépendances inter-apps (depends.yaml).

Offline-first : ne dépend ni du hub ni de Docker. Utilisé par le menu workshop
pour calculer les groupes cohérents d'apps à emmener en atelier ensemble.
"""
from __future__ import annotations

from pathlib import Path

import yaml


def load_depends(app_dir: Path) -> list[str]:
    """Retourne la liste des slugs cibles déclarés dans ``app_dir/depends.yaml``.

    Retourne ``[]`` si le fichier est absent, vide, sans clé ``depends_on``,
    ou illisible (YAML cassé). Jamais d'exception — le workshop doit démarrer
    même si un fichier est corrompu.
    """
    f = app_dir / "depends.yaml"
    if not f.exists():
        return []
    try:
        data = yaml.safe_load(f.read_text()) or {}
    except Exception:
        return []
    entries = data.get("depends_on") or []
    return [e["slug"] for e in entries if isinstance(e, dict) and "slug" in e]
