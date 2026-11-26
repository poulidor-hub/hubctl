# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""`hubctl app install <url-git>` — une commande, l'app tourne.

Isolation : tout se passe sous tmp_path. Hub, git, make, Docker et socle sont
simulés par deux objets injectés (`actions._hub_api`, `actions.utils.run_shell`)
qui journalisent leurs appels dans une liste d'événements commune — c'est elle qui
verrouille les ORDRES (dépendances d'abord, pré-création entre build et up, api
relancé avant le frontend) et les BUDGETS. Le coffre réel n'est jamais atteint.

Valeurs d'exemple uniquement (RFC 5737, slugs génériques) : aucune valeur du parc.
"""
import datetime
import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID
from rich.console import Console

from hubctl_cli import actions, env_manager, hub_state, socle

HOST = "192.0.2.10"
GOOD_PW = "admin-test"        # mot de passe admin courant du faux Hub
STALE_PW = "admin-perime"     # celui du .env du Hub, périmé (changé à la 1ʳᵉ connexion)
WRONG_PW = "admin-faux"       # valeur d'environnement erronée
TOKEN = "jeton-admin-test"
SOCLE_VERSION = "2.40.2"
TEMPLATE_FILES = ("docker-compose.yml", "Makefile", ".env.example")


def _u(slug):
    return f"git@github.com:exemple/{slug}.git"


def tmp_seed(b, urls_by_slug):
    """seed-apps.yaml à la racine du parc (liste d'URL, slug = nom du dépôt)."""
    p = hub_state.PARC_ROOT / "seed-apps.yaml"
    p.write_text(yaml.dump({"apps": list(urls_by_slug.values())}))
    return p


# ── Mini-PKI réelle (Ed25519 : instantané) ───────────────────────────────────


class _Pki:
    """CA du Hub, un cert serveur signé par elle, un cert auto-signé, un cert
    signé par une autre CA."""

    def __init__(self):
        self.ca_key = ed25519.Ed25519PrivateKey.generate()
        self.ca_name = self._name("Hub Example CA")
        self.ca_pem = self._pem(self._signed(self.ca_name, self.ca_name, self.ca_key, self.ca_key, ca=True))
        self.hub_signed = self._issue("app-exemple", self.ca_key, self.ca_name)
        own = ed25519.Ed25519PrivateKey.generate()
        me = self._name("localhost")
        self.self_signed = self._pem(self._signed(me, me, own, own))
        self.other_ca = self._issue("app-exemple", ed25519.Ed25519PrivateKey.generate(),
                                    self._name("Autre CA"))

    @staticmethod
    def _name(cn):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])

    def _signed(self, subject, issuer, subject_key, issuer_key, *, ca=False):
        now = datetime.datetime.now(datetime.timezone.utc)
        builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
                   .public_key(subject_key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(days=1))
                   .not_valid_after(now + datetime.timedelta(days=365)))
        if ca:
            builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None),
                                            critical=True)
        return builder.sign(issuer_key, None)

    def _issue(self, cn, issuer_key, issuer_name):
        key = ed25519.Ed25519PrivateKey.generate()
        return self._pem(self._signed(self._name(cn), issuer_name, key, issuer_key))

    @staticmethod
    def _pem(cert):
        return cert.public_bytes(serialization.Encoding.PEM).decode()


# ── Dépôts simulés ───────────────────────────────────────────────────────────


def _compose(env_lines=(), services=("api", "frontend"), socle_context=None):
    parts = ["services:\n"]
    for svc in services:
        parts.append(f"  {svc}:\n    image: ${{APP_SLUG:-x}}-{svc}\n")
        if svc == "api" and env_lines:
            parts.append("    environment:\n" + "".join(f"      {line}\n" for line in env_lines))
        if svc == "frontend" and socle_context is not None:
            # Gabarit : `socle: ../../HUBsocle` ; app héritée (lien dev gitignored) : `socle: socle`
            parts.append("    build:\n      context: ./frontend\n      additional_contexts:\n"
                         f"        socle: {socle_context}\n")
    return "".join(parts)


MAKEFILE_FULL = """\
.PHONY: sync-env build up restart-api restart-frontend
sync-env:
	@true
build: sync-env
	@true
up: sync-env
	@true
restart-api:
	@true
restart-frontend:
	@true
"""
MAKEFILE_NO_RESTART = MAKEFILE_FULL.replace("restart-api:\n\t@true\n", "").replace(
    "restart-frontend:\n\t@true\n", "")
MAKEFILE_MINIMAL = "build:\n\t@true\nup:\n\t@true\n"
SECRET_LINES = ["SECRET_KEY: ${SECRET_KEY:-changeme-please}",
                "ADMIN_PASSWORD: ${ADMIN_PASSWORD:-admin123}"]


def _env_example(name="Mon application", extra=()):
    return "".join([
        "APP_SLUG=mon-app\n", f"APP_NAME={name}\n",
        "API_PORT=9000\n", "FRONTEND_PORT=4200\n", "FRONTEND_HTTPS_PORT=4201\n",
        "HTTPS_ENABLED=true\n", f"HUB_HOST={HOST}\n", "HUB_API_PORT=8000\n",
        "POSTGRES_PASSWORD=placeholder-pw\n", *extra,
    ])


class _Repo:
    """Ce que `git clone` matérialise (squelette du template POULIDOR)."""

    def __init__(self, url, *, name="Mon application", depends=(), depends_raw=None,
                 env_lines=(), env_example=None, makefile=MAKEFILE_FULL,
                 services=("api", "frontend"), https=True, cert_sync_ok=True,
                 build_fails=False, clone_fails=False, sync_env=None, files=TEMPLATE_FILES,
                 socle_context=None):
        self.url = url
        self.socle_context = socle_context    # contexte de build `socle` du frontend
        self.depends = list(depends)          # [(slug, note)]
        self.depends_raw = depends_raw        # contenu brut (YAML cassé)
        self.env_lines = list(env_lines)
        self.env_example = _env_example(name) if env_example is None else env_example
        self.makefile = makefile
        self.services = tuple(services)
        self.https = https
        self.cert_sync_ok = cert_sync_ok
        self.build_fails = build_fails
        self.clone_fails = clone_fails
        self.sync_env = sync_env or {}        # clés écrites dans le .env par `make sync-env`
        self.files = tuple(files)

    def materialize(self, dest: Path):
        dest.mkdir(parents=True)
        for name in self.files:
            (dest / name).write_text("")
        if "docker-compose.yml" in self.files:
            (dest / "docker-compose.yml").write_text(
                _compose(self.env_lines, self.services, self.socle_context))
        if "Makefile" in self.files:
            (dest / "Makefile").write_text(self.makefile)
        if ".env.example" in self.files:
            (dest / ".env.example").write_text(self.env_example)
        if self.depends_raw is not None:
            (dest / "depends.yaml").write_text(self.depends_raw)
        elif self.depends:
            (dest / "depends.yaml").write_text(
                yaml.dump({"depends_on": [{"slug": s, "note": n} for s, n in self.depends]}))


class _Shell:
    """Stub de `utils.run_shell` : `git clone` matérialise le dépôt ; `make` et
    `docker compose` sont journalisés (cible, budget) avec leurs effets de bord
    attendus (cert auto-signé au `up`, cert du Hub au redémarrage de l'api)."""

    def __init__(self, bench):
        self.b = bench
        self.repos: dict[str, _Repo] = {}
        self.installed: dict[str, _Repo] = {}
        self.make: list[tuple[str, str, object]] = []        # (slug, cible, budget)
        self.compose: list[tuple[str, str, object]] = []     # (service, cwd, budget)
        self.env_at_build: dict[str, dict] = {}
        self.socle_link_at_build: dict[str, bool] = {}

    def add(self, slug, **kw) -> _Repo:
        repo = _Repo(_u(slug), **kw)
        self.repos[repo.url] = repo
        return repo

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        if cmd[:2] == ["git", "clone"]:
            dest = Path(cmd[3])
            self.b.events.append(f"clone:{dest.name}")
            repo = self.repos.get(cmd[2])
            if repo is None or repo.clone_fails:
                return 128
            repo.materialize(dest)
            self.installed[dest.name] = repo
            return 0
        if cmd[0] == "make":
            slug, target = Path(cmd[2]).name, cmd[-1]
            self.make.append((slug, target, kwargs.get("timeout")))
            self.b.events.append(f"make:{slug}:{target}")
            return self._effect(self.installed.get(slug), Path(cmd[2]), target)
        if cmd[:3] == ["docker", "compose", "restart"]:
            cwd = kwargs.get("cwd")
            self.compose.append((cmd[3], str(cwd), kwargs.get("timeout")))
            self.b.events.append(f"compose-restart:{Path(cwd).name}:{cmd[3]}")
            return self._effect(self.installed.get(Path(cwd).name), Path(cwd), f"restart-{cmd[3]}")
        return 0

    def _effect(self, repo, dest, target):
        if repo is None:
            return 0
        if target == "sync-env" and repo.sync_env:
            env_manager.write_env(dest / ".env", repo.sync_env)
        if target == "build":
            if (dest / ".env").exists():
                self.env_at_build[dest.name] = env_manager.read_env(dest / ".env")
            self.socle_link_at_build[dest.name] = (dest / "socle").exists()
            if repo.build_fails:
                return 1
        if target == "up" and repo.https:                    # scripts/generate-ssl.sh
            self._write_cert(dest, self.b.pki.self_signed)
        if target == "restart-api" and repo.cert_sync_ok:    # cert_sync au démarrage du socle
            self._write_cert(dest, self.b.pki.hub_signed)
        return 0

    @staticmethod
    def _write_cert(dest, pem):
        (dest / "ssl").mkdir(parents=True, exist_ok=True)
        (dest / "ssl" / "server.crt").write_text(pem)

    def targets(self, slug):
        return [(t, budget) for s, t, budget in self.make if s == slug]


# ── Hub simulé ───────────────────────────────────────────────────────────────


class _Hub:
    """Hub en mémoire derrière `actions._hub_api` : login, registre, approbation, CA."""

    def __init__(self, bench, admin_pw=GOOD_PW):
        self.b = bench
        self.admin_pw = admin_pw
        self.apps: dict[str, dict] = {}
        self.calls: list[tuple[str, str, object, object]] = []   # (méthode, chemin, corps, jeton)
        self.create_status = 201
        self.login_status: int | None = None   # force le statut du login (403 : compte désactivé)
        self.expires_at = "2999-01-01T00:00:00Z"
        self.down = False

    def __call__(self, method, path, *, token=None, body=None, timeout=30):
        self.calls.append((method, path, body, token))
        self.b.events.append(f"http:{method} {path}")
        if self.down:
            return 0, None
        if method == "POST" and path == "/api/auth/login":
            if self.login_status is not None:
                return self.login_status, {"detail": "Compte désactivé"}
            if body == {"username": "admin", "password": self.admin_pw}:
                return 200, {"access_token": TOKEN, "expires_at": self.expires_at}
            return 401, {"detail": "Identifiants incorrects"}
        if method == "GET" and path == "/api/pki/ca.crt":      # endpoint public
            return 200, self.b.pki.ca_pem
        if token != TOKEN:
            return 401, {"detail": "Non autorisé"}
        if method == "GET" and path == "/api/apps/":
            return 200, list(self.apps.values())
        if method == "POST" and path == "/api/apps/":
            if body["slug"] in self.apps:
                return 400, {"detail": "Ce slug existe déjà"}
            if self.create_status != 201:
                return self.create_status, {"detail": "Dépôt non certifié app Hub"}
            entry = {**body, "approval_status": "approved", "is_active": True,
                     "api_url": f"http://{HOST}:{body.get('api_port')}",
                     "frontend_url": f"https://{HOST}:{body.get('frontend_https_port')}",
                     "callback_url": f"https://{HOST}:{body.get('frontend_https_port')}/callback"}
            self.apps[entry["slug"]] = entry
            return 201, {"application": entry}
        if method == "POST" and path.endswith("/approve"):
            entry = self.apps.get(path.split("/")[3])
            if entry is None:
                return 404, {"detail": "Application non trouvée"}
            if entry["approval_status"] != "pending":
                return 400, {"detail": "n'est pas en attente d'approbation"}
            entry["approval_status"], entry["is_active"] = "approved", True
            return 200, {"message": "ok"}
        return 404, {"detail": "Introuvable"}

    def paths(self):
        return [f"{m} {p}" for m, p, _body, _t in self.calls]

    def bodies(self, path, method=None):
        return [body for m, p, body, _t in self.calls if p == path and method in (None, m)]


# ── Banc de test ─────────────────────────────────────────────────────────────


class _Bench:
    def __init__(self, monkeypatch, tmp_path):
        self.pki = _Pki()
        self.events: list[str] = []
        self.apps_dir = tmp_path / "APPS"
        self.hub_dir = tmp_path / "HUB_base"
        self.socle_dir = tmp_path / "HUBsocle"
        for d in (self.apps_dir, self.hub_dir, self.socle_dir):
            d.mkdir()
        (self.socle_dir / "VERSION").write_text(SOCLE_VERSION + "\n")
        self.set_hub_env()
        self.docker_ok = True
        self.apps_writable = True
        self.images = {f"poulidor/socle-api:{SOCLE_VERSION}", f"poulidor/socle-web:{SOCLE_VERSION}"}
        self.socle_rc = 0
        self.health_calls: list[tuple[str, object]] = []
        self.coffre: list[str] = []
        self.shell = _Shell(self)
        self.hub = _Hub(self)
        self.con = Console(file=io.StringIO(), record=True, width=400, force_terminal=False)
        monkeypatch.delenv("HUBCTL_ADMIN_PASSWORD", raising=False)
        self.real_hub_api = actions._hub_api
        self.real_writable = actions._apps_dir_writable
        self.real_wait_cert = actions._wait_hub_signed_cert

        # ── Aucune écriture hors tmp_path, aucun coffre réel, aucun docker/make réel ──
        monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
        monkeypatch.setattr(hub_state, "HUB_DIR", self.hub_dir)
        monkeypatch.setattr(hub_state, "APPS_DIR", self.apps_dir)
        for name in ("vault_path", "backups_dir", "recovery_dir"):
            monkeypatch.setattr(hub_state, name, lambda *a, _n=name, **k: tmp_path / _n)
        monkeypatch.setattr(actions, "_get_vault", self._never)
        monkeypatch.setattr(actions, "_sync_app_coffre", lambda c, name, **kw: self.coffre.append(name))
        monkeypatch.setattr(actions, "disk_guard", lambda *a, **kw: True)
        monkeypatch.setattr(actions, "_apps_dir_writable", lambda: self.apps_writable)
        monkeypatch.setattr(hub_state, "check_docker", lambda: self.docker_ok)
        monkeypatch.setattr(actions, "_detect_app_config", lambda name: {
            "slug": name, "api_port": 9000, "frontend_port": 4200, "app_subnet": None,
            "postgres_password": "pgpw", "fernet_key": "k", "hub_host": HOST,
            "hub_url": f"http://{HOST}:8000", "host_ip": HOST})
        monkeypatch.setattr(actions, "_hub_api", self.hub)
        monkeypatch.setattr(actions.utils, "run_shell", self.shell)
        monkeypatch.setattr(actions, "_health_check", self._health)
        monkeypatch.setattr(actions, "_docker_image_exists", lambda tag: tag in self.images)
        monkeypatch.setattr(socle, "find_socle_dir", lambda: self.socle_dir)
        self.socle_build = mock.Mock(side_effect=self._socle_build)
        monkeypatch.setattr(socle, "socle_build", self.socle_build)
        monkeypatch.setattr(actions, "_wait_hub_signed_cert",
                            lambda cert, ca_pem, timeout=60: actions._cert_signed_by_hub(
                                Path(cert).read_bytes(), ca_pem) == "hub")

    @staticmethod
    def _never(*a, **k):
        raise AssertionError("le coffre réel ne doit jamais être atteint par `app install`")

    def _health(self, target, console=None, timeout=60, port_override=None):
        self.health_calls.append((target, timeout))
        self.events.append(f"health:{target}")
        return self.health_result(target)

    def health_result(self, target):
        return True

    def _socle_build(self, *a, **kw):
        self.events.append("socle-build")
        if self.socle_rc == 0:      # le build construit réellement les images
            self.images |= {f"poulidor/socle-api:{SOCLE_VERSION}", f"poulidor/socle-web:{SOCLE_VERSION}"}
        return self.socle_rc

    def set_hub_env(self, **kw):
        env = {"HUB_HOST": HOST, "HUB_API_PORT": "8000", "ADMIN_USERNAME": "admin", **kw}
        (self.hub_dir / ".env").write_text("".join(f"{k}={v}\n" for k, v in env.items()))

    def out(self):
        # clear=False : sortie cumulée (un 2ᵉ appel ne vide pas le journal).
        return self.con.export_text(clear=False)

    def install(self, targets, **kw):
        if isinstance(targets, str):
            targets = [targets]
        kw.setdefault("yes", True)
        return actions.action_app_install(self.con, list(targets), **kw)

    def app_dir(self, slug):
        return self.apps_dir / slug

    def app_env(self, slug):
        return env_manager.read_env(self.app_dir(slug) / ".env")


@pytest.fixture
def b(monkeypatch, tmp_path):
    return _Bench(monkeypatch, tmp_path)


@pytest.fixture
def admin_pw(b, monkeypatch):
    """Mot de passe admin fourni par l'environnement (source n°1)."""
    monkeypatch.setenv("HUBCTL_ADMIN_PASSWORD", GOOD_PW)


def _present(b, slug="app-exemple", *, cert=None):
    """App déjà installée : dossier APPS/<slug> complet."""
    d = b.app_dir(slug)
    d.mkdir()
    (d / "Makefile").write_text(MAKEFILE_FULL)
    (d / "docker-compose.yml").write_text(_compose())
    (d / ".env").write_text(
        f"APP_SLUG={slug}\nAPP_NAME={slug}\nAPI_PORT=9000\nFRONTEND_PORT=4200\n"
        f"FRONTEND_HTTPS_PORT=4201\nHTTPS_ENABLED=true\nHUB_HOST={HOST}\n")
    b.shell.installed[slug] = b.shell.add(slug)
    if cert:
        _Shell._write_cert(d, cert)
    return d


# ══ (a) Dépendances ══════════════════════════════════════════════════════════


def test_dependances_installees_d_abord_dans_l_ordre_une_fois_chacune(b):
    """L'app est clonée d'abord (lecture de depends.yaml) ; chaque dépendance
    absente est installée AVANT elle, dans l'ordre déclaré, une seule fois même
    quand deux apps de la commande la partagent."""
    tmp_seed(b, {"dep-a": _u("dep-a"), "dep-b": _u("dep-b")})
    b.shell.add("dep-a")
    b.shell.add("dep-b", depends=[("dep-a", "référentiel")])
    b.shell.add("app-exemple", depends=[("dep-a", "référentiel"), ("dep-b", "compta")])
    b.shell.add("app-b", depends=[("dep-a", "référentiel")])

    rc = b.install([_u("app-exemple"), _u("app-b")])

    assert rc == 0
    ev = b.events
    assert ev.index("clone:app-exemple") < ev.index("clone:dep-a")
    assert ev.index("health:dep-a") < ev.index("health:dep-b") < ev.index("make:app-exemple:build")
    assert ev.index("health:dep-b") < ev.index("make:app-b:build")
    assert ev.count("clone:dep-a") == 1
    assert ev.count("make:dep-a:build") == 1 and ev.count("clone:dep-b") == 1
    for slug in ("dep-a", "dep-b", "app-exemple", "app-b"):
        assert b.app_env(slug), f"{slug} doit avoir un .env"
    assert b.coffre == ["dep-a", "dep-b", "app-exemple", "app-b"]


def test_cycle_de_dependances_erreur_claire(b):
    """A → B → A : erreur explicite, rien n'est construit, clones retirés, rc 1."""
    b.shell.add("app-a", depends=[("app-b", "")])
    b.shell.add("app-b", depends=[("app-a", "")])
    tmp_seed(b, {"app-b": _u("app-b")})
    rc = b.install(_u("app-a"))
    assert rc == 1
    assert "cycle de dépendances : app-a → app-b → app-a" in b.out()
    assert not any(e.startswith("make:") for e in b.events)
    assert not b.app_dir("app-a").exists() and not b.app_dir("app-b").exists()


def test_auto_dependance_est_un_cycle(b):
    b.shell.add("app-a", depends=[("app-a", "moi-même")])
    rc = b.install(_u("app-a"))
    assert rc == 1
    assert "cycle de dépendances : app-a → app-a" in b.out()


def test_no_deps_ignore_les_dependances_avec_avertissement(b):
    b.shell.add("dep-a")
    b.shell.add("app-exemple", depends=[("dep-a", "référentiel")])
    rc = b.install(_u("app-exemple"), no_deps=True)
    assert rc == 0
    assert "clone:dep-a" not in b.events
    assert "make:app-exemple:up" in b.events
    out = b.out()
    assert "--no-deps" in out and "dep-a" in out


def test_dependance_introuvable_echec_et_les_suivantes_continuent(b):
    """(h) ni dans seed-apps.yaml ni au registre : l'app n'est pas installée
    (clone retiré), message-action, l'app suivante continue, rc 1."""
    b.shell.add("app-exemple", depends=[("dep-x", "introuvable")])
    b.shell.add("app-b")
    rc = b.install([_u("app-exemple"), _u("app-b")])
    assert rc == 1
    out = b.out()
    assert "Dépendance « dep-x » introuvable — installe-la d'abord ou renseigne seed-apps.yaml" in out
    assert not b.app_dir("app-exemple").exists()
    assert b.shell.targets("app-exemple") == []
    assert "make:app-b:up" in b.events


def test_dependance_introuvable_sans_jeton_admin_n_interroge_pas_le_registre(b):
    """Sans mot de passe admin (pas de TTY), la source « registre » est
    simplement indisponible : aucun appel Hub, et le message reste clair."""
    b.shell.add("app-exemple", depends=[("dep-x", "")])
    rc = b.install(_u("app-exemple"))
    assert rc == 1
    assert b.hub.calls == []
    assert "Dépendance « dep-x » introuvable" in b.out()


def test_dependance_resolue_via_le_registre_hub(b, admin_pw):
    """(b) absente de seed-apps.yaml → GET /api/apps/ (champ git_repo_url), jeton admin."""
    b.hub.apps["dep-reg"] = {"slug": "dep-reg", "approval_status": "approved", "is_active": True,
                             "git_repo_url": _u("dep-reg")}
    b.shell.add("dep-reg")
    b.shell.add("app-exemple", depends=[("dep-reg", "")])
    rc = b.install(_u("app-exemple"))
    assert rc == 0
    assert b.hub.paths()[0] == "POST /api/auth/login"
    assert "GET /api/apps/" in b.hub.paths()
    assert "clone:dep-reg" in b.events and "make:dep-reg:up" in b.events


def test_dependance_deja_installee_est_ignoree(b):
    """Présente = APPS/<slug>, ou un dossier dont le .env déclare APP_SLUG=<slug>
    (précédent pra_restore._resolve_app_dir : le dossier diverge souvent du slug)."""
    for folder, env in (("dep-a", "APP_SLUG=dep-a\n"), ("Dep_B", "APP_SLUG=dep-b\n")):
        d = b.app_dir(folder)
        d.mkdir()
        (d / "Makefile").write_text("up:\n\t@true\n")
        (d / ".env").write_text(env)
    b.shell.add("dep-a")
    b.shell.add("dep-b")
    b.shell.add("app-exemple", depends=[("dep-a", ""), ("dep-b", "")])
    rc = b.install(_u("app-exemple"))
    assert rc == 0
    assert [e for e in b.events if e.startswith("clone:")] == ["clone:app-exemple"]
    assert "déjà installée" in b.out()


def test_dependance_en_echec_fait_echouer_ses_dependants(b):
    """Le build de la dépendance échoue : l'app qui en dépend n'est pas installée
    (clone retiré) ; son autre dépendant échoue tout de suite, sans la retenter."""
    b.shell.add("dep-a", build_fails=True)
    b.shell.add("app-exemple", depends=[("dep-a", "")])
    b.shell.add("app-b", depends=[("dep-a", "")])
    tmp_seed(b, {"dep-a": _u("dep-a")})
    rc = b.install([_u("app-exemple"), _u("app-b")])
    assert rc == 1
    assert b.events.count("make:dep-a:build") == 1
    assert not b.app_dir("app-exemple").exists() and not b.app_dir("app-b").exists()
    out = b.out()
    assert "dépendance « dep-a » non installée" in out
    assert "Dépendance « dep-a » en échec plus tôt" in out


def test_depends_yaml_illisible_avertit_et_poursuit(b, admin_pw):
    """Tolérance du gabarit : YAML cassé → avertissement, app traitée comme autonome."""
    b.shell.add("app-exemple", depends_raw="depends_on: [slug: unclosed")
    rc = b.install(_u("app-exemple"))
    assert rc == 0
    assert "depends.yaml" in b.out()


# ══ (b) Budgets ══════════════════════════════════════════════════════════════


def test_budgets_build_up_health(b):
    """Premier démarrage d'une app : build 1800 s, up 600 s, health 300 s
    (le seed utilisait 600/120/60 — sans marge pour un premier build sans
    cache sur une petite VM)."""
    b.shell.add("app-exemple")
    assert b.install(_u("app-exemple")) == 0
    assert ("app-exemple", "build", 1800) in b.shell.make
    assert ("app-exemple", "up", 600) in b.shell.make
    assert ("app-exemple", 300) in b.health_calls
    assert (actions.INSTALL_BUILD_TIMEOUT, actions.INSTALL_UP_TIMEOUT,
            actions.INSTALL_HEALTH_TIMEOUT) == (1800, 600, 300)


# ══ (c) Secrets par défaut neutralisés ══════════════════════════════════════


def test_secrets_par_defaut_neutralises_avant_le_build(b):
    """`${SECRET_KEY:-changeme-please}` → le .env porte une valeur aléatoire,
    écrite AVANT `make build` : le conteneur ne voit jamais le défaut."""
    b.shell.add("app-exemple", env_lines=SECRET_LINES)
    assert b.install(_u("app-exemple")) == 0
    env = b.app_env("app-exemple")
    assert env["SECRET_KEY"] != "changeme-please" and len(env["SECRET_KEY"]) >= 32
    assert env["ADMIN_PASSWORD"] != "admin123" and len(env["ADMIN_PASSWORD"]) >= 32
    assert env["SECRET_KEY"].isalnum() and env["ADMIN_PASSWORD"].isalnum()
    assert env["SECRET_KEY"] != env["ADMIN_PASSWORD"]
    assert b.shell.env_at_build["app-exemple"] == env
    out = b.out()
    assert "SECRET_KEY" in out and "ADMIN_PASSWORD" in out        # annoncés par leur nom
    assert "changeme-please" not in out and "admin123" not in out  # jamais de valeur


def test_secret_renseigne_n_est_pas_ecrase(b):
    b.shell.add("app-exemple", env_lines=SECRET_LINES,
                env_example=_env_example(extra=["SECRET_KEY=choisi\n"]))
    b.install(_u("app-exemple"))
    env = b.app_env("app-exemple")
    assert env["SECRET_KEY"] == "choisi", "valeur choisie par l'admin : conservée telle quelle"
    assert env["ADMIN_PASSWORD"] != "admin123"


def test_placeholder_recopie_du_env_example_est_remplace_aussi(b):
    """`_write_app_env` recopie .env.example tel quel : quand le défaut y voyage,
    la clé est déjà dans le .env — mais avec le défaut. Elle est neutralisée aussi."""
    b.shell.add("app-exemple", env_lines=SECRET_LINES,
                env_example=_env_example(extra=["SECRET_KEY=changeme-please\n",
                                                "ADMIN_PASSWORD=admin123\n"]))
    b.install(_u("app-exemple"))
    env = b.app_env("app-exemple")
    assert env["SECRET_KEY"] != "changeme-please" and env["ADMIN_PASSWORD"] != "admin123"


def test_neutralise_epargne_les_variables_non_secretes(b):
    """Faux positifs interdits : un DSN qui CONTIENT « password », un mode
    d'auth nommé « password », un identifiant — rien n'est ajouté ni modifié."""
    lines = ["DATABASE_URL: ${DATABASE_URL:-postgresql://app_user:app_password_secure_123@postgres:5432/app_db}",
             "AUTH_MODE: ${AUTH_MODE:-password}",
             "POSTGRES_USER: ${POSTGRES_USER:-app_user}",
             "CORS_ORIGINS: ${CORS_ORIGINS:-*}",
             "EXCEL_PATH: ${EXCEL_PATH:-DATA/example.xls}"]
    b.shell.add("app-exemple", env_lines=lines)
    assert b.install(_u("app-exemple")) == 0
    assert set(b.app_env("app-exemple")) == set(
        env_manager.read_env(b.app_dir("app-exemple") / ".env.example"))


def test_neutralise_une_cle_fernet_avec_une_cle_valide(b):
    from cryptography.fernet import Fernet
    b.shell.add("app-exemple", env_lines=["SECRETS_ENCRYPTION_KEY: ${SECRETS_ENCRYPTION_KEY:-changeme}"],
                env_example="APP_SLUG=mon-app\nAPI_PORT=9000\nFRONTEND_PORT=4200\n")
    assert b.install(_u("app-exemple")) == 0
    key = b.app_env("app-exemple")["SECRETS_ENCRYPTION_KEY"]
    Fernet(key.encode())                     # lève ValueError si le format est invalide
    assert len(key) == 44


def test_sans_secret_par_defaut_rien_n_est_ecrit(b):
    b.shell.add("app-exemple")
    b.install(_u("app-exemple"))
    assert set(b.app_env("app-exemple")) == set(
        env_manager.read_env(b.app_dir("app-exemple") / ".env.example"))


@pytest.mark.parametrize("name,default", [
    ("SECRET_KEY", "changeme-please"), ("ADMIN_PASSWORD", "admin123"),
    ("JWT_SECRET", "changeit"), ("DB_PASSWORD", "password"), ("API_TOKEN", "ChangeMe123!"),
    ("POSTGRES_PASSWORD", "app_password_secure_123"), ("CLIENT_SECRET", "valeur-insecure"),
    ("SIGNING_KEY", "mon-token-example"), ("DB_PASS", "1234"), ("ENCRYPTION_KEY", "0000"),
    ("SECRET", "secret"), ("DASHBOARD_PASSWORD", "a-changer"),
])
def test_is_placeholder_secret_oui(name, default):
    assert actions._is_placeholder_secret(name, default) is True


@pytest.mark.parametrize("name,default", [
    ("DATABASE_URL", "postgresql://app_user:app_password_secure_123@postgres:5432/app_db"),
    ("AUTH_MODE", "password"), ("LOG_LEVEL", "secret"), ("POSTGRES_USER", "app_user"),
    ("ADMIN_USERNAME", "admin"), ("PUBLIC_KEY", "changeme"), ("SECRET_NAME", "secret"),
    ("PASSWORD_HASH_ITERATIONS", "10000"), ("PASSWORD_RESET_URL", "changeit"),
    ("SECRET_KEY", ""), ("API_TOKEN", "9f3kq2"), ("APP_SECRET", "mz4tx8"),   # défauts réels
    ("FRONTEND_HTTPS_PORT", "4241"), ("EXCEL_PATH", "DATA/example.xls"),
    ("API_TOKEN", "https://auth.example.com/token"), ("DB_PASSWORD", "user:password@db"),
    ("SECRET_KEYS", "mot1,mot2"), ("JWT_SECRET", "{\"k\": \"password\"}"),
])
def test_is_placeholder_secret_non(name, default):
    assert actions._is_placeholder_secret(name, default) is False


# ══ (d) Socle garanti ════════════════════════════════════════════════════════
#
# Deux prérequis « socle » pour qu'une app socle se construise sur un parc neuf :
# les IMAGES locales poulidor/socle-{api,web}:<V> (make sync-env refuse sans
# elles) et le CONTEXTE de build `socle` du frontend.


def test_socle_construit_si_une_image_absente_une_fois(b):
    """`make sync-env` refuse sans poulidor/socle-api:<V> : le socle est garanti
    AVANT, avec un budget de 1800 s — une seule fois pour toute la commande."""
    b.images.discard(f"poulidor/socle-web:{SOCLE_VERSION}")
    b.shell.add("app-exemple")
    b.shell.add("app-b")
    assert b.install([_u("app-exemple"), _u("app-b")]) == 0
    b.socle_build.assert_called_once()
    assert b.socle_build.call_args.kwargs["timeout"] == 1800
    assert b.events.index("socle-build") < b.events.index("make:app-exemple:sync-env")
    assert ("app-exemple", "build", 1800) in b.shell.make


def test_contexte_de_build_socle_cree_avant_le_build(b, admin_pw):
    """L'app héritée passe par un lien `socle` local (lien de dev, gitignored,
    absent d'un clone neuf) : sans lui `make build` échoue. hubctl crée le lien,
    relatif vers le HUBsocle du parc, avant le build."""
    b.shell.add("app-exemple", socle_context="socle")
    assert b.install(_u("app-exemple")) == 0
    lien = b.app_dir("app-exemple") / "socle"
    assert b.shell.socle_link_at_build["app-exemple"] is True
    assert lien.is_symlink() and lien.resolve() == b.socle_dir.resolve()
    assert os.readlink(lien) == "../../HUBsocle", "relatif : relocalisable (PRA, déplacement du parc)"
    assert "HUBsocle du parc" in b.out()


def test_contexte_de_build_socle_du_gabarit_ne_cree_rien(b, admin_pw):
    """Le gabarit pointe directement ../../HUBsocle : rien à créer."""
    b.shell.add("app-exemple", socle_context="../../HUBsocle")
    assert b.install(_u("app-exemple")) == 0
    assert not (b.app_dir("app-exemple") / "socle").exists()
    assert not (b.app_dir("app-exemple") / "socle").is_symlink()


def test_ensure_socle_context_lien_existant_inchangé(b):
    """Un lien (même pendataire) ou un fichier `socle` déjà en place n'est
    jamais remplacé — sauf pendataire ET utilisé par le compose : il est refait."""
    d = b.app_dir("x")
    d.mkdir()
    (d / "docker-compose.yml").write_text(_compose(socle_context="socle"))
    (d / "socle").write_text("pas un lien")            # fichier : respecté
    assert actions._ensure_socle_context(b.con, d) is True
    assert (d / "socle").read_text() == "pas un lien"
    (d / "socle").unlink()
    (d / "socle").symlink_to("../../HUBsocle")          # lien valide : respecté
    assert actions._ensure_socle_context(b.con, d) is True
    assert os.readlink(d / "socle") == "../../HUBsocle"
    (d / "socle").unlink()
    (d / "socle").symlink_to("/ailleurs/HUBsocle")     # pendataire : refait
    assert actions._ensure_socle_context(b.con, d) is True
    assert (d / "socle").resolve() == b.socle_dir.resolve()


def test_ensure_socle_context_sans_hubsocle_avertit(b, monkeypatch):
    """HUBsocle absent du parc : on ne peut rien créer — remède manuel affiché."""
    d = b.app_dir("x")
    d.mkdir()
    (d / "docker-compose.yml").write_text(_compose(socle_context="socle"))
    monkeypatch.setattr(socle, "find_socle_dir", mock.Mock(side_effect=FileNotFoundError("absent")))
    assert actions._ensure_socle_context(b.con, d) is False
    assert "Contexte de build `socle` introuvable (socle)" in b.out() and "HUBsocle" in b.out()
    assert not (d / "socle").exists()


def test_ensure_socle_context_lien_impossible_donne_le_remede(b, monkeypatch):
    d = b.app_dir("x")
    d.mkdir()
    (d / "docker-compose.yml").write_text(_compose(socle_context="socle"))

    def lecture_seule(self, *a, **k):
        raise OSError("système de fichiers en lecture seule")
    monkeypatch.setattr(Path, "symlink_to", lecture_seule)
    assert actions._ensure_socle_context(b.con, d) is False
    assert "ln -s ../../HUBsocle socle" in b.out()


def test_ensure_socle_context_compose_illisible_n_est_pas_une_erreur(b):
    d = b.app_dir("x")
    d.mkdir()
    assert actions._ensure_socle_context(b.con, d) is True
    (d / "docker-compose.yml").write_text("services: [unclosed")
    assert actions._ensure_socle_context(b.con, d) is True


def test_socle_non_construit_si_les_images_sont_la(b):
    b.shell.add("app-exemple")
    assert b.install(_u("app-exemple")) == 0
    b.socle_build.assert_not_called()
    assert "socle" not in b.out().lower(), "no-op silencieux"


def test_socle_build_echoue_avertit_sans_bloquer(b):
    """Hubctl ne sait pas si l'app dépend du socle : avertissement clair, et
    `make sync-env` de l'app tranchera (message actionnable du gabarit)."""
    b.images.clear()
    b.socle_rc = 1
    b.shell.add("app-exemple")
    assert b.install(_u("app-exemple")) == 0
    out = b.out()
    assert "socle" in out.lower() and "échoué" in out.lower()


def test_ensure_socle_image_passe_console_et_budget(b):
    b.images.clear()
    assert actions._ensure_socle_image(b.con) is True
    b.socle_build.assert_called_once_with(console=b.con, timeout=1800)


def test_ensure_socle_image_dry_run_ne_probe_rien(b, monkeypatch):
    """Dry-run : aucune commande docker, même en lecture seule."""
    monkeypatch.setattr(actions, "_docker_image_exists",
                        lambda tag: pytest.fail("dry-run : pas de docker image inspect"))
    assert actions._ensure_socle_image(b.con, dry_run=True) is True
    b.socle_build.assert_not_called()
    out = b.out()
    assert "(dry-run)" in out and "socle build" in out


def test_ensure_socle_image_echec_build_retourne_faux(b):
    b.images.clear()
    b.socle_rc = 1
    assert actions._ensure_socle_image(b.con) is False


def test_ensure_socle_image_sans_hubsocle_ne_bloque_pas(b, monkeypatch):
    monkeypatch.setattr(socle, "find_socle_dir", mock.Mock(side_effect=FileNotFoundError("absent")))
    assert actions._ensure_socle_image(b.con) is True
    b.socle_build.assert_not_called()
    assert "HUBsocle" in b.out()


# ══ (e) Pré-création approuvée ══════════════════════════════════════════════


def test_precreation_login_puis_post_apps_payload_exact(b):
    """(e) `POST /api/auth/login` puis `POST /api/apps/` — payload de l'UI
    (AppCreateSimplified : le Hub construit api_url/frontend_url/callback_url
    depuis les PORTS), lus dans le .env de l'app APRÈS sync-env, entre build et up."""
    b.set_hub_env(ADMIN_PASSWORD=GOOD_PW)
    b.shell.add("app-exemple", sync_env={"API_PORT": "9123", "FRONTEND_PORT": "4321",
                                         "FRONTEND_HTTPS_PORT": "4322"})
    assert b.install(_u("app-exemple")) == 0

    paths = b.hub.paths()
    assert paths[0] == "POST /api/auth/login"
    assert paths.index("POST /api/auth/login") < paths.index("POST /api/apps/")
    assert b.hub.bodies("/api/auth/login") == [{"username": "admin", "password": GOOD_PW}]
    assert b.hub.bodies("/api/apps/", method="POST") == [{
        "name": "app-exemple", "slug": "app-exemple", "requires_auth": True,
        "api_port": 9123, "frontend_port": 4321, "frontend_https_port": 4322,
        "create_service_account": False, "is_shared_service": False}]
    tokens = {p: t for _m, p, _body, t in b.hub.calls}
    assert tokens["/api/apps/"] == TOKEN and tokens["/api/auth/login"] is None

    ev = b.events
    assert (ev.index("make:app-exemple:sync-env") < ev.index("make:app-exemple:build")
            < ev.index("http:POST /api/apps/") < ev.index("make:app-exemple:up"))
    assert "approuvée" in b.out()


def test_app_deja_au_registre_n_est_pas_recree(b, admin_pw):
    b.hub.apps["app-exemple"] = {"slug": "app-exemple", "approval_status": "approved",
                                 "is_active": True, "git_repo_url": None}
    b.shell.add("app-exemple")
    assert b.install(_u("app-exemple")) == 0
    assert "POST /api/apps/" not in b.hub.paths(), "simple saut, jamais de mise à jour"
    assert "approuvée" in b.out()


def test_app_pending_au_registre_est_approuvee_pas_recree(b, admin_pw):
    b.hub.apps["app-exemple"] = {"slug": "app-exemple", "approval_status": "pending",
                                 "is_active": False, "git_repo_url": None}
    b.shell.add("app-exemple")
    assert b.install(_u("app-exemple")) == 0
    assert "POST /api/apps/" not in b.hub.paths()
    assert "POST /api/apps/app-exemple/approve" in b.hub.paths()
    assert b.hub.bodies("/api/apps/app-exemple/approve") == [{"approve": True}]


def test_app_rejetee_par_un_admin_n_est_pas_approuvee(b, admin_pw):
    b.hub.apps["app-exemple"] = {"slug": "app-exemple", "approval_status": "rejected",
                                 "is_active": False, "git_repo_url": None}
    b.shell.add("app-exemple")
    assert b.install(_u("app-exemple")) == 0
    assert not any(p.endswith("/approve") for p in b.hub.paths())
    assert "rejetée" in b.out()


def test_precreation_refusee_422_rest_en_attente(b, admin_pw):
    b.hub.create_status = 422
    b.shell.add("app-exemple")
    rc = b.install(_u("app-exemple"))
    assert rc == 0, "l'app répond : l'approbation manuelle reste possible"
    out = b.out()
    assert "422" in out and "en attente" in out.lower()


def test_payload_app_http_sans_https(b, admin_pw):
    b.shell.add("app-exemple", https=False,
                env_example=_env_example().replace("HTTPS_ENABLED=true", "HTTPS_ENABLED=false"))
    assert b.install(_u("app-exemple")) == 0
    assert "frontend_https_port" not in b.hub.bodies("/api/apps/", method="POST")[0]
    assert f"http://{HOST}:4200" in b.out()


@pytest.mark.parametrize("env,attendu", [
    ({"APP_NAME": "Application exemple", "API_PORT": "9000", "FRONTEND_PORT": "4200",
      "FRONTEND_HTTPS_PORT": "4201", "HTTPS_ENABLED": "true"},
     {"name": "Application exemple", "slug": "app-exemple", "requires_auth": True,
      "api_port": 9000, "frontend_port": 4200, "frontend_https_port": 4201,
      "create_service_account": False, "is_shared_service": False}),
    ({"API_PORT": "9000", "HTTPS_ENABLED": "false", "FRONTEND_PORT": "4200"},
     {"name": "App Exemple", "slug": "app-exemple", "requires_auth": True,
      "api_port": 9000, "frontend_port": 4200,
      "create_service_account": False, "is_shared_service": False}),
    ({}, {"name": "App Exemple", "slug": "app-exemple", "requires_auth": True,
          "create_service_account": False, "is_shared_service": False}),
])
def test_precreate_payload(env, attendu):
    assert actions._precreate_payload("app-exemple", env) == attendu


# ══ (f) Mot de passe admin ═══════════════════════════════════════════════════


def test_sans_mdp_admin_ni_tty_reste_en_attente_rc0(b):
    """(f) ni HUBCTL_ADMIN_PASSWORD, ni ADMIN_PASSWORD utilisable (changé à la
    première connexion), ni TTY : l'installation N'EST PAS bloquée."""
    b.set_hub_env(ADMIN_PASSWORD=STALE_PW)
    b.shell.add("app-exemple")
    rc = b.install(_u("app-exemple"))          # prompt_password=None ⇒ pas de TTY
    assert rc == 0
    assert "make:app-exemple:up" in b.events
    out = b.out()
    assert "en attente" in out.lower()
    assert "En attente" in out and "approuve" in out
    assert "HUBCTL_ADMIN_PASSWORD" in out
    assert "POST /api/apps/" not in b.hub.paths()
    assert STALE_PW not in out and TOKEN not in out


def test_aucun_mdp_ni_jeton_dans_la_sortie(b, admin_pw):
    b.shell.add("app-exemple")
    assert b.install(_u("app-exemple")) == 0
    out = b.out()
    assert GOOD_PW not in out and TOKEN not in out
    assert "Identifiants" not in out, "le corps d'erreur du login n'est pas recopié"


def test_mdp_admin_du_env_du_hub_puis_pas_de_prompt(b):
    b.set_hub_env(ADMIN_PASSWORD=GOOD_PW)
    b.shell.add("app-exemple")
    prompt = mock.Mock(return_value="ne-doit-pas-servir")
    assert b.install(_u("app-exemple"), prompt_password=prompt) == 0
    assert b.hub.bodies("/api/auth/login") == [{"username": "admin", "password": GOOD_PW}]
    prompt.assert_not_called()


def test_mdp_admin_env_faux_puis_celui_du_env_du_hub(b, monkeypatch):
    """Chaîne de résolution stricte : un HUBCTL_ADMIN_PASSWORD erroné ne masque
    pas un ADMIN_PASSWORD du .env encore valide (Hub neuf) — pas de prompt."""
    monkeypatch.setenv("HUBCTL_ADMIN_PASSWORD", WRONG_PW)
    b.set_hub_env(ADMIN_PASSWORD=GOOD_PW)
    prompt = mock.Mock(return_value=GOOD_PW)
    b.shell.add("app-exemple")
    assert b.install(_u("app-exemple"), prompt_password=prompt) == 0
    assert [body["password"] for body in b.hub.bodies("/api/auth/login")] == [WRONG_PW, GOOD_PW]
    prompt.assert_not_called()
    assert "approuvée" in b.out()


def test_mdp_admin_identique_dans_env_et_env_pas_de_double_tentative(b, monkeypatch):
    monkeypatch.setenv("HUBCTL_ADMIN_PASSWORD", STALE_PW)
    b.set_hub_env(ADMIN_PASSWORD=STALE_PW)
    b.shell.add("app-exemple")
    assert b.install(_u("app-exemple")) == 0
    assert len(b.hub.bodies("/api/auth/login")) == 1, "même valeur : une seule tentative"


def test_mdp_admin_variable_d_environnement_prioritaire(b, admin_pw):
    b.set_hub_env(ADMIN_PASSWORD=STALE_PW)
    b.shell.add("app-exemple")
    prompt = mock.Mock(return_value=GOOD_PW)
    assert b.install(_u("app-exemple"), prompt_password=prompt) == 0
    assert b.hub.bodies("/api/auth/login") == [{"username": "admin", "password": GOOD_PW}]
    prompt.assert_not_called()


def test_mdp_admin_perime_bascule_sur_le_prompt_masque(b):
    b.set_hub_env(ADMIN_PASSWORD=STALE_PW)
    b.shell.add("app-exemple")
    saisies = iter(["saisie-fausse-1", GOOD_PW])
    assert b.install(_u("app-exemple"), prompt_password=lambda: next(saisies)) == 0
    mdp = [body["password"] for body in b.hub.bodies("/api/auth/login")]
    assert mdp == [STALE_PW, "saisie-fausse-1", GOOD_PW]
    out = b.out()
    assert "approuvée" in out and "saisie-fausse-1" not in out


def test_mdp_admin_prompt_vide_reste_en_attente(b):
    b.shell.add("app-exemple")
    rc = b.install(_u("app-exemple"), prompt_password=lambda: "")
    assert rc == 0
    assert b.hub.bodies("/api/auth/login") == []
    assert "en attente" in b.out().lower()


def test_mdp_admin_trois_essais_prompt_puis_renoncement(b):
    """3 saisies au plus : on reste sous la limite anti-bruteforce du Hub (5/min)."""
    b.set_hub_env(ADMIN_PASSWORD=STALE_PW)
    b.shell.add("app-exemple")
    saisies = iter(["a", "b", "c", "d"])
    rc = b.install(_u("app-exemple"), prompt_password=lambda: next(saisies))
    assert rc == 0
    assert len(b.hub.bodies("/api/auth/login")) == 4
    assert "en attente" in b.out().lower()


def test_mdp_admin_prompt_interrompu_reste_en_attente(b):
    def interrompu():
        raise EOFError()
    b.shell.add("app-exemple")
    assert b.install(_u("app-exemple"), prompt_password=interrompu) == 0
    assert b.hub.bodies("/api/auth/login") == []


def test_une_seule_connexion_admin_pour_toute_la_commande(b, admin_pw):
    tmp_seed(b, {"dep-a": _u("dep-a")})
    b.shell.add("dep-a")
    b.shell.add("app-exemple", depends=[("dep-a", "")])
    b.shell.add("app-b")
    assert b.install([_u("app-exemple"), _u("app-b")]) == 0
    assert b.hub.paths().count("POST /api/auth/login") == 1


def test_compte_admin_desactive_pas_de_second_essai_ni_prompt(b):
    """HTTP 403 n'est pas un mot de passe périmé : inutile d'insister."""
    b.set_hub_env(ADMIN_PASSWORD=STALE_PW)
    prompt = mock.Mock(return_value=GOOD_PW)
    b.shell.add("app-exemple")
    b.hub.login_status = 403
    assert b.install(_u("app-exemple"), prompt_password=prompt) == 0
    assert len(b.hub.bodies("/api/auth/login")) == 1
    prompt.assert_not_called()
    assert "403" in b.out() and "en attente" in b.out().lower()


def test_jeton_admin_expire_en_cours_de_commande_une_reconnexion(b, monkeypatch):
    """Build très long : le jeton (8 h au Hub) peut expirer avant la pré-création
    — on se reconnecte, mots de passe non interactifs en tête."""
    monkeypatch.setenv("HUBCTL_ADMIN_PASSWORD", GOOD_PW)
    b.hub.expires_at = "2000-01-01T00:00:00Z"
    session = actions._HubAdminSession(b.con)
    assert session.token() == TOKEN
    assert session.token() == TOKEN
    assert len(b.hub.bodies("/api/auth/login")) == 2
    assert "admin-test" not in b.out()


def test_hub_injoignable_pour_le_login_n_bloque_pas(b, admin_pw):
    b.hub.down = True
    b.shell.add("app-exemple")
    assert b.install(_u("app-exemple")) == 0
    assert "en attente" in b.out().lower()


# ══ (g) App déjà installée ═══════════════════════════════════════════════════


def test_app_deja_installee_est_sautee(b, admin_pw):
    """(g) APPS/<slug> existe → « déjà installée — ./hubctl update <slug> », rc 0."""
    _present(b)
    rc = b.install(_u("app-exemple"))
    assert rc == 0
    out = b.out()
    assert "déjà installée" in out and "./hubctl update app-exemple" in out
    assert b.shell.make == [] and not any(e.startswith("clone:") for e in b.events)


def test_deja_installee_rattrape_l_approbation(b, admin_pw):
    """« … ou relance avec HUBCTL_ADMIN_PASSWORD » doit être vrai : une app
    installée mais encore en attente au registre est approuvée, puis son cert
    est resynchronisé (api relancé avant le frontend). Jamais de création."""
    _present(b, cert=b.pki.self_signed)
    b.hub.apps["app-exemple"] = {"slug": "app-exemple", "approval_status": "pending",
                                 "is_active": False, "git_repo_url": None}
    rc = b.install(_u("app-exemple"))
    assert rc == 0
    assert b.hub.paths() == ["POST /api/auth/login", "GET /api/apps/",
                             "POST /api/apps/app-exemple/approve", "GET /api/pki/ca.crt"]
    ev = b.events
    assert (ev.index("http:GET /api/pki/ca.crt") < ev.index("make:app-exemple:restart-api")
            < ev.index("make:app-exemple:restart-frontend"))
    assert b.shell.targets("app-exemple") == [("restart-api", 120), ("restart-frontend", 120)]
    out = b.out()
    assert "approuvée" in out and "CA du Hub" in out


def test_deja_installee_rejetee_n_est_pas_approuvee(b, admin_pw):
    _present(b)
    b.hub.apps["app-exemple"] = {"slug": "app-exemple", "approval_status": "rejected",
                                 "is_active": False, "git_repo_url": None}
    rc = b.install(_u("app-exemple"))
    assert rc == 0
    assert b.hub.paths() == ["POST /api/auth/login", "GET /api/apps/"]
    assert "rejetée" in b.out() and b.shell.make == []


def test_deja_installee_deja_approuvee_n_est_jamais_relancee(b, admin_pw):
    """Ré-exécuter la commande sur un parc déjà en place ne doit PAS redémarrer
    des apps de production : seule une approbation qui vient d'avoir lieu
    justifie la relance (certificat)."""
    _present(b, cert=b.pki.self_signed)
    b.hub.apps["app-exemple"] = {"slug": "app-exemple", "approval_status": "approved",
                                 "is_active": True, "git_repo_url": None}
    rc = b.install(_u("app-exemple"))
    assert rc == 0
    # Lecture seule : login, registre, puis la CA pour étiqueter le certificat en place.
    assert b.hub.paths() == ["POST /api/auth/login", "GET /api/apps/", "GET /api/pki/ca.crt"]
    assert b.shell.make == [] and b.shell.compose == []
    out = b.out()
    assert "déjà installée" in out and "approuvée" in out and "auto-signé" in out


def test_deja_installee_rattrapage_api_ko_apres_relance_echec(b, admin_pw):
    _present(b, cert=b.pki.self_signed)
    b.hub.apps["app-exemple"] = {"slug": "app-exemple", "approval_status": "pending",
                                 "is_active": False, "git_repo_url": None}
    b.health_result = lambda target: target != "app-exemple"
    assert b.install(_u("app-exemple")) == 1


def test_deja_installee_sans_jeton_n_interroge_pas_le_hub_ni_ne_prompte(b):
    _present(b)
    prompt = mock.Mock(return_value=GOOD_PW)
    rc = b.install(_u("app-exemple"), prompt_password=prompt)
    assert rc == 0
    assert b.hub.calls == []
    prompt.assert_not_called()                # jamais de prompt pour une app déjà installée
    assert "déjà installée" in b.out()


# ══ (i) seed = boucle sur le même chemin ═════════════════════════════════════


def test_seed_et_install_passent_par_le_meme_chemin(b, monkeypatch):
    """(i) Nouvelle app : le seed (CAS A) et `app install` traversent le même
    `_build_up_health` avec les mêmes budgets — pas de logique dupliquée."""
    spy = mock.Mock(wraps=actions._build_up_health)
    monkeypatch.setattr(actions, "_build_up_health", spy)
    b.shell.add("mon-app")
    ok, detail = actions._seed_one_app(b.con, _u("mon-app"), "mon-app")
    assert ok is True, detail
    b.shell.add("app-exemple")
    assert b.install(_u("app-exemple")) == 0

    assert spy.call_count == 2
    for slug in ("mon-app", "app-exemple"):
        assert (slug, "build", 1800) in b.shell.make
        assert (slug, "up", 600) in b.shell.make
        assert (slug, 300) in b.health_calls
    assert b.coffre == ["mon-app", "app-exemple"]


def test_seed_convergence_garde_les_budgets_d_update(b):
    """CAS C (app déjà déployée : pull + build + up + health) reste une
    convergence, au budget de `hubctl update` — pas celui du premier démarrage."""
    d = b.app_dir("mon-app")
    d.mkdir()
    (d / ".env").write_text("API_PORT=9000\n")
    b.shell.installed["mon-app"] = b.shell.add("mon-app")
    ok, detail = actions._seed_one_app(b.con, _u("mon-app"), "mon-app")
    assert ok is True, detail
    assert b.shell.targets("mon-app") == [("build", 600), ("up", 120)]
    assert ("mon-app", 60) in b.health_calls


def test_build_up_health_appelle_before_up_entre_build_et_up(b):
    b.shell.installed["app-exemple"] = b.shell.add("app-exemple")
    vu = []
    ok, step = actions._build_up_health(
        b.con, b.app_dir("app-exemple"), build_timeout=1, up_timeout=2, health_timeout=3,
        before_up=lambda: vu.append("before_up"))
    assert (ok, step) == (True, "")
    assert vu == ["before_up"]
    assert b.shell.targets("app-exemple") == [("build", 1), ("up", 2)]


def test_build_up_health_renonce_des_le_build(b):
    b.shell.installed["app-exemple"] = b.shell.add("app-exemple", build_fails=True)
    ok, step = actions._build_up_health(b.con, b.app_dir("app-exemple"))
    assert (ok, step) == (False, "build")
    assert b.shell.targets("app-exemple") == [("build", 1800)]


# ══ Certificat signé par la CA du Hub ════════════════════════════════════════


def test_certificat_api_relance_avant_le_frontend_via_targets_makefile(b, admin_pw):
    b.shell.add("app-exemple")
    assert b.install(_u("app-exemple")) == 0
    ev = b.events
    assert (ev.index("make:app-exemple:up") < ev.index("make:app-exemple:restart-api")
            < ev.index("health:app-exemple", ev.index("make:app-exemple:restart-api"))
            < ev.index("make:app-exemple:restart-frontend"))
    assert b.health_calls == [("hub", 30), ("app-exemple", 300), ("app-exemple", 120)]
    assert b.shell.compose == []
    assert "CA du Hub" in b.out()


def test_certificat_relance_via_docker_compose_si_pas_de_targets(b, admin_pw):
    b.shell.add("app-exemple", makefile=MAKEFILE_NO_RESTART)
    assert b.install(_u("app-exemple")) == 0
    assert b.shell.compose == [("api", str(b.app_dir("app-exemple")), 120),
                               ("frontend", str(b.app_dir("app-exemple")), 120)]
    assert not any(t.startswith("restart") for _s, t, _b in b.shell.make)
    assert "CA du Hub" in b.out()


def test_certificat_relance_impossible_info_auto_signe(b, admin_pw):
    """Ni cible `restart-*` au Makefile, ni service `api` au compose (app d'un
    autre gabarit) : message d'information, certificat auto-signé en service."""
    b.shell.add("app-exemple", makefile=MAKEFILE_MINIMAL, services=("backend", "web"))
    assert b.install(_u("app-exemple")) == 0
    assert not any("restart" in e for e in b.events)
    out = b.out()
    assert "auto-signé" in out and "CA du Hub" not in out


def test_certificat_reste_auto_signe_si_cert_sync_echoue(b, admin_pw):
    b.shell.add("app-exemple", cert_sync_ok=False)
    assert b.install(_u("app-exemple")) == 0
    assert "make:app-exemple:restart-api" in b.events
    assert "auto-signé" in b.out() and "CA du Hub" not in b.out()


def test_api_ne_repond_plus_apres_relance_echec(b, admin_pw):
    b.shell.add("app-exemple")
    controles = []

    def health(target):
        controles.append(target)
        return target != "app-exemple" or len(controles) < 3   # saine après `up`, plus après
    b.health_result = health
    assert b.install(_u("app-exemple")) == 1
    assert controles == ["hub", "app-exemple", "app-exemple"]


def test_cert_signed_by_hub(b):
    f = actions._cert_signed_by_hub
    assert f(b.pki.hub_signed.encode(), b.pki.ca_pem) == "hub"
    assert f(b.pki.self_signed.encode(), b.pki.ca_pem) == "self"
    assert f(b.pki.other_ca.encode(), b.pki.ca_pem) == "other"
    assert f(b.pki.hub_signed.encode(), None) == "other", "sans la CA : signé, mais non vérifié"
    assert f(b"pas un certificat", b.pki.ca_pem) == ""
    assert f(b.pki.hub_signed.encode(), "pas une CA") == "other"


class _Horloge:
    """time simulé pour la boucle d'attente du certificat."""

    def __init__(self, monkeypatch, on_sleep=None):
        self.t = 0.0
        self._on_sleep = on_sleep
        horloge = self

        class FakeTime:
            @staticmethod
            def monotonic():
                return horloge.t

            @staticmethod
            def sleep(s):
                horloge.t += s
                if horloge._on_sleep:
                    horloge._on_sleep(horloge.t)

        monkeypatch.setattr(actions, "time", FakeTime)


def test_wait_hub_signed_cert_attend_la_reecriture(b, monkeypatch):
    """cert_sync tourne au démarrage de l'api : on attend que ssl/server.crt
    soit signé par la CA du Hub avant de relancer le frontend."""
    cert = b.app_dir("app-exemple") / "ssl" / "server.crt"
    _Shell._write_cert(cert.parent.parent, b.pki.self_signed)
    _Horloge(monkeypatch, on_sleep=lambda t: cert.write_text(b.pki.hub_signed) if t >= 4 else None)
    assert b.real_wait_cert(cert, b.pki.ca_pem, timeout=60) is True


def test_wait_hub_signed_cert_renonce_au_budget(b, monkeypatch):
    cert = b.app_dir("app-exemple") / "ssl" / "server.crt"
    _Shell._write_cert(cert.parent.parent, b.pki.self_signed)
    horloge = _Horloge(monkeypatch)
    assert b.real_wait_cert(cert, b.pki.ca_pem, timeout=60) is False
    assert 60 <= horloge.t < 70


def test_wait_hub_signed_cert_refuse_une_autre_ca(b, monkeypatch):
    cert = b.app_dir("app-exemple") / "ssl" / "server.crt"
    _Shell._write_cert(cert.parent.parent, b.pki.other_ca)
    horloge = _Horloge(monkeypatch)
    assert b.real_wait_cert(cert, b.pki.ca_pem, timeout=20) is False
    assert horloge.t >= 20


def test_wait_hub_signed_cert_sans_fichier(b, monkeypatch):
    _Horloge(monkeypatch)
    assert b.real_wait_cert(b.apps_dir / "ssl" / "server.crt", None, timeout=10) is False


# ══ Résumé, prérequis, dry-run, confirmation ═════════════════════════════════


def test_resume_final(b, admin_pw):
    b.shell.add("app-exemple")
    assert b.install(_u("app-exemple")) == 0
    out = b.out()
    assert "app-exemple" in out
    assert f"https://{HOST}:4201" in out
    assert "approuvée" in out and "CA du Hub" in out
    assert f"http://{HOST}:8000/api/pki/ca.crt" in out


def test_precheck_hub_arrete(b):
    b.health_result = lambda target: False
    b.shell.add("app-exemple")
    assert b.install(_u("app-exemple")) == 1
    assert b.health_calls == [("hub", 30)]
    assert b.shell.make == [] and not any(e.startswith("clone:") for e in b.events)
    assert "hubctl start" in b.out()


def test_precheck_docker_injoignable(b):
    b.docker_ok = False
    assert b.install(_u("app-exemple")) == 1
    assert "Docker" in b.out() and b.shell.make == []


def test_precheck_apps_non_inscriptible(b):
    b.apps_writable = False
    assert b.install(_u("app-exemple")) == 1
    assert "inscriptible" in b.out() and not any(e.startswith("clone:") for e in b.events)


def test_apps_dir_absent_est_cree(b):
    b.apps_dir.rmdir()
    b.shell.add("app-exemple")
    assert b.install(_u("app-exemple")) == 0
    assert b.app_dir("app-exemple").is_dir()


@pytest.mark.skipif(os.geteuid() == 0, reason="root écrit partout : os.access toujours vrai")
def test_apps_dir_writable_vrai_fichier(b):
    assert b.real_writable() is True
    b.apps_dir.rmdir()
    assert b.real_writable() is True, "APPS/ absent : c'est le parent qui doit l'être"
    assert not b.apps_dir.exists(), "le contrôle ne crée rien"
    b.apps_dir.mkdir(mode=0o500)
    try:
        assert b.real_writable() is False
    finally:
        os.chmod(b.apps_dir, 0o700)


def test_dry_run_ne_clone_rien(b):
    b.health_result = lambda target: pytest.fail("dry-run : pas de précheck réel")
    b.shell.add("app-exemple")
    assert b.install(_u("app-exemple"), dry_run=True) == 0
    assert b.shell.make == [] and b.hub.calls == [] and not b.app_dir("app-exemple").exists()
    out = b.out()
    assert "(dry-run)" in out and "serait installée" in out and "seraient en service" in out
    assert "✓ installée" not in out and "app(s) en service" not in out, "un dry-run ne met rien en service"


def test_confirmation_par_defaut_annulation(b):
    tmp_seed(b, {"dep-a": _u("dep-a")})
    b.shell.add("dep-a")
    b.shell.add("app-exemple", depends=[("dep-a", "")])
    vues = []

    def confirm(message):
        vues.append(message)
        return False

    rc = b.install(_u("app-exemple"), yes=False, confirm=confirm)
    assert rc == 1
    assert len(vues) == 1 and "app-exemple" in vues[0] and "dep-a" in vues[0]
    assert "clone:dep-a" not in b.events and not b.app_dir("app-exemple").exists()
    assert "annulée" in b.out()


def test_confirmation_acceptee_installe(b):
    b.shell.add("app-exemple")
    rc = b.install(_u("app-exemple"), yes=False, confirm=lambda message: True)
    assert rc == 0 and "make:app-exemple:up" in b.events


def test_yes_et_sans_tty_pas_de_confirmation(b):
    b.shell.add("app-exemple")
    confirm = mock.Mock(return_value=False)
    assert b.install(_u("app-exemple"), confirm=confirm) == 0
    confirm.assert_not_called()


# ══ Cibles, slugs, échecs ════════════════════════════════════════════════════


def test_fichier_seed_yaml_en_argument_unique(b):
    seed = tmp_seed(b, {"app-a": _u("app-a"), "app-b": _u("app-b")})
    b.shell.add("app-a")
    b.shell.add("app-b")
    assert b.install([str(seed)]) == 0
    assert "make:app-a:up" in b.events and "make:app-b:up" in b.events


def test_fichier_yaml_avec_autres_arguments_refuse(b):
    seed = tmp_seed(b, {"app-a": _u("app-a")})
    assert b.install([str(seed), _u("app-b")]) == 1
    assert "unique" in b.out().lower() and b.shell.make == []


def test_fichier_yaml_introuvable_erreur_claire(b):
    assert b.install([str(hub_state.PARC_ROOT / "absent.yaml")]) == 1
    assert "introuvable" in b.out()


def test_fichier_yaml_illisible_erreur_claire(b):
    p = hub_state.PARC_ROOT / "cassee.yaml"
    p.write_text("apps: [unclosed\n")
    assert b.install([str(p)]) == 1
    assert f"Fichier seed illisible : {p}" in b.out()
    assert b.shell.make == []


def test_url_et_slug_invalides_erreurs_claires(b):
    assert b.install(["pas-une-url"]) == 1
    assert "URL git invalide" in b.out()
    assert b.install(["git@github.com:exemple/Mon.App.git"]) == 1
    out = b.out()
    assert "Slug invalide" in out and "tirets" in out and "mon.app" in out
    assert b.install([]) == 1
    assert b.shell.make == []


def test_echec_build_rc1_dossier_conserve(b):
    """Comme le seed : le dossier reste pour diagnostic (logs, .env), rc 1."""
    b.shell.add("app-exemple", build_fails=True)
    assert b.install(_u("app-exemple")) == 1
    assert b.app_dir("app-exemple").exists()
    assert not any(t == "up" for _s, t, _b in b.shell.make)
    assert "build" in b.out().lower()


def test_clone_impossible_rc1(b):
    b.shell.add("app-exemple", clone_fails=True)
    assert b.install(_u("app-exemple")) == 1
    assert "clone" in b.out().lower() and b.shell.make == []


def test_app_absente_du_depot_git(b):
    assert b.install(_u("inconnue")) == 1
    assert "clone" in b.out().lower()


def test_make_sync_env_absent_n_est_pas_une_erreur(b, admin_pw):
    b.shell.add("app-exemple", makefile=MAKEFILE_MINIMAL)
    assert b.install(_u("app-exemple")) == 0
    assert b.shell.targets("app-exemple") == [("build", 1800), ("up", 600)]
    assert [svc for svc, _cwd, _t in b.shell.compose] == ["api", "frontend"]


def test_structure_non_template_supprime_le_clone(b):
    b.shell.add("legacy", files=("README.md",))
    assert b.install(_u("legacy")) == 1
    assert not b.app_dir("legacy").exists()
    assert "template" in b.out().lower()


def test_install_targets_et_slug():
    assert actions._install_targets([_u("APP_EXEMPLE"), _u("Mon-App")]) == [
        (_u("APP_EXEMPLE"), "app-exemple"), (_u("Mon-App"), "mon-app")]
    with pytest.raises(ValueError, match="invalide"):
        actions._install_targets(["git@github.com:exemple/x.git", ""])
    with pytest.raises(ValueError, match="Slug invalide dérivé"):
        actions._install_targets(["git@github.com:exemple/A.git"])      # 1 caractère


def test_read_depends(b):
    d = b.app_dir("x")
    d.mkdir()
    assert actions._read_depends(d) == [], "pas de depends.yaml = app autonome"
    (d / "depends.yaml").write_text(
        "depends_on:\n  - slug: dep-a\n    note: « référentiel »\n  - slug: dep-b\n")
    assert actions._read_depends(d) == [("dep-a", "« référentiel »"), ("dep-b", "")]
    (d / "depends.yaml").write_text("- une liste\n")
    assert actions._read_depends(d) == []


def test_makefile_has_target_et_compose_services(b):
    d = b.app_dir("x")
    d.mkdir()
    (d / "Makefile").write_text(MAKEFILE_FULL)
    (d / "docker-compose.yml").write_text(_compose(services=("api", "frontend", "db")))
    assert actions._makefile_has_target(d, "restart-api") is True
    assert actions._makefile_has_target(d, "restart-api-x") is False
    assert actions._makefile_has_target(d, "sync-env") is True
    assert actions._compose_services(d) == {"api", "frontend", "db"}
    (d / "docker-compose.yml").write_text("services: [unclosed")
    assert actions._compose_services(d) == set()


# ══ _hub_api : vrai HTTP sur boucle locale (le reste de la suite le simule) ═══


class _Echo(BaseHTTPRequestHandler):
    """Mini serveur : jeton attendu, JSON et texte brut, redirection 307."""

    def _send(self, payload, status=200, ctype="application/json"):
        body = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/pki/ca.crt":
            self._send("-----BEGIN CERTIFICATE-----\nEXEMPLE\n-----END CERTIFICATE-----\n",
                       ctype="text/plain")
        elif self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send({"detail": "jeton manquant"}, 401)
        else:
            self._send([{"slug": "app-exemple", "git_repo_url": _u("app-exemple")}])

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"null")
        if self.path == "/api/apps":                 # sans le slash final
            self.send_response(307)
            self.send_header("Location", "/api/apps/")
            self.end_headers()
        elif self.path == "/api/auth/login":         # donne le jeton : n'en exige pas
            if body.get("password") == GOOD_PW:
                self._send({"access_token": TOKEN, "expires_at": "2999-01-01T00:00:00+00:00"})
            else:
                self._send({"detail": "Identifiants incorrects"}, 401)
        elif self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send({"detail": "jeton manquant"}, 401)
        elif self.path == "/api/apps/":
            self._send({"application": {**body, "approval_status": "approved"}}, 201)
        else:
            self._send({"detail": "Introuvable"}, 404)

    def log_message(self, *a):
        pass


@pytest.fixture
def reel(b, monkeypatch):
    """Restaure le vrai `_hub_api` contre un serveur HTTP local."""
    monkeypatch.setattr(actions, "_hub_api", b.real_hub_api)
    srv = HTTPServer(("127.0.0.1", 0), _Echo)
    b.set_hub_env(HUB_API_PORT=str(srv.server_address[1]))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    t.join(timeout=5)


def test_hub_api_get_avec_jeton(reel):
    status, apps = actions._hub_api("GET", "/api/apps/", token=TOKEN)
    assert status == 200 and apps[0]["slug"] == "app-exemple"


def test_hub_api_sans_jeton_401_et_corps(reel):
    assert actions._hub_api("GET", "/api/apps/") == (401, {"detail": "jeton manquant"})


def test_hub_api_corps_texte_brut(reel):
    status, pem = actions._hub_api("GET", "/api/pki/ca.crt")
    assert status == 200 and pem.startswith("-----BEGIN CERTIFICATE-----")


def test_hub_admin_login(reel):
    status, token, expires = actions._hub_admin_login("admin", GOOD_PW)
    assert status == 200 and token == TOKEN and expires > 2 ** 33
    assert actions._hub_admin_login("admin", "erreur")[:2] == (401, None)


def test_hub_api_hub_injoignable(b, reel):
    b.set_hub_env(HUB_API_PORT="1")           # rien n'écoute : connexion refusée
    assert actions._hub_api("GET", "/api/apps/", token=TOKEN) == (0, None)


def test_hub_api_post_apps_201(reel):
    status, data = actions._hub_api("POST", "/api/apps/", token=TOKEN,
                                    body={"slug": "app-exemple", "name": "App exemple"})
    assert status == 201 and data["application"]["approval_status"] == "approved"


def test_hub_api_post_chemin_exact_sans_slash_307(reel):
    """Le POST ne suit pas les redirections : le chemin doit être exact
    (`/api/apps/`, slash final) — sinon 307, jamais 201."""
    status, _ = actions._hub_api("POST", "/api/apps", token=TOKEN, body={})
    assert status == 307


# ── depends.yaml vide et bien formé : pas d'avertissement (verdict VM 26/11) ──
# Le gabarit livre `depends_on: []` : toute app issue du gabarit affichait
# « ⚠ depends.yaml … illisible ou sans dépendance déclarée ».

def _plan_output(tmp_path, depends_text):
    import io
    from hubctl_cli.actions import _InstallResult, _InstallRun, _install_plan
    from rich.console import Console

    (tmp_path / "depends.yaml").write_text(depends_text, encoding="utf-8")
    out = io.StringIO()
    run = _InstallRun(Console(file=out, width=120), no_deps=False, yes=True, dry_run=False)
    res = _InstallResult(slug="app-a", url="git@x:app-a.git")
    plan = _install_plan(run, res, tmp_path)
    return plan, out.getvalue()


def test_depends_yaml_empty_from_template_is_silent(tmp_path):
    plan, output = _plan_output(tmp_path, "depends_on: []\n")
    assert plan == []
    assert "depends.yaml" not in output, "depends_on: [] (gabarit) ne doit produire aucun avertissement"


def test_depends_yaml_without_depends_on_key_is_silent(tmp_path):
    plan, output = _plan_output(tmp_path, "# dépendances inter-apps\n")
    assert plan == []
    assert "depends.yaml" not in output


def test_depends_yaml_unreadable_warns(tmp_path):
    plan, output = _plan_output(tmp_path, "depends_on: [42, {pas: une dependance\n  {{{\n")
    assert plan == []
    assert "depends.yaml" in output and "illisible" in output


# ── Coffre pas encore créé (installation fraîche) : information, pas avertissement ──
# Verdict VM du 26/11/2026 : le premier `app install` d'un déploiement neuf
# affichait « ⚠ Coffre verrouillé — secrets non sauvegardés. Sync manuelle via le
# menu Coffre. » — il n'y a simplement pas encore de coffre (créé à la première
# ouverture interactive du menu Coffre).

def _coffre_output(monkeypatch, tmp_path, *, coffre_existe):
    import io
    from types import SimpleNamespace
    from hubctl_cli import actions, hub_state
    from rich.console import Console

    coffre = tmp_path / "vault" / "parc.vault.enc"
    coffre.parent.mkdir(parents=True, exist_ok=True)
    if coffre_existe:
        coffre.write_bytes(b"coffre")
    monkeypatch.setattr(hub_state, "vault_path", lambda *a, **k: coffre)
    monkeypatch.setattr(actions, "_get_vault", lambda: SimpleNamespace(is_locked=True))
    out = io.StringIO()
    actions._sync_app_coffre(Console(file=out, width=120), "app-a")
    return out.getvalue()


def test_sync_coffre_without_any_vault_is_an_information(monkeypatch, tmp_path):
    text = _coffre_output(monkeypatch, tmp_path, coffre_existe=False)
    assert "⚠" not in text, "sans coffre, pas d'avertissement"
    assert "Pas de coffre" in text and "app-a" in text and "Coffre" in text


def test_sync_coffre_locked_existing_vault_keeps_its_warning(monkeypatch, tmp_path):
    """Un coffre EXISTANT mais verrouillé : le rappel reste (il y a quelque chose à sync)."""
    text = _coffre_output(monkeypatch, tmp_path, coffre_existe=True)
    assert "⚠ Coffre verrouillé" in text
