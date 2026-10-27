"""Tests Task 4 (plan unification updates) : mode dev sur update + ordre all.

- Le mode courant (hub_state._mode : dev/prod) choisit la cible make
  (up-dev vs up-prod/up) pour le Hub, les apps — et le rollback.
- `update all` : le Hub est bloquant — s'il échoue, les apps ne sont
  jamais touchées (elles dépendent du Hub pour le M2M).
"""
from pathlib import Path
from unittest import mock

import pytest
from rich.console import Console

from hubctl_cli import actions
from hubctl_cli.park_checks import Finding

META_SHA = "a" * 40
HUB_SHA = "b" * 40


def _f(ok: bool) -> Finding:
    return Finding(check="hub_base.aligned_origin", ok=ok,
                   detail="ok" if ok else "divergent")


def _setup_hub(monkeypatch, mode: str):
    """Mocks du flux méta (comme Task 3) + mode courant imposé."""
    calls: list[list[str]] = []

    def fake_run_shell(cmd, **kwargs):
        calls.append(list(cmd))
        return 0

    monkeypatch.setattr(actions.utils, "run_shell", fake_run_shell)
    monkeypatch.setattr(actions.park_checks, "run_update_prechecks",
                        lambda root: [_f(True)])
    monkeypatch.setattr(actions, "_db_snapshot", lambda con=None: None)
    monkeypatch.setattr(actions, "_health_check",
                        lambda target, con, timeout=60: True)
    monkeypatch.setattr(actions, "_git_head_sha", lambda path: META_SHA)
    monkeypatch.setattr(actions, "ensure_hub_env",
                        lambda con=None, dry_run=False: None)
    monkeypatch.setattr(actions.hub_state, "_mode", mode)
    return calls


def test_update_hub_uses_up_dev_in_dev_mode(monkeypatch):
    calls = _setup_hub(monkeypatch, mode="dev")
    assert actions._update_hub_via_meta(Console(), dry_run=False) is True
    assert ["make", "-C", str(actions.hub_state.HUB_DIR), "up-dev"] in calls
    assert not any(cmd[-1] == "up-prod" for cmd in calls if cmd[0] == "make")


def test_update_hub_uses_up_prod_in_prod_mode(monkeypatch):
    calls = _setup_hub(monkeypatch, mode="prod")
    assert actions._update_hub_via_meta(Console(), dry_run=False) is True
    assert ["make", "-C", str(actions.hub_state.HUB_DIR), "up-prod"] in calls


def test_update_app_uses_up_dev_in_dev_mode(monkeypatch, tmp_path):
    app_dir = tmp_path / "exemple-app"
    app_dir.mkdir()
    (app_dir / "docker-compose.yml").write_text("services: {}\n")
    calls: list[list[str]] = []

    def fake_run_shell(cmd, **kwargs):
        calls.append(list(cmd))
        return 0

    monkeypatch.setattr(actions.hub_state, "active_apps_dir", lambda: tmp_path)
    monkeypatch.setattr(actions.hub_state, "_mode", "dev")
    monkeypatch.setattr(actions, "_git_up_to_date", lambda *a, **k: False)
    monkeypatch.setattr(actions, "_git_previous_commit", lambda *a, **k: "abc")
    monkeypatch.setattr(actions, "_db_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(actions, "_health_check", lambda *a, **k: True)
    monkeypatch.setattr(actions, "_ensure_branch_attached",
                        lambda *a, **k: "main")
    monkeypatch.setattr(actions.utils, "run_shell", fake_run_shell)

    assert actions._update_target(None, "exemple-app", dry_run=False) is True
    assert ["make", "-C", str(app_dir), "up-dev"] in calls
    assert not any(cmd[-1] == "up" and cmd[0] == "make" for cmd in calls)


def test_update_app_uses_up_in_prod_mode(monkeypatch, tmp_path):
    app_dir = tmp_path / "exemple-app"
    app_dir.mkdir()
    (app_dir / "docker-compose.yml").write_text("services: {}\n")
    calls: list[list[str]] = []

    def fake_run_shell(cmd, **kwargs):
        calls.append(list(cmd))
        return 0

    monkeypatch.setattr(actions.hub_state, "active_apps_dir", lambda: tmp_path)
    monkeypatch.setattr(actions.hub_state, "_mode", "prod")
    monkeypatch.setattr(actions, "_git_up_to_date", lambda *a, **k: False)
    monkeypatch.setattr(actions, "_git_previous_commit", lambda *a, **k: "abc")
    monkeypatch.setattr(actions, "_db_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(actions, "_health_check", lambda *a, **k: True)
    monkeypatch.setattr(actions, "_ensure_branch_attached",
                        lambda *a, **k: "main")
    monkeypatch.setattr(actions.utils, "run_shell", fake_run_shell)

    assert actions._update_target(None, "exemple-app", dry_run=False) is True
    assert ["make", "-C", str(app_dir), "up"] in calls


def test_rollback_reup_uses_dev_mode(monkeypatch):
    """Le rollback re-up avec la cible du mode courant (cohérence dev)."""
    calls: list[list[str]] = []

    def fake_run_shell(cmd, **kwargs):
        calls.append(list(cmd))
        return 0

    monkeypatch.setattr(actions.utils, "run_shell", fake_run_shell)
    monkeypatch.setattr(actions.hub_state, "_mode", "dev")
    actions._rollback("hub", Path("/hb"), "b" * 40, None, Console())
    assert ["make", "-C", "/hb", "up-dev"] in calls


def test_update_all_hub_failure_blocks_apps(monkeypatch):
    """`update all` : hub en échec → les apps ne sont jamais touchées."""
    executed: list[str] = []

    def fake_update_target(con, name, *, dry_run):
        executed.append(name)
        return name != "hub"  # le hub échoue

    monkeypatch.setattr(actions.hub_state, "get_active_apps",
                        lambda: [mock.Mock(name="a"), mock.Mock(name="b")])
    monkeypatch.setattr(actions.hub_state, "APP_NAMES",
                        ["a", "b"], raising=False)
    monkeypatch.setattr(actions, "disk_guard", lambda con=None: True)
    monkeypatch.setattr(actions, "_ensure_jwt_keys", lambda *a, **k: None)
    monkeypatch.setattr(actions, "_update_target", fake_update_target)

    rc = actions.action_update(console=Console(), targets=["*"])
    assert rc >= 1
    assert executed == ["hub"], "les apps ne doivent pas être mises à jour"
