# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Tests de `hubctl atelier serve` — mini-serveur HTTP hôte (list/close-all/vault).

Serveur de test lancé sur un port libre (0) dans un thread, APPS_DIR isolé
dans tmp_path, requêtes via httpx. Contrat JSON (proxifié par le Hub) :
- GET  /health            → 200 {"status": "ok"} (sans auth, liveness)
- GET  /atelier/list      → 200 {"ateliers": [...]} (auth X-Atelier-Token)
- POST /atelier/close-all → 200/207 {"closed": [...], "failed": [...], "rc", "output"}
- GET  /vault/status      → 200 {"status": "ok"|"locked", …} (voir _vault_state)
- POST /vault/backup      → 200 {"status": …} / 409 / 500 (voir _run_backup)
"""
import json
import threading
import time
import types
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from hubctl_cli import __main__ as m
from hubctl_cli import atelier_serve, hub_state, vault_manager

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


# ── Routes vault : /vault/status + /vault/backup ──

def _make_unlocked_vault(tmp_path, monkeypatch, passphrase="test-vault-pw"):
    """Vrai coffre chiffré (vide) + clé dans tmp_path ; hub_state.vault_path patché.

    Pour les tests qui exécutent le VRAI _run_backup (seul backup_all est
    faked au niveau classe) : unlock_stored() doit réussir sans jamais
    toucher au coffre réel du parc.
    """
    from hubctl_cli.coffre import Vault
    vp = tmp_path / "parc.vault.enc"
    v = Vault(vp)
    assert v.unlock(passphrase)  # coffre neuf : data {} en mémoire
    v.save()
    v.store_passphrase(passphrase)
    monkeypatch.setattr(hub_state, "vault_path", lambda: vp)
    return vp


def test_vault_status_ok(serve_env, monkeypatch):
    """GET /vault/status déverrouillé : payload complet du contrat, relayé tel quel."""
    fake = {
        "status": "ok",
        "locked": False,
        "last_backup": "2026-10-28T09:30:00+00:00",
        "vault_size": 4096,
        "entries": {"env": 3, "files": 5, "apps": 2, "db_dump": True},
        "divergences": ["app:canari", "hub"],
        "pra": {"configured": True, "location": "/mnt/pra"},
    }
    monkeypatch.setattr(atelier_serve, "_vault_state", lambda: fake)
    r = _get(serve_env.base, "/vault/status")
    assert r.status_code == 200
    body = r.json()
    assert body == fake
    # shape exact consommé par le Hub (Task 4/5)
    assert set(body["entries"]) == {"env", "files", "apps", "db_dump"}
    assert body["divergences"] == sorted(body["divergences"])


def test_vault_status_locked(serve_env, monkeypatch):
    """Coffre verrouillé → 200 {"status": "locked", "locked": True}."""
    fake = {"status": "locked", "locked": True, "last_backup": None,
            "vault_size": 0, "pra": {"configured": False, "location": None}}
    monkeypatch.setattr(atelier_serve, "_vault_state", lambda: fake)
    r = _get(serve_env.base, "/vault/status")
    assert r.status_code == 200
    assert r.json()["status"] == "locked"
    assert r.json()["locked"] is True


def test_vault_status_auth(serve_env):
    """/vault/status sous auth : 401 sans header, 403 mauvais token, 405 en POST."""
    assert _get(serve_env.base, "/vault/status", token=None).status_code == 401
    assert _get(serve_env.base, "/vault/status", token="wrong").status_code == 403
    assert _post(serve_env.base, "/vault/status").status_code == 405


def test_vault_backup_ok(serve_env, monkeypatch):
    """POST /vault/backup (helper faked) → 200 {"status": "ok", **résultat}."""
    monkeypatch.setattr(atelier_serve, "_run_backup", lambda: (
        200, {"status": "ok", "env": 3, "files": 5, "db": True, "pra": None}))
    r = _post(serve_env.base, "/vault/backup")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "env": 3, "files": 5, "db": True,
                        "pra": None}


def test_vault_backup_locked(serve_env, monkeypatch):
    """Coffre verrouillé au moment du backup → 200 {"status": "locked"}."""
    monkeypatch.setattr(atelier_serve, "_run_backup",
                        lambda: (200, {"status": "locked"}))
    r = _post(serve_env.base, "/vault/backup")
    assert r.status_code == 200
    assert r.json() == {"status": "locked"}


def test_vault_backup_conflict_409(serve_env, monkeypatch):
    """Un backup déjà en cours → 409 immédiat (pas de mise en file), puis reprise.

    backup_all faked bloqué sur un Event ; le 1er POST part dans un thread
    (il bloque tant que le backup tourne), le démarrage effectif est
    signalé par un 2e Event, le 2e POST doit prendre le 409 sans attendre.
    """
    started = threading.Event()
    release = threading.Event()

    def fake_backup_all(self, *, on_log=None):
        started.set()
        release.wait(timeout=30)  # cap anti-blocage si une assert échoue avant
        return {"env": 1, "files": 0, "db": True, "pra": None}

    monkeypatch.setattr(vault_manager.VaultManager, "backup_all", fake_backup_all)
    _make_unlocked_vault(serve_env.tmp, monkeypatch)

    results = {}

    def post_first():
        results["r1"] = _post(serve_env.base, "/vault/backup")

    t = threading.Thread(target=post_first)
    t.start()
    try:
        assert started.wait(timeout=10), "backup_all jamais démarré"
        r2 = _post(serve_env.base, "/vault/backup")
        assert r2.status_code == 409
        assert r2.json() == {"erreur": "backup déjà en cours"}
    finally:
        release.set()
        t.join(timeout=10)
    assert results["r1"].status_code == 200
    assert results["r1"].json() == {"status": "ok", "env": 1, "files": 0,
                                    "db": True, "pra": None}


def test_vault_backup_pra_failure_not_fatal(serve_env, monkeypatch):
    """pra en échec dans backup_all → 200 status ok, pra.error relayé (non fatal).

    Vrai _run_backup (backup_all faked au niveau classe) : valide la fusion
    {"status": "ok", **résultat} sur le chemin réel du worker.
    """
    def fake_backup_all(self, *, on_log=None):
        return {"env": 2, "files": 1, "db": True, "pra": {"error": "kop absent"}}

    monkeypatch.setattr(vault_manager.VaultManager, "backup_all", fake_backup_all)
    _make_unlocked_vault(serve_env.tmp, monkeypatch)
    r = _post(serve_env.base, "/vault/backup")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "env": 2, "files": 1, "db": True,
                        "pra": {"error": "kop absent"}}


def test_vault_backup_timeout_500_then_recovery(serve_env, monkeypatch):
    """Backup qui dépasse la garde → 500, verrou tenu (409) jusqu'à la fin du job.

    Garde raccourcie à 0.2 s par monkeypatch (le contrat reste > 110 s en
    prod) : le POST n°1 expire côté appelant (500), mais le worker détient
    toujours le verrou — un POST immédiat prend 409 — puis le verrou est
    libéré à la fin du job et un POST ultérieur repasse à 200.
    """
    started = threading.Event()
    release = threading.Event()

    def fake_backup_all(self, *, on_log=None):
        started.set()
        release.wait(timeout=30)  # cap anti-blocage
        return {"env": 0, "files": 0, "db": False, "pra": None}

    monkeypatch.setattr(vault_manager.VaultManager, "backup_all", fake_backup_all)
    monkeypatch.setattr(atelier_serve, "_VAULT_BACKUP_TIMEOUT", 0.2)
    _make_unlocked_vault(serve_env.tmp, monkeypatch)

    results = {}

    def post_first():
        results["r1"] = _post(serve_env.base, "/vault/backup")

    t = threading.Thread(target=post_first)
    t.start()
    try:
        assert started.wait(timeout=10), "backup_all jamais démarré"
        t.join(timeout=10)
        r1 = results["r1"]
        assert r1.status_code == 500
        assert "toujours en cours" in r1.json()["erreur"]
        # Le verrou reste tenu par le worker malgré le 500 déjà répondu.
        r2 = _post(serve_env.base, "/vault/backup")
        assert r2.status_code == 409
    finally:
        release.set()
    # Fin du job → verrou libéré : on sonde jusqu'au retour à la normale
    # (409 transitoires admis pendant que le worker se termine).
    deadline = time.monotonic() + 10
    r3 = _post(serve_env.base, "/vault/backup")
    while r3.status_code != 200 and time.monotonic() < deadline:
        assert r3.status_code == 409
        time.sleep(0.05)
        r3 = _post(serve_env.base, "/vault/backup")
    assert r3.status_code == 200
    assert r3.json()["status"] == "ok"


def test_vault_backup_rejects_get(serve_env):
    """backup = mutation → POST obligatoire (GET → 405)."""
    r = _get(serve_env.base, "/vault/backup")
    assert r.status_code == 405


# ── Helpers vault réels (sans HTTP) ──

def test_vault_state_real_vault(monkeypatch, tmp_path):
    """_vault_state sur un vrai coffre déverrouillé : comptages + divergences + pra."""
    from hubctl_cli.coffre import Vault
    vp = tmp_path / "parc.vault.enc"
    v = Vault(vp)
    assert v.unlock("pw")
    v.set_entry("hub", {"SECRET_HUB": "1"})
    v.set_entry("app:canari", {"SECRET": "1"})
    v.set_file("HUB_base/backup/hub_dump.sql", b"-- dump")
    v._data["__apps__"] = [{"slug": "canari", "git_url": None, "version": None}]
    v.save()
    v.store_passphrase("pw")
    monkeypatch.setattr(hub_state, "vault_path", lambda: vp)
    monkeypatch.setattr(hub_state, "vault_dir", lambda: tmp_path)
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path / "APPS")
    monkeypatch.setattr(hub_state, "HUB_DIR", tmp_path / "HUB_base")
    app_dir = tmp_path / "APPS" / "canari"
    app_dir.mkdir(parents=True)
    (app_dir / ".env").write_text("SECRET=2\n", encoding="utf-8")  # ≠ coffre → divergence
    (tmp_path / "pra_location.json").write_text(
        json.dumps({"path": "/mnt/pra"}), encoding="utf-8")

    state = atelier_serve._vault_state()
    assert state["status"] == "ok" and state["locked"] is False
    assert state["entries"] == {"env": 2, "files": 1, "apps": 1, "db_dump": True}
    assert state["divergences"] == ["app:canari"]
    assert state["vault_size"] == vp.stat().st_size
    assert state["last_backup"] == datetime.fromtimestamp(
        vp.stat().st_mtime, tz=timezone.utc).isoformat()
    assert state["pra"] == {"configured": True, "location": "/mnt/pra"}


def test_vault_state_locked_no_key(monkeypatch, tmp_path):
    """Coffre présent mais clé absente → locked, sans entries ni divergences."""
    from hubctl_cli.coffre import Vault
    vp = tmp_path / "parc.vault.enc"
    v = Vault(vp)
    assert v.unlock("pw")
    v.save()  # pas de store_passphrase : unlock_stored() échouera
    monkeypatch.setattr(hub_state, "vault_path", lambda: vp)
    monkeypatch.setattr(hub_state, "vault_dir", lambda: tmp_path)
    state = atelier_serve._vault_state()
    assert state["status"] == "locked" and state["locked"] is True
    assert "entries" not in state and "divergences" not in state
    assert state["vault_size"] == vp.stat().st_size
    assert state["last_backup"]
    assert state["pra"] == {"configured": False, "location": None}


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


def test_collect_ateliers_containers_no_cross_match(monkeypatch, tmp_path):
    """Conteneurs attribués par préfixe strict : le slug « si » ne captura pas
    les conteneurs de « exemple-app-ws-* » (sous-chaîne ≠ préfixe)."""
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path)
    monkeypatch.setattr(atelier_serve, "_ws_containers_state",
                        lambda: {"exemple-app-ws-api-1": "Up 2 minutes"})
    for slug in ("exemple-app", "si"):
        d = tmp_path / slug
        d.mkdir()
        (d / ".workshop.env").write_text("API_PORT=10001\n", encoding="utf-8")
    by_app = {a["app"]: a for a in atelier_serve.collect_ateliers()}
    assert by_app["exemple-app"]["containers_state"] == {"exemple-app-ws-api-1": "Up 2 minutes"}
    assert by_app["si"]["containers_state"] == {}


# ── build_server / token ──

def test_build_server_binds_loopback(serve_env):
    """Défaut inchangé : bind 127.0.0.1 quand host n'est pas passé."""
    assert serve_env.server.server_address[0] == "127.0.0.1"


def test_build_server_binds_host_passe(tmp_path):
    """build_server accepte host et bind dessus (pas de 127.0.0.1 hardcodé).

    127.0.0.2 ∈ 127.0.0.0/8 : bindable sur Linux sans config, et distinct
    de 127.0.0.1 → prouve que l'host passé est bien utilisé.
    """
    token_file = tmp_path / "atelier_token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    server = atelier_serve.build_server(0, token_file, host="127.0.0.2")
    try:
        assert server.server_address[0] == "127.0.0.2"
        assert server.server_address[1] > 0
    finally:
        server.server_close()


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
        assert a.atelier_host == "127.0.0.1"
        assert a.atelier_token_file is None

    def test_parser_port(self):
        a = m.build_parser().parse_args(["atelier", "--serve", "--port", "9000"])
        assert a.atelier_port == 9000

    def test_parser_host(self):
        a = m.build_parser().parse_args(
            ["atelier", "--serve", "--host", "172.17.0.1"])
        assert a.atelier_host == "172.17.0.1"

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
        """Le dispatch appelle serve(port, token_file, host) et remonte son rc."""
        calls = {}

        def fake_serve(port=None, token_file=None, host=None):
            calls["args"] = (port, token_file, host)
            return 0

        monkeypatch.setattr(atelier_serve, "serve", fake_serve)
        assert m.main(["atelier", "--serve", "--port", "1234",
                       "--host", "172.17.0.1",
                       "--token-file", "/tmp/tk"]) == 0
        assert calls["args"] == (1234, Path("/tmp/tk"), "172.17.0.1")
