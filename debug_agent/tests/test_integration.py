"""Tests d'integration du module debug_agent."""
import pytest
from unittest.mock import patch, MagicMock

from mcp.server import Server
from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest


class TestIntegration:

    @pytest.mark.asyncio
    async def test_full_ro_session_flow(self, env_park_root):
        """Une session ro complete : list tools, call read, call blocked write."""
        from hubctl_cli.debug_agent.debug_mcp import register_debug_tools
        from hubctl_cli.debug_agent.tools_catalog import TOOLS

        server = Server("debug-test")
        register_debug_tools(server, app_slug="TESTAPP", mode="ro")

        result = await server.request_handlers[ListToolsRequest](None)
        assert len(result.root.tools) == len(TOOLS)

        fake = MagicMock()
        fake.stdout = "running"
        fake.stderr = ""
        fake.returncode = 0
        with patch("subprocess.run", return_value=fake):
            req = CallToolRequest(
                method="tools/call",
                params=CallToolRequestParams(name="docker_ps", arguments={}),
            )
            resp = await server.request_handlers[CallToolRequest](req)
            assert "running" in resp.root.content[0].text

        req = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(
                name="docker_down", arguments={},
            ),
        )
        resp = await server.request_handlers[CallToolRequest](req)
        assert "blocked" in resp.root.content[0].text.lower() or "lecture" in resp.root.content[0].text.lower()

    @pytest.mark.asyncio
    async def test_full_rw_session_flow(self, env_park_root):
        """Une session rw complete : write tools executent via subprocess."""
        from hubctl_cli.debug_agent.debug_mcp import register_debug_tools

        server = Server("debug-test")
        register_debug_tools(server, app_slug="TESTAPP", mode="rw")

        fake = MagicMock()
        fake.stdout = "done"
        fake.stderr = ""
        fake.returncode = 0
        with patch("subprocess.run", return_value=fake) as mock_run:
            req = CallToolRequest(
                method="tools/call",
                params=CallToolRequestParams(
                    name="docker_restart",
                    arguments={"service": "api"},
                ),
            )
            resp = await server.request_handlers[CallToolRequest](req)
            mock_run.assert_called_once()
            assert "done" in resp.root.content[0].text

    def test_prompt_and_catalog_compatible(self, env_park_root):
        """Le prompt mentionne les outils qui existent dans le catalogue."""
        from hubctl_cli.debug_agent.prompt import build_system_prompt
        from hubctl_cli.debug_agent.tools_catalog import TOOLS

        system_prompt = build_system_prompt("TESTAPP", "ro")
        tool_names = [t.name for t in TOOLS]
        mentioned = sum(
            1 for name in tool_names if name in system_prompt
        )
        assert mentioned >= 3, (
            f"Seulement {mentioned} outils mentionnes dans le prompt"
        )

    def test_all_handlers_have_classification(self):
        """Chaque handler a une entree dans HANDLER_CLASSIFICATION."""
        from hubctl_cli.debug_agent.debug_mcp import HANDLERS
        from hubctl_cli.debug_agent.tools_catalog import HANDLER_CLASSIFICATION

        for name in HANDLERS:
            assert name in HANDLER_CLASSIFICATION, (
                f"Handler '{name}' sans classification"
            )
