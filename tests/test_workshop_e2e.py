"""Test de bout en bout du cycle workshop — copie réelle avec ignore.

Vérifie que :
1. Le copytree fonctionne correctement (pas de blocage)
2. Les fichiers source sont bien copiés (.env, Makefile, etc.)
3. Les répertoires lourds sont exclus (__pycache__, .venv, node_modules)
4. La copie est visible et mesurable
"""
import os
import shutil
import time
from pathlib import Path

import pytest

from hubctl_cli.actions import _workshop_ignore, _WORKSHOP_IGNORE_DIRS


def _touch(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _make_app_dir(base: Path, name: str) -> Path:
    """Crée une arborescence d'app réaliste dans base/name."""
    app = base / name
    # Fichiers essentiels (doivent être copiés)
    _touch(app / "docker-compose.yml", "services:\n  api:\n    build: .\n")
    _touch(app / "Makefile", "build:\n\tdocker compose build\n")
    _touch(app / ".env", "APP_SLUG=testapp\nDB_PASSWORD=secret123\n")
    _touch(app / "VERSION", "1.0.0\n")
    _touch(app / "depends.yaml", "depends_on:\n  - slug: OTHER\n")
    _touch(app / "backend" / "main.py", "print('hello')\n")
    _touch(app / "frontend" / "index.html", "<html></html>\n")
    _touch(app / "README.md", "# Test\n")
    _touch(app / ".gitignore", "*.pyc\n")

    # Fichiers qui DOIVENT être ignorés
    _touch(app / "__pycache__" / "main.cpython-312.pyc", "mock")
    _touch(app / "backend" / "__pycache__" / "utils.cpython-312.pyc", "mock")
    _touch(app / "backend" / "app.pyc", "mock")
    _touch(app / ".venv" / "lib" / "python" / "site-packages" / "rich" / "__init__.py", "mock")
    _touch(app / "node_modules" / "react" / "index.js", "mock")
    _touch(app / ".next" / "cache" / "webpack" / "1.pack", "mock")
    _touch(app / "dist" / "bundle.js", "mock")
    _touch(app / "build" / "app.js", "mock")
    _touch(app / ".mypy_cache" / "3.12" / "main.json", "mock")
    _touch(app / ".pytest_cache" / "v" / "cache" / "lastfailed", "mock")
    _touch(app / ".git" / "HEAD", "ref: refs/heads/main\n")
    _touch(app / ".git" / "objects" / "ab" / "1234567890abcdef", "mock")
    _touch(app / "app.log", "2024 log line\n")
    _touch(app / ".DS_Store", "mock")
    _touch(app / "dist" / "app-1.0.egg-info", "mock")
    _touch(app / "env" / "bin" / "python", "mock")

    return app


class TestWorkshopCopytreeE2E:
    """Tests de bout en bout : copie réelle avec ignore."""

    def test_copytree_copies_essential_files(self, tmp_path):
        """Les fichiers source (.env, Makefile, .py, docker-compose.yml) sont copiés."""
        src = _make_app_dir(tmp_path, "src")
        dst = tmp_path / "dst"

        t0 = time.monotonic()
        shutil.copytree(str(src), str(dst), symlinks=True, ignore=_workshop_ignore)
        elapsed = time.monotonic() - t0

        assert dst.exists(), "le dossier destination doit exister"
        assert (dst / "docker-compose.yml").exists(), "docker-compose.yml doit être copié"
        assert (dst / "Makefile").exists(), "Makefile doit être copié"
        assert (dst / ".env").exists(), ".env doit être copié"
        assert (dst / "VERSION").exists(), "VERSION doit être copié"
        assert (dst / "depends.yaml").exists(), "depends.yaml doit être copié"
        assert (dst / "backend" / "main.py").exists(), "main.py doit être copié"
        assert (dst / "frontend" / "index.html").exists(), "index.html doit être copié"
        assert (dst / ".gitignore").exists(), ".gitignore doit être copié"
        assert elapsed < 5.0, f"copie trop lente ({elapsed:.1f}s)"

    def test_copytree_ignores_heavy_dirs(self, tmp_path):
        """__pycache__, .venv, node_modules, .git, etc. sont exclus."""
        src = _make_app_dir(tmp_path, "src")
        dst = tmp_path / "dst"

        shutil.copytree(str(src), str(dst), symlinks=True, ignore=_workshop_ignore)

        # Ces dossiers ne doivent PAS exister (vérifier uniquement les vrais dossiers)
        for ignored_dir in _WORKSHOP_IGNORE_DIRS:
            ignored_path = dst / ignored_dir
            if ignored_path.is_dir():
                pytest.fail(f"{ignored_dir} doit être ignoré mais est présent dans la copie")

    def test_copytree_ignores_heavy_files(self, tmp_path):
        """*.pyc, *.egg-info, *.log, .DS_Store sont exclus."""
        src = _make_app_dir(tmp_path, "src")
        dst = tmp_path / "dst"

        shutil.copytree(str(src), str(dst), symlinks=True, ignore=_workshop_ignore)

        # .pyc dans backend doit être ignoré
        assert not (dst / "backend" / "app.pyc").exists(), "*.pyc doit être ignoré"
        # __pycache__ dans backend aussi
        assert not (dst / "backend" / "__pycache__").exists()
        # app.log
        assert not (dst / "app.log").exists(), "*.log doit être ignoré"
        # .DS_Store
        assert not (dst / ".DS_Store").exists()
        # egg-info
        assert not (dst / "dist" / "app-1.0.egg-info").exists()

    def test_copytree_keeps_nested_source_dirs(self, tmp_path):
        """Les sous-dossiers source (backend/, frontend/) sont copiés
        mais leurs __pycache__ internes sont ignorés."""
        src = _make_app_dir(tmp_path, "src")
        dst = tmp_path / "dst"

        shutil.copytree(str(src), str(dst), symlinks=True, ignore=_workshop_ignore)

        # backend/ existe
        assert (dst / "backend").is_dir(), "backend/ doit être un dossier"
        # backend/main.py existe
        assert (dst / "backend" / "main.py").exists(), "main.py doit être présent"
        # MAIS backend/__pycache__ est ignoré
        assert not (dst / "backend" / "__pycache__").exists(), \
            "backend/__pycache__ doit être ignoré"

    def test_ignore_function_returns_correct_set(self, tmp_path):
        """_workshop_ignore retourne les bons noms."""
        app = _make_app_dir(tmp_path, "src")
        names = os.listdir(str(app))
        ignored = _workshop_ignore(str(app), names)

        # Dossiers ignorés (uniquement les vrais dossiers dans la source)
        for d in _WORKSHOP_IGNORE_DIRS:
            full = app / d
            if d in names and full.is_dir():
                assert d in ignored, f"{d} devrait être dans la liste ignorée"

        # Fichiers source PAS ignorés (même si le nom ressemble à un dossier ignoré)
        assert ".env" not in ignored, ".env (fichier) ne doit PAS être ignoré"
        assert "Makefile" not in ignored
        assert "VERSION" not in ignored
        assert "backend" not in ignored
        assert "frontend" not in ignored


class TestWorkshopDryRun:
    """Test que l'action en dry-run ne bloque pas et affiche les commandes."""

    def test_workshop_create_dry_run_completes(self, monkeypatch, tmp_path):
        """action_workshop_create en dry-run doit terminer sans erreur."""
        from hubctl_cli import actions
        import io
        from rich.console import Console

        # Créer une app de test
        app = _make_app_dir(tmp_path, "APPS/testapp")
        ws_dir = tmp_path / "APP_Workshop"
        ws_dir.mkdir()

        # Rediriger les constantes APPS_DIR/WORKSHOP_DIR
        monkeypatch.setattr(actions.hub_state, "APPS_DIR", app.parent)
        monkeypatch.setattr(actions.hub_state, "WORKSHOP_DIR", ws_dir)
        monkeypatch.setattr(actions.hub_state, "HUB_DIR", tmp_path / "HUB_base")
        monkeypatch.setattr(actions.hub_state, "check_docker", lambda: True)
        # Simuler un dev-mode marqueur
        (tmp_path / "HUB_base").mkdir()
        (tmp_path / "HUB_base" / ".dev-mode").touch()

        buf = io.StringIO()
        console = Console(file=buf, width=120, force_terminal=True)

        t0 = time.monotonic()
        actions.action_workshop_create(console, "testapp", dry_run=True)
        elapsed = time.monotonic() - t0

        output = buf.getvalue()
        assert "Mise en atelier" in output or "testapp" in output
        assert elapsed < 2.0, f"dry-run trop lent ({elapsed:.1f}s), output: {output[:200]}"
