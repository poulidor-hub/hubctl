# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Résolution du chemin du coffre (vault) — hubctl 0.7.3.

Depuis la restructuration (symlink racine ``hubctl_cli`` supprimé), le coffre
vit par défaut dans le paquet : ``hubctl_src/hubctl_cli/vault/``. Le chemin
legacy ``<PARC_ROOT>/hubctl_cli/vault/`` reste supporté en repli pour les
installations où ``hubctl_cli/`` est un vrai dossier racine.
"""
from pathlib import Path

from hubctl_cli import hub_state


def test_vault_dir_prefers_package_vault(tmp_path):
    pkg = tmp_path / "pkg"
    (pkg / "vault").mkdir(parents=True)
    legacy = tmp_path / "root" / "hubctl_cli" / "vault"
    legacy.mkdir(parents=True)
    assert hub_state.vault_dir(parc_root=tmp_path / "root", pkg_dir=pkg) == pkg / "vault"


def test_vault_dir_falls_back_to_legacy(tmp_path):
    pkg = tmp_path / "pkg"  # vault du paquet inexistant (fresh submodule)
    legacy = tmp_path / "root" / "hubctl_cli" / "vault"
    legacy.mkdir(parents=True)
    assert hub_state.vault_dir(parc_root=tmp_path / "root", pkg_dir=pkg) == legacy


def test_vault_dir_defaults_to_package(tmp_path):
    pkg = tmp_path / "pkg"
    root = tmp_path / "root"  # rien n'existe encore → création côté paquet
    assert hub_state.vault_dir(parc_root=root, pkg_dir=pkg) == pkg / "vault"


def test_vault_path_in_real_package():
    expected = Path(hub_state.__file__).resolve().parent / "vault" / "parc.vault.enc"
    assert hub_state.vault_path() == expected


def test_vault_path_exists_on_this_install():
    # Garde-fou installation réelle : le coffre existant doit être trouvé
    # (il vit dans le paquet depuis la restructuration du symlink racine).
    assert hub_state.vault_path().exists()


def test_backups_dir_follows_same_resolution(tmp_path):
    pkg = tmp_path / "pkg"
    (pkg / "backups").mkdir(parents=True)
    legacy = tmp_path / "root" / "hubctl_cli" / "backups"
    legacy.mkdir(parents=True)
    assert hub_state.backups_dir(parc_root=tmp_path / "root", pkg_dir=pkg) == pkg / "backups"
    # fallback legacy
    assert hub_state.backups_dir(parc_root=tmp_path / "root", pkg_dir=tmp_path / "autre") == legacy


def test_recovery_dir_follows_same_resolution(tmp_path):
    pkg = tmp_path / "pkg"
    (pkg / "recovery").mkdir(parents=True)
    legacy = tmp_path / "root" / "hubctl_cli" / "recovery"
    legacy.mkdir(parents=True)
    assert hub_state.recovery_dir(parc_root=tmp_path / "root", pkg_dir=pkg) == pkg / "recovery"
    # fallback legacy
    assert hub_state.recovery_dir(parc_root=tmp_path / "root", pkg_dir=tmp_path / "autre") == legacy
