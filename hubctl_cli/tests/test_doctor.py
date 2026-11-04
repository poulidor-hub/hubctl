"""Tests de `hubctl doctor` (plan unification updates, Task 2).

Tout est mocké au niveau de `park_checks` : les tests valident la logique de
l'action (rapport, exit codes, délégation des fixes), pas git lui-même.
"""
from pathlib import Path
from unittest import mock

import pytest
from rich.console import Console

from hubctl_cli import actions
from hubctl_cli.park_checks import Finding


def _f(ok: bool, check: str = "hub_base.aligned_origin",
       remedy: str | None = None) -> Finding:
    return Finding(check=check, ok=ok,
                   detail="détail de test" if not ok else "ok",
                   remedy=remedy)


def test_doctor_reports_and_returns_1_when_problem(monkeypatch):
    monkeypatch.setattr(
        actions.park_checks, "run_checks",
        lambda root, apps: [_f(False, remedy="git -C X reset --hard origin/main")],
    )
    con = Console()
    rc = actions.action_doctor(console=con, parc_root=Path("/tmp/fake"), apps=[])
    assert rc == 1


def test_doctor_returns_0_when_healthy(monkeypatch):
    monkeypatch.setattr(
        actions.park_checks, "run_checks",
        lambda root, apps: [_f(True)],
    )
    con = Console()
    rc = actions.action_doctor(console=con, parc_root=Path("/tmp/fake"), apps=[])
    assert rc == 0


def test_doctor_fix_applies_and_returns_0(monkeypatch):
    problems = [_f(False, remedy="git -C X reset --hard origin/main")]
    calls = {"n": 0}

    def fake_run_checks(root, apps):
        calls["n"] += 1
        return list(problems) if calls["n"] == 1 else [_f(True)]

    monkeypatch.setattr(actions.park_checks, "run_checks", fake_run_checks)
    apply_mock = mock.Mock(return_value=1)
    monkeypatch.setattr(actions.park_checks, "apply_safe_fixes", apply_mock)
    con = Console()
    rc = actions.action_doctor(console=con, fix=True, assume_yes=True,
                               parc_root=Path("/tmp/fake"), apps=[])
    assert rc == 0
    assert calls["n"] == 2  # rapport puis re-check après fixes
    apply_mock.assert_called_once()
    assert apply_mock.call_args.kwargs.get("assume_yes") is True


def test_doctor_fix_without_yes_is_interactive(monkeypatch):
    """Sans --yes, la console est passée à apply_safe_fixes (confirmation)."""
    monkeypatch.setattr(
        actions.park_checks, "run_checks",
        lambda root, apps: [_f(False, remedy="r")],
    )
    apply_mock = mock.Mock(return_value=0)
    monkeypatch.setattr(actions.park_checks, "apply_safe_fixes", apply_mock)
    con = Console()
    rc = actions.action_doctor(console=con, fix=True, assume_yes=False,
                               parc_root=Path("/tmp/fake"), apps=[])
    apply_mock.assert_called_once()
    assert apply_mock.call_args.kwargs.get("assume_yes") is False
    assert apply_mock.call_args.kwargs.get("console") is con
    # rien n'a été appliqué et des problèmes subsistent → 1
    assert rc == 1


def test_doctor_passes_apps_to_run_checks(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        actions.park_checks, "run_checks",
        lambda root, apps: (seen.update(root=root, apps=apps), [_f(True)])[1],
    )
    con = Console()
    actions.action_doctor(console=con, parc_root=Path("/tmp/fake"),
                          apps=["exemple-app", "exemple-app"])
    assert seen["apps"] == ["exemple-app", "exemple-app"]


def test_doctor_wired_in_cli_main():
    """Régression : `hubctl doctor` levait `NameError: action_doctor is not
    defined` — la fonction existait dans actions.py mais n'était pas importée
    dans __main__.py. Les tests unitaires de l'action ne couvrent pas ce
    câblage CLI : on le vérifie explicitement."""
    from hubctl_cli import __main__ as cli_main
    assert callable(getattr(cli_main, "action_doctor", None))
