# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Fixtures partagees pour les tests du module debug_agent."""
import pytest
import tempfile
from pathlib import Path


@pytest.fixture
def tmp_park(tmp_path):
    """Retourne un faux parc POULIDOR avec une app de test."""
    apps_dir = tmp_path / "APPS"
    app_dir = apps_dir / "TESTAPP"
    app_dir.mkdir(parents=True)
    (app_dir / "docker-compose.yml").write_text(
        "services:\n  api:\n    image: test:latest\n    ports:\n      - '9001:8000'\n"
    )
    return tmp_path


@pytest.fixture
def env_park_root(monkeypatch, tmp_park):
    """Set POULIDOR_ROOT to tmp_park."""
    monkeypatch.setenv("POULIDOR_ROOT", str(tmp_park))
    return tmp_park
