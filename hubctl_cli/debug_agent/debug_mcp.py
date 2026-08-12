"""Serveur MCP debug dedie pour POULIDOR.

Execute des commandes docker/psql/file directement sur l'hote via subprocess.
Le guard ro/rw est integre dans chaque handler : en mode lecture, les outils
modifiants retournent une suggestion au lieu d'executer.

Point d'entree: python -m hubctl_cli.debug_agent.debug_mcp --app <APP> --mode <ro|rw>
"""
import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from hubctl_cli.debug_agent.tools_catalog import TOOLS


def _park_root() -> Path:
    root = os.environ.get("POULIDOR_ROOT")
    if root:
        return Path(root)
    # Fallback : utiliser hub_state.PARC_ROOT (robuste face aux symlinks)
    from hubctl_cli import hub_state
    return hub_state.PARC_ROOT


# ── Garde-fou SQL pour db_query_select ──
# `psql -c` exécute TOUS les statements séparés par `;` — un simple `SELECT 1;
# COPY ... TO PROGRAM 'cmd'` lancerait un shell sur l'hôte DB. On bloque donc :
#  - le chainage de statements (tout `;` sauf le final optionnel),
#  - les mots-clés modifiants ET les vecteurs RCE (COPY/PROGRAM/DO/CALL/...).
_READONLY_START_RE = re.compile(r"^(SELECT|WITH)\b", re.IGNORECASE)
_READONLY_DENY_RE = re.compile(
    r"\b("
    r"INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|"
    r"COPY|PROGRAM|DO|CALL|VACUUM|GRANT|REVOKE|MERGE|"
    r"ANALYZE|CLUSTER|REINDEX|ATTACH|DETACH"
    r")\b",
    re.IGNORECASE,
)


def _validate_readonly_query(query: str) -> str | None:
    """Valide qu'une requête est un SELECT/WITH en lecture seule, sûr pour psql -c.

    Retourne un message d'erreur si la requête est refusée, None si elle est acceptée.
    Bloque le chainage de statements (`;`) et les mots-clés dangereux afin qu'un
    payload du type `SELECT 1; COPY ... TO PROGRAM 'cmd'` ne puisse exécuter de
    shell ni de code arbitraire côté Postgres.
    """
    q = (query or "").strip()
    if not q:
        return "Requête vide."
    # Un seul statement : on tolère un `;` final, tout autre `;` = chainage → rejet.
    if q.endswith(";"):
        q = q[:-1].rstrip()
    if ";" in q:
        return ("Requêtes multi-statements (;) interdites — "
                "une seule instruction SELECT/WITH par appel.")
    if not _READONLY_START_RE.match(q):
        return "Seules les requêtes SELECT/WITH sont autorisées."
    if _READONLY_DENY_RE.search(q):
        return ("Requête modifiante ou potentiellement dangereuse refusée. "
                "Utilisez le mode --rw dédié pour ce type d'opération.")
    return None



def _app_dir(app_slug: str) -> Path:
    root = _park_root()
    # Les apps vivent dans APPS/ (ateliers inclus — tournent via docker compose -p X-ws
    # depuis APPS/X avec override .workshop.env).
    candidate = root / "APPS" / app_slug
    if candidate.exists():
        return candidate
    raise FileNotFoundError(
        f"App '{app_slug}' introuvable dans APPS/"
    )


def _compose_path(app_slug: str) -> Path:
    return _app_dir(app_slug) / "docker-compose.yml"


def _run_subprocess(cmd: list[str], timeout: int = 30) -> dict:
    """Execute une commande et retourne un dict stdout/stderr/returncode."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"error": f"Timeout apres {timeout}s", "cmd": " ".join(cmd)}
    except FileNotFoundError:
        return {"error": f"Commande introuvable: {cmd[0]}"}


# ============================================================
# READ HANDLERS
# ============================================================

async def _handle_docker_logs(args, app_slug, mode):
    service = args.get("service", "")
    tail = args.get("tail", 100)
    compose = _compose_path(app_slug)
    result = _run_subprocess([
        "docker", "compose", "-f", str(compose),
        "logs", "--tail", str(tail), "--no-color", service,
    ])
    return result


async def _handle_docker_ps(args, app_slug, mode):
    compose = _compose_path(app_slug)
    result = _run_subprocess([
        "docker", "compose", "-f", str(compose),
        "ps", "--format", "json",
    ])
    return result


async def _handle_docker_inspect(args, app_slug, mode):
    service = args.get("service", "")
    compose = _compose_path(app_slug)
    result = _run_subprocess([
        "docker", "compose", "-f", str(compose),
        "inspect", service,
    ])
    return result


async def _handle_docker_stats(args, app_slug, mode):
    compose = _compose_path(app_slug)
    no_stream = "--no-stream" if args.get("no_stream", True) else ""
    cmd = ["docker", "compose", "-f", str(compose), "stats"]
    if no_stream:
        cmd.append("--no-stream")
    result = _run_subprocess(cmd)
    return result


async def _handle_db_query_select(args, app_slug, mode):
    query = args.get("query", "").strip()
    err = _validate_readonly_query(query)
    if err:
        return {"error": err}
    compose = _compose_path(app_slug)
    result = _run_subprocess([
        "docker", "compose", "-f", str(compose),
        "exec", "-T", "db",
        "psql", "-U", "app", "-c", query,
    ], timeout=60)
    return result


async def _handle_app_status(args, app_slug, mode):
    compose = _compose_path(app_slug)
    ps = _run_subprocess([
        "docker", "compose", "-f", str(compose), "ps",
    ])
    return {"app": app_slug, "compose": str(compose), "ps": ps.get("stdout", "")}


async def _handle_db_dump_inspect(args, app_slug, mode):
    compose = _compose_path(app_slug)
    cmd = [
        "docker", "compose", "-f", str(compose),
        "exec", "-T", "db", "pg_dump", "-U", "app",
    ]
    return _run_subprocess(cmd, timeout=120)


async def _handle_read_file(args, app_slug, mode):
    rel_path = args.get("path", "")
    max_lines = args.get("max_lines", 200)
    root = _park_root()
    app_dir = _app_dir(app_slug)

    # Resoudre et verifier que le chemin reste dans les limites autorisees
    allowed_bases = [app_dir.resolve(), (root / "HUB_base").resolve()]
    target = (app_dir / rel_path).resolve()

    # Verifier que target est sous un des allowed_bases
    if not any(
        str(target) == str(base) or (str(target) + os.sep).startswith(str(base) + os.sep)
        for base in allowed_bases
    ):
        return {"error": f"Acces interdit: {rel_path} hors du perimetre autorise."}

    if not target.exists():
        return {"error": f"Fichier introuvable: {rel_path}"}
    if not target.is_file():
        return {"error": f"Ce n'est pas un fichier: {rel_path}"}

    try:
        lines = target.read_text(errors="replace").splitlines()
        return {
            "path": rel_path,
            "lines": len(lines),
            "content": "\n".join(lines[:max_lines]),
            "truncated": len(lines) > max_lines,
        }
    except PermissionError:
        return {"error": f"Permission refusee: {rel_path}"}


# ============================================================
# WRITE HANDLERS (avec guard ro/rw)
# ============================================================

def _suggest_command(cmd: list[str]) -> dict:
    """Retourne une suggestion en mode ro."""
    return {
        "blocked": True,
        "message": "Mode lecture seule — commande suggeree :",
        "command": " ".join(cmd),
    }


async def _handle_docker_restart(args, app_slug, mode):
    service = args.get("service", "")
    compose = _compose_path(app_slug)
    cmd = ["docker", "compose", "-f", str(compose), "restart", service]
    if mode == "ro":
        return _suggest_command(cmd)
    return _run_subprocess(cmd)


async def _handle_docker_recreate(args, app_slug, mode):
    service = args.get("service", "")
    compose = _compose_path(app_slug)
    cmd = [
        "docker", "compose", "-f", str(compose),
        "up", "-d", "--force-recreate", service,
    ]
    if mode == "ro":
        return _suggest_command(cmd)
    return _run_subprocess(cmd)


async def _handle_docker_down(args, app_slug, mode):
    compose = _compose_path(app_slug)
    cmd = ["docker", "compose", "-f", str(compose), "down"]
    if mode == "ro":
        return _suggest_command(cmd)
    return _run_subprocess(cmd)


async def _handle_docker_prune(args, app_slug, mode):
    compose = _compose_path(app_slug)
    cmd = ["docker", "compose", "-f", str(compose), "down", "--rmi", "local"]
    if mode == "ro":
        return _suggest_command(cmd)
    return _run_subprocess(cmd)


async def _handle_docker_rm(args, app_slug, mode):
    service = args.get("service", "")
    compose = _compose_path(app_slug)
    cmd = ["docker", "compose", "-f", str(compose), "rm", "-f", service]
    if mode == "ro":
        return _suggest_command(cmd)
    return _run_subprocess(cmd)


async def _handle_db_reset(args, app_slug, mode):
    compose = _compose_path(app_slug)
    cmd = [
        "docker", "compose", "-f", str(compose),
        "exec", "-T", "db", "psql", "-U", "app",
        "-c", "DROP SCHEMA public CASCADE; CREATE SCHEMA public;",
    ]
    if mode == "ro":
        return _suggest_command(cmd)
    return _run_subprocess(cmd)


async def _handle_volume_delete(args, app_slug, mode):
    volume = args.get("volume", "")
    cmd = ["docker", "volume", "rm", volume]
    if mode == "ro":
        return _suggest_command(cmd)
    return _run_subprocess(cmd)


# ============================================================
# HANDLER REGISTRY
# ============================================================

HANDLERS = {
    "docker_logs": _handle_docker_logs,
    "docker_ps": _handle_docker_ps,
    "docker_inspect": _handle_docker_inspect,
    "docker_stats": _handle_docker_stats,
    "db_query_select": _handle_db_query_select,
    "app_status": _handle_app_status,
    "db_dump_inspect": _handle_db_dump_inspect,
    "read_file": _handle_read_file,
    "docker_restart": _handle_docker_restart,
    "docker_recreate": _handle_docker_recreate,
    "docker_down": _handle_docker_down,
    "docker_prune": _handle_docker_prune,
    "docker_rm": _handle_docker_rm,
    "db_reset": _handle_db_reset,
    "volume_delete": _handle_volume_delete,
}


# ============================================================
# REGISTRATION
# ============================================================

def register_debug_tools(server: Server, app_slug: str, mode: str):
    """Enregistre tous les tools debug sur le serveur MCP.

    Args:
        server: instance mcp.server.Server
        app_slug: nom de l'app cible (ex: exemple-app)
        mode: "ro" (lecture seule) ou "rw" (semi-auto)
    """

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        handler = HANDLERS.get(name)
        if handler is None:
            return [TextContent(
                type="text",
                text=json.dumps({"error": f"Outil inconnu: {name}"}),
            )]

        try:
            result = await handler(arguments, app_slug, mode)
            text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            text = json.dumps(
                {"error": str(e), "tool": name},
                indent=2,
                ensure_ascii=False,
            )

        return [TextContent(type="text", text=text)]


# ============================================================
# ENTRY POINT
# ============================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Serveur MCP debug pour POULIDOR",
    )
    parser.add_argument("--app", required=True, help="Slug de l'app cible")
    parser.add_argument(
        "--mode", choices=["ro", "rw"], default="ro",
        help="Mode d'execution (ro=lecture, rw=semi-auto)",
    )
    args = parser.parse_args()

    server = Server("debug-mcp")
    register_debug_tools(server, app_slug=args.app, mode=args.mode)

    async def run():
        async with stdio_server() as (read, write):
            await server.run(
                read, write,
                server.create_initialization_options(),
            )

    asyncio.run(run())


if __name__ == "__main__":
    main()
