# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Tests de construction du prompt systeme."""
import pytest
from unittest.mock import patch, MagicMock

from hubctl_cli.debug_agent import prompt


class TestBuildSystemPrompt:

    def test_prompt_has_three_layers(self, env_park_root):
        result = prompt.build_system_prompt("TESTAPP", "ro")
        assert "CONVENTIONS" in result.upper() or "CONVENTION" in result.upper()
        assert "CONTEXTE" in result.upper() or "CONTEXT" in result.upper()
        assert "SECURITE" in result.upper() or "SECURITY" in result.upper()

    def test_prompt_includes_mode_ro(self, env_park_root):
        result = prompt.build_system_prompt("TESTAPP", "ro")
        assert "lecture" in result.lower() or "read" in result.lower()

    def test_prompt_includes_mode_rw(self, env_park_root):
        result = prompt.build_system_prompt("TESTAPP", "rw")
        assert "semi-auto" in result.lower() or "rw" in result.lower()

    def test_prompt_includes_app_name(self, env_park_root):
        result = prompt.build_system_prompt("TESTAPP", "ro")
        assert "TESTAPP" in result


class TestFilterEnvVars:

    def test_password_is_masked(self):
        env_text = "DATABASE_URL=postgresql://user:secretpass@localhost/db\nDEBUG=true"
        result = prompt.filter_env_vars(env_text)
        assert "secretpass" not in result
        assert "DEBUG=true" in result

    def test_secret_key_is_masked(self):
        env_text = "SECRET_KEY=abc123supersecret\nPORT=8000"
        result = prompt.filter_env_vars(env_text)
        assert "abc123supersecret" not in result
        assert "PORT=8000" in result

    def test_token_is_masked(self):
        env_text = "JWT_TOKEN=eyJhbGci.token.here\nLOG_LEVEL=info"
        result = prompt.filter_env_vars(env_text)
        assert "eyJhbGci" not in result
        assert "LOG_LEVEL=info" in result

    def test_normal_vars_preserved(self):
        env_text = "APP_NAME=testapp\nPORT=8000\nDEBUG=true"
        result = prompt.filter_env_vars(env_text)
        assert "APP_NAME=testapp" in result
        assert "PORT=8000" in result


class TestBuildAppContext:

    def test_context_includes_compose(self, env_park_root):
        with patch("hubctl_cli.debug_agent.prompt._run_subprocess",
                   return_value={"stdout": "", "stderr": "", "returncode": 0}):
            result = prompt.build_app_context("TESTAPP")
            assert "TESTAPP" in result or "docker-compose" in result.lower()
