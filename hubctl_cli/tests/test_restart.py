# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Tests de `hubctl restart` — recharger la configuration par recreate.

Constat VM du 26/11/2026 : `./hub.sh restart` fait un `compose restart`, qui
relance les processus SANS relire le `.env` ; `hubctl start prod` est un no-op
quand le Hub tourne déjà (et sans app dans `APPS/`) ; `hubctl restart`
n'existait pas. Modifier `HUB_base/.env` (whitelist, ports…) n'était donc
jamais appliqué, sans que rien ne le dise.

`restart` passe par `compose up -d` : chaque service dont la configuration a
changé est recréé, les autres restent en place.
"""
import io
from types import SimpleNamespace

from rich.console import Console

from hubctl_cli import actions
from hubctl_cli.__main__ import build_parser


def _env(monkeypatch, calls, *, healthy=True, apps=("app-a", "app-b")):
    """Hub + apps actives factices ; `calls` reçoit (cible, make, dry-run)."""
    monkeypatch.setattr(actions.hub_state, "get_apps",
                        lambda: [SimpleNamespace(name=n) for n in apps])

    def fake_hub_make(target, *, dry_run, console=None, timeout=None):
        calls.append(("hub", target, dry_run))
        return 0

    def fake_app_make(name, target, *, dry_run, console=None, timeout=120, apps_dir=None):
        calls.append((name, target, dry_run))
        return 0

    monkeypatch.setattr(actions, "hub_make", fake_hub_make)
    monkeypatch.setattr(actions, "app_make", fake_app_make)
    monkeypatch.setattr(actions, "_health_check", lambda *a, **kw: healthy)
    out = io.StringIO()
    return Console(file=out, width=120), out


def test_restart_recreates_hub_then_active_apps(monkeypatch):
    calls = []
    con, _ = _env(monkeypatch, calls)
    assert actions.action_restart(con, dry_run=False) == 0
    assert calls == [("hub", "up-prod", False), ("app-a", "up", False), ("app-b", "up", False)]


def test_restart_hub_unhealthy_fails(monkeypatch):
    calls = []
    con, _ = _env(monkeypatch, calls, healthy=False)
    assert actions.action_restart(con, dry_run=False) == 1
    assert [c[0] for c in calls] == ["hub"], "Hub non sain : les apps ne sont pas retouchées"


def test_restart_dry_run_passes_the_flag_everywhere(monkeypatch):
    calls = []
    con, _ = _env(monkeypatch, calls)
    assert actions.action_restart(con, dry_run=True) == 0
    assert calls and all(c[2] is True for c in calls)


def test_restart_without_apps(monkeypatch):
    calls = []
    con, _ = _env(monkeypatch, calls, apps=())
    assert actions.action_restart(con, dry_run=False) == 0
    assert calls == [("hub", "up-prod", False)]


def test_parser_parses_restart_subcommand():
    args = build_parser().parse_args(["restart"])
    assert args.command == "restart"
