import tempfile
from pathlib import Path
from hubctl_cli import env_manager


ENV_CONTENT = """# PostgreSQL
POSTGRES_USER=hubadmin
POSTGRES_PASSWORD=secret123
POSTGRES_DB=hub_auth

# Hub API
APP_ENV=production
HUB_HOST=192.0.2.10
"""


def test_read_env_parses_keys():
    path = _write_temp(ENV_CONTENT)
    result = env_manager.read_env(path)
    assert result["POSTGRES_USER"] == "hubadmin"
    assert result["POSTGRES_PASSWORD"] == "secret123"
    assert result["APP_ENV"] == "production"
    assert result["HUB_HOST"] == "192.0.2.10"


def test_read_env_ignores_comments_and_blanks():
    path = _write_temp("# comment\n\nKEY=value\n")
    result = env_manager.read_env(path)
    assert result == {"KEY": "value"}


def test_write_env_updates_existing_key():
    path = _write_temp(ENV_CONTENT)
    env_manager.write_env(path, {"APP_ENV": "development"})
    result = env_manager.read_env(path)
    assert result["APP_ENV"] == "development"
    assert result["POSTGRES_USER"] == "hubadmin"
    content = path.read_text()
    assert "# Hub API" in content


def test_write_env_adds_new_key():
    path = _write_temp(ENV_CONTENT)
    env_manager.write_env(path, {"NEW_KEY": "new_value"})
    result = env_manager.read_env(path)
    assert result["NEW_KEY"] == "new_value"
    assert result["APP_ENV"] == "production"


def test_write_env_handles_missing_file():
    with tempfile.NamedTemporaryFile(suffix=".env", delete=False) as f:
        p = Path(f.name)
    try:
        env_manager.write_env(p, {"KEY": "val"})
        assert p.read_text().strip() == "KEY=val"
    finally:
        p.unlink()


def test_env_defaults_production():
    d = env_manager.env_defaults("production")
    assert d["APP_ENV"] == "production"
    assert d["DOCKER_SCAN_ENABLED"] == "false"
    assert d["HUB_HTTPS_ENABLED"] == "true"


def test_env_defaults_development():
    d = env_manager.env_defaults("development")
    assert d["APP_ENV"] == "development"
    assert d["DOCKER_SCAN_ENABLED"] == "true"
    assert d["HUB_HTTPS_ENABLED"] == "false"


def _write_temp(content: str) -> Path:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False)
    f.write(content)
    f.close()
    return Path(f.name)
