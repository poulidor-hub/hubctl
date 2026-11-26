"""E2E VM 26/11 : `hubctl init` échouait à 120 s sur le build `up-prod` du
premier boot (VM 2 vCPU : le premier `docker compose up -d` build les images,
> 120 s) → « ✗ Échec du build/démarrage du Hub » alors que le build continuait
orphelin et finissait healthy. Le nouvel utilisateur était accueilli par une
erreur sur le premier start.

Fix attendu :
- `hub_make` applique un budget généreux et surchargeable
  (HUBCTL_BUILD_TIMEOUT, défaut 1800 s) aux cibles de build du Hub ;
- l'init attend le health check avec un plafond remonté et surchargeable
  (HUBCTL_HEALTH_TIMEOUT, défaut 600 s) ;
- sur timeout (rc 124), la guidance est actionable (relancer ./hubctl init
  converge), pas une simple ligne d'échec.
"""
import io

import pytest
from rich.console import Console

from hubctl_cli import actions, hub_state


def _capture_run_shell(monkeypatch, rc=0):
    calls = []

    def fake_run_shell(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        return rc

    monkeypatch.setattr(actions.utils, "run_shell", fake_run_shell)
    return calls


def _bootstrap_env(monkeypatch, tmp_path, hub_make_rc=0):
    """Environnement bootstrap déterministe (style test_main.py)."""
    hub = tmp_path / "HUB_base"
    hub.mkdir(parents=True)
    (hub / "docker-compose.yml").write_text("services:\n  api:\n    image: test\n")
    (hub / "Makefile").write_text("up-prod:\n\techo up\n")
    monkeypatch.setattr(hub_state, "HUB_DIR", hub)
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path / "APPS")
    monkeypatch.setattr(actions.hub_state, "active_apps_dir", lambda: tmp_path / "APPS")
    monkeypatch.setattr(actions, "_sync_db_password_if_needed", lambda *a, **kw: None)
    monkeypatch.setattr(actions, "_install_shell_marker", lambda *a, **kw: None)
    monkeypatch.setattr(actions, "_ensure_atelier_service", lambda *a, **kw: None)
    monkeypatch.setattr(actions, "_ensure_update_service", lambda *a, **kw: None)
    monkeypatch.setattr(actions, "_cleanup_existing_hub_containers", lambda *a, **kw: None)
    make_calls = {}

    def fake_hub_make(target, *, dry_run, console=None, timeout=None):
        make_calls["target"] = target
        make_calls["timeout"] = timeout
        return hub_make_rc

    monkeypatch.setattr(actions, "hub_make", fake_hub_make)
    health_calls = {}

    def fake_health(target, console=None, timeout=60, port_override=None):
        health_calls["timeout"] = timeout
        return True

    monkeypatch.setattr(actions, "_health_check", fake_health)
    return make_calls, health_calls


def test_up_prod_gets_build_budget(monkeypatch):
    """Cible de build → budget généreux (1800 s), pas le défaut 120 s."""
    calls = _capture_run_shell(monkeypatch)
    rc = actions.hub_make("up-prod", dry_run=False, console=Console())
    assert rc == 0
    cmd, kwargs = calls[-1]
    assert cmd == ["make", "-C", str(hub_state.HUB_DIR), "up-prod"]
    assert kwargs.get("timeout") == 1800


def test_build_timeout_env_override(monkeypatch):
    monkeypatch.setenv("HUBCTL_BUILD_TIMEOUT", "60")
    calls = _capture_run_shell(monkeypatch)
    actions.hub_make("up-prod", dry_run=False, console=Console())
    assert calls[-1][1].get("timeout") == 60


def test_non_build_target_keeps_historic_default(monkeypatch):
    calls = _capture_run_shell(monkeypatch)
    actions.hub_make("status", dry_run=False, console=Console())
    assert calls[-1][1].get("timeout", 120) == 120


def test_explicit_timeout_wins_over_budget(monkeypatch):
    calls = _capture_run_shell(monkeypatch)
    actions.hub_make("build", dry_run=False, console=Console(), timeout=5)
    assert calls[-1][1].get("timeout") == 5


def test_bootstrap_health_check_gets_health_budget(monkeypatch, tmp_path):
    make_calls, health_calls = _bootstrap_env(monkeypatch, tmp_path)
    rc = actions.action_bootstrap(Console(file=io.StringIO(), width=120),
                                  force=True, dry_run=False)
    assert rc == 0
    assert make_calls["target"] == "up-prod"
    # l'init ne passe PAS de timeout explicite à hub_make : le budget build
    # s'applique à l'intérieur de hub_make (test ci-dessus)
    assert make_calls["timeout"] is None
    assert health_calls["timeout"] == 600


def test_bootstrap_build_timeout_gives_guidance(monkeypatch, tmp_path):
    make_calls, _ = _bootstrap_env(monkeypatch, tmp_path, hub_make_rc=124)
    out = io.StringIO()
    rc = actions.action_bootstrap(Console(file=out, width=120),
                                  force=True, dry_run=False)
    assert rc == 1
    output = out.getvalue()
    assert "Relancez" in output and "hubctl init" in output, (
        "sur timeout de build, la guidance doit indiquer que relancer "
        "./hubctl init converge (build orphelin continué)"
    )
