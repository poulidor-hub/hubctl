# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Catalogue des outils debug MCP et leur classification de securite.

Chaque outil est defini avec son inputSchema (JSON Schema).
HANDLER_CLASSIFICATION mappe chaque nom d'outil vers une categorie.
CATEGORIES definit le comportement ro/rw par categorie.
"""
from mcp.types import Tool


# ============================================================
# CATEGORY METADATA
# ============================================================

CATEGORIES = {
    "read": {
        "ro": "allow",
        "rw_confirm": False,
        "rw_confirm_double": False,
    },
    "non_destructive": {
        "ro": "suggest",
        "rw_confirm": False,
        "rw_confirm_double": False,
    },
    "destructive_soft": {
        "ro": "suggest",
        "rw_confirm": True,
        "rw_confirm_double": False,
    },
    "destructive_hard": {
        "ro": "suggest",
        "rw_confirm": True,
        "rw_confirm_double": True,
    },
}


# ============================================================
# TOOL DEFINITIONS
# ============================================================

TOOLS = [
    # --- Read: Docker ---
    Tool(
        name="docker_logs",
        description="Recupere les logs d'un conteneur Docker de l'app cible.",
        inputSchema={
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Nom du service (ex: api, db, frontend)"},
                "tail": {"type": "integer", "description": "Nombre de lignes recentes (default 100)"},
            },
            "required": ["service"],
        },
    ),
    Tool(
        name="docker_ps",
        description="Liste les conteneurs Docker de l'app cible avec leur statut.",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="docker_inspect",
        description="Inspecte un conteneur Docker (config complete, healthcheck, reseau).",
        inputSchema={
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Nom du service"},
            },
            "required": ["service"],
        },
    ),
    Tool(
        name="docker_stats",
        description="Statistiques ressources (CPU, memoire, reseau) des conteneurs de l'app.",
        inputSchema={
            "type": "object",
            "properties": {"no_stream": {"type": "boolean", "default": True}},
            "required": [],
        },
    ),
    # --- Read: Database ---
    Tool(
        name="db_query_select",
        description="Execute une requete SELECT en lecture seule sur la base de l'app cible.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Requete SQL SELECT (lecture seule)"},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="db_dump_inspect",
        description="Dump la base de donnees de l'app pour inspection (pg_dump, lecture seule).",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    # --- Read: App status ---
    Tool(
        name="app_status",
        description="Statut global de l'app cible : conteneurs, healthchecks, ports, uptime.",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    # --- Read: Files ---
    Tool(
        name="read_file",
        description="Lit le contenu d'un fichier dans le dossier de l'app cible ou HUB_base/.",
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Chemin relatif (ex: docker-compose.yml, backend/main.py)"},
                "max_lines": {"type": "integer", "description": "Nombre max de lignes (default 200)"},
            },
            "required": ["path"],
        },
    ),
    # --- Non-destructive ---
    Tool(
        name="docker_restart",
        description="Redemarre un service de l'app cible.",
        inputSchema={
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Nom du service (ex: api)"},
            },
            "required": ["service"],
        },
    ),
    Tool(
        name="docker_recreate",
        description="Reconstruit et redemarre un service (docker compose up -d --force-recreate).",
        inputSchema={
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Nom du service"},
            },
            "required": ["service"],
        },
    ),
    # --- Destructive soft ---
    Tool(
        name="docker_down",
        description="Arrete et supprime les conteneurs de l'app (docker compose down).",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="docker_prune",
        description="Supprime les images et conteneurs inutilises de l'app.",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    # --- Destructive hard ---
    Tool(
        name="docker_rm",
        description="Supprime definitivement un conteneur de l'app.",
        inputSchema={
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Nom du service"},
            },
            "required": ["service"],
        },
    ),
    Tool(
        name="db_reset",
        description="Reinitialise la base de donnees de l'app (perte de donnees!).",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="volume_delete",
        description="Supprime un volume Docker de l'app (perte de donnees!).",
        inputSchema={
            "type": "object",
            "properties": {
                "volume": {"type": "string", "description": "Nom du volume"},
            },
            "required": ["volume"],
        },
    ),
]


# ============================================================
# CLASSIFICATION MAP
# ============================================================

HANDLER_CLASSIFICATION = {
    # Read
    "docker_logs": "read",
    "docker_ps": "read",
    "docker_inspect": "read",
    "docker_stats": "read",
    "db_query_select": "read",
    "app_status": "read",
    "db_dump_inspect": "read",
    "read_file": "read",
    # Non-destructive
    "docker_restart": "non_destructive",
    "docker_recreate": "non_destructive",
    # Destructive soft
    "docker_down": "destructive_soft",
    "docker_prune": "destructive_soft",
    # Destructive hard
    "docker_rm": "destructive_hard",
    "db_reset": "destructive_hard",
    "volume_delete": "destructive_hard",
}


def get_classification(tool_name: str) -> str:
    """Retourne la categorie de securite d'un outil."""
    return HANDLER_CLASSIFICATION.get(tool_name, "read")


def is_allowed_in_mode(tool_name: str, mode: str) -> bool:
    """Retourne True si l'outil peut s'executer dans le mode donne."""
    cat = get_classification(tool_name)
    rules = CATEGORIES[cat]
    if mode == "ro":
        return rules["ro"] == "allow"
    return True  # rw: tout est autorise (avec confirm si necessaire)
