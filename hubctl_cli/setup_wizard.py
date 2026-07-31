from __future__ import annotations
from pathlib import Path
from rich.text import Text

from hubctl_cli import auto_detect, env_manager, widgets
from hubctl_cli.canvas import Canvas
from hubctl_cli.hub_state import PARC_ROOT

ENV_PATH = PARC_ROOT / "HUB_base" / ".env"


def _text(s: str) -> Text:
    return Text(s)


def _pick_mode(canvas: Canvas) -> str | None:
    mode = widgets.select(canvas, "Mode :", ["production", "développement"])
    if mode is widgets.ESC or mode is None:
        return None
    return "production" if mode == "production" else "development"


def _pick_whitelist(canvas: Canvas) -> str | None:
    ip = auto_detect.detect_server_ip()
    default_wl = auto_detect.default_whitelist(ip)
    wl = widgets.text(canvas, f"Plages IP à autoriser (CIDR) [{default_wl}] :")
    if wl is widgets.ESC or wl is None:
        return None
    return wl.strip() or default_wl


def _pick_admin(canvas: Canvas) -> dict | None:
    username = widgets.text(canvas, "Admin username [admin] :")
    if username is widgets.ESC or username is None:
        return None
    username = username.strip() or "admin"

    password = widgets.password(canvas, "Admin password (min 8 car.) :")
    if password is widgets.ESC or password is None or len(password) < 8:
        if password is not widgets.ESC and password is not None:
            canvas.render(_text("Mot de passe trop court (min 8)."))
        return None

    email = widgets.text(canvas, "Admin email [admin@local.lan] :")
    if email is widgets.ESC or email is None:
        return None
    return {
        "ADMIN_USERNAME": username,
        "ADMIN_PASSWORD": password,
        "ADMIN_EMAIL": email.strip() or "admin@local.lan",
    }


def _recap(canvas: Canvas, values: dict) -> bool:
    lines = ["Récapitulatif :", ""]
    for k, v in values.items():
        lines.append(f"  {k} = {v}")
    canvas.render(_text("\n".join(lines)))
    return widgets.confirm(canvas, "Appliquer ces valeurs ?", default=True) is True


def quick_setup(canvas: Canvas, env_path: Path | None = None) -> bool:
    if env_path is None:
        env_path = ENV_PATH

    mode = _pick_mode(canvas)
    if mode is None:
        return False

    wl = _pick_whitelist(canvas)
    if wl is None:
        return False

    admin = _pick_admin(canvas)
    if admin is None:
        return False

    values = {**env_manager.env_defaults(mode), "APP_WHITELIST_NETWORKS": wl, **admin}
    values["HUB_HOST"] = auto_detect.detect_server_ip()

    if not _recap(canvas, values):
        return False

    env_manager.write_env(env_path, values)
    canvas.render(_text("✓ Configuration écrite dans .env"))
    from hubctl_cli.menu import pause
    pause(canvas)
    return True


def submenu_network(canvas: Canvas, env_path: Path | None = None) -> None:
    if env_path is None:
        env_path = ENV_PATH
    existing = env_manager.read_env(env_path)

    ip = auto_detect.detect_server_ip()
    default_wl = existing.get("APP_WHITELIST_NETWORKS", auto_detect.default_whitelist(ip))
    canvas.render(_text(f"Whitelist actuelle : {default_wl}"))
    wl = widgets.text(canvas, "Whitelist réseaux (CIDR) :")
    if wl is widgets.ESC or wl is None:
        return
    wl_val = wl.strip() or default_wl

    default_cors = existing.get("ALLOWED_ORIGINS", auto_detect.default_cors(ip))
    canvas.render(_text(f"CORS actuel : {default_cors}"))
    origins = widgets.text(canvas, "Origines CORS :")
    if origins is widgets.ESC or origins is None:
        origins = default_cors

    https_default = existing.get("HUB_HTTPS_ENABLED", "true") == "true"
    https = widgets.confirm(canvas, "Activer HTTPS ?", default=https_default)
    if https is widgets.ESC or https is None:
        https = https_default

    updates = {
        "APP_WHITELIST_NETWORKS": wl_val,
        "ALLOWED_ORIGINS": origins.strip() or default_cors,
        "HUB_HTTPS_ENABLED": str(https).lower(),
    }
    env_manager.write_env(env_path, updates)
    canvas.render(_text("✓ Réseau mis à jour"))
    from hubctl_cli.menu import pause
    pause(canvas)


def submenu_admin(canvas: Canvas, env_path: Path | None = None) -> None:
    if env_path is None:
        env_path = ENV_PATH
    existing = env_manager.read_env(env_path)

    username = widgets.text(canvas, f"Admin username [{existing.get('ADMIN_USERNAME', 'admin')}] :")
    if username is widgets.ESC or username is None:
        return

    password = widgets.password(canvas, "Admin password (min 8, laisser vide = inchangé) :")
    if password is widgets.ESC or password is None:
        return
    if password and len(password) < 8:
        canvas.render(_text("Mot de passe trop court."))
        return

    email = widgets.text(canvas, f"Admin email [{existing.get('ADMIN_EMAIL', 'admin@local.lan')}] :")
    if email is widgets.ESC or email is None:
        return

    updates: dict = {}
    if username.strip():
        updates["ADMIN_USERNAME"] = username.strip()
    if password:
        updates["ADMIN_PASSWORD"] = password
    if email.strip():
        updates["ADMIN_EMAIL"] = email.strip()

    if updates:
        env_manager.write_env(env_path, updates)
        canvas.render(_text("✓ Admin mis à jour"))
    from hubctl_cli.menu import pause
    pause(canvas)


def submenu_advanced(canvas: Canvas, env_path: Path | None = None) -> None:
    if env_path is None:
        env_path = ENV_PATH
    existing = env_manager.read_env(env_path)

    current_mode = existing.get("APP_ENV", "production")
    mode = widgets.select(canvas, f"Mode [{current_mode}] :", ["production", "développement"])
    if mode is widgets.ESC or mode is None:
        return
    mode_val = "production" if mode == "production" else "development"

    api_port = widgets.text(canvas, f"Port API [{existing.get('HUB_API_PORT', '8000')}] :")
    if api_port is widgets.ESC or api_port is None:
        return
    http_port = widgets.text(canvas, f"Port HTTP [{existing.get('HUB_FRONTEND_PORT', '80')}] :")
    if http_port is widgets.ESC or http_port is None:
        return
    https_port = widgets.text(canvas, f"Port HTTPS [{existing.get('HUB_HTTPS_PORT', '443')}] :")
    if https_port is widgets.ESC or https_port is None:
        return

    scan_default = existing.get("DOCKER_SCAN_ENABLED", "false") == "true"
    docker_scan = widgets.confirm(canvas, "Scan Docker activé ?", default=scan_default)
    if docker_scan is widgets.ESC or docker_scan is None:
        docker_scan = scan_default

    updates = {
        **env_manager.env_defaults(mode_val),
        "HUB_API_PORT": api_port.strip() or existing.get("HUB_API_PORT", "8000"),
        "HUB_FRONTEND_PORT": http_port.strip() or existing.get("HUB_FRONTEND_PORT", "80"),
        "HUB_HTTPS_PORT": https_port.strip() or existing.get("HUB_HTTPS_PORT", "443"),
        "DOCKER_SCAN_ENABLED": str(docker_scan).lower(),
    }
    env_manager.write_env(env_path, updates)
    canvas.render(_text("✓ Mode & avancé mis à jour"))
    from hubctl_cli.menu import pause
    pause(canvas)
