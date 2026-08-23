from unittest import mock

import pytest

from hubctl_cli.__main__ import build_parser
from hubctl_cli import __main__ as m


def test_parser_flags():
    p = build_parser()
    args = p.parse_args(["--no-anim", "--dry-run"])
    assert args.no_anim is True
    assert args.global_dry_run is True


def test_parser_defaults():
    p = build_parser()
    args = p.parse_args([])
    assert args.no_anim is False
    assert args.global_dry_run is False


class TestAtelierFlags:
    def test_maj(self):
        a = build_parser().parse_args(["atelier", "X", "--maj"])
        assert a.atelier_maj is True and a.atelier_prod is False and a.ref is None

    def test_maj_ref(self):
        a = build_parser().parse_args(["atelier", "X", "--maj", "--ref", "feat/x"])
        assert a.atelier_maj is True and a.ref == "feat/x"

    def test_prod(self):
        a = build_parser().parse_args(["atelier", "X", "--prod"])
        assert a.atelier_prod is True and a.atelier_maj is False

    def test_maj_prod_exclusifs(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["atelier", "X", "--maj", "--prod"])

    def test_prod_global_pas_ecrase(self):
        a = build_parser().parse_args(["--prod", "atelier", "X"])
        assert a.prod is True and a.atelier_prod is False


def test_non_interactive_exits_cleanly_without_menu(monkeypatch):
    monkeypatch.setattr(m.hub_state, "check_docker", lambda: False)
    monkeypatch.setattr(m.hub_state, "get_hub_services", lambda: [])
    with mock.patch.object(m, "_interactive", return_value=False), \
         mock.patch.object(m.animation, "preload_with_bar"), \
         mock.patch.object(m.menu, "main_loop") as ml:
        rc = m.main([])
    assert rc == 0
    ml.assert_not_called()


def test_keyboard_interrupt_during_intro_goes_to_menu():
    fake_canvas = mock.Mock()
    fake_canvas.is_too_small = False
    fake_canvas.inner_cols = 100
    fake_canvas.inner_rows = 30
    with mock.patch.object(m, "_interactive", return_value=True), \
         mock.patch.object(m, "Canvas", return_value=fake_canvas), \
         mock.patch.object(m.animation, "preload_with_bar", side_effect=KeyboardInterrupt), \
         mock.patch.object(m, "Vault"), \
         mock.patch.object(m, "VaultManager"), \
         mock.patch.object(m, "_resolve_mode", return_value="prod"), \
         mock.patch.object(m.menu, "_require_vault"), \
         mock.patch.object(m.menu, "main_loop") as ml:
        rc = m.main([])
    assert rc == 0
    ml.assert_called_once()


def test_layout_installed_before_choose_mode():
    fake_canvas = mock.Mock()
    fake_canvas.is_too_small = False
    order = []
    with mock.patch.object(m, "_interactive", return_value=True), \
         mock.patch.object(m, "Canvas", return_value=fake_canvas), \
         mock.patch.object(m.animation, "preload_with_bar", return_value={}), \
         mock.patch.object(m, "Vault"), \
         mock.patch.object(m, "VaultManager"), \
         mock.patch.object(m.menu, "install_layout",
                           side_effect=lambda *a, **k: order.append("layout")), \
          mock.patch.object(m, "_resolve_mode",
                            side_effect=lambda *a, **k: order.append("mode") or "prod"), \
          mock.patch.object(m.menu, "_require_vault"), \
          mock.patch.object(m.menu, "main_loop"):
        rc = m.main(["--no-anim"])
    assert rc == 0
    assert order.index("layout") < order.index("mode")


# ── _require_vault : déverrouillage / création du coffre ──


def test_require_vault_autounlock_with_stored_key(monkeypatch):
    """Clé locale valide → déverrouillage silencieux, aucune saisie."""
    from hubctl_cli import menu
    canvas = mock.Mock()
    vault = mock.Mock()
    vault.path.exists.return_value = True
    vault.unlock_stored.return_value = True
    vault_mgr = mock.Mock(vault=vault)
    vault_mgr.backup_all.return_value = {"env": 1, "files": 1}
    prompted = []
    monkeypatch.setattr(menu.widgets, "password",
                        lambda *a, **k: prompted.append(1) or "pw")
    monkeypatch.setattr(menu, "pause", lambda *a, **kw: None)
    monkeypatch.setattr(menu, "_offer_pra_setup", lambda c: None)
    menu._require_vault(canvas, vault_mgr)
    assert prompted == []  # aucune saisie
    vault_mgr.backup_all.assert_called_once()


def test_require_vault_existing_prompts_and_backs_up(monkeypatch):
    """Pas de clé → saisie manuelle, puis migration (écriture de la clé)."""
    from hubctl_cli import menu
    canvas = mock.Mock()
    vault = mock.Mock()
    vault.path.exists.return_value = True
    vault.unlock_stored.return_value = False
    vault.unlock.return_value = True
    vault_mgr = mock.Mock(vault=vault)
    vault_mgr.backup_all.return_value = {"env": 1, "files": 1}
    monkeypatch.setattr(menu.widgets, "password", lambda *a, **k: "pw")
    monkeypatch.setattr(menu, "pause", lambda c: None)
    monkeypatch.setattr(menu, "_offer_pra_setup", lambda c: None)
    menu._require_vault(canvas, vault_mgr)
    vault.unlock.assert_called_once_with("pw")
    vault.store_passphrase.assert_called_once_with("pw")  # migration
    vault_mgr.backup_all.assert_called_once()


def test_require_vault_create_autogenerates_without_prompt(monkeypatch):
    """Création : passphrase générée + stockée, zéro interaction."""
    from hubctl_cli import menu
    canvas = mock.Mock()
    vault = mock.Mock()
    vault.path.exists.return_value = False
    vault_mgr = mock.Mock(vault=vault)
    vault_mgr.backup_all.return_value = {"env": 0, "files": 0}
    prompted = []
    monkeypatch.setattr(menu.widgets, "password",
                        lambda *a, **k: prompted.append(1) or "x")
    monkeypatch.setattr(menu, "pause", lambda c: None)
    monkeypatch.setattr(menu, "_offer_pra_setup", lambda c: None)
    menu._require_vault(canvas, vault_mgr)
    assert prompted == []
    pw = vault.unlock.call_args[0][0]
    assert len(pw) >= 32  # token_urlsafe(32) ≈ 43 caractères
    vault.store_passphrase.assert_called_once_with(pw)
    vault_mgr.backup_all.assert_called_once()


# ── Parser: new simplified subcommands ──

def test_parser_start():
    p = build_parser()
    args = p.parse_args(["start"])
    assert args.command == "start"


def test_parser_stop():
    p = build_parser()
    args = p.parse_args(["stop"])
    assert args.command == "stop"


def test_parser_update():
    p = build_parser()
    args = p.parse_args(["update"])
    assert args.command == "update"


def test_parser_atelier():
    p = build_parser()
    args = p.parse_args(["atelier", "shop"])
    assert args.command == "atelier"
    assert args.app == "shop"


def test_parser_export_flags():
    p = build_parser()
    a = p.parse_args(["export"])
    assert a.command == "export"
    assert a.app is None
    assert a.no_encrypt is False
    b = p.parse_args(["export", "--app", "shop", "--no-encrypt", "--passphrase-stdin"])
    assert b.app == "shop"
    assert b.no_encrypt is True
    assert b.passphrase_stdin is True


def test_parser_restore():
    p = build_parser()
    a = p.parse_args(["restore", "archive.tar.gz"])
    assert a.command == "restore"
    assert a.archive == "archive.tar.gz"
    assert a.passphrase_stdin is False


def test_parser_vpn_rejects_unknown_subcmd(capsys):
    import pytest as _pytest
    p = build_parser()
    with _pytest.raises(SystemExit):
        p.parse_args(["vpn", "bogus"])


def test_parser_mode_flags():
    p = build_parser()
    assert p.parse_args(["--dev", "start"]).dev is True
    assert p.parse_args(["--prod", "start"]).prod is True
    args = p.parse_args(["start"])
    assert args.dev is False
    assert args.prod is False


def test_parser_dev_prod_mutually_exclusive():
    import pytest as _pytest
    p = build_parser()
    with _pytest.raises(SystemExit):
        p.parse_args(["--dev", "--prod", "start"])


def test_resolve_mode_precedence(monkeypatch):
    p = build_parser()
    monkeypatch.setattr(m.hub_state, "auto_detect_mode", lambda: "prod")
    monkeypatch.delenv("HUBCTL_MODE", raising=False)
    assert m._resolve_mode(p.parse_args(["--dev", "start"])) == "dev"
    assert m._resolve_mode(p.parse_args(["--prod", "start"])) == "prod"
    monkeypatch.setenv("HUBCTL_MODE", "dev")
    assert m._resolve_mode(p.parse_args(["start"])) == "dev"
    monkeypatch.delenv("HUBCTL_MODE", raising=False)
    monkeypatch.setattr(m.hub_state, "auto_detect_mode", lambda: "dev")
    assert m._resolve_mode(p.parse_args(["start"])) == "dev"


# ── Dispatch tests ──

def test_dispatch_start_calls_action_start_all(monkeypatch):
    monkeypatch.setattr(m, "_resolve_mode", lambda args: "prod")
    with mock.patch.object(m, "action_start_all") as fn:
        rc = m.main(["start"])
    assert rc == 0
    fn.assert_called_once()


def test_dispatch_stop_calls_action_stop_all(monkeypatch):
    monkeypatch.setattr(m, "_resolve_mode", lambda args: "prod")
    with mock.patch.object(m, "action_stop_all") as fn:
        rc = m.main(["stop"])
    assert rc == 0
    fn.assert_called_once()


def test_dispatch_update_calls_action_update_all(monkeypatch):
    monkeypatch.setattr(m, "_resolve_mode", lambda args: "prod")
    with mock.patch.object(m, "action_update_all") as fn:
        rc = m.main(["update"])
    assert rc == 0
    fn.assert_called_once()


def test_dispatch_atelier_calls_action_atelier(monkeypatch):
    monkeypatch.setattr(m, "_resolve_mode", lambda args: "dev")
    with mock.patch.object(m, "action_atelier") as fn:
        rc = m.main(["atelier", "shop"])
    assert rc == 0
    fn.assert_called_once()
    assert fn.call_args.kwargs["app_slug"] == "shop"


def test_dispatch_returns_1_on_error(monkeypatch):
    monkeypatch.setattr(m, "_resolve_mode", lambda args: "prod")
    with mock.patch.object(m, "action_start_all", side_effect=RuntimeError("boom")):
        rc = m.main(["start"])
    assert rc == 1


def test_dry_run_passed_to_actions(monkeypatch):
    monkeypatch.setattr(m, "_resolve_mode", lambda args: "prod")
    with mock.patch.object(m, "action_start_all") as fn:
        rc = m.main(["--dry-run", "start"])
    assert rc == 0
    fn.assert_called_once()
    assert fn.call_args.kwargs["dry_run"] is True




# ── Init subcommand tests ──

def test_parser_parses_init_subcommand():
    parser = build_parser()
    args = parser.parse_args(["init"])
    assert args.command == "init"
    assert args.force == False


def test_parser_parses_init_with_force():
    parser = build_parser()
    args = parser.parse_args(["init", "--force"])
    assert args.command == "init"
    assert args.force == True


def test_init_dispatches_to_action_bootstrap(monkeypatch):
    from hubctl_cli.__main__ import _run
    called = []
    def fake_bootstrap(console, force=False, dry_run=False):
        called.append(("bootstrap", force, dry_run))
        return 0
    monkeypatch.setattr("hubctl_cli.__main__.action_bootstrap", fake_bootstrap)
    args = build_parser().parse_args(["init"])
    result = _run(args)
    assert result == 0
    assert len(called) == 1
    assert called[0][1] == False


def test_init_dispatch_with_force_flag(monkeypatch):
    from hubctl_cli.__main__ import _run
    called = []
    def fake_bootstrap(console, force=False, dry_run=False):
        called.append(("bootstrap", force, dry_run))
        return 0
    monkeypatch.setattr("hubctl_cli.__main__.action_bootstrap", fake_bootstrap)
    args = build_parser().parse_args(["init", "--force"])
    _run(args)
    assert called[0][1] == True


# ── Securite/safety tests ──





def test_bootstrap_does_not_leak_password(monkeypatch, tmp_path):
    import io
    from hubctl_cli import actions, hub_state
    from rich.console import Console

    hub = tmp_path / "HUB_base"
    hub.mkdir(parents=True)
    (hub / "docker-compose.yml").write_text("services:\n  api:\n    image: test\n")
    (hub / "Makefile").write_text("up-prod:\n\techo up\n")
    monkeypatch.setattr(hub_state, "HUB_DIR", hub)
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path / "APPS")
    monkeypatch.setattr(actions.hub_state, "active_apps_dir", lambda: tmp_path / "APPS")
    monkeypatch.setattr(actions, "hub_make", lambda *a, **kw: 0)
    monkeypatch.setattr(actions, "_health_check", lambda *a, **kw: True)
    monkeypatch.setattr(actions, "_sync_db_password_if_needed", lambda *a, **kw: None)
    # Ne pas écrire dans le vrai ~/.bashrc du user qui lance les tests
    monkeypatch.setattr(actions, "_install_shell_marker", lambda *a, **kw: None)
    # Ne PAS détruire les conteneurs hub-* réels (force-remove par nom dans
    # _cleanup_existing_hub_containers n'est pas isolée du HUB_DIR mocké).
    monkeypatch.setattr(actions, "_cleanup_existing_hub_containers", lambda *a, **kw: None)

    out = io.StringIO()
    console = Console(file=out, width=120)
    rc = actions.action_bootstrap(console, force=True, dry_run=False)
    output = out.getvalue()
    assert rc == 0
    import re
    pwd_pattern = re.compile(r'[A-Za-z0-9!@#$%^&*()+=]{8,}')
    for line in output.splitlines():
        if 'Mot de passe' in line or 'password' in line.lower():
            match = pwd_pattern.search(line)
            if match and len(match.group()) >= 12:
                raise AssertionError(f"potential password leak in bootstrap output: {line}")


def test_restore_command_catches_exception(monkeypatch):
    """restore doit attraper les exceptions prévisibles (archive corrompue, magic
    absent, docker manquant) et retourner 1 avec un message, pas cracher un
    traceback non contrôlé (contrairement à l'ancien code sans try/except)."""

    def _raise(*a, **k):
        raise ValueError("Archive sans magic PARKEV1")
    monkeypatch.setattr(m, "restore_park", _raise)

    args = build_parser().parse_args(["restore", "/tmp/inexistent.tar.gz"])
    rc = m._run(args)
    assert rc == 1
