# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Tests de update_serve — statut (Task 1) + état/exécution du job (Task 2).

Chaque scénario monte un « parc » factice dans tmp_path : un méta git avec
VERSION + un submodule HUB_base initialisé depuis un dépôt bare (origin).
Contrat collect_update_status (spec §5.1, consommé par GET /update/status) :
{"service": "ok", "checked_at": <ISO8601>, "meta": <component>,
 "components": {rel_path: <component>}}.
Contrat job (spec §5.2, consommé par POST /update/run + GET /update/job) :
start_update → (202, {"job": running}) ou (409, ...) ; l'état final ne vit
que dans update_state.json.
"""
import importlib
import time
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
    assert "--dry-run" in marker.read_text()
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
