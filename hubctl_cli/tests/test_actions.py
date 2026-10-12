import subprocess
from unittest import mock

import pytest

from hubctl_cli import actions, env_manager




def test_hub_make_builds_make_command():
    with mock.patch.object(actions.utils, "run_shell", return_value=0) as rs:
        actions.hub_make("down", dry_run=True)
    args = rs.call_args.args[0]
    assert args[0] == "make"
    assert "-C" in args
    assert args[-1] == "down"


def test_app_make_targets_app_dir():
    with mock.patch.object(actions.utils, "run_shell", return_value=0) as rs:
        actions.app_make("exemple-apps", "logs", dry_run=True)
    args = rs.call_args.args[0]
    assert str(actions.hub_state.APPS_DIR / "exemple-apps") in args
    assert args[-1] == "logs"


def test_disk_guard_blocks_when_critical():
    fake = mock.Mock(percent_free=3.0, free=1, total=100)
    with mock.patch.object(actions.hub_state, "get_disk_usage", return_value=fake):
        assert actions.disk_guard() is False


def test_disk_guard_ok_when_low_but_not_critical():
    fake = mock.Mock(percent_free=10.0, free=1, total=100)
    with mock.patch.object(actions.hub_state, "get_disk_usage", return_value=fake):
        assert actions.disk_guard() is True


def test_disk_guard_ok_when_plenty():
    fake = mock.Mock(percent_free=50.0, free=1, total=100)
    with mock.patch.object(actions.hub_state, "get_disk_usage", return_value=fake):
        assert actions.disk_guard() is True




def test_repo_name_from_url():
    assert actions.repo_name_from_url("git@github.com:Poutchouli/exemple-app-2.git") == "exemple-app-2"
    assert actions.repo_name_from_url("https://github.com/Poutchouli/exemple-app-2.git") == "exemple-app-2"
    assert actions.repo_name_from_url("git@github.com:Org/my-app") == "my-app"
    assert actions.repo_name_from_url("") is None
    assert actions.repo_name_from_url("nom avec espaces/") is None


def _menu_canvas():
    c = mock.MagicMock()
    c.inner_cols = 98
    c.inner_rows = 28
    c.set_layout = mock.MagicMock()
    return c


def _mock_vault_mgr():
    mgr = mock.MagicMock()
    mgr.vault = mock.MagicMock()
    mgr.vault.is_locked = True
    mgr.backup_all = mock.MagicMock(return_value={"env": 1, "files": 1})
    return mgr


def test_pause_waits_for_enter():
    from hubctl_cli import menu
    canvas = _menu_canvas()
    with mock.patch("sys.stdin.fileno", return_value=0), \
         mock.patch.object(menu.keys, "read_key", side_effect=[menu.keys.KeyEnter()]):
        menu.pause(canvas)
    assert canvas.render.call_count >= 1


def test_main_loop_quits_on_zero_key():
    from hubctl_cli import menu
    canvas = _menu_canvas()
    vault_mgr = _mock_vault_mgr()
    with mock.patch("sys.stdin.fileno", return_value=0), \
         mock.patch.object(menu.keys, "read_key",
                           side_effect=[menu.keys.KeyChar("0")]), \
         mock.patch.object(menu.widgets, "confirm", return_value=True):
        menu.main_loop(canvas, vault_mgr)
    canvas.render.assert_called()


def test_main_loop_escape_confirms_then_quits():
    from hubctl_cli import menu
    canvas = _menu_canvas()
    vault_mgr = _mock_vault_mgr()
    with mock.patch("sys.stdin.fileno", return_value=0), \
         mock.patch.object(menu.keys, "read_key",
                           side_effect=[menu.keys.KeyEsc()]), \
         mock.patch.object(menu.widgets, "confirm", return_value=True):
        menu.main_loop(canvas, vault_mgr)
    canvas.render.assert_called()


def test_main_loop_ctrlc_at_menu_exits():
    from hubctl_cli import menu
    canvas = _menu_canvas()
    vault_mgr = _mock_vault_mgr()
    with mock.patch("sys.stdin.fileno", return_value=0), \
         mock.patch.object(menu.keys, "read_key",
                           side_effect=[menu.keys.KeyCtrlC()]):
        with pytest.raises(KeyboardInterrupt):
            menu.main_loop(canvas, vault_mgr)


# ── action_init tests ──

def _mock_console():
    c = mock.MagicMock()
    return c








def _fake_path(name):
    p = mock.MagicMock()
    p.configure_mock(**{"parent.mkdir": mock.Mock(), "__str__.return_value": f"/fake/parc/{name}"})
    return p




# ── Workshop move tests ──

from pathlib import Path
from rich.console import Console


def _fake_console():
    return Console(force_terminal=True, width=120)










import json
from unittest import mock


def test_ensure_jwt_keys_does_not_write_to_dotenv(tmp_path, monkeypatch):
    """H10 régression : ne PAS écrire AUTO_GENERATE_KEYS dans le .env de prod."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    hub_dir = tmp_path
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", hub_dir)

    dotenv = tmp_path / ".env"
    dotenv.write_text("APP_ENV=production\n")  # prod, pas de AUTO_GENERATE_KEYS

    actions._ensure_jwt_keys()

    content = dotenv.read_text()
    assert "AUTO_GENERATE_KEYS" not in content, (
        "H10: _ensure_jwt_keys ne doit PAS persister AUTO_GENERATE_KEYS dans le .env de prod"
    )
    assert "APP_ENV=production" in content  # .env non modifié


def test_ensure_hub_env_adds_missing_key(tmp_path, monkeypatch):
    """Serveur pré-3.71.1 : APPS_HOST_PATH absente du .env → ajoutée automatiquement.

    Le contenu existant est préservé (merge atomique), la valeur pointe vers
    APPS_DIR réel du parc — réparation persistante, zéro action manuelle.
    """
    hub_dir = tmp_path / "HUB_base"
    hub_dir.mkdir()
    apps_dir = tmp_path / "APPS"
    apps_dir.mkdir()
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", hub_dir)
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", apps_dir)

    dotenv = hub_dir / ".env"
    dotenv.write_text("POSTGRES_PASSWORD=x\nADMIN_USERNAME=a\n")

    actions.ensure_hub_env()

    env = env_manager.read_env(dotenv)
    assert env["APPS_HOST_PATH"] == str(apps_dir.resolve())
    assert env["POSTGRES_PASSWORD"] == "x"  # contenu existant préservé
    assert env["ADMIN_USERNAME"] == "a"


def test_ensure_hub_env_never_overwrites_custom_value(tmp_path, monkeypatch):
    """Une APPS_HOST_PATH déjà définie (custom) n'est jamais écrasée."""
    hub_dir = tmp_path
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", hub_dir)
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", tmp_path / "APPS")

    dotenv = hub_dir / ".env"
    dotenv.write_text("APPS_HOST_PATH=/srv/custom/APPS\n")

    actions.ensure_hub_env()

    assert env_manager.read_env(dotenv)["APPS_HOST_PATH"] == "/srv/custom/APPS"


def test_ensure_hub_env_fills_empty_value(tmp_path, monkeypatch):
    """APPS_HOST_PATH présente mais vide (= absente) → remplie."""
    hub_dir = tmp_path
    apps_dir = tmp_path / "APPS"
    apps_dir.mkdir()
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", hub_dir)
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", apps_dir)

    dotenv = hub_dir / ".env"
    dotenv.write_text("APPS_HOST_PATH=\n")

    actions.ensure_hub_env()

    assert env_manager.read_env(dotenv)["APPS_HOST_PATH"] == str(apps_dir.resolve())


def test_ensure_hub_env_dry_run_writes_nothing(tmp_path, monkeypatch):
    """dry_run : aucun fichier écrit."""
    hub_dir = tmp_path
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", hub_dir)
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", tmp_path / "APPS")

    actions.ensure_hub_env(dry_run=True)

    assert not (hub_dir / ".env").exists()


def test_cleanup_orphan_tests_only_targets_hubctl_projects(monkeypatch):
    """Seuls les projets -test correspondant au hub ou à une app réelle sont nettoyés.
    Les projets -test étrangers (load-test, integration-test) doivent être préservés."""
    projects = [
        {"Name": "exemple-apps-test"},   # hubctl-owned (app réelle)
        {"Name": "hub-test"},              # hubctl-owned (hub)
        {"Name": "load-test"},             # ÉTRANGER — ne doit PAS être nettoyé
        {"Name": "integration-test"},      # ÉTRANGER — ne doit PAS être nettoyé
    ]

    cleaned: list[str] = []

    class _FakeRun:
        def __call__(self, cmd, **kwargs):
            if "ls" in cmd:
                return mock.Mock(stdout=json.dumps(projects), returncode=0)
            if "down" in cmd:
                # Capture le nom du projet nettoyé
                idx = cmd.index("-p")
                cleaned.append(cmd[idx + 1])
                return mock.Mock(stdout="", returncode=0)
            return mock.Mock(stdout="", returncode=0)

    monkeypatch.setattr(actions.subprocess, "run", _FakeRun())

    # Simuler get_apps : une seule app "exemple-apps"
    fake_app = mock.Mock()
    fake_app.name = "exemple-apps"
    monkeypatch.setattr(actions.hub_state, "get_apps", lambda: [fake_app])
    monkeypatch.setattr(actions.hub_state, "get_workshop_apps", lambda: [])

    actions.cleanup_orphan_tests()

    assert "exemple-apps-test" in cleaned, "L'app hubctl doit être nettoyée"
    assert "hub-test" in cleaned, "Le hub-test doit être nettoyé"
    assert "load-test" not in cleaned, "Le projet étranger load-test ne doit PAS être nettoyé"
    assert "integration-test" not in cleaned, "Le projet étranger integration-test ne doit PAS être nettoyé"


# ── action_update (Lot 2 : flux séquentiel + rollback) ──






def test_is_healthy_body_with_spaces_after_colon():
    """L'ancien code matchait '"status":"healthy"' littéralement → un corps
    `{"status": "healthy"}` (espace après le `:`, format json.dumps par défaut)
    n'était JAMAIS reconnu sain → health check faux négatif → rollbacks destructeurs.
    """
    assert actions._is_healthy_body('{"status": "healthy"}') is True


def test_is_healthy_body_without_spaces():
    assert actions._is_healthy_body('{"status":"healthy"}') is True


def test_is_healthy_body_with_extra_fields():
    assert actions._is_healthy_body('{"status": "healthy", "version": "1.2.3"}') is True


def test_is_healthy_body_unhealthy_status():
    assert actions._is_healthy_body('{"status": "unhealthy"}') is False
    assert actions._is_healthy_body('{"status":"starting"}') is False


def test_is_healthy_body_accepte_status_ok_apps_node():
    """Apps Node d'ancien template (ex. exemple-app) : /api/health répond
    {"status":"ok", …} — reconnues saines au même titre que "healthy",
    sinon health check faux négatif systématique → rollback à chaque
    création d'atelier, quel que soit le timeout."""
    assert actions._is_healthy_body('{"status":"ok"}') is True
    assert actions._is_healthy_body('{"status":"ok","timestamp":"2026-10-10T16:33:12Z"}') is True
    assert actions._is_healthy_body('{"status": "OK"}') is False  # sensibilité exacte conservée


def test_is_healthy_body_non_json():
    assert actions._is_healthy_body("<html>404 Not Found</html>") is False
    assert actions._is_healthy_body("Service Unavailable") is False


def test_is_healthy_body_empty():
    assert actions._is_healthy_body("") is False
    assert actions._is_healthy_body(None) is False


# ── ALTER USER : échappement SQL du mot de passe (#3) ──


def test_build_alter_user_sql_escapes_single_quotes():
    """Un mot de passe contenant une apostrophe (ex: 'OConnor123') ne doit pas
    casser le ALTER USER ni permettre une injection SQL. Standard SQL : doubler
    les quotes à l'intérieur d'un string literal."""
    sql = actions._build_alter_user_sql("hubadmin", "My'Pass'Word")
    # Les ' internes au password sont doublées (échappement SQL) → pas d'injection.
    assert "My''Pass''Word" in sql
    # L'instruction reste bien formée (un seul statement, terminé par ;).
    assert sql.endswith(";")
    assert sql.count(";") == 1


def test_build_alter_user_sql_simple_password_unchanged():
    sql = actions._build_alter_user_sql("hubadmin", "plainsecret123")
    assert sql == "ALTER USER hubadmin WITH PASSWORD 'plainsecret123';"


def test_build_alter_user_sql_rejects_invalid_username():
    """Un POSTGRES_USER contenant un caractère non-identifiant doit être refusé
    (et non interpolé tel quel dans le SQL)."""
    import pytest
    with pytest.raises(ValueError):
        actions._build_alter_user_sql("hub';DROP--", "x")
    with pytest.raises(ValueError):
        actions._build_alter_user_sql("", "x")


def test_health_check_reads_app_port(tmp_path, monkeypatch):
    """H3: _health_check doit lire API_PORT du .env de l'app cible."""
    app_dir = tmp_path / "myapp"
    app_dir.mkdir()
    (app_dir / ".env").write_text("API_PORT=9999\n")
    monkeypatch.setattr(actions.hub_state, "active_apps_dir", lambda: tmp_path)

    curl_result = mock.Mock(stdout='{"status":"healthy"}', returncode=0)

    def fake_run(cmd, **kwargs):
        # Capture the URL to verify the port
        if "curl" in cmd:
            test_health_check_reads_app_port.curl_url = cmd[-1]
        return curl_result

    test_health_check_reads_app_port.curl_url = None
    monkeypatch.setattr(actions.subprocess, "run", fake_run)
    monkeypatch.setattr(actions.time, "sleep", lambda x: None)

    actions._health_check("myapp", timeout=1)

    assert "9999" in test_health_check_reads_app_port.curl_url, (
        f"health_check doit utiliser API_PORT=9999, URL vue: {test_health_check_reads_app_port.curl_url}"
    )


























# ── Task 2: _detect_host_ip ──

def test_detect_host_ip_returns_string():
    ip = actions._detect_host_ip()
    assert isinstance(ip, str)
    assert len(ip) > 0
    # Doit ressembler à une IP v4 ou v6
    parts = ip.split(".")
    if len(parts) == 4:
        assert all(0 <= int(p) <= 255 for p in parts)


def test_detect_host_ip_fallback(monkeypatch):
    import socket
    # Simule échec de connexion
    def fake_socket(*args, **kwargs):
        raise OSError("no route")
    monkeypatch.setattr(socket, "socket", fake_socket)
    ip = actions._detect_host_ip()
    assert ip == "127.0.0.1"  # fallback


# ── Task 1: action_logs + action_restart + _compose_file ──













# ── Task 2: health + db-shell ──

















def test_ensure_jwt_keys_skips_when_present(tmp_path, monkeypatch):
    from hubctl_cli import actions, hub_state
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "jwt_private_key.pem").write_text("x")
    (secrets / "jwt_public_key.pem").write_text("x")
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", tmp_path)
    with mock.patch.object(actions.utils, "run_shell") as rs:
        actions._ensure_jwt_keys(dry_run=False)
    rs.assert_not_called()


def test_ensure_jwt_keys_generates_when_missing(tmp_path, monkeypatch):
    from hubctl_cli import actions, hub_state
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", tmp_path)
    with mock.patch.object(actions.utils, "run_shell", return_value=0) as rs:
        actions._ensure_jwt_keys(mock.Mock(), dry_run=False)
    cmds = [c.args[0] for c in rs.call_args_list]
    assert any(c[:2] == ["openssl", "genrsa"] for c in cmds)
    assert any(c[:2] == ["openssl", "rsa"] for c in cmds)
    assert any(c[:2] == ["chmod", "600"] for c in cmds)


def test_ensure_jwt_keys_dry_run_no_openssl(tmp_path, monkeypatch):
    from hubctl_cli import actions, hub_state
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", tmp_path)
    with mock.patch.object(actions.utils, "run_shell", return_value=0) as rs:
        actions._ensure_jwt_keys(mock.Mock(), dry_run=True)
    cmds = [c.args[0] for c in rs.call_args_list]
    assert not any(c[:2] == ["openssl", "genrsa"] for c in cmds)


# ── Task 5: Blue-green helpers ──















# ── Task 6: Blue-green deploy ──













# ── Workshop create tests ([→] sans suppression de prod) ──

















    # Aucun effet secondaire : on ne doit pas avoir tenté de git pull.
    # (test passe si pas d'exception levée — le code retourne tôt avec un message.)


# ── Task 3: action_prod_deploy --copy-db (destructif, CLI-only) ──







# ── Task 4: action_workshop_remove ([×] suppression atelier) ──







# ── Task 2: action_vpn (helpers + sync + status) ──











    # Un warning a été imprimé (on vérifie juste que ça ne plante pas et que compose up a été appelé)






# ── Task 3: action_vpn on/off (flag DB + reconcile) ──









# ── Task 4: hook boot WG (action_start_stop start+hub → vpn sync) ──







def _raise(*a, **k):
    raise RuntimeError("boom")




# ── Task 3: _generate_dotenv tests ──

import secrets
import string


def _random_pwd(length=16):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _mock_env_path(monkeypatch, tmp_path, exists=False):
    """Configure un HUB_DIR temporaire pour les tests de _generate_dotenv."""
    hub_dir = tmp_path / "HUB_base"
    hub_dir.mkdir(parents=True, exist_ok=True)
    if exists:
        (hub_dir / ".env").write_text("POSTGRES_PASSWORD=dummy\n")
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", hub_dir)
    return hub_dir


def test_generate_dotenv_creates_fresh(monkeypatch, tmp_path):
    hub_dir = _mock_env_path(monkeypatch, tmp_path, exists=False)
    console = _mock_console()
    result = actions._generate_dotenv(console, force=False, dry_run=False)
    assert result is not None
    assert "admin_username" in result
    assert "admin_password" in result
    assert len(result["admin_password"]) >= 12
    env_file = hub_dir / ".env"
    assert env_file.exists()
    content = env_file.read_text()
    assert "POSTGRES_PASSWORD=" in content
    assert "ADMIN_PASSWORD=" in content
    assert "SECRETS_ENCRYPTION_KEY=" in content
    assert "DATABASE_URL=" in content
    assert "POSTGRES_USER=hubadmin" in content


def test_generate_dotenv_prompt_overwrite(monkeypatch, tmp_path):
    _mock_env_path(monkeypatch, tmp_path, exists=True)
    console = _mock_console()
    # Non-TTY → doit abort sans confirmation
    import sys
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    result = actions._generate_dotenv(console, force=False, dry_run=False)
    assert result is None  # aborted


def test_generate_dotenv_force_overwrite(monkeypatch, tmp_path):
    hub_dir = _mock_env_path(monkeypatch, tmp_path, exists=True)
    console = _mock_console()
    result = actions._generate_dotenv(console, force=True, dry_run=False)
    assert result is not None
    assert "admin_password" in result
    env_file = hub_dir / ".env"
    content = env_file.read_text()
    assert "POSTGRES_PASSWORD=" in content
    # L'ancienne valeur ne doit pas apparaître
    assert "dummy" not in content


def test_generate_dotenv_dry_run_no_write(monkeypatch, tmp_path):
    hub_dir = _mock_env_path(monkeypatch, tmp_path, exists=False)
    console = _mock_console()
    result = actions._generate_dotenv(console, force=False, dry_run=True)
    assert result is not None
    assert "admin_password" in result
    env_file = hub_dir / ".env"
    assert not env_file.exists()  # rien écrit sur le disque


def test_generate_dotenv_creates_env_from_template(monkeypatch, tmp_path):
    """Vérifie que toutes les variables obligatoires du docker-compose sont présentes."""
    hub_dir = _mock_env_path(monkeypatch, tmp_path, exists=False)
    console = _mock_console()
    result = actions._generate_dotenv(console, force=False, dry_run=False)
    env_file = hub_dir / ".env"
    content = env_file.read_text()
    required_vars = [
        "POSTGRES_USER=", "POSTGRES_PASSWORD=", "POSTGRES_DB=",
        "ADMIN_USERNAME=", "ADMIN_PASSWORD=", "ADMIN_EMAIL=",
        "SECRETS_ENCRYPTION_KEY=", "DATABASE_URL=",
        "HUB_API_PORT=", "HUB_FRONTEND_PORT=", "HUB_HTTPS_PORT=",
        "ALLOWED_ORIGINS=", "APP_WHITELIST_NETWORKS=",
        "HOST_UID=", "HOST_DOCKER_GID=",
    ]
    for var in required_vars:
        assert var in content, f"Variable obligatoire manquante: {var}"

def test_generate_dotenv_whitelist_includes_host_subnet(monkeypatch, tmp_path):
    """La whitelist générée inclut le /24 du subnet détecté de l'hôte.

    Avant : APP_WHITELIST_NETWORKS était en dur (10.42.0.0/16,192.168.1.0/24)
    → allocate-ports/auto-enregistrement en 403 sur toute machine hors
    192.168.1.0/24 (constaté fresh-install Debian, subnet 10.0.2.0/24).
    """
    hub_dir = _mock_env_path(monkeypatch, tmp_path, exists=False)
    console = _mock_console()
    monkeypatch.setattr(actions, "_detect_host_ip", lambda: "10.0.2.15")
    result = actions._generate_dotenv(console, force=False, dry_run=False)
    assert result is not None
    content = (hub_dir / ".env").read_text()
    assert "APP_WHITELIST_NETWORKS=10.42.0.0/16,10.0.2.0/24" in content
    # Le subnet magique legacy ne doit plus apparaître quand le subnet réel
    # est détecté
    assert "192.168.1.0/24" not in content

def test_generate_dotenv_whitelist_fallback_loopback(monkeypatch, tmp_path):
    """Sans réseau détecté (IP loopback), la whitelist legacy est conservée."""
    hub_dir = _mock_env_path(monkeypatch, tmp_path, exists=False)
    console = _mock_console()
    monkeypatch.setattr(actions, "_detect_host_ip", lambda: "127.0.0.1")
    result = actions._generate_dotenv(console, force=False, dry_run=False)
    assert result is not None
    content = (hub_dir / ".env").read_text()
    assert "APP_WHITELIST_NETWORKS=10.42.0.0/16,192.168.1.0/24" in content

    # Fernet key doit être valide
    import re
    fkey_match = re.search(r"SECRETS_ENCRYPTION_KEY=(\S+)", content)
    assert fkey_match
    from cryptography.fernet import Fernet
    Fernet(fkey_match.group(1))  # ne doit pas lever d'exception


# ── action_bootstrap tests ──

def _patch_bootstrap_mocks(monkeypatch, tmp_path):
    """Setup commun pour les tests d'action_bootstrap."""
    hub_dir = tmp_path / "HUB_base"
    hub_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", hub_dir)
    monkeypatch.setattr(actions.hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", tmp_path / "APPS")
    # Mock docker
    mock_docker_check = mock.Mock(return_value=True)
    monkeypatch.setattr(actions.hub_state, "check_docker", mock_docker_check)
    # Ne pas écrire dans le vrai ~/.bashrc / ~/.config/poulidor/parc.sh du user
    monkeypatch.setattr(actions, "_install_shell_marker", lambda *a, **kw: None)
    return hub_dir


def test_action_bootstrap_full_flow(monkeypatch, tmp_path):
    """Test du flux complet avec mocks."""
    hub_dir = _patch_bootstrap_mocks(monkeypatch, tmp_path)
    console = _mock_console()
    # Créer le docker-compose.yml pour que hub_make ne faille pas
    (hub_dir / "docker-compose.yml").write_text("services: {}\n")
    (hub_dir / "Makefile").write_text("up-prod:\n\tdocker compose up -d\n")
    (hub_dir / "secrets").mkdir(exist_ok=True)

    with mock.patch.object(actions, "_generate_dotenv",
                           return_value={"admin_username": "admin", "admin_password": "testpwd", "host_ip": "10.0.0.1"}) as mock_gen, \
         mock.patch.object(actions, "_ensure_jwt_keys") as mock_jwt, \
         mock.patch.object(actions, "_cleanup_existing_hub_containers") as mock_cleanup, \
         mock.patch.object(actions, "_sync_db_password_if_needed") as mock_sync, \
         mock.patch.object(actions, "hub_make", return_value=0) as mock_make, \
         mock.patch.object(actions, "_health_check", return_value=True) as mock_health, \
         mock.patch.object(actions, "disk_guard", return_value=True):
        result = actions.action_bootstrap(console, force=False, dry_run=False)

    assert result == 0
    mock_gen.assert_called_once()
    mock_jwt.assert_called_once()
    mock_cleanup.assert_called_once()
    mock_make.assert_called_once()
    mock_health.assert_called_once()
    # Vérifie que le résumé a été affiché
    assert console.print.call_count >= 1


def test_action_bootstrap_fails_when_no_dotenv_generated(monkeypatch, tmp_path):
    _patch_bootstrap_mocks(monkeypatch, tmp_path)
    console = _mock_console()
    with mock.patch.object(actions, "_generate_dotenv", return_value=None):
        result = actions.action_bootstrap(console, force=False, dry_run=False)
    assert result == 1


def test_action_bootstrap_fails_when_make_fails(monkeypatch, tmp_path):
    hub_dir = _patch_bootstrap_mocks(monkeypatch, tmp_path)
    (hub_dir / "docker-compose.yml").write_text("services: {}\n")
    (hub_dir / "Makefile").write_text("up-prod:\n\tdocker compose up -d\n")
    (hub_dir / "secrets").mkdir(exist_ok=True)
    console = _mock_console()
    with mock.patch.object(actions, "_generate_dotenv",
                           return_value={"admin_username": "admin", "admin_password": "x", "host_ip": "1.2.3.4"}), \
         mock.patch.object(actions, "_ensure_jwt_keys"), \
         mock.patch.object(actions, "_cleanup_existing_hub_containers"), \
         mock.patch.object(actions, "_sync_db_password_if_needed"), \
         mock.patch.object(actions, "hub_make", return_value=1):
        result = actions.action_bootstrap(console, force=False, dry_run=False)
    assert result == 1


def test_action_bootstrap_fails_when_health_fails(monkeypatch, tmp_path):
    hub_dir = _patch_bootstrap_mocks(monkeypatch, tmp_path)
    (hub_dir / "docker-compose.yml").write_text("services: {}\n")
    (hub_dir / "Makefile").write_text("up-prod:\n\tdocker compose up -d\n")
    (hub_dir / "secrets").mkdir(exist_ok=True)
    console = _mock_console()
    with mock.patch.object(actions, "_generate_dotenv",
                           return_value={"admin_username": "admin", "admin_password": "x", "host_ip": "1.2.3.4"}), \
         mock.patch.object(actions, "_ensure_jwt_keys"), \
         mock.patch.object(actions, "_cleanup_existing_hub_containers"), \
         mock.patch.object(actions, "_sync_db_password_if_needed"), \
         mock.patch.object(actions, "hub_make", return_value=0), \
         mock.patch.object(actions, "_health_check", return_value=False):
        result = actions.action_bootstrap(console, force=False, dry_run=False)
    assert result == 1


def test_action_bootstrap_dry_run(monkeypatch, tmp_path):
    hub_dir = _patch_bootstrap_mocks(monkeypatch, tmp_path)
    (hub_dir / "docker-compose.yml").write_text("services: {}\n")
    (hub_dir / "Makefile").write_text("up-prod:\n\tdocker compose up -d\n")
    (hub_dir / "secrets").mkdir(exist_ok=True)
    console = _mock_console()
    with mock.patch.object(actions, "_generate_dotenv",
                           return_value={"admin_username": "admin", "admin_password": "drypwd", "host_ip": "1.1.1.1"}), \
         mock.patch.object(actions, "_ensure_jwt_keys") as jwt_mock, \
         mock.patch.object(actions, "_sync_db_password_if_needed") as sync_mock, \
         mock.patch.object(actions, "hub_make", return_value=0) as make_mock:
        result = actions.action_bootstrap(console, force=False, dry_run=True)
    assert result == 0
    # En dry-run, hub_make ne doit pas être appelé
    make_mock.assert_not_called()


# ── _cleanup_existing_hub_containers ──

def test_cleanup_calls_compose_down_then_rm_orphans(monkeypatch, tmp_path):
    """compose down gracieux + docker rm -f pour les conteneurs orphelins."""
    hub_dir = tmp_path / "HUB_base"
    hub_dir.mkdir(parents=True, exist_ok=True)
    (hub_dir / "docker-compose.yml").write_text("services: {}\n")
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", hub_dir)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "inspect" in cmd:
            name = cmd[-1]
            return mock.Mock(returncode=0 if name == "hub-db" else 1)
        return mock.Mock(returncode=0)

    monkeypatch.setattr(actions.subprocess, "run", fake_run)
    actions._cleanup_existing_hub_containers(console=_mock_console(), dry_run=False)

    # compose down a été appelé
    assert any("down" in c for c in calls)
    # hub-db existait → docker rm -f hub-db
    rm_calls = [c for c in calls if "rm" in c]
    assert any("hub-db" in c for c in rm_calls)
    # hub-api et hub-frontend n'existaient pas → pas de rm
    assert not any("hub-api" in c for c in rm_calls)
    assert not any("hub-frontend" in c for c in rm_calls)


def test_cleanup_dry_run_no_docker_commands(monkeypatch, tmp_path):
    """En dry-run, aucune commande docker n'est exécutée."""
    hub_dir = tmp_path / "HUB_base"
    hub_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", hub_dir)

    calls = []
    monkeypatch.setattr(actions.subprocess, "run",
                        lambda *a, **kw: calls.append(a) or mock.Mock(returncode=0))
    actions._cleanup_existing_hub_containers(console=_mock_console(), dry_run=True)
    assert len(calls) == 0


def test_cleanup_no_orphans_means_no_rm(monkeypatch, tmp_path):
    """Si aucun conteneur n'existe (inspect retourne 1), pas de docker rm."""
    hub_dir = tmp_path / "HUB_base"
    hub_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", hub_dir)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return mock.Mock(returncode=1)  # tous les inspect échouent

    monkeypatch.setattr(actions.subprocess, "run", fake_run)
    actions._cleanup_existing_hub_containers(console=_mock_console(), dry_run=False)

    rm_calls = [c for c in calls if "rm" in c]
    assert len(rm_calls) == 0


# ── _sync_db_password_if_needed ──

def test_sync_db_password_skips_when_no_volume(monkeypatch, tmp_path):
    """Si le volume PostgreSQL n'existe pas (fresh install), la fonction skip."""
    hub_dir = tmp_path / "HUB_base"
    hub_dir.mkdir(parents=True, exist_ok=True)
    (hub_dir / ".env").write_text("POSTGRES_PASSWORD=newpass\nPOSTGRES_USER=hubadmin\n")
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", hub_dir)

    calls = []
    monkeypatch.setattr(actions.subprocess, "run",
                        lambda *a, **kw: calls.append(a) or mock.Mock(returncode=0, stdout=""))

    actions._sync_db_password_if_needed(console=_mock_console(), dry_run=False)
    # Seul l'appel `docker volume ls` doit avoir lieu
    assert len(calls) == 1
    assert "volume" in calls[0][0] and "ls" in calls[0][0]


def test_sync_db_password_syncs_when_volume_exists(monkeypatch, tmp_path):
    """Si le volume existe, la fonction démarre le DB, attend qu'il soit sain, puis ALTER USER."""
    hub_dir = tmp_path / "HUB_base"
    hub_dir.mkdir(parents=True, exist_ok=True)
    (hub_dir / ".env").write_text("POSTGRES_PASSWORD=newpass123\nPOSTGRES_USER=hubadmin\n")
    (hub_dir / "docker-compose.yml").write_text("services: {}\n")
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", hub_dir)

    calls = []
    psql_inputs = []

    def fake_run(cmd, **kwargs):
        cmd_list = list(cmd)
        calls.append(cmd_list)
        if "volume" in cmd_list and "ls" in cmd_list:
            return mock.Mock(returncode=0, stdout="hub_base_postgres_data\n")
        if "inspect" in cmd_list:
            return mock.Mock(returncode=0, stdout="healthy\n")
        if "psql" in cmd_list:
            psql_inputs.append(kwargs.get("input", ""))
            return mock.Mock(returncode=0, stdout="ALTER ROLE\n", stderr="")
        return mock.Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(actions.subprocess, "run", fake_run)

    actions._sync_db_password_if_needed(console=_mock_console(), dry_run=False)

    # Le ALTER USER doit avoir été appelé avec le bon mot de passe
    psql_calls = [c for c in calls if "psql" in c]
    assert len(psql_calls) == 1
    # La connexion psql doit utiliser POSTGRES_USER (hubadmin), pas 'postgres'
    assert "hubadmin" in psql_calls[0]
    # Le mot de passe doit être passé via stdin (input=)
    assert len(psql_inputs) == 1
    assert "newpass123" in psql_inputs[0]


def test_sync_db_password_dry_run_no_docker(monkeypatch, tmp_path):
    """En dry-run, aucune commande docker n'est exécutée."""
    hub_dir = tmp_path / "HUB_base"
    hub_dir.mkdir(parents=True, exist_ok=True)
    (hub_dir / ".env").write_text("POSTGRES_PASSWORD=x\n")
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", hub_dir)

    calls = []
    monkeypatch.setattr(actions.subprocess, "run",
                        lambda *a, **kw: calls.append(a) or mock.Mock(returncode=0))

    actions._sync_db_password_if_needed(console=_mock_console(), dry_run=True)
    assert len(calls) == 0


# ── _clone_app ──







# ── _validate_app_structure ──







# ── _detect_app_config ──







# ── _show_and_confirm_config ──





# ── _write_app_env ──


def test_write_app_env_https_enabled_true_si_absent(tmp_path):
    """Spec 2026-09-19 : HTTPS par défaut — la clé absente du .env.example est ajoutée à true."""
    (tmp_path / ".env.example").write_text("APP_SLUG=mon-app\nAPI_PORT=9000\nFRONTEND_PORT=4250\n")
    config = {"slug": "mon-app", "api_port": 9000, "frontend_port": 4250,
              "host_ip": "192.0.2.10", "postgres_password": "pw",
              "fernet_key": "k", "hub_url": "http://192.0.2.10:8000",
              "hub_host": "192.0.2.10"}
    actions._write_app_env(None, tmp_path, config)
    env = (tmp_path / ".env").read_text()
    assert "HTTPS_ENABLED=true" in env


def test_write_app_env_https_choice_app_respecte(tmp_path):
    """Une app qui déclare HTTPS_ENABLED=false dans son .env.example n'est pas écrasée."""
    (tmp_path / ".env.example").write_text("APP_SLUG=mon-app\nHTTPS_ENABLED=false\n")
    config = {"slug": "mon-app", "api_port": 9000, "frontend_port": 4250,
              "host_ip": "192.0.2.10", "postgres_password": "pw",
              "fernet_key": "k", "hub_url": "http://192.0.2.10:8000",
              "hub_host": "192.0.2.10"}
    actions._write_app_env(None, tmp_path, config)
    assert "HTTPS_ENABLED=false" in (tmp_path / ".env").read_text()





# ── Task 5: _build_and_start_app + _sync_app_coffre ──





# ── _sync_app_coffre ──





# ── action_add_app (flow complet) ──

    # Pas d'exception = succès




    # Le dossier doit être supprimé
    # (selon l'implémentation, soit shutil.rmtree, soit un check)




# ── regressions: sync_app_coffre vers le coffre reel ──



    # Ne doit pas lever d'exception


# ── _load_seed_file ──
import yaml as _yaml


def test_load_seed_file_valid_apps(tmp_path):
    from pathlib import Path
    p = tmp_path / "seed.yaml"
    p.write_text(_yaml.dump({"apps": [
        "git@github.com:Poutchouli/exemple-app-2.git",
        "git@github.com:POULIDOR-Audit-Developpement/exemple-app.git",
    ]}))
    result = actions._load_seed_file(Path(p))
    assert result == [
        ("git@github.com:Poutchouli/exemple-app-2.git", "exemple-app"),
        ("git@github.com:POULIDOR-Audit-Developpement/exemple-app.git", "exemple-app"),
    ]


def test_load_seed_file_slug_normalization(tmp_path):
    from pathlib import Path
    p = tmp_path / "seed.yaml"
    p.write_text(_yaml.dump({"apps": ["git@github.com:Poutchouli/exemple-app-2.git"]}))
    result = actions._load_seed_file(Path(p))
    assert result[0][1] == "exemple-app"  # underscore → tiret, minuscules


def test_load_seed_file_missing_file(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        actions._load_seed_file(tmp_path / "nexiste.yaml")


def test_load_seed_file_no_apps_key(tmp_path):
    from pathlib import Path
    import pytest
    p = tmp_path / "seed.yaml"
    p.write_text(_yaml.dump({"autre": []}))
    with pytest.raises(ValueError, match="apps"):
        actions._load_seed_file(Path(p))


def test_load_seed_file_empty_apps(tmp_path):
    from pathlib import Path
    p = tmp_path / "seed.yaml"
    p.write_text(_yaml.dump({"apps": []}))
    result = actions._load_seed_file(Path(p))
    assert result == []


def test_load_seed_file_duplicate_slug(tmp_path):
    from pathlib import Path
    import pytest
    p = tmp_path / "seed.yaml"
    p.write_text(_yaml.dump({"apps": [
        "git@github.com:Poutchouli/exemple-app-2.git",
        "git@github.com:Someone/exemple-app.git",
    ]}))
    with pytest.raises(ValueError, match="dupliqué"):
        actions._load_seed_file(Path(p))


def test_load_seed_file_invalid_url(tmp_path):
    from pathlib import Path
    import pytest
    p = tmp_path / "seed.yaml"
    p.write_text(_yaml.dump({"apps": ["pas-une-url"]}))
    with pytest.raises(ValueError, match="invalide"):
        actions._load_seed_file(Path(p))


# ── _seed_one_app ──
def test_seed_one_app_case_a_new(tmp_path, monkeypatch):
    """CAS A : dossier absent → clone + init + build + sync."""
    apps_dir = tmp_path / "APPS"
    apps_dir.mkdir()
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", apps_dir)
    console = mock.Mock()
    fake_config = {"api_port": 9100, "frontend_port": 4300}
    with mock.patch.object(actions, "_clone_app", return_value=True) as cl, \
         mock.patch.object(actions, "_validate_app_structure", return_value=True), \
         mock.patch.object(actions, "_detect_app_config", return_value=fake_config), \
         mock.patch.object(actions, "_write_app_env") as we, \
         mock.patch.object(actions, "_build_and_start_app", return_value=True) as bs, \
         mock.patch.object(actions, "_sync_app_coffre") as sc, \
         mock.patch.object(actions, "disk_guard", return_value=True):
        ok, detail = actions._seed_one_app(console, "git@github.com:Foo/bar.git", "bar")
    assert ok is True
    cl.assert_called_once()
    we.assert_called_once()    # .env généré
    bs.assert_called_once()
    sc.assert_called_once()    # coffre syncé


def test_seed_one_app_case_b_cloned_no_env(tmp_path, monkeypatch):
    """CAS B : dossier présent SANS .env → pull + init + build + sync."""
    apps_dir = tmp_path / "APPS"
    app = apps_dir / "bar"
    app.mkdir(parents=True)
    # pas de .env
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", apps_dir)
    console = mock.Mock()
    fake_config = {"api_port": 9101, "frontend_port": 4301}
    with mock.patch.object(actions.utils, "run_shell", return_value=0), \
         mock.patch.object(actions, "_detect_app_config", return_value=fake_config), \
         mock.patch.object(actions, "_write_app_env") as we, \
         mock.patch.object(actions, "_build_and_start_app", return_value=True), \
         mock.patch.object(actions, "_sync_app_coffre"):
        ok, detail = actions._seed_one_app(console, "git@github.com:Foo/bar.git", "bar")
    assert ok is True
    we.assert_called_once()    # .env généré (cas B)


def test_seed_one_app_case_c_preserves_env(tmp_path, monkeypatch):
    """CAS C : dossier présent AVEC .env → pull + build + up, .env NON régégénéré."""
    apps_dir = tmp_path / "APPS"
    app = apps_dir / "bar"
    app.mkdir(parents=True)
    (app / ".env").write_text("API_PORT=9999\n")  # .env présent
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", apps_dir)
    console = mock.Mock()
    with mock.patch.object(actions.utils, "run_shell", return_value=0) as rs, \
         mock.patch.object(actions, "_write_app_env") as we, \
         mock.patch.object(actions, "app_make", return_value=0) as am, \
         mock.patch.object(actions, "_health_check", return_value=True):
        ok, detail = actions._seed_one_app(console, "git@github.com:Foo/bar.git", "bar")
    assert ok is True
    we.assert_not_called()     # .env PRÉSERVÉ (point clé du CAS C)
    assert am.call_count == 2  # build + up


def test_seed_one_app_clone_failure(tmp_path, monkeypatch):
    """Échec clone → (False, raison), pas d'exception."""
    apps_dir = tmp_path / "APPS"
    apps_dir.mkdir()
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", apps_dir)
    console = mock.Mock()
    with mock.patch.object(actions, "_clone_app", return_value=False), \
         mock.patch.object(actions, "disk_guard", return_value=True):
        ok, detail = actions._seed_one_app(console, "git@github.com:Foo/bar.git", "bar")
    assert ok is False
    assert "clone" in detail.lower()


def test_seed_one_app_dry_run(tmp_path, monkeypatch):
    """Dry-run : aucune sous-fonction exécutée, retour (True, simulé)."""
    apps_dir = tmp_path / "APPS"
    apps_dir.mkdir()
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", apps_dir)
    console = mock.Mock()
    with mock.patch.object(actions, "_clone_app") as cl, \
         mock.patch.object(actions, "_detect_app_config") as dc:
        ok, detail = actions._seed_one_app(console, "git@github.com:Foo/bar.git", "bar", dry_run=True)
    assert ok is True
    cl.assert_not_called()
    dc.assert_not_called()


def test_seed_one_app_git_pull_fails(tmp_path, monkeypatch):
    """CAS C : git pull en échec (rc=1) → (False, 'git pull failed'), app_make NON appelé."""
    apps_dir = tmp_path / "APPS"
    app = apps_dir / "bar"
    app.mkdir(parents=True)
    (app / ".env").write_text("API_PORT=9999\n")  # CAS C (.env présent)
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", apps_dir)
    console = mock.Mock()
    with mock.patch.object(actions.utils, "run_shell", return_value=1), \
         mock.patch.object(actions, "app_make") as am, \
         mock.patch.object(actions, "_health_check"):
        ok, detail = actions._seed_one_app(console, "git@github.com:Foo/bar.git", "bar")
    assert ok is False
    assert detail == "git pull failed"
    am.assert_not_called()


# ── _seed_prune ──
def _make_app_info(name):
    info = mock.Mock()
    info.name = name
    return info


def test_seed_prune_nothing_to_remove(monkeypatch):
    """Aucune app non-listée → 0 suppression, pas de prompt."""
    monkeypatch.setattr(actions.hub_state, "get_active_apps",
                        lambda: [_make_app_info("exemple-app"), _make_app_info("canari")])
    console = mock.Mock()
    n = actions._seed_prune(console, {"exemple-app", "canari"}, vault=None, dry_run=True)
    assert n == 0


def test_seed_prune_confirmed(monkeypatch):
    """Apps non-listées + confirmation Oui → suppression via action_remove_app."""
    monkeypatch.setattr(actions.hub_state, "get_active_apps",
                        lambda: [_make_app_info("exemple-app"), _make_app_info("vikunja"), _make_app_info("old")])
    monkeypatch.setattr("builtins.input", lambda *a: "o")
    console = mock.Mock()
    with mock.patch.object(actions, "action_remove_app") as rm:
        n = actions._seed_prune(console, {"exemple-app"}, vault=None, dry_run=True)
    assert n == 2
    removed = {c.args[1] for c in rm.call_args_list}
    assert removed == {"vikunja", "old"}


def test_seed_prune_refused(monkeypatch):
    """Confirmation Non → 0 suppression."""
    monkeypatch.setattr(actions.hub_state, "get_active_apps",
                        lambda: [_make_app_info("exemple-app"), _make_app_info("vikunja")])
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    console = mock.Mock()
    with mock.patch.object(actions, "action_remove_app") as rm:
        n = actions._seed_prune(console, {"exemple-app"}, vault=None, dry_run=True)
    assert n == 0
    rm.assert_not_called()


# ── action_seed ──
def _seed_yaml(tmp_path, apps_list):
    from pathlib import Path
    import yaml
    p = tmp_path / "seed.yaml"
    p.write_text(yaml.dump({"apps": apps_list}))
    return p


def test_action_seed_hub_down_aborts(tmp_path, monkeypatch):
    """Précheck hub KO → abort immédiat, code 1, aucune app déployée."""
    seed = _seed_yaml(tmp_path, ["git@github.com:Foo/bar.git"])
    console = mock.Mock()
    with mock.patch.object(actions, "_health_check", return_value=False) as hc, \
         mock.patch.object(actions, "_seed_one_app") as soa:
        rc = actions.action_seed(console, str(seed), dry_run=False)
    assert rc == 1
    soa.assert_not_called()


def test_action_seed_best_effort_continues(tmp_path, monkeypatch):
    """Une app en échec ne stoppe pas les autres. Code de sortie = 1."""
    seed = _seed_yaml(tmp_path, [
        "git@github.com:Foo/ok.git",
        "git@github.com:Foo/bad.git",
        "git@github.com:Foo/ok2.git",
    ])
    console = mock.Mock()

    def fake_one(c, url, slug, *, dry_run=False):
        return (True, "ok") if "bad" not in slug else (False, "build failed")

    with mock.patch.object(actions, "_health_check", return_value=True), \
         mock.patch.object(actions, "_seed_one_app", side_effect=fake_one):
        rc = actions.action_seed(console, str(seed), dry_run=False)
    assert rc == 1  # au moins un échec


def test_action_seed_all_ok_returns_zero(tmp_path, monkeypatch):
    """Toutes les apps réussissent → code 0."""
    seed = _seed_yaml(tmp_path, ["git@github.com:Foo/a.git", "git@github.com:Foo/b.git"])
    console = mock.Mock()
    with mock.patch.object(actions, "_health_check", return_value=True), \
         mock.patch.object(actions, "_seed_one_app", return_value=(True, "ok")):
        rc = actions.action_seed(console, str(seed), dry_run=False)
    assert rc == 0


def test_action_seed_dry_run_skips_health(tmp_path, monkeypatch):
    """Dry-run : pas de précheck hub réel."""
    seed = _seed_yaml(tmp_path, ["git@github.com:Foo/a.git"])
    console = mock.Mock()
    with mock.patch.object(actions, "_health_check") as hc, \
         mock.patch.object(actions, "_seed_one_app", return_value=(True, "simulé")):
        rc = actions.action_seed(console, str(seed), dry_run=True)
    assert rc == 0
    hc.assert_not_called()  # précheck skippé en dry-run


def test_action_seed_prune_called(tmp_path, monkeypatch):
    """Flag --prune → _seed_prune est appelé."""
    seed = _seed_yaml(tmp_path, ["git@github.com:Foo/a.git"])
    console = mock.Mock()
    with mock.patch.object(actions, "_health_check", return_value=True), \
         mock.patch.object(actions, "_seed_one_app", return_value=(True, "ok")), \
         mock.patch.object(actions, "_seed_prune", return_value=0) as sp, \
         mock.patch.object(actions, "_get_vault", return_value=mock.Mock()):
        actions.action_seed(console, str(seed), prune=True, dry_run=False)
    sp.assert_called_once()


def test_action_seed_missing_file(tmp_path):
    """Fichier inexistant → code 1, message d'erreur."""
    console = mock.Mock()
    rc = actions.action_seed(console, str(tmp_path / "nexiste.yaml"), dry_run=True)
    assert rc == 1


def test_action_seed_empty_apps_returns_zero(tmp_path, monkeypatch):
    """apps: [] → 'Aucune app à déployer', code 0."""
    seed = _seed_yaml(tmp_path, [])
    console = mock.Mock()
    with mock.patch.object(actions, "_health_check", return_value=True), \
         mock.patch.object(actions, "_seed_one_app") as soa:
        rc = actions.action_seed(console, str(seed), dry_run=False)
    assert rc == 0
    soa.assert_not_called()


# ── action_seed_export ──────────────────────────────────────────

def test_action_seed_export_generates_yaml(tmp_path, monkeypatch):
    """Vérifie que le YAML généré contient les URLs attendues."""
    apps = tmp_path / "APPS"
    apps.mkdir()
    for slug in ("app-a", "app-b"):
        d = apps / slug
        d.mkdir()
        (d / ".git").mkdir()

    monkeypatch.setattr(actions.hub_state, "APPS_DIR", apps)

    # Mock subprocess.run pour simuler git remote
    git_urls = {
        "app-a": "git@github.com:user/app-a.git",
        "app-b": "git@github.com:user/app-b.git",
    }
    orig_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[0] == "git" and cmd[-1] == "origin":
            slug = Path(cmd[2]).name
            url = git_urls.get(slug, "")
            return subprocess.CompletedProcess(cmd, 0, stdout=url.encode(), stderr=b"")
        return orig_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    out_path = tmp_path / "seed-test.yaml"
    console = Console()

    rc = actions.action_seed_export(console, output_path=str(out_path), force=False)
    assert rc == 0

    content = out_path.read_text()
    assert "git@github.com:user/app-a.git" in content
    assert "git@github.com:user/app-b.git" in content
    assert content.strip().startswith("#")
    assert "apps:" in content


def test_action_seed_export_empty_apps_returns_1(tmp_path, monkeypatch):
    """APPS/ vide → retourne 1, pas de fichier créé."""
    apps = tmp_path / "APPS"
    apps.mkdir()
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", apps)

    out_path = tmp_path / "seed-empty.yaml"
    console = Console()

    rc = actions.action_seed_export(console, output_path=str(out_path), force=False)
    assert rc == 1
    assert not out_path.exists()


def test_action_seed_export_refuses_overwrite(tmp_path, monkeypatch):
    """Fichier existant sans --force → retourne 1, fichier inchangé."""
    apps = tmp_path / "APPS"
    apps.mkdir()
    app = apps / "app-x"
    app.mkdir()
    (app / ".git").mkdir()
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", apps)

    def fake_run(cmd, **kwargs):
        if cmd[0] == "git" and cmd[-1] == "origin":
            return subprocess.CompletedProcess(cmd, 0, stdout=b"git@github.com:user/app-x.git", stderr=b"")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    out_path = tmp_path / "existing.yaml"
    out_path.write_text("original content")

    console = Console()

    rc = actions.action_seed_export(console, output_path=str(out_path), force=False)
    assert rc == 1
    assert out_path.read_text() == "original content"


def test_action_seed_export_force_overwrites(tmp_path, monkeypatch):
    """Fichier existant avec --force → écrase le fichier."""
    apps = tmp_path / "APPS"
    apps.mkdir()
    app = apps / "app-x"
    app.mkdir()
    (app / ".git").mkdir()
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", apps)

    def fake_run(cmd, **kwargs):
        if cmd[0] == "git" and cmd[-1] == "origin":
            return subprocess.CompletedProcess(cmd, 0, stdout=b"git@github.com:user/app-x.git", stderr=b"")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    out_path = tmp_path / "existing.yaml"
    out_path.write_text("original content")

    console = Console()

    rc = actions.action_seed_export(console, output_path=str(out_path), force=True)
    assert rc == 0
    assert "git@github.com:user/app-x.git" in out_path.read_text()


def test_action_seed_export_default_filename(tmp_path, monkeypatch):
    """Sans output_path → génère le nom par défaut seed-export-YYYY-MM-DD.yaml."""
    apps = tmp_path / "APPS"
    apps.mkdir()
    app = apps / "app-x"
    app.mkdir()
    (app / ".git").mkdir()
    monkeypatch.setattr(actions.hub_state, "APPS_DIR", apps)
    monkeypatch.setattr(actions.hub_state, "PARC_ROOT", tmp_path)

    def fake_run(cmd, **kwargs):
        if cmd[0] == "git" and cmd[-1] == "origin":
            return subprocess.CompletedProcess(cmd, 0, stdout=b"git@github.com:user/app-x.git", stderr=b"")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    console = Console()
    rc = actions.action_seed_export(console)
    assert rc == 0

    # Vérifier qu'un fichier seed-export-*.yaml a été créé
    exports = list(tmp_path.glob("seed-export-*.yaml"))
    assert len(exports) == 1
    assert exports[0].name.startswith("seed-export-")
    assert exports[0].name.endswith(".yaml")


# ── Task 6 : ports réels dans résumé + OIDC_ISSUER (correctif changement de port) ──


def test_summary_urls_uses_configured_ports(tmp_path, monkeypatch):
    """Le résumé affiche les ports du .env, pas 80/8000 en dur."""
    env_dir = tmp_path / "HUB_base"
    env_dir.mkdir()
    (env_dir / ".env").write_text(
        "HUB_FRONTEND_PORT=18080\nHUB_HTTPS_PORT=18443\nHUB_API_PORT=18000\n")
    # HUB_DIR = dossier contenant le .env (cf. _mock_env_path)
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", env_dir)
    urls = dict(actions._summary_urls("192.0.2.7"))
    assert urls["Hub (HTTPS)"] == "https://192.0.2.7:18443"
    assert urls["Hub (HTTP)"] == "http://192.0.2.7:18080"
    assert urls["API"] == "http://192.0.2.7:18000"


def test_summary_urls_defaults_when_env_empty(tmp_path, monkeypatch):
    """Sans .env renseigné : https sans :443, API sur :8000."""
    env_dir = tmp_path / "HUB_base"
    env_dir.mkdir()
    (env_dir / ".env").write_text("")
    # HUB_DIR = dossier contenant le .env (cf. _mock_env_path)
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", env_dir)
    urls = dict(actions._summary_urls("192.0.2.7"))
    assert urls["Hub (HTTPS)"] == "https://192.0.2.7"
    # Défaut 80 → port implicite (http sans port), cf. spec _summary_urls
    assert urls["Hub (HTTP)"] == "http://192.0.2.7"
    assert urls["API"] == "http://192.0.2.7:8000"


def test_generate_dotenv_issuer_matches_api_port(monkeypatch, tmp_path):
    """OIDC_ISSUER du template = http://{host_ip}:{HUB_API_PORT du .env généré}.

    Adaptation à la mécanique réelle (pas de helper _generate_dotenv_content) :
    on génère le .env dans un HUB_DIR temporaire et on compare les deux valeurs
    écrites — le contrat est la cohérence issuer ↔ HUB_API_PORT.
    """
    hub_dir = _mock_env_path(monkeypatch, tmp_path, exists=False)
    console = _mock_console()
    monkeypatch.setattr(actions, "_detect_host_ip", lambda: "192.0.2.7")
    result = actions._generate_dotenv(console, force=True, dry_run=False)
    assert result is not None
    content = (hub_dir / ".env").read_text()
    issuer_line = next(l for l in content.splitlines() if l.startswith("OIDC_ISSUER="))
    api_port_line = next(l for l in content.splitlines() if l.startswith("HUB_API_PORT="))
    port = api_port_line.split("=", 1)[1].strip()
    assert issuer_line == f"OIDC_ISSUER=http://192.0.2.7:{port}"


# ── Tolérance detached HEAD lors de `hubctl update` ──
# Un déploiement posé par copie/export (pas par clone) laisse HEAD détaché :
# `git pull` y échoue avec « You are not currently on a branch » et tout le
# cycle d'update part en rollback (incident prod 2026-09-14).


def _mkrepo(tmp_path, name, *, bare=False):
    repo = tmp_path / name
    args = ["git", "init", "--quiet"]
    if bare:
        args.append("--bare")
    args += ["-b", "main", str(repo)]
    subprocess.run(args, check=True, capture_output=True)
    return repo


def _git_run(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _git_out(repo, *args):
    r = _git_run(repo, *args)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _commit(repo, filename, content, msg):
    (repo / filename).write_text(content)
    _git_run(repo, "add", ".")
    r = _git_run(repo, "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-qm", msg)
    assert r.returncode == 0, r.stderr
    return _git_out(repo, "rev-parse", "HEAD")


def _init_remote_and_clone(tmp_path):
    """Dépôt bare + source avec 1 commit sur main ; retourne (bare, src, sha)."""
    bare = _mkrepo(tmp_path, "remote.git", bare=True)
    src = _mkrepo(tmp_path, "src")
    sha = _commit(src, "f.txt", "v1\n", "v1")
    _git_run(src, "remote", "add", "origin", str(bare))
    _git_run(src, "push", "-q", "origin", "main")
    return bare, src, sha


def test_ensure_branch_reattaches_detached_head(tmp_path):
    bare, src, sha = _init_remote_and_clone(tmp_path)
    repo = tmp_path / "deploy"
    subprocess.run(["git", "clone", "--quiet", str(bare), str(repo)],
                   check=True, capture_output=True)
    _git_run(repo, "checkout", "--detach")   # état prod : HEAD détaché
    _git_run(repo, "branch", "-D", "main")   # branche locale absente
    assert _git_out(repo, "branch", "--show-current") == ""

    actions._ensure_branch_attached(repo)

    assert _git_out(repo, "branch", "--show-current") == "main"
    assert _git_out(repo, "rev-parse", "HEAD") == sha  # le commit déployé fait foi


def test_ensure_branch_noop_when_on_branch(tmp_path):
    bare, src, sha = _init_remote_and_clone(tmp_path)
    repo = tmp_path / "deploy"
    subprocess.run(["git", "clone", "--quiet", str(bare), str(repo)],
                   check=True, capture_output=True)
    assert _git_out(repo, "branch", "--show-current") == "main"

    actions._ensure_branch_attached(repo)

    assert _git_out(repo, "branch", "--show-current") == "main"
    assert _git_out(repo, "rev-parse", "HEAD") == sha


def test_ensure_branch_sets_missing_upstream(tmp_path):
    """Repo déployé SANS clone (init + fetch) : aucune config branch.main —
    le helper doit la restaurer pour que le pull sache quoi tirer."""
    bare, src, sha = _init_remote_and_clone(tmp_path)
    repo = _mkrepo(tmp_path, "deploy")
    _commit(repo, "f.txt", "v1\n", "v1")
    _git_run(repo, "remote", "add", "origin", str(bare))
    _git_run(repo, "fetch", "--quiet", "origin")
    _git_run(repo, "checkout", "--detach")
    assert _git_out(repo, "branch", "--show-current") == ""

    actions._ensure_branch_attached(repo)

    assert _git_out(repo, "branch", "--show-current") == "main"
    assert _git_out(repo, "rev-parse", "--abbrev-ref",
                    "--symbolic-full-name", "main@{u}") == "origin/main"


def test_update_target_reattaches_detached_head_before_pull(tmp_path, monkeypatch):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    order = []
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", tmp_path)
    monkeypatch.setattr(actions, "_git_up_to_date", lambda *a, **k: False)
    monkeypatch.setattr(actions, "_git_previous_commit", lambda *a, **k: "abc123")
    monkeypatch.setattr(actions, "_db_snapshot", lambda *a, **k: "snap.sql")
    monkeypatch.setattr(actions, "_health_check", lambda *a, **k: True)

    def fake_attach(path, console=None, **kw):
        order.append("attach")
        return "main"
    monkeypatch.setattr(actions, "_ensure_branch_attached", fake_attach)

    def fake_run_shell(cmd, **kwargs):
        order.append("pull" if "pull" in cmd else "other")
        return 0
    monkeypatch.setattr(actions.utils, "run_shell", fake_run_shell)

    assert actions._update_target(None, "hub", dry_run=False) is True
    assert "attach" in order
    assert order.index("attach") < order.index("pull")
