"""Construction du prompt systeme pour Granite (3 couches).

Couche 1 : Conventions POULIDOR (statique, depuis prompts/system.txt)
Couche 2 : Contexte de l'app cible (dynamique : docker ps, logs, compose)
Couche 3 : Consignes de securite selon le mode (ro/rw)
"""
import os
import re
import subprocess
from pathlib import Path


def _prompts_dir() -> Path:
    return Path(__file__).parent / "prompts"


def _park_root() -> Path:
    root = os.environ.get("POULIDOR_ROOT")
    if root:
        return Path(root)
    return Path(__file__).resolve().parents[2]


def _app_dir(app_slug: str) -> Path:
    root = _park_root()
    for base in (root / "APPS", root / "APP_Workshop"):
        candidate = base / app_slug
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"App '{app_slug}' introuvable dans APPS/ ou APP_Workshop/"
    )


def _compose_path(app_slug: str) -> Path:
    return _app_dir(app_slug) / "docker-compose.yml"


def _run_subprocess(cmd: list[str], timeout: int = 15) -> dict:
    """Execute une commande et retourne stdout/stderr/returncode."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return {"stdout": "", "stderr": str(e), "returncode": -1}


# ============================================================
# SECRET FILTERING
# ============================================================

# Cles dont la valeur doit etre masquee
_SECRET_PATTERNS = re.compile(
    r"(password|passwd|secret|token|key|credential|private"
    r"|jwt|fernet|api_key|access_key)",
    re.IGNORECASE,
)


def filter_env_vars(env_text: str) -> str:
    """Masque les valeurs des variables contenant des secrets.

    Args:
        env_text: contenu d'un fichier .env ou output de `env`

    Retourne le texte avec les valeurs sensibles remplacees par ***.
    """
    lines = []
    for line in env_text.splitlines():
        if "=" not in line or line.strip().startswith("#"):
            lines.append(line)
            continue
        key, _, value = line.partition("=")
        if _SECRET_PATTERNS.search(key) or _SECRET_PATTERNS.search(value):
            lines.append(f"{key}=***")
        else:
            lines.append(line)
    return "\n".join(lines)


# ============================================================
# COUCHE 2 : CONTEXTE APP DYNAMIQUE
# ============================================================

def build_app_context(app_slug: str) -> str:
    """Construit le contexte dynamique de l'app cible.

    Lit docker-compose.yml, docker ps, et un echantillon de logs.
    """
    parts = [f"## CONTEXTE DE L'APPLICATION : {app_slug}\n"]

    # docker-compose.yml
    try:
        compose = _compose_path(app_slug)
        if compose.exists():
            content = compose.read_text(errors="replace")
            parts.append(f"### docker-compose.yml\n```yaml\n{content}\n```\n")
    except Exception as e:
        parts.append(f"### docker-compose.yml\n(erreur de lecture: {e})\n")

    # docker ps
    compose = _compose_path(app_slug)
    ps = _run_subprocess([
        "docker", "compose", "-f", str(compose), "ps",
    ])
    if ps.get("stdout"):
        parts.append(f"### Statut des conteneurs\n```\n{ps['stdout']}\n```\n")

    # logs api (tail 30)
    logs = _run_subprocess([
        "docker", "compose", "-f", str(compose),
        "logs", "--tail", "30", "--no-color", "api",
    ])
    if logs.get("stdout"):
        parts.append(
            f"### Logs recents (api, 30 lignes)\n```\n{logs['stdout']}\n```\n"
        )

    # variables d'env (filtrees)
    env_file = _app_dir(app_slug) / ".env"
    if env_file.exists():
        try:
            env_content = env_file.read_text(errors="replace")
            filtered = filter_env_vars(env_content)
            parts.append(f"### Variables d'environnement\n```\n{filtered}\n```\n")
        except PermissionError:
            pass

    return "\n".join(parts)


# ============================================================
# COUCHE 3 : CONSIGNES DE SECURITE
# ============================================================

_SECURITY_RO = """## CONSIGNES DE SECURITE — MODE LECTURE SEULE

Tu es en mode lecture seule. Tu NE PEUX PAS executer de commandes modifiantes.
- Les outils docker_restart, docker_down, docker_rm, etc. sont bloques.
- Ils te retourneront une commande suggeree que tu dois presenter a l'admin.
- Affiche toujours les commandes suggerees dans un bloc de code avec le prefixe :
  "Commande suggeree (a executer manuellement) :"
- L'admin copiera-collera la commande pour l'executer lui-meme."""

_SECURITY_RW = """## CONSIGNES DE SECURITE — MODE SEMI-AUTO

Tu es en mode semi-auto. Tu peux executer des actions sur l'app cible.
- Les outils de lecture s'executent librement.
- Les outils non-destructifs (restart, recreate) s'executent sans confirmation.
- Les outils destructifs (down, rm, reset) demandent confirmation avant execution.
- Avant toute action destructive, explique ce que tu vas faire et pourquoi."""


# ============================================================
# PROMPT COMPLET
# ============================================================

def build_system_prompt(app_slug: str, mode: str) -> str:
    """Construit le prompt systeme complet en 3 couches.

    Args:
        app_slug: nom de l'app cible (ex: exemple-app)
        mode: "ro" ou "rw"

    Retourne le prompt systeme complet pour Granite.
    """
    # Couche 1 : statique
    static_path = _prompts_dir() / "system.txt"
    layer1 = static_path.read_text(encoding="utf-8")

    # Couche 2 : dynamique
    layer2 = build_app_context(app_slug)

    # Couche 3 : securite
    layer3 = _SECURITY_RW if mode == "rw" else _SECURITY_RO

    return f"{layer1}\n\n---\n\n{layer2}\n\n---\n\n{layer3}\n"
