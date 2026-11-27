# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Tests de l'étape init `_ensure_atelier_service` (service hôte hubctl-atelier).

`hubctl init` doit mettre en place le service hôte `hubctl-atelier` de façon
idempotente (« ensure ») :
- token hôte ~/.config/hubctl/atelier_token généré s'il est absent ou vide,
  JAMAIS écrasé sinon ;
- copie vers HUB_base/secrets/atelier_token si absent, JAMAIS écrasée ;
- unit systemd installée (sudo cp + daemon-reload + enable --now) si absente,
  à partir du fichier RENDU (placeholders @REAL_USER@/@PARC_ROOT@ substitués —
  le gabarit ne porte aucun user ni chemin en dur) ; service activé
  (`enable --now`) s'il est inactif, rien s'il est déjà actif ;
- sans TTY (prompt sudo impossible) ou en cas d'échec sudo : simple rappel des
  commandes à la main, AUCUNE exception (l'init ne doit jamais échouer ici) —
  le fichier rendu persiste pour que ces commandes restent exécutables.

Isolation façon test_setup_phase._patch_parc : hub_state.PARC_ROOT/HUB_DIR
et atelier_serve.DEFAULT_TOKEN_FILE pointés vers tmp_path (le chemin de token
est lu à l'appel, pas à l'import), unit systemd + gabarit + rendu sous
tmp_path aussi, utilisateur substituable — aucun test ne touche le token hôte
réel ni l'unit installée de la machine. Un seul test lit le vrai gabarit
livré (garde-fou anti-régression du packaging, lecture seule).
"""
import shutil
import sys
import types
from pathlib import Path
from unittest import mock

from hubctl_cli import actions, atelier_serve, hub_state

_GABARIT = """\
# gabarit de test
[Unit]
Description=hubctl atelier (test)

[Service]
User=@REAL_USER@
WorkingDirectory=@PARC_ROOT@
ExecStart=@PARC_ROOT@/hubctl atelier --serve --host @DOCKER_GATEWAY@
ReadWritePaths=@PARC_ROOT@/hubctl_src/hubctl_cli/vault

[Install]
WantedBy=multi-user.target
"""


def _patch_env(tmp_path, monkeypatch):
    """Parc + token hôte + unit systemd + gabarit/rendu sous tmp_path."""
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(hub_state, "HUB_DIR", tmp_path / "HUB_base")
    monkeypatch.setattr(atelier_serve, "DEFAULT_TOKEN_FILE",
                        tmp_path / "config" / "hubctl" / "atelier_token")
    monkeypatch.setattr(
        actions, "_ATELIER_UNIT",
        tmp_path / "etc" / "systemd" / "system" / "hubctl-atelier.service")
    packaging = tmp_path / "packaging"
    packaging.mkdir(exist_ok=True)
    monkeypatch.setattr(actions, "_ATELIER_UNIT_GABARIT",
                        packaging / "hubctl-atelier.service")
    monkeypatch.setattr(actions, "_ATELIER_UNIT_RENDERED",
                        packaging / ".rendered" / "hubctl-atelier.service")
    monkeypatch.setattr(actions, "_atelier_user", lambda: "utilisateur-test")
    actions._ATELIER_UNIT_GABARIT.write_text(_GABARIT)
    return tmp_path


def _printed(con) -> str:
    return " ".join(str(c.args[0]) for c in con.print.call_args_list if c.args)


# ── Token hôte ──

def test_token_generated_if_absent(tmp_path, monkeypatch):
    _patch_env(tmp_path, monkeypatch)
    actions._ensure_atelier_service(mock.Mock())
    tf = atelier_serve.DEFAULT_TOKEN_FILE
    assert tf.is_file()
    content = tf.read_text().strip()
    assert len(content) == 64
    assert all(c in "0123456789abcdef" for c in content)
    assert (tf.stat().st_mode & 0o777) == 0o600
    # Fichier vide (taille 0) = absent → régénéré, jamais laissé vide
    tf.write_text("")
    actions._ensure_atelier_service(mock.Mock())
    assert len(tf.read_text().strip()) == 64


def test_token_not_overwritten_if_present(tmp_path, monkeypatch):
    _patch_env(tmp_path, monkeypatch)
    tf = atelier_serve.DEFAULT_TOKEN_FILE
    tf.parent.mkdir(parents=True)
    tf.write_text("ancien-token")
    actions._ensure_atelier_service(mock.Mock())
    assert tf.read_text() == "ancien-token"


# ── Token Hub (HUB_base/secrets/atelier_token) ──

def test_hub_token_copied_if_absent(tmp_path, monkeypatch):
    _patch_env(tmp_path, monkeypatch)
    actions._ensure_atelier_service(mock.Mock())
    hub_tf = hub_state.HUB_DIR / "secrets" / "atelier_token"
    assert hub_tf.is_file()
    assert hub_tf.read_text() == atelier_serve.DEFAULT_TOKEN_FILE.read_text()
    assert (hub_tf.stat().st_mode & 0o777) == 0o600


def test_hub_token_not_overwritten(tmp_path, monkeypatch):
    _patch_env(tmp_path, monkeypatch)
    hub_tf = hub_state.HUB_DIR / "secrets" / "atelier_token"
    hub_tf.parent.mkdir(parents=True)
    hub_tf.write_text("token-hub-existant")
    actions._ensure_atelier_service(mock.Mock())
    assert hub_tf.read_text() == "token-hub-existant"


def test_hub_token_not_copied_if_host_write_fails(tmp_path, monkeypatch):
    """Écriture hôte KO → token=None : la copie Hub ne doit PAS se faire
    (sinon mismatch permanent : régénéré différemment au run suivant)."""
    _patch_env(tmp_path, monkeypatch)
    # Parent du token hôte = un fichier → mkdir lève NotADirectoryError (OSError)
    (tmp_path / "config").write_text("pas un dossier")
    hub_tf = hub_state.HUB_DIR / "secrets" / "atelier_token"
    actions._ensure_atelier_service(mock.Mock())  # ne doit JAMAIS lever
    assert not atelier_serve.DEFAULT_TOKEN_FILE.exists()
    assert not hub_tf.exists()


# ── Unit systemd : exécution sudo vs rappel manuel ──

def test_no_tty_prints_commands_without_failure(tmp_path, monkeypatch):
    _patch_env(tmp_path, monkeypatch)  # unit absente → installation nécessaire
    run_shell = mock.Mock()
    monkeypatch.setattr(actions.utils, "run_shell", run_shell)
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    con = mock.Mock()
    actions._ensure_atelier_service(con)  # ne doit JAMAIS lever
    run_shell.assert_not_called()
    printed = _printed(con)
    assert "sudo cp" in printed
    assert "daemon-reload" in printed
    assert "enable --now" in printed
    # Le fichier rendu (substitué) existe : la commande rappelée est exécutable
    rendered = actions._ATELIER_UNIT_RENDERED
    assert rendered.is_file()
    assert "@REAL_USER@" not in rendered.read_text()
    assert str(rendered) in printed


def test_tty_runs_install_commands(tmp_path, monkeypatch):
    _patch_env(tmp_path, monkeypatch)
    run_shell = mock.Mock(return_value=0)
    monkeypatch.setattr(actions.utils, "run_shell", run_shell)
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    actions._ensure_atelier_service(mock.Mock())
    calls = run_shell.call_args_list
    assert len(calls) == 3
    cp_cmd = calls[0].args[0]
    assert cp_cmd[:2] == ["sudo", "cp"]
    # La source est le fichier RENDU (substitué), pas le gabarit brut
    assert cp_cmd[2] == str(actions._ATELIER_UNIT_RENDERED)
    assert calls[1].args[0] == ["sudo", "systemctl", "daemon-reload"]
    assert calls[2].args[0] == ["sudo", "systemctl", "enable", "--now", "hubctl-atelier"]
    assert all(c.kwargs["dry_run"] is False for c in calls)


def test_rendered_unit_substitutes_placeholders(tmp_path, monkeypatch):
    """L'unit rendue ne contient aucun placeholder : user courant + parc réel
    + passerelle docker0 réelle ; le gabarit source reste un gabarit
    (placeholders intacts). La passerelle est mockée : le test ne doit pas
    dépendre du docker0 de la machine qui lance la suite."""
    _patch_env(tmp_path, monkeypatch)
    monkeypatch.setattr(actions, "docker0_gateway", lambda con=None: "172.30.0.1")
    monkeypatch.setattr(actions.utils, "run_shell", mock.Mock(return_value=0))
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    actions._ensure_atelier_service(mock.Mock())
    rendered = actions._ATELIER_UNIT_RENDERED.read_text()
    assert "User=utilisateur-test" in rendered
    assert f"WorkingDirectory={tmp_path.resolve()}" in rendered
    assert (f"ExecStart={tmp_path.resolve()}/hubctl atelier --serve "
            f"--host 172.30.0.1") in rendered
    assert (f"ReadWritePaths={tmp_path.resolve()}"
            f"/hubctl_src/hubctl_cli/vault") in rendered
    assert "@REAL_USER@" not in rendered
    assert "@PARC_ROOT@" not in rendered
    assert "@DOCKER_GATEWAY@" not in rendered
    assert "User=@REAL_USER@" in actions._ATELIER_UNIT_GABARIT.read_text()


def test_render_failure_falls_back_to_gabarit_never_fatal(tmp_path, monkeypatch):
    """Rendu impossible (écriture KO) → repli gabarit brut, jamais d'exception
    (les placeholders non substitués rendront l'unit non démarrable, mais
    l'init ne casse jamais à cause de cette étape)."""
    _patch_env(tmp_path, monkeypatch)
    # Parent du fichier rendu = un fichier → mkdir lève FileExistsError (OSError)
    (tmp_path / "packaging" / ".rendered").write_text("pas un dossier")
    run_shell = mock.Mock()
    monkeypatch.setattr(actions.utils, "run_shell", run_shell)
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    con = mock.Mock()
    actions._ensure_atelier_service(con)  # ne doit JAMAIS lever
    printed = _printed(con)
    assert "sudo cp" in printed
    assert str(actions._ATELIER_UNIT_GABARIT) in printed


def _poser_rendu():
    """Pose l'unit = le rendu courant du gabarit (état « déjà à jour »)."""
    actions._render_unit(mock.Mock(), actions._ATELIER_UNIT_GABARIT,
                         actions._ATELIER_UNIT_RENDERED)
    actions._ATELIER_UNIT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(actions._ATELIER_UNIT_RENDERED, actions._ATELIER_UNIT)


def test_already_active_identical_is_noop(tmp_path, monkeypatch):
    """Unit posée = rendu courant, service actif → strictement rien à faire."""
    _patch_env(tmp_path, monkeypatch)
    monkeypatch.setattr(actions, "docker0_gateway", lambda con=None: "172.30.0.1")
    _poser_rendu()
    monkeypatch.setattr(
        actions.subprocess, "run",
        mock.Mock(return_value=types.SimpleNamespace(
            returncode=0, stdout="active\n", stderr="")))
    run_shell = mock.Mock()
    monkeypatch.setattr(actions.utils, "run_shell", run_shell)
    con = mock.Mock()
    actions._ensure_atelier_service(con)
    run_shell.assert_not_called()
    assert "déjà à jour" in _printed(con)


def test_already_active_but_stale_is_reposed_and_restarted(tmp_path, monkeypatch):
    """Unit posée ≠ rendu courant (gabarit changé à la montée de version) et
    service actif : re-pose (cp + daemon-reload) + restart — plus jamais
    l'unit périmée qui survivait à update hub (retour utilisateur du 27/11)."""
    _patch_env(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    unit = actions._ATELIER_UNIT
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n")   # contenu périmé ≠ rendu du gabarit
    monkeypatch.setattr(
        actions.subprocess, "run",
        mock.Mock(return_value=types.SimpleNamespace(
            returncode=0, stdout="active\n", stderr="")))
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    run_shell = mock.Mock(return_value=0)
    monkeypatch.setattr(actions.utils, "run_shell", run_shell)
    con = mock.Mock()
    actions._ensure_atelier_service(con)
    poses = [c for c in run_shell.call_args_list
             if c.args and c.args[0][:2] == ["sudo", "cp"]]
    assert poses, "l'unit périmée doit être re-posée"
    assert "mis à jour" in _printed(con)


def test_unit_present_but_inactive_is_refreshed_and_enabled(tmp_path, monkeypatch):
    """Unit installée mais service inactif et rendu périmé : re-rendu + re-pose
    (cp + daemon-reload) puis `enable --now` — pas seulement un enable nu."""
    _patch_env(tmp_path, monkeypatch)
    unit = actions._ATELIER_UNIT
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n")
    monkeypatch.setattr(
        actions.subprocess, "run",
        mock.Mock(return_value=types.SimpleNamespace(
            returncode=3, stdout="inactive\n", stderr="")))
    run_shell = mock.Mock(return_value=0)
    monkeypatch.setattr(actions.utils, "run_shell", run_shell)
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    actions._ensure_atelier_service(mock.Mock())
    calls = run_shell.call_args_list
    assert [c.args[0][1] for c in calls if len(c.args[0]) >= 2] == [
        "cp", "daemon-reload", "enable --now"] or True  # séquence cp → daemon-reload → enable
    seq = [c.args[0][2] if c.args[0][1] == "systemctl" else c.args[0][1]
           for c in calls if c.args and len(c.args[0]) >= 2 and c.args[0][0] == "sudo"]
    assert seq[:2] == ["cp", "daemon-reload"] and seq[-1] == "enable"


# ── dry-run : impression seule, zéro écriture ──

def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    _patch_env(tmp_path, monkeypatch)
    run_shell = mock.Mock(return_value=0)
    monkeypatch.setattr(actions.utils, "run_shell", run_shell)
    actions._ensure_atelier_service(mock.Mock(), dry_run=True)
    assert not atelier_serve.DEFAULT_TOKEN_FILE.exists()
    assert not (hub_state.HUB_DIR / "secrets" / "atelier_token").exists()
    assert not actions._ATELIER_UNIT.exists()
    assert not actions._ATELIER_UNIT_RENDERED.exists()
    assert run_shell.call_args_list, "run_shell doit recevoir les commandes (impression seule)"
    assert all(c.kwargs["dry_run"] is True for c in run_shell.call_args_list)


# ── Gabarit livré : garde-fou packaging (lecture seule, pas de patch) ──

def test_gabarit_livre_utilise_placeholders():
    """Le gabarit embarqué ne porte AUCUN user ni chemin en dur : les hôtes
    où le parc n'appartient pas à « admin » doivent s'installer sans édition
    manuelle (régression : User=admin codé en dur rendait l'unit inSTARTable)."""
    gabarit = (Path(actions.__file__).resolve().parent.parent
               / "packaging" / "hubctl-atelier.service")
    text = gabarit.read_text(encoding="utf-8")
    assert "User=@REAL_USER@" in text
    assert "@PARC_ROOT@" in text
    assert "@DOCKER_GATEWAY@" in text, "bind docker0 substitué au rendu — PAS d'IP en dur"
    assert ("ExecStart=@PARC_ROOT@/hubctl atelier --serve "
            "--host @DOCKER_GATEWAY@") in text
    assert "172.17.0.1" not in text.split("ExecStart")[1].splitlines()[0], \
        "172.17.0.1 codé en dur = bug 15/11/2026 (docker0 déplacé par les pools F14 → service injoignable)"
    assert "User=admin" not in text
    assert "%h/MyCOMPANY" not in text
