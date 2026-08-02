"""Tests du mécanisme workshop (ateliers Docker Compose -p).

Vérifie que :
1. _ws_project génère le bon nom de projet
2. _ws_env_path pointe vers APPS/X/.workshop.env
3. _ws_exists détecte l'existence d'un atelier
4. _ws_read_env lit la config atelier
5. _ws_compose_env fusionne os.environ + .workshop.env
6. _ws_used_ports collecte les ports occupés
"""
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from hubctl_cli import hub_state
from hubctl_cli.actions import (
    _ws_project,
    _ws_env_path,
    _ws_exists,
    _ws_read_env,
    _ws_compose_env,
    _ws_used_ports,
    _ws_session_ports,
)


class TestWsProject:
    """Noms de projet Docker Compose pour les ateliers."""

    def test_simple_slug(self):
        assert _ws_project("canari") == "canari-ws"

    def test_slug_with_dash(self):
        assert _ws_project("exemple-app") == "exemple-app-ws"

    def test_slug_with_uppercase(self):
        # Le slug est passé tel quel (la normalisation se fait en amont)
        assert _ws_project("exemple-app") == "exemple-app-ws"


class TestWsEnvPath:
    """Chemin du fichier .workshop.env."""

    def test_returns_app_dir_workshop_env(self):
        path = _ws_env_path("canari")
        assert path == hub_state.APPS_DIR / "canari" / ".workshop.env"

    def test_different_slugs_give_different_paths(self):
        p1 = _ws_env_path("canari")
        p2 = _ws_env_path("exemple-app")
        assert p1 != p2


class TestWsExists:
    """Détection d'existence d'un atelier via .workshop.env."""

    def test_returns_false_when_no_workshop_env(self, tmp_path, monkeypatch):
        # APPS_DIR vide → pas de .workshop.env
        monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path)
        assert _ws_exists("canari") is False

    def test_returns_true_when_workshop_env_exists(self, tmp_path, monkeypatch):
        app_dir = tmp_path / "canari"
        app_dir.mkdir()
        (app_dir / ".workshop.env").write_text("APP_SLUG=canari-ws\n")
        monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path)
        assert _ws_exists("canari") is True


class TestWsReadEnv:
    """Lecture de la config atelier."""

    def test_reads_workshop_env(self, tmp_path, monkeypatch):
        app_dir = tmp_path / "canari"
        app_dir.mkdir()
        (app_dir / ".workshop.env").write_text(
            "APP_SLUG=canari-ws\nAPI_PORT=10000\nFRONTEND_PORT=5200\n"
        )
        monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path)

        env = _ws_read_env("canari")
        assert env["APP_SLUG"] == "canari-ws"
        assert env["API_PORT"] == "10000"
        assert env["FRONTEND_PORT"] == "5200"

    def test_returns_empty_dict_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path)
        assert _ws_read_env("canari") == {}


class TestWsComposeEnv:
    """Construction de l'environnement pour docker compose -p."""

    def test_returns_none_when_no_workshop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path)
        assert _ws_compose_env("canari") is None

    def test_merges_os_env_with_workshop_env(self, tmp_path, monkeypatch):
        app_dir = tmp_path / "canari"
        app_dir.mkdir()
        (app_dir / ".workshop.env").write_text(
            "APP_SLUG=canari-ws\nAPI_PORT=10000\n"
        )
        monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path)

        env = _ws_compose_env("canari")
        assert env is not None
        # Workshop override
        assert env["APP_SLUG"] == "canari-ws"
        assert env["API_PORT"] == "10000"
        # os.environ hérité
        assert "PATH" in env

    def test_workshop_overrides_os_env(self, tmp_path, monkeypatch):
        """Les valeurs de .workshop.env prennent priorité sur os.environ."""
        app_dir = tmp_path / "canari"
        app_dir.mkdir()
        (app_dir / ".workshop.env").write_text("APP_SLUG=canari-ws\n")
        monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path)
        monkeypatch.setenv("APP_SLUG", "canari")  # valeur prod

        env = _ws_compose_env("canari")
        assert env is not None
        assert env["APP_SLUG"] == "canari-ws"  # workshop gagne


class TestWsUsedPorts:
    """Collecte des ports déjà utilisés par les ateliers."""

    def test_returns_empty_when_no_workshops(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path)
        assert _ws_used_ports() == set()

    def test_collects_ports_from_existing_workshops(self, tmp_path, monkeypatch):
        for slug, api, fe in [("canari", "10000", "5200"), ("exemple-app", "10001", "5202")]:
            d = tmp_path / slug
            d.mkdir()
            (d / ".workshop.env").write_text(
                f"APP_SLUG={slug}-ws\nAPI_PORT={api}\nFRONTEND_PORT={fe}\n"
                f"FRONTEND_HTTPS_PORT={int(fe)+1}\n"
            )
        monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path)
        # get_apps() doit lister les apps
        monkeypatch.setattr(hub_state, "get_apps", lambda: [
            type("A", (), {"name": "canari"})(),
            type("A", (), {"name": "exemple-app"})(),
        ])

        used = _ws_used_ports()
        assert 10000 in used
        assert 10001 in used
        assert 5200 in used
        assert 5202 in used
        assert 5201 in used  # HTTPS port (5200 + 1)
        assert 5203 in used  # HTTPS port (5202 + 1)


class TestWsSessionPorts:
    """Le tracker de ports de session (pour dry-run)."""

    def test_session_ports_is_a_set(self):
        assert isinstance(_ws_session_ports, set)

    def test_can_clear(self):
        _ws_session_ports.add(9999)
        _ws_session_ports.clear()
        assert len(_ws_session_ports) == 0
