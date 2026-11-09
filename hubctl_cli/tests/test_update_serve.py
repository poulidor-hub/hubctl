# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Tests de update_serve — collecte du statut de mise à jour (Task 1).

Chaque scénario monte un « parc » factice dans tmp_path : un méta git avec
VERSION + un submodule HUB_base initialisé depuis un dépôt bare (origin).
Contrat collect_update_status (spec §5.1, consommé par GET /update/status) :
{"service": "ok", "checked_at": <ISO8601>, "meta": <component>,
 "components": {rel_path: <component>}}.
"""
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
def _reset_status_cache():
    """Le cache statut est module-level : chaque test repart à froid."""
    update_serve._STATUS_CACHE = None
    update_serve._STATUS_TS = 0.0
    yield
    update_serve._STATUS_CACHE = None
    update_serve._STATUS_TS = 0.0


# ── Collecte du statut ──────────────────────────────────────────────────────


def test_status_up_to_date(tmp_path):
    """Parc aligné : méta à jour (behind 0, version locale lue)."""
    parc, _sub, _sub_origin = _make_parc(tmp_path)
    status = update_serve.collect_update_status(parc)
    assert status["service"] == "ok"
    assert status["meta"]["update_available"] is False
    assert status["meta"]["commits_behind"] == 0
    assert status["meta"]["version"] == "1.0.0"
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
