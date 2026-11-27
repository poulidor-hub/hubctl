# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Tests du flux `update hub` via le méta (plan unification updates, Task 3).

Tout l'environnement est mocké (run_shell, park_checks, snapshot DB, health,
SHA) : les tests valident la SÉQUENCE et le rollback étendu, pas git lui-même.
Une dernière section laisse tourner les VRAIS pré-checks `park_checks` sur un
dépôt réel (déploiement en HEAD détaché) : seuls les problèmes bloquants
doivent refuser l'update, pas les constats d'information.
"""
from pathlib import Path
from unittest import mock

import pytest
from rich.console import Console

from hubctl_cli import actions
from hubctl_cli.park_checks import Finding
from hubctl_cli.tests.test_park_checks import (
    _commit, _git, _make_deployment)

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
    monkeypatch.setattr(actions, "_ensure_atelier_service",
                        lambda con=None, dry_run=False: None)
    monkeypatch.setattr(actions, "_ensure_update_service",
                        lambda con=None, dry_run=False: None)
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


# ── Pré-checks : l'information ne bloque pas, seul un problème bloque ──────
#
# VM 26/11 : sur un clone public frais, `update hub` (CLI comme bouton
# « Mettre à jour » de l'UI) refusait de démarrer parce que les pré-checks
# prenaient pour des problèmes deux constats normaux d'un déploiement
# (HUB_base détaché, origin/main du composant en avance).

def _pull_meta_cmd() -> list[str]:
    return ["git", "-C", str(actions.hub_state.PARC_ROOT), "pull", "--ff-only"]


def test_update_hub_shows_information_findings_without_blocking(monkeypatch):
    info = Finding(check="hub_base.aligned_origin", ok=True, info=True,
                   detail="mise à jour disponible : HUB_base aaa1111 → bbb2222 "
                          "(origin/main) — mettre le méta à jour d'abord")
    calls, rollback = _setup(monkeypatch, precheck_problems=[info])
    con = Console(record=True, width=250)
    assert actions._update_hub_via_meta(con, dry_run=False) is True
    text = con.export_text()
    assert "ℹ hub_base.aligned_origin — mise à jour disponible" in text
    assert "pré-checks échoués" not in text
    assert _pull_meta_cmd() in calls
    rollback.assert_not_called()


def _setup_real_prechecks(monkeypatch, meta: Path):
    """Comme ``_setup``, mais les VRAIS pré-checks tournent sur le dépôt
    ``meta`` (hub_state pointé dessus) — seule l'exécution reste mockée."""
    real_prechecks = actions.park_checks.run_update_prechecks
    calls, rollback = _setup(monkeypatch)
    monkeypatch.setattr(actions.park_checks, "run_update_prechecks", real_prechecks)
    monkeypatch.setattr(actions.hub_state, "PARC_ROOT", meta)
    monkeypatch.setattr(actions.hub_state, "HUB_DIR", meta / "HUB_base")
    return calls, rollback


def test_update_hub_starts_on_deployment_reporting_only_information(monkeypatch,
                                                                   tmp_path):
    """Déploiement sain (détaché au gitlink), origin/main en avance :
    l'information « mise à jour disponible » ne refuse pas l'update."""
    meta, _hub, hub_origin = _make_deployment(tmp_path)
    _commit(hub_origin, "release.txt", "release du composant")
    calls, rollback = _setup_real_prechecks(monkeypatch, meta)

    con = Console(record=True, width=250)
    assert actions._update_hub_via_meta(con, dry_run=False) is True
    text = con.export_text()
    assert "ℹ hub_base.aligned_origin — mise à jour disponible" in text
    # le vrai chemin d'un déploiement, pas une commande qui ne ferait rien
    assert "git fetch upstream && git merge --ff-only upstream/main" in text
    assert "pré-checks échoués" not in text
    assert _pull_meta_cmd() in calls, "l'update doit démarrer"
    rollback.assert_not_called()


def test_update_hub_refuses_on_blocking_problem_with_real_prechecks(monkeypatch,
                                                                   tmp_path):
    """Gitlink du méta modifié (HUB_base dérivé) : vrai problème → l'update
    refuse proprement, sans lancer le moindre git, avec le remède."""
    meta, hub, hub_origin = _make_deployment(tmp_path)
    _commit(hub_origin, "release.txt", "release du composant")
    _git("fetch", "origin", cwd=hub)
    _git("checkout", "--detach", "HEAD~1", cwd=hub)  # gitlink du méta modifié
    calls, rollback = _setup_real_prechecks(monkeypatch, meta)

    con = Console(record=True, width=250)
    assert actions._update_hub_via_meta(con, dry_run=False) is False
    text = con.export_text()
    assert "✗ hub_base.pointer" in text and "✗ meta.gitlink_clean" in text
    assert "submodule update" in text
    assert "✗ hub_base.aligned_origin" not in text, \
        "l'information ne doit pas passer pour un problème bloquant"
    assert not any(c and c[0] == "git" for c in calls), \
        "pré-checks en échec → aucun git ne doit tourner"
    rollback.assert_not_called()


def test_update_hub_information_prints_git_text_verbatim(monkeypatch):
    """Le message d'une information peut citer du stderr git : il est
    imprimé tel quel — rich ne doit pas avaler un « [b] » pour une balise."""
    info = Finding(check="hub_base.fetch", ok=True, info=True,
                   detail="impossible de vérifier les mises à jour : remote: "
                          "error: ref a[b] rejeté")
    _setup(monkeypatch, precheck_problems=[info])
    con = Console(record=True, width=250)
    assert actions._update_hub_via_meta(con, dry_run=False) is True
    text = con.export_text()  # un seul export : il vide l'enregistrement
    assert "ℹ hub_base.fetch — impossible de vérifier" in text
    assert "ref a[b] rejeté" in text


def test_update_hub_refreshes_host_units(tmp_path, monkeypatch):
    """Succès d'update hub → les unités système atelier/update sont re-rendues
    et re-posées si le gabarit a changé (jamais fatal)."""
    calls, _rollback = _setup(monkeypatch)
    ens_atelier = mock.Mock()
    ens_update = mock.Mock()
    monkeypatch.setattr(actions, "_ensure_atelier_service", ens_atelier)
    monkeypatch.setattr(actions, "_ensure_update_service", ens_update)
    con = mock.Mock()
    assert actions._update_hub_via_meta(con, dry_run=False) is True
    ens_atelier.assert_called_once()
    ens_update.assert_called_once()
