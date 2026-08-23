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
import subprocess
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
    _ws_git_state,
    _ws_worktree_clean,
    _ws_resolve_ref,
    _ws_git_restore,
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


# ── Helpers git du cycle atelier (--maj / --prod) ──


def _git(app: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(app), *args],
                          capture_output=True, text=True, timeout=15)


@pytest.fixture()
def git_app(tmp_path):
    """Mini-repo git réaliste : APPS/wstest cloné depuis un bare origin (1 commit)."""
    apps = tmp_path / "APPS"
    apps.mkdir()
    app = apps / "wstest"
    app.mkdir()
    (app / "docker-compose.yml").write_text("services: {}\n")
    (app / ".env").write_text("API_PORT=9000\nFRONTEND_PORT=4200\nAPP_VERSION=0.1.0\n")
    _git(app, "init", "-q", "-b", "main")
    _git(app, "config", "user.email", "test@example.com")
    _git(app, "config", "user.name", "test")
    (app / "f.txt").write_text("v1\n")
    _git(app, "add", "-A")
    _git(app, "commit", "-qm", "v1")
    # origin.git vit dans APPS/ : c'est là que _origin_commit / test_branche_distante
    # le cherchent (git_app.parent / "origin.git")
    origin = apps / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(app), str(origin)],
                   capture_output=True, timeout=15)
    _git(app, "remote", "add", "origin", str(origin))
    _git(app, "fetch", "-q", "origin")
    _git(app, "branch", "--set-upstream-to=origin/main", "main")
    return app


def _origin_commit(git_app: Path, message: str) -> str:
    """Pousse un commit supplémentaire sur origin (depuis un clone jetable), retourne son sha."""
    work = git_app.parent / f"work-{message.replace(' ', '')}"
    subprocess.run(["git", "clone", "-q", str(git_app.parent / "origin.git"), str(work)],
                   capture_output=True, timeout=15)
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "test")
    (work / "f.txt").write_text(f"{message}\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", message)
    _git(work, "push", "-q", "origin", "main")
    # _ws_resolve_ref lit origin/main côté git_app : il faut le rafraîchir
    # (dans le vrai flow, hubctl fetch avant de résoudre)
    _git(git_app, "fetch", "-q", "origin")
    sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    return sha


class TestWsGitState:
    def test_ok_sur_repo_avec_origin(self, git_app):
        st = _ws_git_state(git_app)
        assert st["ok"] is True
        assert st["branch"] == "main"
        assert st["head"] and len(st["head"]) >= 7

    def test_ko_sans_remote(self, git_app):
        _git(git_app, "remote", "remove", "origin")
        assert _ws_git_state(git_app)["ok"] is False

    def test_ko_pas_un_repo(self, tmp_path):
        d = tmp_path / "pasunrepo"
        d.mkdir()
        assert _ws_git_state(d)["ok"] is False


class TestWsWorktreeClean:
    def test_clean_apres_clone(self, git_app):
        assert _ws_worktree_clean(git_app) is True

    def test_sale_si_fichier_modifie(self, git_app):
        (git_app / "f.txt").write_text("modif locale\n")
        assert _ws_worktree_clean(git_app) is False

    def test_sale_si_commit_non_pousse(self, git_app):
        (git_app / "g.txt").write_text("new\n")
        _git(git_app, "add", "-A")
        _git(git_app, "commit", "-qm", "local ahead")
        assert _ws_worktree_clean(git_app) is False


class TestWsResolveRef:
    def test_main_resout_origin_main(self, git_app):
        new_sha = _origin_commit(git_app, "v2")
        assert _ws_resolve_ref(git_app, "main") == new_sha

    def test_branche_distante(self, git_app):
        work = git_app.parent / "work-branch"
        subprocess.run(["git", "clone", "-q", str(git_app.parent / "origin.git"), str(work)],
                       capture_output=True, timeout=15)
        _git(work, "config", "user.email", "test@example.com")
        _git(work, "config", "user.name", "test")
        _git(work, "checkout", "-q", "-b", "feat/x")
        (work / "f.txt").write_text("feat\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-qm", "feat commit")
        _git(work, "push", "-q", "origin", "feat/x")
        _git(git_app, "fetch", "-q", "origin")
        sha = _git(work, "rev-parse", "HEAD").stdout.strip()
        assert _ws_resolve_ref(git_app, "feat/x") == sha

    def test_ref_introuvable(self, git_app):
        assert _ws_resolve_ref(git_app, "nexiste_pas") is None


class TestWsGitRestore:
    def test_restaure_branche_et_commit(self, git_app):
        prev_commit = _git(git_app, "rev-parse", "HEAD").stdout.strip()
        # partir en detached sur un autre commit
        _git(git_app, "checkout", "-q", "--detach", "HEAD~0")
        _ws_git_restore(None, git_app, "main", prev_commit, dry_run=False)
        assert _git(git_app, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
        assert _git(git_app, "rev-parse", "HEAD").stdout.strip() == prev_commit

    def test_restaure_detached_si_branche_head(self, git_app):
        prev_commit = _git(git_app, "rev-parse", "HEAD").stdout.strip()
        _ws_git_restore(None, git_app, "HEAD", prev_commit, dry_run=False)
        assert _git(git_app, "rev-parse", "HEAD").stdout.strip() == prev_commit
