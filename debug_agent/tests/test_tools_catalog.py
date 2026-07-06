"""Tests du catalogue d'outils debug."""
from hubctl_cli.debug_agent.tools_catalog import (
    TOOLS,
    HANDLER_CLASSIFICATION,
    CATEGORIES,
)


class TestToolsCatalog:

    def test_tools_not_empty(self):
        assert len(TOOLS) > 0

    def test_all_tools_have_name_and_schema(self):
        for tool in TOOLS:
            assert tool.name, f"Tool sans nom: {tool}"
            assert tool.inputSchema is not None
            assert tool.inputSchema.get("type") == "object"

    def test_every_tool_is_classified(self):
        for tool in TOOLS:
            assert tool.name in HANDLER_CLASSIFICATION, (
                f"Outil non classifie: {tool.name}"
            )

    def test_classification_values_are_valid(self):
        valid = set(CATEGORIES.keys())
        for tool_name, cat in HANDLER_CLASSIFICATION.items():
            assert cat in valid, (
                f"Categorie inconnue '{cat}' pour l'outil {tool_name}"
            )

    def test_read_tools_exist(self):
        names = [t.name for t in TOOLS]
        assert "docker_logs" in names
        assert "docker_ps" in names
        assert "db_query_select" in names
        assert "read_file" in names

    def test_destructive_tools_exist(self):
        names = [t.name for t in TOOLS]
        assert "docker_down" in names
        assert "docker_restart" in names

    def test_categories_have_ro_rw_rules(self):
        for cat_name, cat_meta in CATEGORIES.items():
            assert "ro" in cat_meta, f"Categorie {cat_name} sans regle 'ro'"
            assert "rw_confirm" in cat_meta

    def test_read_category_allows_ro(self):
        assert CATEGORIES["read"]["ro"] == "allow"
        assert CATEGORIES["read"]["rw_confirm"] is False

    def test_destructive_hard_requires_double_confirm(self):
        assert CATEGORIES["destructive_hard"]["rw_confirm"] is True
        assert CATEGORIES["destructive_hard"]["rw_confirm_double"] is True
