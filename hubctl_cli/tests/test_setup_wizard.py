# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Tests setup_wizard — réécriture OIDC_ISSUER au changement de port API."""

from hubctl_cli import setup_wizard


def test_wizard_rewrites_oidc_issuer_when_port_changes(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "HUB_HOST=192.0.2.7\nHUB_API_PORT=18000\nHUB_FRONTEND_PORT=18080\n"
        "OIDC_ISSUER=http://192.0.2.7:18000\n")
    updates = {"HUB_API_PORT": "18001", "HUB_FRONTEND_PORT": "18081",
               "HUB_HTTPS_PORT": "18444"}
    setup_wizard._rewrite_oidc_issuer(env_path, updates)
    text = env_path.read_text()
    assert "OIDC_ISSUER=http://192.0.2.7:18001" in text


def test_wizard_keeps_custom_oidc_issuer(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "HUB_HOST=192.0.2.7\nOIDC_ISSUER=https://sso.exemple.com\n")
    setup_wizard._rewrite_oidc_issuer(
        env_path, {"HUB_API_PORT": "18001"})
    assert "OIDC_ISSUER=https://sso.exemple.com" in env_path.read_text()
