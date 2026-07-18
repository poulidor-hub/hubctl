from pathlib import Path
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
