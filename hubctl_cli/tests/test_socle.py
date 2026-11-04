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
