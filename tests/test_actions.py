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
        "HOST_UID=",
    ]
    for var in required_vars:
        assert var in content, f"Variable obligatoire manquante: {var}"
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





# ── Task 5: _build_and_start_app + _sync_app_coffre ──





# ── _sync_app_coffre ──





# ── action_add_app (flow complet) ──

    # Pas d'exception = succès




    # Le dossier doit être supprimé
    # (selon l'implémentation, soit shutil.rmtree, soit un check)




# ── regressions: sync_app_coffre vers le coffre reel ──



    # Ne doit pas lever d'exception
