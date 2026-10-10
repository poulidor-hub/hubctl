"""Tests du mécanisme workshop (ateliers Docker Compose -p).

Vérifie que :
1. _ws_project génère le bon nom de projet
2. _ws_env_path pointe vers APPS/X/.workshop.env
3. _ws_exists détecte l'existence d'un atelier
4. _ws_read_env lit la config atelier
5. _ws_compose_env fusionne os.environ + .workshop.env
6. _ws_used_ports collecte les ports occupés
"""
import json
import os
import subprocess
from pathlib import Path
from unittest import mock
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
    _ws_used_subnets,
    _find_free_subnet,
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
        # hubctl 0.7.2 : le nom de projet compose est entièrement slugifié
        # (lowercase, `_` → `-`) pour que la résolution des rôles JWT matche
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

    def test_ne_fuit_pas_env_hote(self, tmp_path, monkeypatch):
        """Parité HUB_base 3.47.3 : seules les clés de l'allowlist (accès docker,
        locales) + .workshop.env sont transmises au compose atelier.

        Incident 2026-09 : HUB_HTTPS_ENABLED a deux sens opposés (hub = « le hub
        sert du HTTPS » ≠ app = « joindre l'API hub en HTTPS ») — une clé exportée
        dans le shell hôte écrasait le .env de l'app via l'interpolation compose
        et mettait l'atelier en « Mode dégradé » (SSL WRONG_VERSION_NUMBER)."""
        app_dir = tmp_path / "canari"
        app_dir.mkdir()
        (app_dir / ".workshop.env").write_text(
            "APP_SLUG=canari-ws\nAPI_PORT=10000\n"
        )
        monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path)
        monkeypatch.setenv("HUB_HTTPS_ENABLED", "true")
        monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
        monkeypatch.setenv("DOCKER_HOST", "unix:///run/docker.sock")

        env = _ws_compose_env("canari")
        assert env is not None
        assert "HUB_HTTPS_ENABLED" not in env
        assert "DATABASE_URL" not in env
        # allowlist (accès docker) + contrat .workshop.env intacts
        assert env["DOCKER_HOST"] == "unix:///run/docker.sock"
        assert env["APP_SLUG"] == "canari-ws"
        assert env["API_PORT"] == "10000"


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


class TestFindFreeSubnet:
    """Allocation d'un /24 dans le pool atelier (10.210.0.0/24 …)."""

    def test_first_free(self):
        assert _find_free_subnet(set()) == "10.210.0.0/24"

    def test_skips_used(self):
        used = {"10.210.0.0/24", "10.210.1.0/24"}
        assert _find_free_subnet(used) == "10.210.2.0/24"

    def test_skips_broad_overlap(self):
        # Un CIDR large qui recouvre tout le pool (10.0.0.0/8 ⊇ 10.210.x)
        # → aucun candidat, même si 10.210.x n'est pas listé littéralement.
        assert _find_free_subnet({"10.0.0.0/8"}) is None

    def test_exhausted_returns_none(self):
        used = {f"10.210.{i}.0/24" for i in range(64)}
        assert _find_free_subnet(used) is None

    def test_skips_malformed_cidr_garde_les_valides(self):
        # Fix D : un CIDR malformé est ignoré élément par élément, les valides
        # restent honorés (avant : une seule valeur boguée vidait tout `taken`
        # → allocation en overlap possible).
        assert _find_free_subnet({"bogus", "10.210.0.0/24"}) == "10.210.1.0/24"


class TestWsUsedSubnets:
    """Collecte des subnets déjà pris (réseaux docker + APP_SUBNET des ateliers)."""

    def test_collects_docker_and_workshops(self, tmp_path, monkeypatch):
        app = tmp_path / "canari"
        app.mkdir()
        (app / ".workshop.env").write_text(
            "APP_SLUG=canari-ws\nAPI_PORT=10000\nAPP_SUBNET=10.210.5.0/24\n"
        )
        monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path)
        monkeypatch.setattr(hub_state, "get_apps", lambda: [
            type("A", (), {"name": "canari"})(),
        ])

        def fake_run(cmd, *args, **kwargs):
            m = mock.Mock(returncode=0)
            if cmd[:3] == ["docker", "network", "ls"]:
                m.stdout = "netid1\nnetid2\n"
                return m
            if cmd[:3] == ["docker", "network", "inspect"]:
                m.stdout = json.dumps([
                    {"Name": "bridge", "IPAM": {"Config": [{"Subnet": "172.17.0.0/16"}]}},
                    {"Name": "x_default", "IPAM": {"Config": [{"Subnet": "172.18.0.0/16"}]}},
                ])
                return m
            return mock.Mock(returncode=1)

        with patch.object(subprocess, "run", side_effect=fake_run):
            used = _ws_used_subnets()
        assert "172.17.0.0/16" in used   # réseaux docker existants
        assert "172.18.0.0/16" in used
        assert "10.210.5.0/24" in used   # APP_SUBNET d'un atelier existant

    def test_docker_indisponible_retourne_workshops_seulement(self, tmp_path, monkeypatch):
        app = tmp_path / "canari"
        app.mkdir()
        (app / ".workshop.env").write_text("APP_SLUG=canari-ws\nAPP_SUBNET=10.210.7.0/24\n")
        monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path)
        monkeypatch.setattr(hub_state, "get_apps", lambda: [
            type("A", (), {"name": "canari"})(),
        ])
        with patch.object(subprocess, "run", return_value=mock.Mock(returncode=1)):
            assert _ws_used_subnets() == {"10.210.7.0/24"}


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

    def test_clean_avec_workshop_env_non_suivi(self, git_app):
        # .workshop.env = état interne hubctl, pas du travail local (parc réel :
        # non suivi et non ignoré dans les dépôts APPS/X)
        (git_app / ".workshop.env").write_text("APP_SLUG=wstest-ws\nWS_REF=main\n")
        assert _ws_worktree_clean(git_app) is True

    def test_sale_si_sans_upstream_commit_non_pousse_vs_origin(self, git_app):
        # Fix B : branche sans tracking (@{u} irrésolvable) → repli sur
        # origin/<branche> ; un commit non poussé doit quand même bloquer.
        _git(git_app, "branch", "--unset-upstream")
        (git_app / "g.txt").write_text("new\n")
        _git(git_app, "add", "-A")
        _git(git_app, "commit", "-qm", "local ahead sans tracking")
        assert _ws_worktree_clean(git_app) is False

    def test_clean_sans_upstream_sync_avec_origin_branche(self, git_app):
        # Fix B (compagnon du précédent, pince le chemin positif du repli) :
        # sans tracking mais sync avec origin/<branche> → clean.
        _git(git_app, "branch", "--unset-upstream")
        assert _ws_worktree_clean(git_app) is True

    def test_refuse_si_ni_upstream_ni_origin_branche(self, git_app):
        # Fix B : fail-closed — ni @{u} ni origin/<branche> résolvables →
        # on refuse l'opération (l'opérateur corrige le tracking), on ne
        # fail-open PAS.
        _git(git_app, "checkout", "-q", "-b", "orpheline")  # jamais poussée
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


# ── _update_workshop (--maj) ──

from hubctl_cli import actions as actions_mod
from hubctl_cli import env_manager
from hubctl_cli.actions import _update_workshop

# Pattern des tests --maj/--prod : GIT RÉEL (repo tmp_path, commandes réellement
# exécutées → rev-parse vérifiable), DOCKER SIMULÉ (aucun conteneur lancé).
_real_run_shell = actions_mod.utils.run_shell


def _docker_ok(cmd, **kwargs):
    """run_shell side-effect : commandes docker simulées OK, git réellement exécuté."""
    if cmd[:1] == ["docker"]:
        return 0
    return _real_run_shell(cmd, **kwargs)


def _docker_build_ko(cmd, **kwargs):
    """run_shell side-effect : build docker KO, reste identique à _docker_ok."""
    if cmd[:2] == ["docker", "compose"] and "build" in cmd:
        return 1
    return _docker_ok(cmd, **kwargs)


def _ws_setup(git_app: Path, monkeypatch, *, ws_ref="main", ws_commit=None):
    """Prépare .workshop.env + patche hub_state.APPS_DIR vers le tmp_path."""
    apps_root = git_app.parent
    if ws_commit is None:
        ws_commit = _git(git_app, "rev-parse", "HEAD").stdout.strip()
    (git_app / ".workshop.env").write_text(
        f"APP_SLUG=wstest-ws\nAPI_PORT=10000\nFRONTEND_PORT=5200\n"
        f"FRONTEND_HTTPS_PORT=5201\nWS_REF={ws_ref}\nWS_COMMIT={ws_commit}\n"
    )
    monkeypatch.setattr(actions_mod.hub_state, "APPS_DIR", apps_root)
    monkeypatch.setattr(actions_mod.hub_state, "check_docker", lambda: True)


class TestUpdateWorkshopPrechecks:
    def test_refuse_atelier_inexistant(self, tmp_path, monkeypatch):
        apps = tmp_path / "APPS"; apps.mkdir()
        monkeypatch.setattr(actions_mod.hub_state, "APPS_DIR", apps)
        monkeypatch.setattr(actions_mod.hub_state, "check_docker", lambda: True)
        assert _update_workshop(None, "wstest") is False

    def test_refuse_app_sans_remote(self, tmp_path, monkeypatch):
        app = tmp_path / "APPS" / "wstest"; app.mkdir(parents=True)
        (app / ".workshop.env").write_text("APP_SLUG=wstest-ws\nWS_REF=n/a\nWS_COMMIT=n/a\n")
        monkeypatch.setattr(actions_mod.hub_state, "APPS_DIR", tmp_path / "APPS")
        monkeypatch.setattr(actions_mod.hub_state, "check_docker", lambda: True)
        assert _update_workshop(None, "wstest") is False

    def test_refuse_worktree_sale(self, git_app, monkeypatch):
        _ws_setup(git_app, monkeypatch)
        (git_app / "f.txt").write_text("sale\n")
        assert _update_workshop(None, "wstest") is False

    def test_idempotent_sans_rebuild(self, git_app, monkeypatch):
        _ws_setup(git_app, monkeypatch)  # WS_COMMIT == HEAD == origin/main
        with patch.object(actions_mod.utils, "run_shell", side_effect=_docker_ok) as rs:
            assert _update_workshop(None, "wstest", "main") is True
        docker_calls = [c for c in rs.call_args_list
                        if c.args and c.args[0][:1] == ["docker"]]
        assert docker_calls == []  # rien à faire : aucun build/up


class TestUpdateWorkshopNominal:
    def test_maj_complete_et_persiste(self, git_app, monkeypatch):
        _ws_setup(git_app, monkeypatch)
        new_sha = _origin_commit(git_app, "v2 maj atelier")
        with patch.object(actions_mod.utils, "run_shell", side_effect=_docker_ok) as rs, \
             patch.object(actions_mod, "_health_check", return_value=True) as hc:
            assert _update_workshop(None, "wstest", "main") is True
        # git RÉEL : retour propre sur main au commit testé
        assert _git(git_app, "rev-parse", "HEAD").stdout.strip() == new_sha
        assert _git(git_app, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
        # .workshop.env mis à jour
        ws = env_manager.read_env(git_app / ".workshop.env")
        assert ws["WS_REF"] == "main" and ws["WS_COMMIT"] == new_sha
        # health appelé sur le port atelier
        assert hc.call_args.kwargs.get("port_override") == "10000"
        # build avec slug -ws dans l'env compose
        build_calls = [c for c in rs.call_args_list
                       if c.args and c.args[0][:2] == ["docker", "compose"]
                       and "build" in c.args[0]]
        assert build_calls
        assert build_calls[0].kwargs.get("env", {}).get("APP_SLUG") == "wstest-ws"

    def test_maj_sur_branche_laisse_detached(self, git_app, monkeypatch):
        _ws_setup(git_app, monkeypatch)
        # pousser feat/y sur origin
        work = git_app.parent / "work-feat"
        subprocess.run(["git", "clone", "-q", str(git_app.parent / "origin.git"), str(work)],
                       capture_output=True, timeout=15)
        _git(work, "config", "user.email", "t@t"); _git(work, "config", "user.name", "t")
        _git(work, "checkout", "-q", "-b", "feat/y")
        (work / "f.txt").write_text("feat\n"); _git(work, "add", "-A"); _git(work, "commit", "-qm", "feat y")
        _git(work, "push", "-q", "origin", "feat/y")
        feat_sha = _git(work, "rev-parse", "HEAD").stdout.strip()
        with patch.object(actions_mod.utils, "run_shell", side_effect=_docker_ok), \
             patch.object(actions_mod, "_health_check", return_value=True):
            assert _update_workshop(None, "wstest", "feat/y") is True
        # detached sur le commit testé (pas de retour branche pour ref != main)
        assert _git(git_app, "rev-parse", "HEAD").stdout.strip() == feat_sha
        assert _git(git_app, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "HEAD"
        ws = env_manager.read_env(git_app / ".workshop.env")
        assert ws["WS_REF"] == "feat/y" and ws["WS_COMMIT"] == feat_sha

    def test_ref_introuvable_ne_touche_rien(self, git_app, monkeypatch):
        _ws_setup(git_app, monkeypatch)
        prev = _git(git_app, "rev-parse", "HEAD").stdout.strip()
        with patch.object(actions_mod.utils, "run_shell", side_effect=_docker_ok):
            assert _update_workshop(None, "wstest", "branche/fantome") is False
        assert _git(git_app, "rev-parse", "HEAD").stdout.strip() == prev


class TestUpdateWorkshopRollback:
    def test_echec_build_rollback_git(self, git_app, monkeypatch):
        _ws_setup(git_app, monkeypatch)
        prev = _git(git_app, "rev-parse", "HEAD").stdout.strip()
        _origin_commit(git_app, "v2 casse build")
        with patch.object(actions_mod.utils, "run_shell", side_effect=_docker_build_ko):
            assert _update_workshop(None, "wstest", "main") is False
        # git RÉEL restauré sur main@prev (le rollback checkout/reset est réel)
        assert _git(git_app, "rev-parse", "HEAD").stdout.strip() == prev
        assert _git(git_app, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
        # .workshop.env NON mis à jour (WS_COMMIT == prev)
        ws = env_manager.read_env(git_app / ".workshop.env")
        assert ws["WS_COMMIT"] == prev

    def test_echec_health_rollback_git(self, git_app, monkeypatch):
        _ws_setup(git_app, monkeypatch)
        prev = _git(git_app, "rev-parse", "HEAD").stdout.strip()
        _origin_commit(git_app, "v2 casse health")
        with patch.object(actions_mod.utils, "run_shell", side_effect=_docker_ok), \
             patch.object(actions_mod, "_health_check", return_value=False):
            assert _update_workshop(None, "wstest", "main") is False
        assert _git(git_app, "rev-parse", "HEAD").stdout.strip() == prev

    def test_ctrl_c_pendant_maj_dry_run_rollback_en_dry_run(self, git_app, monkeypatch):
        """Fix A : Ctrl-C pendant un --maj --dry-run ne doit JAMAIS déclencher
        un vrai rebuild/up (le rollback Ctrl-C codait dry_run=False en dur →
        prod reconstruite pour un simple aperçu interrompu)."""
        _ws_setup(git_app, monkeypatch)
        interrupted = {"done": False}

        def ki_on_first_docker(cmd, **kwargs):
            if cmd[:1] == ["docker"]:
                if not interrupted["done"]:
                    interrupted["done"] = True
                    raise KeyboardInterrupt
                return 0
            return _real_run_shell(cmd, **kwargs)

        with patch.object(actions_mod.utils, "run_shell", side_effect=ki_on_first_docker) as rs:
            assert _update_workshop(None, "wstest", "main", dry_run=True) is False
        assert interrupted["done"]  # le build (dry-run) a bien été interrompu
        # Tout appel run_shell de l'exécution (rollback compris) doit porter
        # dry_run=True : aucun docker/git du rollback ne tourne pour de vrai.
        assert rs.call_args_list
        assert all(c.kwargs.get("dry_run") is True for c in rs.call_args_list)


# ── _ship_workshop (--prod) ──

from hubctl_cli.actions import _ship_workshop


class TestShipPrechecks:
    def test_refuse_atelier_inexistant(self, tmp_path, monkeypatch):
        apps = tmp_path / "APPS"; apps.mkdir()
        monkeypatch.setattr(actions_mod.hub_state, "APPS_DIR", apps)
        monkeypatch.setattr(actions_mod.hub_state, "check_docker", lambda: True)
        assert _ship_workshop(None, "wstest") is False

    def test_refuse_ref_non_main(self, git_app, monkeypatch):
        _ws_setup(git_app, monkeypatch, ws_ref="feat/x")
        with patch.object(actions_mod.utils, "run_shell", side_effect=_docker_ok) as rs, \
             patch.object(actions_mod, "app_make") as am:
            assert _ship_workshop(None, "wstest") is False
        rs.assert_not_called()
        am.assert_not_called()

    def test_refuse_worktree_sale(self, git_app, monkeypatch):
        _ws_setup(git_app, monkeypatch)
        (git_app / "f.txt").write_text("sale\n")
        with patch.object(actions_mod, "app_make"):
            assert _ship_workshop(None, "wstest") is False

    def test_refuse_atelier_pas_sain(self, git_app, monkeypatch):
        _ws_setup(git_app, monkeypatch)
        with patch.object(actions_mod, "_health_check", return_value=False), \
             patch.object(actions_mod, "app_make") as am:
            assert _ship_workshop(None, "wstest") is False
        am.assert_not_called()

    def test_refuse_main_avance_depuis_test(self, git_app, monkeypatch):
        _ws_setup(git_app, monkeypatch)  # WS_COMMIT = HEAD actuel
        new_sha = _origin_commit(git_app, "main a avance")  # origin/main avance
        prev = _git(git_app, "rev-parse", "HEAD").stdout.strip()
        with patch.object(actions_mod.utils, "run_shell", side_effect=_docker_ok), \
             patch.object(actions_mod, "app_make") as am, \
             patch.object(actions_mod, "_health_check", return_value=True):
            assert _ship_workshop(None, "wstest") is False
        am.assert_not_called()
        # git RÉEL : fetch/checkout/reset réels (side_effect laisse passer git),
        # garde-fou déclenché (origin/main != WS_COMMIT), puis git restauré à prev
        assert _git(git_app, "rev-parse", "HEAD").stdout.strip() == prev
        assert _git(git_app, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"


class TestShipNominal:
    def test_ship_succes_supprime_atelier(self, git_app, monkeypatch):
        # VERSION trackée sur main avant setup (bilan : 0.1.0 → 0.2.0) — laissée
        # non suivie, elle rendrait le worktree sale au pré-check --prod
        (git_app / "VERSION").write_text("0.2.0\n")
        _git(git_app, "add", "-A")
        _git(git_app, "commit", "-qm", "VERSION 0.2.0")
        _git(git_app, "push", "-q", "origin", "main")
        _ws_setup(git_app, monkeypatch)  # atelier testant main@HEAD, origin/main == HEAD
        prev = _git(git_app, "rev-parse", "HEAD").stdout.strip()
        with patch.object(actions_mod.utils, "run_shell", side_effect=_docker_ok), \
             patch.object(actions_mod, "app_make", return_value=0) as am, \
             patch.object(actions_mod, "_health_check", return_value=True):
            assert _ship_workshop(None, "wstest") is True
        # build + up prod via make, dans cet ordre
        targets = [c.args[1] for c in am.call_args_list]
        assert targets == ["build", "up"]
        # atelier supprimé : plus de .workshop.env (unlink RÉEL dans tmp_path ;
        # le down -v est simulé par _docker_ok)
        assert not (git_app / ".workshop.env").exists()
        # git toujours sur main@commit testé
        assert _git(git_app, "rev-parse", "HEAD").stdout.strip() == prev

    def test_ship_echec_build_rollback_et_atelier_conserve(self, git_app, monkeypatch):
        _ws_setup(git_app, monkeypatch)
        prev = _git(git_app, "rev-parse", "HEAD").stdout.strip()
        with patch.object(actions_mod.utils, "run_shell", side_effect=_docker_ok), \
             patch.object(actions_mod, "app_make", return_value=1), \
             patch.object(actions_mod, "_health_check", return_value=True):
            assert _ship_workshop(None, "wstest") is False
        assert _git(git_app, "rev-parse", "HEAD").stdout.strip() == prev
        # atelier conservé
        assert (git_app / ".workshop.env").exists()

    def test_ship_echec_sync_git_restore(self, git_app, monkeypatch):
        """Fix C : échec git en plein SYNC (fetch ok, checkout ok, reset ko) →
        _ws_git_restore doit rejouer — sinon le tree reste sur main alors que
        .workshop.env trace un autre ref."""
        _ws_setup(git_app, monkeypatch)
        # départ sur une branche propre distincte de main (poussée + tracking)
        _git(git_app, "checkout", "-q", "-b", "integration")
        _git(git_app, "push", "-q", "-u", "origin", "integration")
        prev = _git(git_app, "rev-parse", "HEAD").stdout.strip()

        def sync_reset_ko(cmd, **kwargs):
            # le reset --hard origin/main du SYNC échoue ; tout le reste est
            # réel (git) ou simulé OK (docker)
            if cmd[:1] == ["git"] and cmd[-3:] == ["reset", "--hard", "origin/main"]:
                return 1
            return _docker_ok(cmd, **kwargs)

        with patch.object(actions_mod.utils, "run_shell", side_effect=sync_reset_ko), \
             patch.object(actions_mod, "_health_check", return_value=True), \
             patch.object(actions_mod, "app_make") as am:
            assert _ship_workshop(None, "wstest") is False
        am.assert_not_called()  # on n'atteint jamais le build prod
        # le restore a bien rejoué : retour sur la branche/commit d'avant
        assert _git(git_app, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "integration"
        assert _git(git_app, "rev-parse", "HEAD").stdout.strip() == prev


# ── _create_workshop : traçage WS_REF/WS_COMMIT + APP_VERSION ──

from hubctl_cli.actions import _create_workshop

_real_subprocess_run = subprocess.run


def _sub_docker_ok(cmd, *args, **kwargs):
    """subprocess.run side-effect : docker simulé (aucun volume créé sur
    l'hôte), git réellement exécuté — pendant subprocess de _docker_ok."""
    if cmd[:1] == ["docker"]:
        return mock.Mock(returncode=0)
    return _real_subprocess_run(cmd, *args, **kwargs)


class TestCreateWorkshopTracing:
    def test_create_ecrit_ws_ref_commit_et_app_version(self, git_app, monkeypatch):
        monkeypatch.setattr(actions_mod.hub_state, "APPS_DIR", git_app.parent)
        monkeypatch.setattr(actions_mod.hub_state, "check_docker", lambda: True)
        head = _git(git_app, "rev-parse", "HEAD").stdout.strip()
        (git_app / "VERSION").write_text("0.2.0\n")
        # _find_free_subnet/_ws_used_subnets patchés : herméticité (pas de
        # docker network ls réel ni lecture du parc)
        with patch.object(actions_mod, "_find_free_port", side_effect=[10000, 5200]), \
             patch.object(actions_mod, "_find_free_subnet", return_value="10.210.0.0/24"), \
             patch.object(actions_mod, "_ws_used_subnets", return_value=set()), \
             patch.object(actions_mod.utils, "run_shell", return_value=0) as rs, \
             patch.object(actions_mod, "_health_check", return_value=True), \
             patch.object(actions_mod.subprocess, "run", side_effect=_sub_docker_ok):
            assert _create_workshop(None, "wstest") is True
        ws = env_manager.read_env(git_app / ".workshop.env")
        assert ws["WS_REF"] == "main"
        assert ws["WS_COMMIT"] == head
        # APP_VERSION injectée dans l'env du build atelier
        # (filtre traduit : la cmd réelle est ["docker","compose","-p",…,"build"],
        #  le filtre du brief [:3]==["docker","compose","build"] ne matchait jamais)
        build_calls = [c for c in rs.call_args_list
                       if c.args and c.args[0][:2] == ["docker", "compose"]
                       and "build" in c.args[0]]
        assert build_calls
        assert build_calls[0].kwargs.get("env", {}).get("APP_VERSION") == "0.2.0"

    def test_create_sans_git_ecrit_na(self, tmp_path, monkeypatch):
        app = tmp_path / "APPS" / "noapp"; app.mkdir(parents=True)
        (app / "docker-compose.yml").write_text("services: {}\n")
        (app / ".env").write_text("API_PORT=9000\nFRONTEND_PORT=4200\n")
        monkeypatch.setattr(actions_mod.hub_state, "APPS_DIR", tmp_path / "APPS")
        # returncode=1 (et non 0 comme dans le brief) : avec 0 les checks git de
        # _ws_git_state passeraient et head/branch seraient des Mocks → WS_REF
        # ne vaudrait jamais "n/a". Il faut un returncode non nul pour le
        # chemin « pas un repo git ».
        # _find_free_subnet/_ws_used_subnets patchés : herméticité (get_apps
        # sinon lirait le parc réel de la machine).
        with patch.object(actions_mod, "_find_free_port", side_effect=[10000, 5200]), \
             patch.object(actions_mod, "_find_free_subnet", return_value="10.210.0.0/24"), \
             patch.object(actions_mod, "_ws_used_subnets", return_value=set()), \
             patch.object(actions_mod.utils, "run_shell", return_value=0), \
             patch.object(actions_mod, "_health_check", return_value=True), \
             patch.object(actions_mod.subprocess, "run", return_value=mock.Mock(returncode=1)):
            assert _create_workshop(None, "noapp") is True
        ws = env_manager.read_env(app / ".workshop.env")
        assert ws["WS_REF"] == "n/a" and ws["WS_COMMIT"] == "n/a"

    def test_create_persiste_app_subnet_dedie(self, git_app, monkeypatch):
        """Subnet atelier dédié alloué, persisté et injecté dans l'env compose.

        Fix « Pool overlaps » : les apps template récent épinglent APP_SUBNET
        (mandatory) dans docker-compose.yml — sans override, l'atelier
        réutilise le subnet prod et le up échoue quand la prod tourne.
        """
        monkeypatch.setattr(actions_mod.hub_state, "APPS_DIR", git_app.parent)
        monkeypatch.setattr(actions_mod.hub_state, "check_docker", lambda: True)
        with patch.object(actions_mod, "_find_free_port", side_effect=[10000, 5200]), \
             patch.object(actions_mod, "_find_free_subnet", return_value="10.210.9.0/24"), \
             patch.object(actions_mod, "_ws_used_subnets", return_value=set()), \
             patch.object(actions_mod.utils, "run_shell", return_value=0) as rs, \
             patch.object(actions_mod, "_health_check", return_value=True), \
             patch.object(actions_mod.subprocess, "run", side_effect=_sub_docker_ok):
            assert _create_workshop(None, "wstest") is True
        ws = env_manager.read_env(git_app / ".workshop.env")
        assert ws["APP_SUBNET"] == "10.210.9.0/24"
        # APP_SUBNET override le .env prod dans chaque appel compose -ws
        build_calls = [c for c in rs.call_args_list
                       if c.args and c.args[0][:2] == ["docker", "compose"]
                       and "build" in c.args[0]]
        assert build_calls
        assert build_calls[0].kwargs.get("env", {}).get("APP_SUBNET") == "10.210.9.0/24"

    def test_create_sans_subnet_libre_omet_cle(self, git_app, monkeypatch):
        """Pool atelier épuisé → pas de APP_SUBNET (fallback subnet prod + warning)."""
        monkeypatch.setattr(actions_mod.hub_state, "APPS_DIR", git_app.parent)
        monkeypatch.setattr(actions_mod.hub_state, "check_docker", lambda: True)
        with patch.object(actions_mod, "_find_free_port", side_effect=[10000, 5200]), \
             patch.object(actions_mod, "_find_free_subnet", return_value=None), \
             patch.object(actions_mod, "_ws_used_subnets", return_value=set()), \
             patch.object(actions_mod.utils, "run_shell", return_value=0), \
             patch.object(actions_mod, "_health_check", return_value=True), \
             patch.object(actions_mod.subprocess, "run", side_effect=_sub_docker_ok):
            assert _create_workshop(None, "wstest") is True
        ws = env_manager.read_env(git_app / ".workshop.env")
        assert "APP_SUBNET" not in ws


# ── action_atelier : dispatch flags ──

from hubctl_cli.actions import action_atelier


class TestActionAtelierDispatch:
    def test_ref_sans_maj_refuse(self, tmp_path, monkeypatch):
        app = tmp_path / "APPS" / "wstest"; app.mkdir(parents=True)
        (app / "docker-compose.yml").write_text("services: {}\n")
        monkeypatch.setattr(actions_mod.hub_state, "APPS_DIR", tmp_path / "APPS")
        with patch.object(actions_mod, "_update_workshop") as uw:
            action_atelier(None, "wstest", ref="feat/x")
        uw.assert_not_called()

    def test_maj_sur_atelier_inexistant_appelle_update_qui_refuse(self, tmp_path, monkeypatch):
        app = tmp_path / "APPS" / "wstest"; app.mkdir(parents=True)
        (app / "docker-compose.yml").write_text("services: {}\n")
        monkeypatch.setattr(actions_mod.hub_state, "APPS_DIR", tmp_path / "APPS")
        with patch.object(actions_mod, "_create_workshop") as cw, \
             patch.object(actions_mod, "_update_workshop", return_value=False) as uw:
            action_atelier(None, "wstest", maj=True)
        cw.assert_not_called()
        uw.assert_called_once()

    def test_maj_atelier_existant_route_vers_update(self, git_app, monkeypatch):
        _ws_setup(git_app, monkeypatch)
        with patch.object(actions_mod, "_update_workshop", return_value=True) as uw:
            action_atelier(None, "wstest", maj=True, ref="feat/x")
        uw.assert_called_once()
        assert uw.call_args.args[2] == "feat/x"  # positionnel ref après (console, slug)

    def test_prod_atelier_existant_route_vers_ship(self, git_app, monkeypatch):
        _ws_setup(git_app, monkeypatch)
        with patch.object(actions_mod, "_ship_workshop", return_value=True) as sw:
            action_atelier(None, "wstest", prod=True)
        sw.assert_called_once()

    def test_menu_non_tty_annule(self, git_app, monkeypatch):
        _ws_setup(git_app, monkeypatch)
        with patch("sys.stdin") as stdin, \
             patch.object(actions_mod, "_remove_workshop") as rw:
            stdin.isatty.return_value = False
            action_atelier(None, "wstest")  # pas de flags → menu
        rw.assert_not_called()


# ── HTTPS atelier (spec 2026-09-19 : atelier = miroir prod) ──

from hubctl_cli.actions import _start_workshop


def _compose_build_up_calls(rs) -> list[list]:
    """Commandes docker compose build/up capturées via le mock run_shell."""
    return [c.args[0] for c in rs.call_args_list
            if c.args and c.args[0][:2] == ["docker", "compose"]
            and ("build" in c.args[0] or "up" in c.args[0])]


def _passe_override_https(cmd: list) -> bool:
    """True si la commande a un -f <…>/docker-compose.https.yml (chemin complet)."""
    return any(a == "-f" and b.endswith("docker-compose.https.yml")
               for a, b in zip(cmd, cmd[1:]))


class TestWsComposeFiles:
    """Helper pur : liste -f du compose atelier (override https si actif + embarqué)."""

    def test_ws_compose_files_ajoute_https_si_present_et_actif(self, tmp_path):
        from hubctl_cli.actions import _ws_compose_files
        (tmp_path / "docker-compose.yml").write_text("services: {}\n")
        assert _ws_compose_files(tmp_path, {"HTTPS_ENABLED": "true"}) == [str(tmp_path / "docker-compose.yml")]
        (tmp_path / "docker-compose.https.yml").write_text("services: {}\n")
        assert _ws_compose_files(tmp_path, {"HTTPS_ENABLED": "true"}) == [
            str(tmp_path / "docker-compose.yml"), str(tmp_path / "docker-compose.https.yml")]
        assert _ws_compose_files(tmp_path, {"HTTPS_ENABLED": "false"}) == [str(tmp_path / "docker-compose.yml")]
        assert _ws_compose_files(tmp_path, {}) == [   # défaut true (spec)
            str(tmp_path / "docker-compose.yml"), str(tmp_path / "docker-compose.https.yml")]


class TestWorkshopHttpsE2E:
    """Les commandes build/up atelier voient l'override docker-compose.https.yml.

    Sans l'override, le :80 du compose https renverrait un 301 vers le port
    https non publié → atelier inaccessible. Non-régression apps anciennes :
    sans docker-compose.https.yml dans le checkout, pas de -f https.
    """

    def test_create_https_enabled_et_override_compose(self, git_app, monkeypatch):
        (git_app / "docker-compose.https.yml").write_text("services: {}\n")
        _git(git_app, "add", "-A")
        _git(git_app, "commit", "-qm", "override https")
        monkeypatch.setattr(actions_mod.hub_state, "APPS_DIR", git_app.parent)
        monkeypatch.setattr(actions_mod.hub_state, "check_docker", lambda: True)
        with patch.object(actions_mod, "_find_free_port", side_effect=[10000, 5200]), \
             patch.object(actions_mod, "_find_free_subnet", return_value="10.210.0.0/24"), \
             patch.object(actions_mod, "_ws_used_subnets", return_value=set()), \
             patch.object(actions_mod.utils, "run_shell", return_value=0) as rs, \
             patch.object(actions_mod, "_health_check", return_value=True), \
             patch.object(actions_mod.subprocess, "run", side_effect=_sub_docker_ok):
            assert _create_workshop(None, "wstest") is True
        # .workshop.env écrit avec le flag HTTPS
        ws = env_manager.read_env(git_app / ".workshop.env")
        assert ws["HTTPS_ENABLED"] == "true"
        # build + up de création voient l'override
        calls = _compose_build_up_calls(rs)
        assert calls, "aucune commande build/up capturee"
        for cmd in calls:
            assert _passe_override_https(cmd), cmd

    def test_maj_passe_override_https_si_embarque(self, git_app, monkeypatch):
        (git_app / "docker-compose.https.yml").write_text("services: {}\n")
        _git(git_app, "add", "-A")
        _git(git_app, "commit", "-qm", "override https")
        _git(git_app, "push", "-q", "origin", "main")
        _ws_setup(git_app, monkeypatch)
        _origin_commit(git_app, "v2 https")  # avance origin/main → build réel
        with patch.object(actions_mod.utils, "run_shell", side_effect=_docker_ok) as rs, \
             patch.object(actions_mod, "_health_check", return_value=True):
            assert _update_workshop(None, "wstest", "main") is True
        calls = _compose_build_up_calls(rs)
        assert calls, "aucune commande build/up capturee"
        for cmd in calls:
            assert _passe_override_https(cmd), cmd

    def test_maj_sans_https_yml_pas_de_f_https(self, git_app, monkeypatch):
        """Non-régression apps anciennes : pas d'override → pas de -f https."""
        _ws_setup(git_app, monkeypatch)
        _origin_commit(git_app, "v2 sans https")
        with patch.object(actions_mod.utils, "run_shell", side_effect=_docker_ok) as rs, \
             patch.object(actions_mod, "_health_check", return_value=True):
            assert _update_workshop(None, "wstest", "main") is True
        calls = _compose_build_up_calls(rs)
        assert calls, "aucune commande build/up capturee"
        for cmd in calls:
            assert not _passe_override_https(cmd), cmd

    def test_maj_rollback_rebuild_passe_override_https(self, git_app, monkeypatch):
        """Atelier « ancien » (.workshop.env sans HTTPS_ENABLED) : le défaut true
        s'applique quand même — le rebuild de rollback voit l'override."""
        (git_app / "docker-compose.https.yml").write_text("services: {}\n")
        _git(git_app, "add", "-A")
        _git(git_app, "commit", "-qm", "override https")
        _git(git_app, "push", "-q", "origin", "main")
        _ws_setup(git_app, monkeypatch)  # .workshop.env SANS HTTPS_ENABLED (atelier pré-spec)
        _origin_commit(git_app, "v2 casse build https")
        with patch.object(actions_mod.utils, "run_shell", side_effect=_docker_build_ko) as rs:
            assert _update_workshop(None, "wstest", "main") is False
        calls = _compose_build_up_calls(rs)
        # build maj (KO) + rebuild rollback build + up
        assert len(calls) == 3, calls
        for cmd in calls:
            assert _passe_override_https(cmd), cmd

    def test_start_passe_override_https_si_embarque(self, git_app, monkeypatch):
        (git_app / "docker-compose.https.yml").write_text("services: {}\n")
        _git(git_app, "add", "-A")
        _git(git_app, "commit", "-qm", "override https")
        _ws_setup(git_app, monkeypatch)
        with patch.object(actions_mod, "_app_running", return_value=False), \
             patch.object(actions_mod.utils, "run_shell", side_effect=_docker_ok) as rs, \
             patch.object(actions_mod, "_health_check", return_value=True):
            assert _start_workshop(None, "wstest") is True
        up_calls = [c.args[0] for c in rs.call_args_list
                    if c.args and c.args[0][:2] == ["docker", "compose"]]
        assert up_calls, "aucune commande compose capturee"
        for cmd in up_calls:
            assert _passe_override_https(cmd), cmd


# ── Résolution du volume DB (datadir PostgreSQL) ──


class TestResolveRunningDbVolume:
    """Source du clone : datadir PostgreSQL du conteneur prod en cours."""

    def test_trouve_le_volume_pgdata(self):
        out = (
            "volume|exemple-app_backend_logs|/app/logs\n"
            "bind||/app/uploads\n"
            "volume|exemple-app_postgres_data|/var/lib/postgresql/data\n"
        )
        fake = subprocess.CompletedProcess([], 0, stdout=out, stderr="")
        with patch.object(actions_mod.subprocess, "run", return_value=fake):
            assert actions_mod._resolve_running_db_volume("exemple-app-postgres") == \
                "exemple-app_postgres_data"

    def test_variant_datadir_versionne(self):
        # postgres:18 : PGDATA inclut la version (/var/lib/postgresql/18/docker)
        out = "volume|exemple_pgdata|/var/lib/postgresql/18/docker\n"
        fake = subprocess.CompletedProcess([], 0, stdout=out, stderr="")
        with patch.object(actions_mod.subprocess, "run", return_value=fake):
            assert actions_mod._resolve_running_db_volume("exemple-app-postgres") == "exemple_pgdata"

    def test_conteneur_absent_retourne_none(self):
        fake = subprocess.CompletedProcess([], 1, stdout="", stderr="No such object")
        with patch.object(actions_mod.subprocess, "run", return_value=fake):
            assert actions_mod._resolve_running_db_volume("ghost-postgres") is None

    def test_sans_datadir_postgres_retourne_none(self):
        out = "volume|app_storage|/app/storage\n"
        fake = subprocess.CompletedProcess([], 0, stdout=out, stderr="")
        with patch.object(actions_mod.subprocess, "run", return_value=fake):
            assert actions_mod._resolve_running_db_volume("app-backend") is None


class TestResolveComposeDbVolume:
    """Cible du clone : datadir PostgreSQL du projet -ws résolu depuis le compose."""

    _CFG_DEFAULT = {
        "services": {
            "db": {
                "volumes": [
                    {"type": "volume", "source": "postgres_data",
                     "target": "/var/lib/postgresql/data"},
                ]
            },
            "backend": {
                "volumes": [
                    {"type": "volume", "source": "backend_storage",
                     "target": "/app/storage"},
                ]
            },
        },
        "volumes": {"postgres_data": {"driver": "local"},
                    "backend_storage": {"driver": "local"}},
    }

    def test_naming_compose_par_defaut(self, tmp_path):
        fake = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps(self._CFG_DEFAULT), stderr="")
        env = {"APP_SLUG": "exemple-app-ws"}
        with patch.object(actions_mod.subprocess, "run", return_value=fake):
            vol = actions_mod._resolve_compose_db_volume(
                tmp_path, [tmp_path / "docker-compose.yml"],
                "exemple-app-ws", env)
        assert vol == "exemple-app-ws_postgres_data"

    def test_name_explicite_prio(self, tmp_path):
        cfg = {
            "services": {"db": {"volumes": [
                {"type": "volume", "source": "postgres_data",
                 "target": "/var/lib/postgresql/data"}]}},
            "volumes": {"postgres_data": {"name": "legacy_pin_pgdata"}},
        }
        fake = subprocess.CompletedProcess([], 0, stdout=json.dumps(cfg), stderr="")
        with patch.object(actions_mod.subprocess, "run", return_value=fake):
            vol = actions_mod._resolve_compose_db_volume(
                tmp_path, [tmp_path / "docker-compose.yml"], "x-ws", {})
        assert vol == "legacy_pin_pgdata"

    def test_bind_ignore(self, tmp_path):
        cfg = {
            "services": {"db": {"volumes": [
                {"type": "bind", "source": "./DATA/pg",
                 "target": "/var/lib/postgresql/data"}]}},
            "volumes": {},
        }
        fake = subprocess.CompletedProcess([], 0, stdout=json.dumps(cfg), stderr="")
        with patch.object(actions_mod.subprocess, "run", return_value=fake):
            assert actions_mod._resolve_compose_db_volume(
                tmp_path, [tmp_path / "docker-compose.yml"], "x-ws", {}) is None

    def test_config_echoue_retourne_none(self, tmp_path):
        fake = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        with patch.object(actions_mod.subprocess, "run", return_value=fake):
            assert actions_mod._resolve_compose_db_volume(
                tmp_path, [tmp_path / "docker-compose.yml"], "x-ws", {}) is None

    def test_json_invalide_retourne_none(self, tmp_path):
        fake = subprocess.CompletedProcess([], 0, stdout="not json", stderr="")
        with patch.object(actions_mod.subprocess, "run", return_value=fake):
            assert actions_mod._resolve_compose_db_volume(
                tmp_path, [tmp_path / "docker-compose.yml"], "x-ws", {}) is None

