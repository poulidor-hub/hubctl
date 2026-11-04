# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Tests du serveur MCP debug."""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from mcp.server import Server
from mcp.types import ListToolsRequest, CallToolRequest, CallToolRequestParams


class TestDebugMCPListTools:

    @pytest.fixture
    def server(self):
        return Server("debug-mcp-test")

    @pytest.mark.asyncio
    async def test_list_tools_returns_all_tools(self, server):
        from hubctl_cli.debug_agent.debug_mcp import register_debug_tools
        register_debug_tools(server, app_slug="TESTAPP", mode="ro")

        result = await server.request_handlers[ListToolsRequest](None)
        tools = result.root.tools
        names = [t.name for t in tools]
        assert "docker_logs" in names
        assert "docker_ps" in names
        assert "docker_restart" in names

    @pytest.mark.asyncio
    async def test_docker_ps_calls_subprocess(self, server, env_park_root):
        from hubctl_cli.debug_agent.debug_mcp import register_debug_tools
        register_debug_tools(server, app_slug="TESTAPP", mode="ro")

        fake_result = MagicMock()
        fake_result.stdout = '{"status": "running"}'
        fake_result.stderr = ""
        fake_result.returncode = 0

        with patch("subprocess.run", return_value=fake_result) as mock_run:
            req = CallToolRequest(
                method="tools/call",
                params=CallToolRequestParams(name="docker_ps", arguments={}),
            )
            result = await server.request_handlers[CallToolRequest](req)
            mock_run.assert_called_once()
            assert "running" in result.root.content[0].text


class TestDebugMCPGuard:

    @pytest.fixture
    def server(self):
        return Server("debug-mcp-test")

    @pytest.mark.asyncio
    async def test_restart_blocked_in_ro_mode(self, server, env_park_root):
        from hubctl_cli.debug_agent.debug_mcp import register_debug_tools
        register_debug_tools(server, app_slug="TESTAPP", mode="ro")

        req = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(
                name="docker_restart",
                arguments={"service": "api"},
            ),
        )
        result = await server.request_handlers[CallToolRequest](req)
        text = result.root.content[0].text
        assert "Lecture seule" in text or "blocked" in text
        assert "docker" in text.lower()

    @pytest.mark.asyncio
    async def test_restart_executed_in_rw_mode(self, server, env_park_root):
        from hubctl_cli.debug_agent.debug_mcp import register_debug_tools
        register_debug_tools(server, app_slug="TESTAPP", mode="rw")

        fake_result = MagicMock()
        fake_result.stdout = "restarted"
        fake_result.stderr = ""
        fake_result.returncode = 0

        with patch("subprocess.run", return_value=fake_result) as mock_run:
            req = CallToolRequest(
                method="tools/call",
                params=CallToolRequestParams(
                    name="docker_restart",
                    arguments={"service": "api"},
                ),
            )
            result = await server.request_handlers[CallToolRequest](req)
            mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_db_query_select_rejects_non_select(self, server, env_park_root):
        from hubctl_cli.debug_agent.debug_mcp import register_debug_tools
        register_debug_tools(server, app_slug="TESTAPP", mode="ro")

        req = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(
                name="db_query_select",
                arguments={"query": "DROP TABLE users;"},
            ),
        )
        result = await server.request_handlers[CallToolRequest](req)
        text = result.root.content[0].text
        assert "SELECT" in text or "refuse" in text.lower()

    @pytest.mark.asyncio
    async def test_db_query_select_accepts_select(self, server, env_park_root):
        from hubctl_cli.debug_agent.debug_mcp import register_debug_tools
        register_debug_tools(server, app_slug="TESTAPP", mode="ro")

        fake_result = MagicMock()
        fake_result.stdout = "id | name\n1 | test"
        fake_result.stderr = ""
        fake_result.returncode = 0

        with patch("subprocess.run", return_value=fake_result):
            req = CallToolRequest(
                method="tools/call",
                params=CallToolRequestParams(
                    name="db_query_select",
                    arguments={"query": "SELECT * FROM users LIMIT 10;"},
                ),
            )
            result = await server.request_handlers[CallToolRequest](req)
            assert "test" in result.root.content[0].text

    @pytest.mark.asyncio
    async def test_read_file_restricted_to_app_dir(self, server, env_park_root):
        from hubctl_cli.debug_agent.debug_mcp import register_debug_tools
        register_debug_tools(server, app_slug="TESTAPP", mode="ro")

        req = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(
                name="read_file",
                arguments={"path": "../../../etc/passwd"},
            ),
        )
        result = await server.request_handlers[CallToolRequest](req)
        text = result.root.content[0].text
        assert "refus" in text.lower() or "interdit" in text.lower() or "error" in text.lower()
