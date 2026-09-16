"""Sous-commande `hubctl jwt` — pilotage du trousseau JWT via l'API du hub."""
import json
import os
import urllib.error
import urllib.request

from rich.console import Console

from hubctl_cli.hub_state import HUB_DIR
from hubctl_cli.pra_restore import admin_login

console = Console()


def _env_lookup(key: str) -> str | None:
    env = HUB_DIR / ".env"
    if not env.exists():
        return None
    for line in env.read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip() or None
    return None


def _api_base_url() -> str:
    return f"http://localhost:{_env_lookup('HUB_API_PORT') or '8000'}"


def _call(base_url: str, token: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base_url + path, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SystemExit(f"Erreur HTTP {e.code} sur {path} : {detail}")


def _token() -> str:
    username = os.environ.get("ADMIN_USERNAME") or _env_lookup("ADMIN_USERNAME") or "admin"
    pw = os.environ.get("ADMIN_PASSWORD")
    if not pw:
        import getpass
        pw = getpass.getpass(f"Mot de passe admin ({username}) : ")
    return admin_login(_api_base_url(), username, pw)


def run_keys() -> int:
    data = _call(_api_base_url(), _token(), "GET", "/api/auth/keys")
    for k in data.get("keys", []):
        style = "green" if k["status"] == "active" else (
            "yellow" if k["status"] == "retiring" else "red")
        console.print(
            f"[{style}]●[/] {k['kid']}  {k['status']:<10} "
            f"créé {k['created_at']}  publié JWKS={k['published']}")
    return 0


def run_rotate() -> int:
    result = _call(_api_base_url(), _token(), "POST", "/api/auth/keys/rotate")
    console.print(
        f"✅ Rotation effectuée : nouveau kid [bold]{result['new_kid']}[/], "
        f"ancien kid {result['old_kid']} en overlap jusqu'à {result['overlap_ends_at']}")
    console.print(f"ℹ️  Après vérification des apps : hubctl jwt revoke --kid {result['old_kid']}")
    return 0


def run_revoke(kid: str) -> int:
    _call(_api_base_url(), _token(), "POST", "/api/auth/keys/revoke", {"kid": kid})
    console.print(f"🚫 Clé {kid} révoquée (retirée du JWKS immédiatement)")
    return 0


def run_cleanup(older_than_months: int) -> int:
    result = _call(_api_base_url(), _token(), "POST",
                   f"/api/auth/keys/cleanup?older_than_months={older_than_months}")
    console.print(f"🧹 PEM supprimés : {result['removed'] or 'aucun'}")
    return 0
