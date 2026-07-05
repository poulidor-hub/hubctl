"""Tests park-restore : restauration de l'arborescence secrets sans écrasement.

Couvre la cohérence du round-trip export -> restore pour l'arbre HUB_base/secrets/
(sous-dossier service_accounts/ inclus).
"""
from __future__ import annotations
from pathlib import Path

import hubctl_cli.park_restore as pr


def test_restore_tree_copies_new_files_recursively(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    (src / "service_accounts").mkdir(parents=True)
    (src / "jwt_private_key.pem").write_bytes(b"PRIVATE")
    (src / "service_accounts" / "sa.env").write_text("SA=1\n")

    pr._restore_tree(src, dest)

    assert (dest / "jwt_private_key.pem").read_bytes() == b"PRIVATE"
    assert (dest / "service_accounts" / "sa.env").read_text() == "SA=1\n"


def test_restore_tree_does_not_overwrite_existing(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    (src / "jwt_private_key.pem").write_bytes(b"NEW")
    (dest / "jwt_private_key.pem").write_bytes(b"EXISTING")

    pr._restore_tree(src, dest)

    assert (dest / "jwt_private_key.pem").read_bytes() == b"EXISTING"


def test_restore_tree_creates_missing_dest(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest" / "nested"  # n'existe pas
    src.mkdir()
    (src / "f.pem").write_bytes(b"X")

    pr._restore_tree(src, dest)

    assert (dest / "f.pem").read_bytes() == b"X"
