from unittest import mock
from hubctl_cli.__main__ import build_parser
from hubctl_cli import __main__ as m


def test_parser_flags():
    p = build_parser()
    args = p.parse_args(["--no-anim", "--dry-run", "--anim", "fly-to-flock"])
    assert args.no_anim is True
    assert args.dry_run is True
    assert args.anim == "fly-to-flock"


def test_parser_defaults():
    p = build_parser()
    args = p.parse_args([])
    assert args.no_anim is False
    assert args.dry_run is False
    assert args.anim is None
    assert args.init is False


def test_parser_init_flag():
    p = build_parser()
    args = p.parse_args(["--init"])
    assert args.init is True


def test_init_calls_action_init_and_exits():
    fake_canvas = mock.Mock()
    fake_canvas.is_too_small = False
    with mock.patch.object(m, "_interactive", return_value=False), \
         mock.patch.object(m.actions, "action_init") as ai, \
         mock.patch.object(m, "Canvas", return_value=fake_canvas):
        rc = m.main(["--init"])
    assert rc == 0
    ai.assert_called_once()


def test_non_interactive_exits_cleanly_without_menu():
    with mock.patch.object(m, "_interactive", return_value=False), \
         mock.patch.object(m.animation, "preload_with_bar",
                           return_value={"docker": False, "services": []}), \
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
         mock.patch.object(m.animation, "play_intro", side_effect=KeyboardInterrupt), \
         mock.patch.object(m, "Vault"), \
         mock.patch.object(m, "VaultManager"), \
         mock.patch.object(m, "_resolve_mode", return_value="prod"), \
         mock.patch.object(m.menu, "_require_vault"), \
         mock.patch.object(m.menu, "_offer_snapshot"), \
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
         mock.patch.object(m.animation, "play_intro"), \
         mock.patch.object(m.animation, "preload_with_bar", return_value={}), \
         mock.patch.object(m, "Vault"), \
         mock.patch.object(m, "VaultManager"), \
         mock.patch.object(m.menu, "install_layout",
                           side_effect=lambda *a, **k: order.append("layout")), \
          mock.patch.object(m, "_resolve_mode",
                            side_effect=lambda *a, **k: order.append("mode") or "prod"), \
          mock.patch.object(m.menu, "_require_vault"), \
          mock.patch.object(m.menu, "_offer_snapshot"), \
          mock.patch.object(m.menu, "main_loop"):
        rc = m.main(["--no-anim"])
    assert rc == 0
    assert order.index("layout") < order.index("mode")


def test_require_vault_existing_prompts_and_backs_up(monkeypatch):
    from unittest import mock
    from hubctl_cli import menu
    canvas = mock.Mock()
    vault = mock.Mock()
    vault.path.exists.return_value = True
    vault.unlock.return_value = True
    vault_mgr = mock.Mock(vault=vault)
    vault_mgr.backup_all.return_value = {"env": 1, "files": 1}
    monkeypatch.setattr(menu.widgets, "password", lambda *a, **k: "pw")
    monkeypatch.setattr(menu, "pause", lambda c: None)
    menu._require_vault(canvas, vault_mgr)
    vault.unlock.assert_called_once_with("pw")
    vault_mgr.backup_all.assert_called_once()


def test_require_vault_creates_when_absent(monkeypatch):
    from unittest import mock
    from hubctl_cli import menu
    canvas = mock.Mock()
    vault = mock.Mock()
    vault.path.exists.return_value = False
    vault_mgr = mock.Mock(vault=vault)
    vault_mgr.backup_all.return_value = {"env": 0, "files": 0}
    monkeypatch.setattr(menu.widgets, "password", lambda *a, **k: "newpw")
    monkeypatch.setattr(menu, "pause", lambda c: None)
    menu._require_vault(canvas, vault_mgr)
    vault.unlock.assert_called_once_with("newpw")
    vault_mgr.backup_all.assert_called_once()


def test_parser_subcommands_slug_and_target():
    p = build_parser()
    assert p.parse_args(["work", "shop"]).slug == "shop"
    assert p.parse_args(["ship", "shop"]).slug == "shop"
    assert p.parse_args(["update", "hub"]).target == "hub"
    assert p.parse_args(["start", "hub"]).target == "hub"
    assert p.parse_args(["stop", "shop"]).target == "shop"
    assert p.parse_args(["status"]).command == "status"


def test_parser_mode_flags():
    p = build_parser()
    assert p.parse_args(["--dev", "status"]).dev is True
    assert p.parse_args(["--prod", "status"]).prod is True
    args = p.parse_args(["status"])
    assert args.dev is False
    assert args.prod is False


def test_resolve_mode_precedence(monkeypatch):
    p = build_parser()
    monkeypatch.setattr(m.hub_state, "auto_detect_mode", lambda: "prod")
    monkeypatch.delenv("HUBCTL_MODE", raising=False)
    assert m._resolve_mode(p.parse_args(["--dev", "status"])) == "dev"
    assert m._resolve_mode(p.parse_args(["--prod", "status"])) == "prod"
    monkeypatch.setenv("HUBCTL_MODE", "dev")
    assert m._resolve_mode(p.parse_args(["status"])) == "dev"
    monkeypatch.delenv("HUBCTL_MODE", raising=False)
    monkeypatch.setattr(m.hub_state, "auto_detect_mode", lambda: "dev")
    assert m._resolve_mode(p.parse_args(["status"])) == "dev"


def test_dispatch_status_calls_action_status(monkeypatch):
    monkeypatch.setattr(m, "_resolve_mode", lambda args: "prod")
    with mock.patch.object(m, "action_status") as st:
        rc = m.main(["status"])
    assert rc == 0
    st.assert_called_once()


def test_dispatch_work_calls_action_work(monkeypatch):
    monkeypatch.setattr(m, "_resolve_mode", lambda args: "dev")
    with mock.patch.object(m, "action_work") as w:
        rc = m.main(["work", "shop"])
    assert rc == 0
    w.assert_called_once()
    assert w.call_args.args[1] == "shop"


def test_dispatch_returns_1_on_error(monkeypatch):
    monkeypatch.setattr(m, "_resolve_mode", lambda args: "prod")
    with mock.patch.object(m, "action_ship", side_effect=RuntimeError("boom")):
        rc = m.main(["ship", "shop"])
    assert rc == 1


# ── Task 1: logs + restart parser tests ──

def test_parser_logs_mutually_exclusive():
    from hubctl_cli.__main__ import build_parser
    p = build_parser()
    a = p.parse_args(["logs", "hub", "--api"])
    assert a.target == "hub" and a.api is True and a.db is False
    try:
        p.parse_args(["logs", "hub", "--api", "--db"])
        assert False, "devrait échouer"
    except SystemExit:
        pass


def test_parser_restart_optional_service():
    from hubctl_cli.__main__ import build_parser
    p = build_parser()
    assert p.parse_args(["restart", "hub"]).service is None
    assert p.parse_args(["restart", "hub", "api"]).service == "api"


# ── Task 2: health + db-shell parser tests ──

def test_parser_health_detailed():
    from hubctl_cli.__main__ import build_parser
    p = build_parser()
    a = p.parse_args(["health", "hub", "--detailed"])
    assert a.detailed is True


def test_parser_db_shell_app():
    from hubctl_cli.__main__ import build_parser
    p = build_parser()
    assert p.parse_args(["db-shell", "shop"]).app == "shop"
