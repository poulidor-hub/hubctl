"""Lecture disque des dépendances inter-apps (depends.yaml).

Offline-first : ne dépend ni du hub ni de Docker. Utilisé par le menu workshop
pour calculer les groupes cohérents d'apps à emmener en atelier ensemble.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from . import hub_state


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


def build_graph() -> dict[str, list[str]]:
    """Construit le graphe dirigé des dépendances en lisant APPS/ + APP_Workshop/.

    Seules les apps avec un ``docker-compose.yml`` sont considérées (les dossiers
    incomplets sont ignorés). Une app sans ``depends.yaml`` est présente dans le
    graphe avec une liste vide (noeud autonome).
    """
    graph: dict[str, list[str]] = {}
    for apps_dir in [hub_state.APPS_DIR, hub_state.WORKSHOP_DIR]:
        if not apps_dir.exists():
            continue
        for app_dir in sorted(apps_dir.iterdir()):
            if not app_dir.is_dir() or app_dir.name.startswith("."):
                continue
            if not (app_dir / "docker-compose.yml").exists():
                continue
            graph[app_dir.name] = load_depends(app_dir)
    return graph
