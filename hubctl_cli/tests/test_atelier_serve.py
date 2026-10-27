"""Tests de `hubctl atelier serve` — mini-serveur HTTP hôte (list/close-all).

Serveur de test lancé sur un port libre (0) dans un thread, APPS_DIR isolé
dans tmp_path, requêtes via httpx. Contrat JSON (proxifié par le Hub) :
- GET  /health            → 200 {"status": "ok"} (sans auth, liveness)
- GET  /atelier/list      → 200 {"ateliers": [...]} (auth X-Atelier-Token)
- POST /atelier/close-all → 200/207 {"closed": [...], "failed": [...], "rc", "output"}
"""
import threading
import types
from pathlib import Path

import httpx
import pytest

from hubctl_cli import __main__ as m
from hubctl_cli import atelier_serve, hub_state

TOKEN = "test-token-1234"


@pytest.fixture
def serve_env(monkeypatch, tmp_path):
    """Serveur de test sur port libre dans un thread + APPS_DIR isolé + faux docker."""
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path)
    monkeypatch.setattr(atelier_serve, "_ws_containers_state", lambda: {})
    token_file = tmp_path / "atelier_token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    server = atelier_serve.build_server(0, token_file)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield types.SimpleNamespace(
        server=server,
        base=f"http://127.0.0.1:{server.server_address[1]}",
        tmp=tmp_path,
        token_file=token_file,
    )
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _get(base, path, token=TOKEN):
    headers = {"X-Atelier-Token": token} if token is not None else {}
    return httpx.get(base + path, headers=headers, timeout=10)


def _post(base, path, token=TOKEN):
    headers = {"X-Atelier-Token": token} if token is not None else {}
    return httpx.post(base + path, headers=headers, timeout=10)


def _make_atelier(tmp_path, slug, api_port="10001", fe_port="5201"):
    d = tmp_path / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / ".workshop.env").write_text(
        f"APP_SLUG={slug}-ws\nAPI_PORT={api_port}\nFRONTEND_PORT={fe_port}\n",
        encoding="utf-8")
    return d


# ── Routes : les 6 cas du brief ──

def test_health(serve_env):
    """GET /health : 200, sans auth (liveness), shape fixe."""
    r = httpx.get(serve_env.base + "/health", timeout=10)
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_list_empty(serve_env):
    """Aucun .workshop.env → {"ateliers": []}."""
    r = _get(serve_env.base, "/atelier/list")
    assert r.status_code == 200
    assert r.json() == {"ateliers": []}


def test_list_shows_atelier(serve_env, monkeypatch):
    """Un .workshop.env → entrée {app, ws_slug, api_port, frontend_port, containers_state}."""
    _make_atelier(serve_env.tmp, "exemple-app")
    monkeypatch.setattr(atelier_serve, "_ws_containers_state",
                        lambda: {"exemple-app-ws-api-1": "Up 2 minutes"})
    r = _get(serve_env.base, "/atelier/list")
    assert r.status_code == 200
    assert r.json() == {"ateliers": [{
        "app": "exemple-app",
        "ws_slug": "exemple-app-ws",
        "api_port": 10001,
        "frontend_port": 5201,
        "containers_state": {"exemple-app-ws-api-1": "Up 2 minutes"},
    }]}


def test_close_all_ok(serve_env, monkeypatch):
    """close-all qui ferme tout → 200 {"closed": ["exemple-app"], "failed": []}."""
    _make_atelier(serve_env.tmp, "exemple-app")

    def fake_close_all(console, *, dry_run=False, oui=False):
        assert oui is True  # le serveur ne doit jamais re-demander confirmation
        (serve_env.tmp / "exemple-app" / ".workshop.env").unlink()
        console.print("[green]✓ 1 atelier(s) fermé(s) — base propre.[/green]")
        return 0

    monkeypatch.setattr(atelier_serve.actions, "action_atelier_close_all",
                        fake_close_all)
    r = _post(serve_env.base, "/atelier/close-all")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["closed"] == ["exemple-app"]
    assert body["failed"] == []
    assert body["rc"] == 0
    assert "fermé" in body["output"]


def test_close_all_partial(serve_env, monkeypatch):
    """Un atelier en échec → 207 + failed [{slug, erreur}], closed pour les autres."""
    _make_atelier(serve_env.tmp, "exemple-app")
    _make_atelier(serve_env.tmp, "canari")

    def fake_close_all(console, *, dry_run=False, oui=False):
        (serve_env.tmp / "exemple-app" / ".workshop.env").unlink()
        console.print("[red]✗ canari : suppression en échec.[/red]")
        return 1

    monkeypatch.setattr(atelier_serve.actions, "action_atelier_close_all",
                        fake_close_all)
    r = _post(serve_env.base, "/atelier/close-all")
    assert r.status_code == 207
    body = r.json()
    assert body["closed"] == ["exemple-app"]
    assert [f["slug"] for f in body["failed"]] == ["canari"]
    assert body["failed"][0]["erreur"]
    assert body["rc"] == 1
    assert "échec" in body["output"]


def test_close_all_nothing_to_do(serve_env, monkeypatch):
    """Aucun atelier actif → 200, listes vides (rc 0 de l'action)."""
    monkeypatch.setattr(atelier_serve.actions, "action_atelier_close_all",
                        lambda console, *, dry_run=False, oui=False: 0)
    r = _post(serve_env.base, "/atelier/close-all")
    assert r.status_code == 200
    assert r.json()["closed"] == [] and r.json()["failed"] == []


def test_auth_required(serve_env):
    """Sans header → 401 ; mauvais token → 403 (list et close-all)."""
    assert _get(serve_env.base, "/atelier/list", token=None).status_code == 401
    assert _get(serve_env.base, "/atelier/list", token="wrong").status_code == 403
    assert _post(serve_env.base, "/atelier/close-all", token=None).status_code == 401
    assert _post(serve_env.base, "/atelier/close-all", token="wrong").status_code == 403


def test_unknown_route_404(serve_env):
    assert _get(serve_env.base, "/atelier/nope").status_code == 404
    assert _get(serve_env.base, "/n'importe/quoi").status_code == 404
    assert _post(serve_env.base, "/whatever").status_code == 404


# ── Méthodes HTTP ──

def test_close_all_rejects_get(serve_env):
    """close-all mute l'état : POST obligatoire (GET → 405)."""
    r = _get(serve_env.base, "/atelier/close-all")
    assert r.status_code == 405


def test_list_rejects_post(serve_env):
    assert _post(serve_env.base, "/atelier/list").status_code == 405


# ── collect_ateliers / docker ──

def test_parse_docker_ps():
    out = ("exemple-app-ws-api-1\tUp 3 minutes\n"
           "canari-ws-frontend-1\tUp About a minute\n"
           "pas-de-tab\n"
           "\n")
    assert atelier_serve._parse_docker_ps(out) == {
        "exemple-app-ws-api-1": "Up 3 minutes",
        "canari-ws-frontend-1": "Up About a minute",
    }


def test_collect_ateliers_docker_unreachable(monkeypatch, tmp_path):
    """docker absent/en échec → containers_state {} (liste des ateliers intacte)."""
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path)
    monkeypatch.setattr(atelier_serve, "_ws_containers_state", lambda: None)
    _make_atelier(tmp_path, "canari")
    ateliers = atelier_serve.collect_ateliers()
    assert len(ateliers) == 1
    assert ateliers[0]["app"] == "canari"
    assert ateliers[0]["containers_state"] == {}


def test_collect_ateliers_ports_invalid(monkeypatch, tmp_path):
    """Ports absents/non numériques → None, pas de crash."""
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path)
    monkeypatch.setattr(atelier_serve, "_ws_containers_state", lambda: {})
    d = tmp_path / "canari"
    d.mkdir()
    (d / ".workshop.env").write_text("APP_SLUG=canari-ws\n", encoding="utf-8")
    ateliers = atelier_serve.collect_ateliers()
    assert ateliers[0]["api_port"] is None
    assert ateliers[0]["frontend_port"] is None


# ── build_server / token ──

def test_build_server_binds_loopback(serve_env):
    assert serve_env.server.server_address[0] == "127.0.0.1"


def test_build_server_rejects_missing_token_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        atelier_serve.build_server(0, tmp_path / "absent")


def test_build_server_rejects_empty_token(tmp_path):
    f = tmp_path / "vide"
    f.write_text("  \n", encoding="utf-8")
    with pytest.raises(ValueError):
        atelier_serve.build_server(0, f)


def test_serve_missing_token_file_returns_2(tmp_path, capsys):
    """hubctl atelier --serve sans token : erreur explicite, rc 2, pas de blocage."""
    rc = atelier_serve.serve(port=0, token_file=tmp_path / "absent")
    assert rc == 2
    out = capsys.readouterr().out
    assert "token" in out.lower()


# ── Dispatch CLI (`atelier --serve`) ──

class TestDispatchServe:
    def test_parser_serve(self):
        a = m.build_parser().parse_args(["atelier", "--serve"])
        assert a.atelier_serve is True
        assert a.atelier_port == 8777
        assert a.atelier_token_file is None

    def test_parser_port(self):
        a = m.build_parser().parse_args(["atelier", "--serve", "--port", "9000"])
        assert a.atelier_port == 9000

    def test_parser_token_file(self):
        a = m.build_parser().parse_args(
            ["atelier", "--serve", "--token-file", "/tmp/mon-token"])
        assert a.atelier_token_file == Path("/tmp/mon-token")

    def test_serve_exclusif_avec_maj(self):
        with pytest.raises(SystemExit):
            m.build_parser().parse_args(["atelier", "--serve", "--maj"])

    def test_serve_exclusif_avec_prod(self):
        with pytest.raises(SystemExit):
            m.build_parser().parse_args(["atelier", "--serve", "--prod"])

    def test_serve_exclusif_avec_close_all(self):
        with pytest.raises(SystemExit):
            m.build_parser().parse_args(["atelier", "--serve", "--close-all"])

    def test_serve_avec_slug_erreur_explicite(self, capsys):
        """--serve ne prend pas de slug : message clair, rc 2, rien ne démarre."""
        assert m.main(["atelier", "exemple-app", "--serve"]) == 2
        out = capsys.readouterr().out
        assert "--serve" in out and "slug" in out

    def test_dispatch_serve_wiring(self, monkeypatch):
        """Le dispatch appelle serve(port, token_file) et remonte son rc."""
        calls = {}

        def fake_serve(port=None, token_file=None):
            calls["args"] = (port, token_file)
            return 0

        monkeypatch.setattr(atelier_serve, "serve", fake_serve)
        assert m.main(["atelier", "--serve", "--port", "1234",
                       "--token-file", "/tmp/tk"]) == 0
        assert calls["args"] == (1234, Path("/tmp/tk"))
