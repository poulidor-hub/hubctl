"""Tests de l'étape init `_ensure_atelier_service` (service hôte hubctl-atelier).

`hubctl init` doit mettre en place le service hôte `hubctl-atelier` de façon
idempotente (« ensure ») :
- token hôte ~/.config/hubctl/atelier_token généré s'il est absent ou vide,
  JAMAIS écrasé sinon ;
- copie vers HUB_base/secrets/atelier_token si absent, JAMAIS écrasée ;
- unit systemd installée (sudo cp + daemon-reload + enable --now) si absente,
  service activé (`enable --now`) s'il est inactif, rien s'il est déjà actif ;
- sans TTY (prompt sudo impossible) ou en cas d'échec sudo : simple rappel des
  commandes à la main, AUCUNE exception (l'init ne doit jamais échouer ici).

Isolation façon test_setup_phase._patch_parc : hub_state.PARC_ROOT/HUB_DIR
et atelier_serve.DEFAULT_TOKEN_FILE pointés vers tmp_path (le chemin de token
est lu à l'appel, pas à l'import), unit systemd sous tmp_path aussi — aucun
test ne touche le token hôte réel ni l'unit installée de la machine.
"""
import sys
import types
from unittest import mock

from hubctl_cli import actions, atelier_serve, hub_state


def _patch_env(tmp_path, monkeypatch):
    """Parc + token hôte + unit systemd entièrement sous tmp_path."""
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(hub_state, "HUB_DIR", tmp_path / "HUB_base")
    monkeypatch.setattr(atelier_serve, "DEFAULT_TOKEN_FILE",
                        tmp_path / "config" / "hubctl" / "atelier_token")
    monkeypatch.setattr(
        actions, "_ATELIER_UNIT",
        tmp_path / "etc" / "systemd" / "system" / "hubctl-atelier.service")
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
    assert cp_cmd[2].endswith("packaging/hubctl-atelier.service")
    assert calls[1].args[0] == ["sudo", "systemctl", "daemon-reload"]
    assert calls[2].args[0] == ["sudo", "systemctl", "enable", "--now", "hubctl-atelier"]
    assert all(c.kwargs["dry_run"] is False for c in calls)


def test_already_active_is_noop(tmp_path, monkeypatch):
    _patch_env(tmp_path, monkeypatch)
    unit = actions._ATELIER_UNIT
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n")
    monkeypatch.setattr(
        actions.subprocess, "run",
        mock.Mock(return_value=types.SimpleNamespace(
            returncode=0, stdout="active\n", stderr="")))
    run_shell = mock.Mock()
    monkeypatch.setattr(actions.utils, "run_shell", run_shell)
    con = mock.Mock()
    actions._ensure_atelier_service(con)
    run_shell.assert_not_called()
    assert "✓" in _printed(con)
    assert "hubctl-atelier" in _printed(con)


def test_unit_present_but_inactive_runs_enable_only(tmp_path, monkeypatch):
    """Unit installée mais service inactif → seul `enable --now` relancé
    (pas de cp / daemon-reload)."""
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
    assert len(calls) == 1
    assert calls[0].args[0] == ["sudo", "systemctl", "enable", "--now", "hubctl-atelier"]


# ── dry-run : impression seule, zéro écriture ──

def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    _patch_env(tmp_path, monkeypatch)
    run_shell = mock.Mock(return_value=0)
    monkeypatch.setattr(actions.utils, "run_shell", run_shell)
    actions._ensure_atelier_service(mock.Mock(), dry_run=True)
    assert not atelier_serve.DEFAULT_TOKEN_FILE.exists()
    assert not (hub_state.HUB_DIR / "secrets" / "atelier_token").exists()
    assert not actions._ATELIER_UNIT.exists()
    assert run_shell.call_args_list, "run_shell doit recevoir les commandes (impression seule)"
    assert all(c.kwargs["dry_run"] is True for c in run_shell.call_args_list)
