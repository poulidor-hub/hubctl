# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Tests de update_serve — statut (Task 1), état/exécution du job (Task 2),
serveur HTTP + branchage CLI `update --serve` (Task 3).

Chaque scénario monte un « parc » factice dans tmp_path : un méta git avec
VERSION + un submodule HUB_base initialisé depuis un dépôt bare (origin).
Contrat collect_update_status (spec §5.1, consommé par GET /update/status) :
{"service": "ok", "checked_at": <ISO8601>, "meta": <component>,
 "components": {rel_path: <component>}}.
Contrat job (spec §5.2, consommé par POST /update/run + GET /update/job) :
start_update → (202, {"job": running}) ou (409, ...) ; l'état final ne vit
que dans update_state.json.
Contrat HTTP (spec §4.1) : GET /health sans auth, GET /update/status
(?refresh=1 → force), GET /update/job, POST /update/run — auth header
X-Atelier-Token (401 absent / 403 invalide), même recette atelier_serve.
"""
import importlib
import json
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from hubctl_cli import park_checks
from hubctl_cli import update_serve


# ── Helpers (recopiés de test_park_checks.py) ───────────────────────────────


def _git(*args: str, cwd: Path) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", cwd=path)
    _git("config", "user.email", "test@test.local", cwd=path)
    _git("config", "user.name", "test", cwd=path)
    # les dépôts « origin » de test acceptent les push (worktree mis à jour)
    _git("config", "receive.denyCurrentBranch", "updateInstead", cwd=path)
    return path


def _commit(path: Path, name: str, msg: str) -> None:
    (path / name).write_text(f"{msg}\n")
    _git("add", "-A", cwd=path)
    _git("commit", "-m", msg, cwd=path)


def _init_repo_with_origin(tmp_path: Path, name: str = "repo") -> tuple[Path, Path]:
    """(work, origin) : clone main + bare servant d'origin."""
    work = tmp_path / name
    origin = tmp_path / f"{name}-origin.git"
    origin.mkdir()
    _git("init", "--bare", "-b", "main", cwd=origin)  # origin = dépôt bare
    _init_repo(work)
    _git("remote", "add", "origin", str(origin), cwd=work)
    _commit(work, "VERSION", "1.0.0")
    _git("push", "-u", "origin", "main", cwd=work)
    return work, origin


def _make_parc(tmp_path: Path) -> tuple[Path, Path, Path]:
    """(parc, sub_checkout, sub_origin) : méta VERSION 1.0.0 + submodule
    HUB_base initialisé (cloné depuis un bare), aligné sur origin/main."""
    _sub, sub_origin = _init_repo_with_origin(tmp_path, "sub")
    parc, _parc_origin = _init_repo_with_origin(tmp_path, "parc")
    _git("-c", "protocol.file.allow=always", "submodule", "add",
         str(sub_origin), "HUB_base", cwd=parc)
    _git("config", "protocol.file.allow", "always", cwd=parc)
    _git("commit", "-am", "add submodule", cwd=parc)
    return parc, parc / "HUB_base", sub_origin


@pytest.fixture(autouse=True)
def _reset_update_serve_state():
    """Cache statut + flag job sont module-level : chaque test repart à froid."""
    update_serve._STATUS_CACHE = None
    update_serve._STATUS_TS = 0.0
    yield
    update_serve._STATUS_CACHE = None
    update_serve._STATUS_TS = 0.0
    update_serve._JOB_RUNNING = False  # aucun état mémoire résiduel de job


# ── Collecte du statut ──────────────────────────────────────────────────────


def test_status_up_to_date(tmp_path):
    """Parc aligné : méta à jour (behind 0, version locale lue)."""
    parc, _sub, _sub_origin = _make_parc(tmp_path)
    status = update_serve.collect_update_status(parc)
    assert status["service"] == "ok"
    assert status["meta"]["update_available"] is False
    assert status["meta"]["commits_behind"] == 0
    assert status["meta"]["version"] == "1.0.0"
    assert status["meta"]["drifted"] == []
    assert "HUB_base" in status["components"]
    assert status["components"]["HUB_base"]["update_available"] is False


def test_status_behind(tmp_path):
    """Un commit poussé côté origin → update_available + version_target."""
    parc, _sub, sub_origin = _make_parc(tmp_path)
    # clone jetable : pousser VERSION 2.0.0 sur l'origin du submodule
    throwaway = tmp_path / "throwaway"
    _git("clone", str(sub_origin), str(throwaway), cwd=tmp_path)
    _git("config", "user.email", "test@test.local", cwd=throwaway)
    _git("config", "user.name", "test", cwd=throwaway)
    (throwaway / "VERSION").write_text("2.0.0\n")
    _git("add", "-A", cwd=throwaway)
    _git("commit", "-m", "2.0.0", cwd=throwaway)
    _git("push", "origin", "main", cwd=throwaway)

    status = update_serve.collect_update_status(parc)
    comp = status["components"]["HUB_base"]
    assert comp["update_available"] is True
    assert comp["commits_behind"] == 1
    assert comp["version_target"] == "2.0.0"


def test_meta_drifted_submodule_gitlink(tmp_path):
    """Gitlink méta ≠ HEAD local d'un submodule → drifted + update_available.

    Le submodule avance localement (commit non référencé par le gitlink
    méta) : ``commits_behind`` du méta reste 0 mais une mise à jour
    réalignera les pointeurs → spec §5.1 veut le méta « à mettre à jour ».
    """
    parc, sub, _sub_origin = _make_parc(tmp_path)
    (sub / "VERSION").write_text("1.1.0-local\n")
    _git("add", "-A", cwd=sub)
    _git("commit", "-m", "avance locale", cwd=sub)

    status = update_serve.collect_update_status(parc)
    # le submodule lui-même n'a pas de retard sur son origin…
    assert status["components"]["HUB_base"]["update_available"] is False
    # …mais le gitlink méta ne pointe plus son HEAD local → drift
    assert status["meta"]["drifted"] == ["HUB_base"]
    assert status["meta"]["update_available"] is True


def test_status_dirty(tmp_path):
    """Fichier non commité dans le méta → dirty is True."""
    parc, _sub, _sub_origin = _make_parc(tmp_path)
    (parc / "VERSION").write_text("modifié localement\n")
    status = update_serve.collect_update_status(parc)
    assert status["meta"]["dirty"] is True


def test_status_dirty_ignores_untracked(tmp_path):
    """Fichier NON SUIVI seul → dirty is False.

    Bug 1 (volet dirty-check) : « sale » = fichiers SUIVIS modifiés,
    même définition que ``park_checks._dirty_tracked`` (pré-checks
    ``update hub``). Un artefact runtime non suivi (ex. ``secrets/``,
    uploads) ne doit pas afficher un arbre « sale » permanent dans
    l'UI Mises à jour avec un remède « committer ou nettoyer »
    inapplicable à un secret.
    """
    parc, _sub, _sub_origin = _make_parc(tmp_path)
    (parc / "secrets").mkdir()
    (parc / "secrets" / "atelier_token").write_text("token\n")
    status = update_serve.collect_update_status(parc)
    assert status["meta"]["dirty"] is False


def test_status_fetch_failure_isolated_per_component(tmp_path):
    """Origin HS (chmod 000) : error sur le composant, statut global intact."""
    parc, _sub, sub_origin = _make_parc(tmp_path)
    try:
        sub_origin.chmod(0o000)
        status = update_serve.collect_update_status(parc)
    finally:
        sub_origin.chmod(0o755)
    comp = status["components"]["HUB_base"]
    assert comp["error"] is not None
    assert comp["error"].startswith("fetch failed")  # prefix + détail stderr
    # refs locales : rien de nouveau connu → pas de « mise à jour » annoncée
    assert comp["update_available"] is False
    assert "meta" in status
    assert status["meta"]["error"] is None


def test_status_version_from_package_json(tmp_path):
    """Composant JS (HUBCSS…) : version lue depuis package.json."""
    parc, _sub, _sub_origin = _make_parc(tmp_path)
    sub = parc / "HUB_base"
    (sub / "VERSION").unlink()
    (sub / "package.json").write_text('{"version": "0.5.0"}\n')
    _git("add", "-A", cwd=sub)
    _git("commit", "-m", "package.json", cwd=sub)

    status = update_serve.collect_update_status(parc)
    assert status["components"]["HUB_base"]["version"] == "0.5.0"


def test_status_cache_ttl(monkeypatch, tmp_path):
    """TTL 60 s : deux appels sans force = un seul cycle de fetch ; force recalcul."""
    parc, _sub, _sub_origin = _make_parc(tmp_path)
    fetches: list[list[str]] = []
    real_run = park_checks._run

    def spy(*args, **kwargs):
        if args[:2] == ("git", "fetch"):
            fetches.append(list(args))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(park_checks, "_run", spy)

    update_serve.collect_update_status(parc)
    first_round = len(fetches)
    assert first_round > 0

    update_serve.collect_update_status(parc)  # cache hit : aucun fetch de plus
    assert len(fetches) == first_round

    update_serve.collect_update_status(parc, force=True)  # force : refetch
    assert len(fetches) > first_round


# ── État + exécution du job (Task 2, spec §5.2) ─────────────────────────────


FAKE_HUBCTL = """#!/usr/bin/env bash
echo "fake update: $*"
echo "$*" >> "${STATE_MARKER}"
exit "${FAKE_EXIT:-0}"
"""

# Variante lente : le job est encore en cours pendant le second appel
# (test du refus 409 concurrent — un vrai update dure des minutes).
FAKE_HUBCTL_SLOW = """#!/usr/bin/env bash
echo "fake update: $*"
echo "$*" >> "${STATE_MARKER}"
sleep "${FAKE_SLEEP:-2}"
exit "${FAKE_EXIT:-0}"
"""


def _fake_parc(tmp_path: Path, script: str = FAKE_HUBCTL) -> tuple[Path, Path]:
    """(parc, marker) : parc factice réduit à un faux binaire ``hubctl``
    exécutable — le job le subprocess tel quel (aucun git requis). Il
    note ses arguments dans ``$STATE_MARKER`` et sort avec ``$FAKE_EXIT``:
    les tests observent ce qui a réellement tourné côté subprocess."""
    parc = tmp_path / "parc"
    parc.mkdir()
    hubctl = parc / "hubctl"
    hubctl.write_text(script, encoding="utf-8")
    hubctl.chmod(0o755)  # exécutable, comme le vrai point d'entrée CLI
    return parc, tmp_path / "marker"


def _wait_state(state_file: Path, state: str, timeout: float = 10.0) -> dict:
    """Poll ``read_job_state`` jusqu'à l'état attendu — le job tourne dans
    le worker du ThreadPoolExecutor, sa fin est asynchrone. Timeout →
    échec du test (avec le dernier état lu pour le diagnostic)."""
    deadline = time.monotonic() + timeout
    payload: dict = {}
    while time.monotonic() < deadline:
        payload = update_serve.read_job_state(state_file)
        if payload.get("state") == state:
            return payload
        time.sleep(0.05)
    pytest.fail(f"état {state!r} non atteint en {timeout} s (dernier: {payload})")


def test_start_update_happy_path(monkeypatch, tmp_path):
    """202 immédiat (running) → succès sur disque : exit 0, args CLI
    exacts sans --dry-run dans le marqueur, log écrit, tail non vide."""
    parc, marker = _fake_parc(tmp_path)
    monkeypatch.setenv("STATE_MARKER", str(marker))
    state_file = tmp_path / "update_state.json"
    log_dir = tmp_path / "update_logs"

    code, payload = update_serve.start_update(
        parc_root=parc, state_file=state_file, log_dir=log_dir)
    assert code == 202
    assert payload["job"]["state"] == "running"

    job = _wait_state(state_file, "success", timeout=10)
    assert job["exit_code"] == 0
    assert marker.read_text().strip() == "update hub"  # sans --dry-run
    assert Path(job["log_path"]).exists()
    assert job["tail"]  # 50 dernières lignes : ici non vide
    assert job["tail"][-1] == "fake update: update hub"


def test_start_update_dry_run_flag(monkeypatch, tmp_path):
    """dry_run=True → --dry-run passé au CLI réel (visible au marqueur)."""
    parc, marker = _fake_parc(tmp_path)
    monkeypatch.setenv("STATE_MARKER", str(marker))
    state_file = tmp_path / "update_state.json"

    code, _payload = update_serve.start_update(
        parc_root=parc, state_file=state_file,
        log_dir=tmp_path / "logs", dry_run=True)
    assert code == 202
    job = _wait_state(state_file, "success", timeout=10)
    # Ordre verrouillé (revue finale D1) : --dry-run (option GLOBALE du
    # parser) AVANT le subcommand « update hub » — sinon argparse répond
    # « unrecognized arguments » (cf. test_dry_run_global_option_contract).
    assert "--dry-run update hub" in marker.read_text()
    assert job["exit_code"] == 0


def test_start_update_failure_state(monkeypatch, tmp_path):
    """CLI en échec (FAKE_EXIT=2) → état final failed + exit_code 2."""
    parc, marker = _fake_parc(tmp_path)
    monkeypatch.setenv("STATE_MARKER", str(marker))
    monkeypatch.setenv("FAKE_EXIT", "2")
    state_file = tmp_path / "update_state.json"

    update_serve.start_update(parc_root=parc, state_file=state_file,
                              log_dir=tmp_path / "logs")
    job = _wait_state(state_file, "failed", timeout=10)
    assert job["exit_code"] == 2
    assert job["tail"]  # le log (stdout+stderr confondus) reste consultable


def test_start_update_conflict_while_running(monkeypatch, tmp_path):
    """Job en cours → second appel (409, état disque) ; aucun second
    subprocess : le marqueur ne reçoit qu'une ligne, un seul log créé."""
    parc, marker = _fake_parc(tmp_path, script=FAKE_HUBCTL_SLOW)
    monkeypatch.setenv("STATE_MARKER", str(marker))
    state_file = tmp_path / "update_state.json"
    log_dir = tmp_path / "logs"

    code, payload = update_serve.start_update(
        parc_root=parc, state_file=state_file, log_dir=log_dir)
    assert code == 202
    assert payload["job"]["state"] == "running"

    code2, payload2 = update_serve.start_update(
        parc_root=parc, state_file=state_file, log_dir=log_dir)
    assert code2 == 409
    assert payload2["detail"] == "update deja en cours"
    assert payload2["job"]["state"] == "running"

    _wait_state(state_file, "success", timeout=10)
    assert marker.read_text().strip().splitlines() == ["update hub"]
    assert len(list(log_dir.glob("update-*.log"))) == 1


def test_state_survives_service_restart(monkeypatch, tmp_path):
    """L'état final ne vit QUE dans update_state.json : module rechargé
    (service « redémarré », mémoire à zéro) → verdict relu à l'identique."""
    parc, marker = _fake_parc(tmp_path)
    monkeypatch.setenv("STATE_MARKER", str(marker))
    state_file = tmp_path / "update_state.json"

    update_serve.start_update(parc_root=parc, state_file=state_file,
                              log_dir=tmp_path / "logs")
    before = _wait_state(state_file, "success", timeout=10)

    importlib.reload(update_serve)  # recrée executor/flags : mémoire vide
    after = update_serve.read_job_state(state_file)
    assert after == before
    assert after["state"] == "success"


def test_read_job_state_idle_when_missing(tmp_path):
    """Fichier absent OU JSON corrompu → {"state": "idle"}, jamais
    d'exception (le service redémarre proprement sans état)."""
    assert update_serve.read_job_state(tmp_path / "absent.json") == {
        "state": "idle"}
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{état tronqué", encoding="utf-8")
    assert update_serve.read_job_state(corrupt) == {"state": "idle"}


def _running_state(started_at: str) -> dict:
    """État ``running`` tel qu'écrit par start_update (shape §5.2)."""
    return {"state": "running", "started_at": started_at,
            "log_path": "/tmp/update-00000000-000000.log"}


def test_read_job_state_stale_running_guard(tmp_path):
    """Garde d'obsolescence (revue finale D2) : ``running`` + started_at
    vieux de 2 h (> ``_JOB_TIMEOUT + 300``) → ``failed`` (exit_code None
    + detail), réécrit sur disque (second appel cohérent) ; ``running``
    frais ou started_at illisible → inchangé."""
    state_file = tmp_path / "update_state.json"

    # Job périmé : started_at = il y a 2 h → transformé en failed
    stale = _running_state(
        (datetime.now().astimezone() - timedelta(hours=2))
        .isoformat(timespec="seconds"))
    update_serve._write_state(state_file, stale)
    job = update_serve.read_job_state(state_file)
    assert job["state"] == "failed"
    assert job["exit_code"] is None
    assert job["detail"] == ("job obsolet (service hote redemarre "
                             "pendant le job ?)")
    assert job["started_at"] == stale["started_at"]  # mêmes champs conservés
    assert job["log_path"] == stale["log_path"]
    # persisté sur disque : le second appel relit l'état corrigé à l'identique
    assert update_serve.read_job_state(state_file) == job

    # Job frais : ``running`` inchangé, fichier intact (lecture pure)
    fresh = _running_state(
        datetime.now().astimezone().isoformat(timespec="seconds"))
    update_serve._write_state(state_file, fresh)
    assert update_serve.read_job_state(state_file) == fresh
    assert json.loads(state_file.read_text(encoding="utf-8")) == fresh

    # started_at illisible → garde désactivée, comportement inchangé
    bad = _running_state("pas-une-date")
    update_serve._write_state(state_file, bad)
    assert update_serve.read_job_state(state_file) == bad


# ── Serveur HTTP + branchage CLI (Task 3, spec §4.1) ────────────────────────

TEST_TOKEN = "tok-test-123"


@contextmanager
def _running_server(tmp_path: Path, script: str = FAKE_HUBCTL):
    """Serveur update_serve sur port 0 (thread daemon, recette tests
    atelier_serve) : yield {port, parc, marker, state_file, log_dir} ;
    arrêt propre (shutdown + server_close) en sortie de contexte."""
    token_file = tmp_path / "atelier_token"
    token_file.write_text(TEST_TOKEN + "\n", encoding="utf-8")
    parc, marker = _fake_parc(tmp_path, script=script)
    state_file = tmp_path / "update_state.json"
    log_dir = tmp_path / "update_logs"
    server = update_serve.build_server(0, token_file, parc_root=parc,
                                       state_file=state_file, log_dir=log_dir)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"port": server.server_address[1], "parc": parc,
               "marker": marker, "state_file": state_file,
               "log_dir": log_dir}
    finally:
        server.shutdown()
        server.server_close()


def _request(port: int, method: str, path: str, *,
             token: str | None = TEST_TOKEN,
             body: dict | None = None) -> tuple[int, dict]:
    """Requête HTTP locale → (code, payload JSON). Les 4xx arrivent comme
    HTTPError (urllib lève) : le handler ne doit jamais couper la
    connexion, on lit le JSON d'erreur tel quel."""
    headers = {}
    if token is not None:
        headers["X-Atelier-Token"] = token
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_http_health_no_auth(tmp_path):
    """GET /health SANS header → 200 (liveness publique : ne divulgue
    rien, évite d'imposer le token aux healthchecks — contrat §4.1)."""
    with _running_server(tmp_path) as srv:
        code, payload = _request(srv["port"], "GET", "/health", token=None)
    assert code == 200
    assert payload == {"service": "update-agent", "ok": True}


def test_http_auth_401_403(tmp_path):
    """Auth X-Atelier-Token (copie atelier_serve) : header absent → 401 ;
    token invalide → 403 (comparaison constant-time côté serveur)."""
    with _running_server(tmp_path) as srv:
        code, payload = _request(srv["port"], "GET", "/update/status",
                                 token=None)
        assert code == 401
        assert payload  # erreur JSON propre, pas de connexion coupée
        code, payload = _request(srv["port"], "GET", "/update/status",
                                 token="mauvais-token")
        assert code == 403


def test_http_status_and_job(tmp_path):
    """GET /update/status → contrat Task 1 (service ok + meta) ;
    GET /update/job → état disque, idle sans fichier d'état."""
    with _running_server(tmp_path) as srv:
        code, status = _request(srv["port"], "GET", "/update/status")
        assert code == 200
        assert status["service"] == "ok"
        assert "meta" in status
        code, job = _request(srv["port"], "GET", "/update/job")
        assert code == 200
        assert job == {"state": "idle"}


def test_http_run_job_end_to_end(monkeypatch, tmp_path):
    """POST /update/run {dry_run: true} → 202 (running) ; POST pendant le
    sleep du fake hubctl → 409 passthrough ; GET /update/job pollé
    jusqu'au verdict success (--dry-run visible au marqueur)."""
    monkeypatch.setenv("STATE_MARKER", str(tmp_path / "marker"))
    with _running_server(tmp_path, script=FAKE_HUBCTL_SLOW) as srv:
        port = srv["port"]
        code, payload = _request(port, "POST", "/update/run",
                                 body={"dry_run": True})
        assert code == 202
        assert payload["job"]["state"] == "running"

        # job encore en cours (sleep du fake hubctl) → refus concurrent,
        # le payload 409 de start_update passe tel quel
        code, payload = _request(port, "POST", "/update/run")
        assert code == 409
        assert payload["detail"] == "update deja en cours"

        deadline = time.monotonic() + 10
        job: dict = {}
        while time.monotonic() < deadline:
            code, job = _request(port, "GET", "/update/job")
            assert code == 200
            if job.get("state") == "success":
                break
            time.sleep(0.05)
        assert job.get("state") == "success"
        assert job["exit_code"] == 0
        assert "--dry-run" in srv["marker"].read_text()


def test_cli_dispatch_serve(monkeypatch, tmp_path):
    """`hubctl update --serve` branche le dispatch CLI vers
    update_serve.serve (port/host/token-file transmis ; défauts
    8778 / 127.0.0.1 / token par défaut)."""
    from hubctl_cli.__main__ import main
    calls: dict = {}

    def fake_serve(*, port, token_file, host):
        calls.update(port=port, token_file=token_file, host=host)
        return 0

    monkeypatch.setattr(update_serve, "serve", fake_serve)

    tf = tmp_path / "tf"
    assert main(["update", "--serve", "--port", "0",
                 "--token-file", str(tf), "--host", "127.0.0.1"]) == 0
    assert calls == {"port": 0, "token_file": tf, "host": "127.0.0.1"}

    calls.clear()
    assert main(["update", "--serve"]) == 0
    assert calls == {"port": 8778, "token_file": None, "host": "127.0.0.1"}


def test_cli_dispatch_serve_rejects_targets(monkeypatch):
    """`update <cible> --serve` refuse (code 2, même ruling que
    `atelier --serve` sans slug d'app) et serve() n'est JAMAIS appelé :
    l'agent gère le parc entier, la cible se passe à POST /update/run."""
    from hubctl_cli.__main__ import main

    def boom(*args, **kwargs):
        raise AssertionError("serve() ne doit pas être appelé avec une cible")

    monkeypatch.setattr(update_serve, "serve", boom)

    assert main(["update", "hub", "--serve"]) == 2
    assert main(["update", "hub", "monapp", "--serve"]) == 2
    assert main(["update", "all", "--serve"]) == 2


def test_dry_run_global_option_contract():
    """Contrat argparse verrouillé (revue finale D1) : ``--dry-run`` est
    une option GLOBALE du parser principal — elle doit PRÉCÉDER le
    subcommand « update » (c'est l'ordre que _run_update_job construit :
    ``[hubctl, --dry-run, update, hub]``). L'ordre inverse ``update hub
    --dry-run`` (bug initial) est refusé par argparse (SystemExit 2)."""
    from hubctl_cli.__main__ import build_parser

    args = build_parser().parse_args(["--dry-run", "update", "hub"])
    assert args.global_dry_run is True
    assert args.command == "update"

    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["update", "hub", "--dry-run"])
    assert exc.value.code == 2  # usage + « unrecognized arguments »
