"""Tests du flux `update hub` via le méta (plan unification updates, Task 3).

Tout l'environnement est mocké (run_shell, park_checks, snapshot DB, health,
SHA) : les tests valident la SÉQUENCE et le rollback étendu, pas git lui-même.
"""
from pathlib import Path
from unittest import mock

import pytest
from rich.console import Console

from hubctl_cli import actions
from hubctl_cli.park_checks import Finding

META_SHA = "a" * 40
HUB_SHA = "b" * 40


def _f(ok: bool, remedy: str | None = None) -> Finding:
    return Finding(check="hub_base.aligned_origin",
                   ok=ok,
                   detail="ok" if ok else "historique divergent",
                   remedy=None if ok else remedy)


def _setup(monkeypatch, *, precheck_problems=None, run_shell_side=None,
           health=True):
    """Mocke l'environnement de _update_hub_via_meta → (calls, rollback)."""
    calls: list[list[str]] = []

    def fake_run_shell(cmd, **kwargs):
        calls.append(list(cmd))
        if run_shell_side:
            return run_shell_side(cmd)
        return 0

    monkeypatch.setattr(actions.utils, "run_shell", fake_run_shell)
    monkeypatch.setattr(actions.park_checks, "run_update_prechecks",
                        lambda root: precheck_problems or [_f(True)])
    monkeypatch.setattr(actions, "_db_snapshot", lambda con=None: None)
    monkeypatch.setattr(actions, "_health_check",
                        lambda target, con, timeout=60: health)
    monkeypatch.setattr(actions, "_git_head_sha",
                        lambda path: META_SHA
                        if Path(path) == actions.hub_state.PARC_ROOT else HUB_SHA)
    monkeypatch.setattr(actions, "ensure_hub_env",
                        lambda con=None, dry_run=False: None)
    # mode déterministe (le _mode global peut fuiter d'autres tests)
    monkeypatch.setattr(actions.hub_state, "_mode", "prod")
    rollback = mock.Mock()
    monkeypatch.setattr(actions, "_rollback", rollback)
    return calls, rollback


def test_update_hub_pulls_meta_then_submodules(monkeypatch):
    calls, rollback = _setup(monkeypatch)
    con = Console()
    assert actions._update_hub_via_meta(con, dry_run=False) is True
    assert ["git", "-C", str(actions.hub_state.PARC_ROOT),
            "pull", "--ff-only"] in calls
    assert ["git", "-C", str(actions.hub_state.PARC_ROOT),
            "submodule", "update", "--init", "--recursive"] in calls
    assert ["make", "-C", str(actions.hub_state.HUB_DIR), "build"] in calls
    assert ["make", "-C", str(actions.hub_state.HUB_DIR), "up-prod"] in calls
    rollback.assert_not_called()


def test_update_hub_fails_clean_on_precheck(monkeypatch):
    remedy = "git -C /x reset --hard origin/main"
    calls, rollback = _setup(
        monkeypatch, precheck_problems=[_f(False, remedy=remedy)])
    con = Console(record=True)
    assert actions._update_hub_via_meta(con, dry_run=False) is False
    assert not any(c and c[0] == "git" for c in calls), \
        "pré-checks en échec → aucun git ne doit tourner"
    assert "reset --hard origin/main" in con.export_text()
    rollback.assert_not_called()


def test_update_hub_rollback_restores_meta_and_submodule(monkeypatch):
    def side(cmd):
        # le build échoue → rollback étendu
        return 1 if cmd[:2] == ["make", "-C"] and cmd[-1] == "build" else 0

    calls, rollback = _setup(monkeypatch, run_shell_side=side)
    con = Console()
    assert actions._update_hub_via_meta(con, dry_run=False) is False
    rollback.assert_called_once()
    kwargs = rollback.call_args.kwargs
    assert kwargs.get("meta_root") == actions.hub_state.PARC_ROOT
    assert kwargs.get("prev_meta_sha") == META_SHA
    # prev_commit (positionnel) = SHA HUB_base avant update
    assert rollback.call_args.args[2] == HUB_SHA


def test_rollback_extended_resets_meta_before_submodule(monkeypatch):
    calls: list[list[str]] = []

    def fake_run_shell(cmd, **kwargs):
        calls.append(list(cmd))
        return 0

    monkeypatch.setattr(actions.utils, "run_shell", fake_run_shell)
    actions._rollback("hub", Path("/hb"), HUB_SHA, None, Console(),
                      meta_root=Path("/meta"), prev_meta_sha=META_SHA)
    git_calls = [c for c in calls if c and c[0] == "git"]
    meta_reset = ["git", "-C", "/meta", "reset", "--hard", META_SHA]
    hub_reset = ["git", "-C", "/hb", "reset", "--hard", HUB_SHA]
    assert meta_reset in git_calls
    assert hub_reset in git_calls
    assert git_calls.index(meta_reset) < git_calls.index(hub_reset), \
        "le pointeur méta est restauré AVANT le submodule"


def test_update_hub_pull_ff_only_never_plain_pull(monkeypatch):
    calls, _ = _setup(monkeypatch)
    actions._update_hub_via_meta(Console(), dry_run=False)
    for cmd in calls:
        if cmd and cmd[0] == "git" and "pull" in cmd:
            assert "--ff-only" in cmd, f"pull sans --ff-only : {cmd}"
    old_flow = ["git", "-C", str(actions.hub_state.HUB_DIR),
                "pull", "--recurse-submodules"]
    assert old_flow not in calls, "l'ancien flux (pull dans HUB_base) est mort"
