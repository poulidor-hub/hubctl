# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Tests unitaires des helpers socle (sans Docker)."""
from pathlib import Path
from hubctl_cli.socle import collect_app_socle_versions, socle_version


def test_socle_version_reads_version_file(tmp_path: Path):
    (tmp_path / "VERSION").write_text("2.22.0\n")
    assert socle_version(tmp_path) == "2.22.0"


def test_socle_version_missing_returns_none(tmp_path: Path):
    assert socle_version(tmp_path) is None


def test_collect_app_socle_versions(tmp_path: Path):
    apps = tmp_path / "APPS"
    for slug, ver in {"alpha": "2.22.0", "beta": "2.21.1", "gamma": None}.items():
        d = apps / slug
        d.mkdir(parents=True)
        env = d / ".env"
        if ver:
            env.write_text(f"APP_SLUG={slug}\nAPP_SOCLE_VERSION={ver}\n")
        else:
            env.write_text(f"APP_SLUG={slug}\n")
    result = collect_app_socle_versions(tmp_path)
    assert result == {"alpha": "2.22.0", "beta": "2.21.1", "gamma": "-"}


def test_collect_ignores_non_app_dirs(tmp_path: Path):
    (tmp_path / "APPS").mkdir()
    (tmp_path / "APPS" / ".gitkeep").touch()
    (tmp_path / "APPS" / "MIGRATION").mkdir()
    assert collect_app_socle_versions(tmp_path) == {}



# ── socle_build : console et budget (utilisé par `hubctl app install`) ──

from unittest import mock

from rich.console import Console

from hubctl_cli import socle as socle_mod


def _socle_pret(tmp_path, monkeypatch, rc=0):
    socle_dir = tmp_path / "HUBsocle"
    (socle_dir / "images" / "web").mkdir(parents=True)
    (socle_dir / "VERSION").write_text("2.40.2\n")
    monkeypatch.setattr(socle_mod, "find_socle_dir", lambda: socle_dir)
    monkeypatch.setattr(socle_mod, "_image_exists", lambda tag: False)
    appels = []
    monkeypatch.setattr(socle_mod.utils, "run_shell",
                        lambda cmd, **kw: appels.append(kw) or rc)
    return appels


def test_socle_build_passe_le_budget_et_la_console(tmp_path, monkeypatch):
    appels = _socle_pret(tmp_path, monkeypatch)
    con = Console()
    assert socle_mod.socle_build(console=con, timeout=900) == 0
    assert len(appels) == 2, "une image api + une image web"
    assert [kw["timeout"] for kw in appels] == [900, 900]
    assert all(kw["console"] is con and kw["dry_run"] is False for kw in appels)


def test_socle_build_budget_par_defaut_generieux(tmp_path, monkeypatch):
    """Sans budget explicite : 1800 s par image — jamais le défaut 120 s de
    run_shell, qui tuerait le build en cours."""
    appels = _socle_pret(tmp_path, monkeypatch)
    assert socle_mod.socle_build() == 0
    assert [kw["timeout"] for kw in appels] == [1800, 1800]


def test_socle_build_timeout_remonte_124_et_arrete(tmp_path, monkeypatch):
    appels = _socle_pret(tmp_path, monkeypatch, rc=124)
    assert socle_mod.socle_build(timeout=60) == 124
    assert len(appels) == 1, "pas de 2ᵉ image après un échec"


def test_socle_build_dry_run_n_execute_rien(tmp_path, monkeypatch):
    appels = _socle_pret(tmp_path, monkeypatch)
    assert socle_mod.socle_build(dry_run=True, console=Console()) == 0
    assert [kw["dry_run"] for kw in appels] == [True, True]
