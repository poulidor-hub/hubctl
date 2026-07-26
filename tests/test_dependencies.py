from pathlib import Path

from hubctl_cli import dependencies
from hubctl_cli.dependencies import load_depends


def test_load_depends_returns_empty_when_file_absent(tmp_path):
    assert load_depends(tmp_path) == []


def test_load_depends_reads_slugs(tmp_path):
    (tmp_path / "depends.yaml").write_text(
        "depends_on:\n"
        "  - slug: exemple-app-2\n"
        "    note: Catalogue\n"
        "  - slug: exemple-app\n"
    )
    assert load_depends(tmp_path) == ["exemple-app-2", "exemple-app"]


def test_load_depends_returns_empty_when_depends_on_absent(tmp_path):
    (tmp_path / "depends.yaml").write_text("# fichier vide\n")
    assert load_depends(tmp_path) == []


def test_load_depends_returns_empty_when_yaml_corrupted(tmp_path):
    (tmp_path / "depends.yaml").write_text("depends_on: [[unclosed")
    assert load_depends(tmp_path) == []


def test_load_depends_skips_entries_without_slug(tmp_path):
    (tmp_path / "depends.yaml").write_text(
        "depends_on:\n"
        "  - slug: exemple-app-2\n"
        "  - note: pas de slug\n"
    )
    assert load_depends(tmp_path) == ["exemple-app-2"]


def test_build_graph_reads_apps_and_workshop(monkeypatch, tmp_path):
    apps = tmp_path / "APPS"
    ws = tmp_path / "APP_Workshop"
    apps.mkdir()
    ws.mkdir()

    sc = apps / "exemple-apps"
    sc.mkdir()
    (sc / "docker-compose.yml").write_text("version: '3'\n")
    (sc / "depends.yaml").write_text("depends_on:\n  - slug: exemple-app-2\n")

    inv = apps / "exemple-app-2"
    inv.mkdir()
    (inv / "docker-compose.yml").write_text("version: '3'\n")

    (ws / "foo-ws").mkdir()

    monkeypatch.setattr(dependencies.hub_state, "APPS_DIR", apps)
    monkeypatch.setattr(dependencies.hub_state, "WORKSHOP_DIR", ws)

    graph = dependencies.build_graph()
    assert graph == {"exemple-apps": ["exemple-app-2"], "exemple-app-2": []}


def test_build_graph_empty_when_no_apps(monkeypatch, tmp_path):
    monkeypatch.setattr(dependencies.hub_state, "APPS_DIR", tmp_path / "APPS")
    monkeypatch.setattr(dependencies.hub_state, "WORKSHOP_DIR", tmp_path / "APP_Workshop")
    assert dependencies.build_graph() == {}


def test_resolve_group_includes_transitive_deps():
    graph = {
        "A": ["B"],
        "B": ["C"],
        "C": [],
    }
    assert dependencies.resolve_group("A", graph) == {"A", "B", "C"}


def test_resolve_group_single_autonomous_app():
    assert dependencies.resolve_group("X", {"X": []}) == {"X"}


def test_resolve_group_unknown_slug_returns_empty():
    assert dependencies.resolve_group("unknown", {"X": []}) == set()


def test_resolve_group_handles_cycle():
    graph = {"A": ["B"], "B": ["A"]}
    assert dependencies.resolve_group("A", graph) == {"A", "B"}


def test_resolve_group_self_reference():
    assert dependencies.resolve_group("A", {"A": ["A"]}) == {"A"}


def test_resolve_group_uses_build_graph_when_no_graph_given(monkeypatch):
    monkeypatch.setattr(
        dependencies, "build_graph", lambda: {"X": ["Y"], "Y": []}
    )
    assert dependencies.resolve_group("X") == {"X", "Y"}
