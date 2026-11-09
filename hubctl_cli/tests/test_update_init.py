# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Tests de l'étape init `_ensure_update_service` (agent de mise a jour).

`hubctl init` doit mettre en place le service hôte `hubctl-update` comme
l'atelier, en plus simple : PAS de token dédié (l'agent réutilise
~/.config/hubctl/atelier_token, déjà assuré par _ensure_atelier_service) —
il ne reste que le rendu de l'unit (placeholders @REAL_USER@/@PARC_ROOT@
substitués, fichier rendu sous packaging/.rendered/, gitigné) puis
l'installation sudo idempotente (cp + daemon-reload + enable --now si
absente, enable --now si inactive, rien si active), avec simple rappel des
commandes à la main sans TTY — AUCUNE exception (l'init ne doit jamais
échouer ici).

Isolation façon test_atelier_init._patch_env : unit + gabarit + rendu sous
tmp_path, utilisateur substituable — aucun test ne touche l'unit installée
de la machine. Un seul test lit le vrai gabarit livré (garde-fou
anti-régression du packaging, lecture seule).
"""
import sys
import types
from pathlib import Path
from unittest import mock

from hubctl_cli import actions, hub_state

_GABARIT = """\
# gabarit de test
[Unit]
Description=hubctl agent de mise a jour du parc

[Service]
User=@REAL_USER@
WorkingDirectory=@PARC_ROOT@
ExecStart=@PARC_ROOT@/hubctl update --serve --host 172.17.0.1 --port 8778
ReadWritePaths=@PARC_ROOT@ %h/.config/hubctl

[Install]
WantedBy=multi-user.target
"""


def _patch_env(tmp_path, monkeypatch):
    """Parc + unit systemd + gabarit/rendu sous tmp_path."""
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(
        actions, "_UPDATE_UNIT",
        tmp_path / "etc" / "systemd" / "system" / "hubctl-update.service")
    packaging = tmp_path / "packaging"
    packaging.mkdir(exist_ok=True)
    monkeypatch.setattr(actions, "_UPDATE_UNIT_GABARIT",
                        packaging / "hubctl-update.service")
    monkeypatch.setattr(actions, "_UPDATE_UNIT_RENDERED",
                        packaging / ".rendered" / "hubctl-update.service")
    monkeypatch.setattr(actions, "_atelier_user", lambda: "utilisateur-test")
    actions._UPDATE_UNIT_GABARIT.write_text(_GABARIT)
    return tmp_path


def _printed(con) -> str:
    return " ".join(str(c.args[0]) for c in con.print.call_args_list if c.args)


# ── Unit systemd : exécution sudo vs rappel manuel ──


def test_no_tty_prints_commands_without_failure(tmp_path, monkeypatch):
    _patch_env(tmp_path, monkeypatch)  # unit absente → installation nécessaire
    run_shell = mock.Mock()
    monkeypatch.setattr(actions.utils, "run_shell", run_shell)
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    con = mock.Mock()
    actions._ensure_update_service(con)  # ne doit JAMAIS lever
    run_shell.assert_not_called()
    printed = _printed(con)
    assert "sudo cp" in printed
    assert "daemon-reload" in printed
    assert "enable --now hubctl-update" in printed
    # Le fichier rendu (substitué) existe : la commande rappelée est exécutable
    rendered = actions._UPDATE_UNIT_RENDERED
    assert rendered.is_file()
    assert "@REAL_USER@" not in rendered.read_text()
    assert str(rendered) in printed


def test_tty_runs_install_commands(tmp_path, monkeypatch):
    _patch_env(tmp_path, monkeypatch)
    run_shell = mock.Mock(return_value=0)
    monkeypatch.setattr(actions.utils, "run_shell", run_shell)
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    actions._ensure_update_service(mock.Mock())
    calls = run_shell.call_args_list
    assert len(calls) == 3
    cp_cmd = calls[0].args[0]
    assert cp_cmd[:2] == ["sudo", "cp"]
    # La source est le fichier RENDU (substitué), pas le gabarit brut
    assert cp_cmd[2] == str(actions._UPDATE_UNIT_RENDERED)
    assert calls[1].args[0] == ["sudo", "systemctl", "daemon-reload"]
    assert calls[2].args[0] == ["sudo", "systemctl", "enable", "--now", "hubctl-update"]
    assert all(c.kwargs["dry_run"] is False for c in calls)


def test_rendered_unit_substitutes_placeholders(tmp_path, monkeypatch):
    """L'unit rendue ne contient aucun placeholder : user courant + parc réel ;
    le gabarit source reste un gabarit (placeholders intacts)."""
    _patch_env(tmp_path, monkeypatch)
    monkeypatch.setattr(actions.utils, "run_shell", mock.Mock(return_value=0))
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    actions._ensure_update_service(mock.Mock())
    rendered = actions._UPDATE_UNIT_RENDERED.read_text()
    assert "User=utilisateur-test" in rendered
    assert f"WorkingDirectory={tmp_path.resolve()}" in rendered
    assert (f"ExecStart={tmp_path.resolve()}/hubctl update --serve "
            f"--host 172.17.0.1 --port 8778") in rendered
    assert (f"ReadWritePaths={tmp_path.resolve()} %h/.config/hubctl") in rendered
    assert "@REAL_USER@" not in rendered
    assert "@PARC_ROOT@" not in rendered
    assert "User=@REAL_USER@" in actions._UPDATE_UNIT_GABARIT.read_text()


def test_already_active_is_noop(tmp_path, monkeypatch):
    _patch_env(tmp_path, monkeypatch)
    unit = actions._UPDATE_UNIT
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n")
    monkeypatch.setattr(
        actions.subprocess, "run",
        mock.Mock(return_value=types.SimpleNamespace(
            returncode=0, stdout="active\n", stderr="")))
    run_shell = mock.Mock()
    monkeypatch.setattr(actions.utils, "run_shell", run_shell)
    con = mock.Mock()
    actions._ensure_update_service(con)
    run_shell.assert_not_called()
    assert "✓" in _printed(con)
    assert "hubctl-update" in _printed(con)


def test_unit_present_but_inactive_runs_enable_only(tmp_path, monkeypatch):
    """Unit installée mais service inactif → seul `enable --now` relancé
    (pas de cp / daemon-reload, pas de re-rendu)."""
    _patch_env(tmp_path, monkeypatch)
    unit = actions._UPDATE_UNIT
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n")
    monkeypatch.setattr(
        actions.subprocess, "run",
        mock.Mock(return_value=types.SimpleNamespace(
            returncode=3, stdout="inactive\n", stderr="")))
    run_shell = mock.Mock(return_value=0)
    monkeypatch.setattr(actions.utils, "run_shell", run_shell)
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    actions._ensure_update_service(mock.Mock())
    calls = run_shell.call_args_list
    assert len(calls) == 1
    assert calls[0].args[0] == ["sudo", "systemctl", "enable", "--now", "hubctl-update"]


# ── dry-run : impression seule, zéro écriture ──


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    _patch_env(tmp_path, monkeypatch)
    run_shell = mock.Mock(return_value=0)
    monkeypatch.setattr(actions.utils, "run_shell", run_shell)
    actions._ensure_update_service(mock.Mock(), dry_run=True)
    assert not actions._UPDATE_UNIT.exists()
    assert not actions._UPDATE_UNIT_RENDERED.exists()
    assert run_shell.call_args_list, "run_shell doit recevoir les commandes (impression seule)"
    assert all(c.kwargs["dry_run"] is True for c in run_shell.call_args_list)


# ── Gabarit livré : garde-fou packaging (lecture seule, pas de patch) ──


def test_gabarit_livre_utilise_placeholders():
    """Le gabarit embarqué ne porte AUCUN user ni chemin en dur : les hôtes
    où le parc n'appartient pas à « admin » doivent s'installer sans édition
    manuelle. Contrat « Centre de mises à jour » (spec §4.1) : routes
    update/*, port 8778, ReadWritePaths parc ENTIER + ~/.config/hubctl."""
    gabarit = (Path(actions.__file__).resolve().parent.parent
               / "packaging" / "hubctl-update.service")
    text = gabarit.read_text(encoding="utf-8")
    assert "User=@REAL_USER@" in text
    assert "@PARC_ROOT@" in text
    assert "Description=hubctl agent de mise a jour du parc" in text
    assert ("ExecStart=@PARC_ROOT@/hubctl update --serve "
            "--host 172.17.0.1 --port 8778") in text
    assert "ReadWritePaths=@PARC_ROOT@ %h/.config/hubctl" in text
    # Durcissement hérité de l'atelier, jamais allégé
    for directive in ("NoNewPrivileges=true", "ProtectSystem=strict",
                      "PrivateTmp=true", "ProtectHome=read-only",
                      "UMask=0077", "Restart=on-failure",
                      "After=network-online.target docker.service",
                      "CapabilityBoundingSet=",
                      "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6"):
        assert directive in text, directive
    assert "User=admin" not in text
    assert "%h/MyCOMPANY" not in text
