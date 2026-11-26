# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Tests de `hubctl doctor` (plan unification updates, Task 2).

Tout est mocké au niveau de `park_checks` : les tests valident la logique de
l'action (rapport, exit codes, délégation des fixes), pas git lui-même.
Une dernière section monte de vrais dépôts (déploiement en HEAD détaché) :
les allers-retours `doctor --fix` ne se jouent que sur du git réel.
"""
import re
from pathlib import Path
from unittest import mock

import pytest
from rich.console import Console

from hubctl_cli import actions, park_checks
from hubctl_cli.park_checks import Finding
from hubctl_cli.tests.test_park_checks import (
    _branch_exists, _commit, _git, _head_branch, _make_deployment, _rev_parse,
)


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
                          apps=["app-exemple", "app-exemple-e"])
    assert seen["apps"] == ["app-exemple", "app-exemple-e"]


def test_doctor_wired_in_cli_main():
    """Régression : `hubctl doctor` levait `NameError: action_doctor is not
    defined` — la fonction existait dans actions.py mais n'était pas importée
    dans __main__.py. Les tests unitaires de l'action ne couvrent pas ce
    câblage CLI : on le vérifie explicitement."""
    from hubctl_cli import __main__ as cli_main
    assert callable(getattr(cli_main, "action_doctor", None))


# ── Déploiement (HUB_base détaché sur le gitlink) ────────────────────────────────────────────────
#
# Un clone public + `submodule update` laisse HUB_base en HEAD détaché : c'est
# un déploiement qui suit les releases du méta, pas un checkout de
# développement. origin/main en avance y est une information (VM 26/11 : doctor
# rouge, et `--fix` rebouclait checkout main / reset --hard ↔ submodule update).


def _info(detail: str, check: str = "hub_base.aligned_origin") -> Finding:
    return Finding(check=check, ok=True, info=True, detail=detail)


def _recording() -> Console:
    """Console d'enregistrement large : les assertions portent sur le texte,
    jamais sur l'enroulement à 80 colonnes de rich."""
    return Console(record=True, width=250)


def _spy_git(monkeypatch) -> list[tuple[str | None, tuple[str, ...]]]:
    """Enregistre chaque git lancé par park_checks → [(cwd, argv)]."""
    calls: list[tuple[str | None, tuple[str, ...]]] = []
    real_run = park_checks._run

    def spy(*args, cwd=None, timeout=None):
        calls.append((str(cwd) if cwd else None, tuple(args)))
        return real_run(*args, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(park_checks, "_run", spy)
    return calls


def _mutations_in(hub: Path, calls) -> list[tuple[str, ...]]:
    """checkout / reset / branch exécutés PAR hubctl DANS le sous-module."""
    verbs = ("checkout", "reset", "branch")
    return [args for cwd, args in calls
            if cwd == str(hub) and args[1:2] and args[1] in verbs]


def _fix(con: Console, meta: Path) -> int:
    return actions.action_doctor(console=con, fix=True, assume_yes=True,
                                 parc_root=meta, apps=[])


def test_doctor_information_finding_is_reported_but_never_a_problem(monkeypatch):
    """Un constat d'information est montré avec un marqueur ℹ neutre (ni
    ✓ vert ni ✗), ne compte pas dans les problèmes (exit 0, « parc sain »)
    et ne déclenche aucun --fix."""
    info = _info("mise à jour disponible : HUB_base aaa1111 → bbb2222")
    monkeypatch.setattr(actions.park_checks, "run_checks", lambda root, apps: [info])
    apply_mock = mock.Mock(return_value=0)
    monkeypatch.setattr(actions.park_checks, "apply_safe_fixes", apply_mock)
    con = _recording()
    assert actions.action_doctor(console=con, fix=True, assume_yes=True,
                                 parc_root=Path("/tmp/fake"), apps=[]) == 0
    styled = con.export_text(styles=True)  # un seul export : il vide le tampon
    text = re.sub(r"\x1b\[[0-9;]*m", "", styled)
    assert "ℹ hub_base.aligned_origin — mise à jour disponible" in text
    assert "✓ hub_base.aligned_origin" not in text and "✗" not in text
    # ni le vert du sain (SGR 32) ni le rouge du problème (SGR 31)
    marker = styled.splitlines()[0]
    assert not marker.startswith(("\x1b[32", "\x1b[31")), marker
    assert "parc sain" in text
    apply_mock.assert_not_called()


def test_doctor_fix_on_deployment_never_checks_out_or_resets(monkeypatch, tmp_path):
    """Déploiement sain, origin/main en avance : `--fix` ne fait NI checkout
    main NI reset --hard dans HUB_base, sort 0, et le 2ᵉ passage est un no-op."""
    meta, hub, hub_origin = _make_deployment(tmp_path)
    gitlink = _rev_parse(hub, "HEAD")
    main_before = _rev_parse(hub, "main")
    _commit(hub_origin, "release.txt", "release du composant")
    calls = _spy_git(monkeypatch)

    out = _recording()
    assert _fix(out, meta) == 0
    text = out.export_text()
    assert "ℹ hub_base.aligned_origin — mise à jour disponible" in text
    assert "./hubctl update hub" in text
    assert "✗" not in text and "pull --ff-only" not in text
    assert "parc sain" in text and "remède(s) appliqué(s)" not in text

    again = _recording()
    assert _fix(again, meta) == 0
    assert "remède(s) appliqué(s)" not in again.export_text()

    assert _mutations_in(hub, calls) == []
    assert _head_branch(hub) is None, "un déploiement reste en HEAD détaché"
    assert _rev_parse(hub, "HEAD") == gitlink, "jamais au-delà du gitlink du méta"
    assert _rev_parse(hub, "main") == main_before and _branch_exists(hub, "main")


def test_doctor_fix_realigns_drifted_deployment_with_submodule_update_only(
        monkeypatch, tmp_path):
    """Déploiement détaché qui a dérivé du gitlink : `--fix` se contente de
    `submodule update`, converge en un passage (exit 0), le 2ᵉ est un no-op."""
    meta, hub, hub_origin = _make_deployment(tmp_path)
    gitlink = _rev_parse(hub, "HEAD")
    _commit(hub_origin, "release.txt", "release du composant")
    _git("fetch", "origin", cwd=hub)
    _git("checkout", "--detach", "HEAD~1", cwd=hub)  # dérive : commit plus ancien
    calls = _spy_git(monkeypatch)

    out = _recording()
    assert _fix(out, meta) == 0
    text = out.export_text()
    assert "1 remède(s) appliqué(s)" in text, "le seul remède : submodule update"
    assert "parc sain après correction" in text

    calls.clear()
    again = _recording()
    assert _fix(again, meta) == 0
    assert "remède(s) appliqué(s)" not in again.export_text()

    assert _mutations_in(hub, calls) == []
    assert _head_branch(hub) is None
    assert _rev_parse(hub, "HEAD") == gitlink
    assert _branch_exists(hub, "main")


def test_doctor_drifted_gitlink_stays_a_problem_on_deployment(tmp_path):
    """Vrai problème (gitlink du méta modifié) : reste rouge, exit 1, sur un
    déploiement aussi — et seuls les constats qui le disent."""
    meta, hub, hub_origin = _make_deployment(tmp_path)
    _commit(hub_origin, "release.txt", "release du composant")
    _git("fetch", "origin", cwd=hub)
    _git("checkout", "--detach", "HEAD~1", cwd=hub)

    out = _recording()
    assert actions.action_doctor(console=out, parc_root=meta, apps=[]) == 1
    text = out.export_text()
    failed = set(re.findall(r"✗ ((?:meta|hub_base|app)\.[\w.-]+) —", text))
    assert failed == {"meta.gitlink_clean", "hub_base.pointer"}
    assert "submodule update" in text
    assert "ℹ hub_base.aligned_origin — mise à jour disponible" in text
    assert "pull --ff-only" not in text and "reset --hard" not in text


def test_doctor_detached_meta_stays_a_problem(tmp_path):
    """Le MÉTA détaché n'est pas un déploiement : branche ≠ main reste rouge."""
    meta, _, _ = _make_deployment(tmp_path)
    _git("checkout", "--detach", cwd=meta)
    out = _recording()
    assert actions.action_doctor(console=out, parc_root=meta, apps=[]) == 1
    assert "✗ meta.branch — branche courante du méta : HEAD détaché" \
        in out.export_text()
