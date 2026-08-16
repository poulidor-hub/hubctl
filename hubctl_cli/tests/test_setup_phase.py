"""Tests phase « emplacement PRA » au setup + intégration menu."""
from unittest import mock
from pathlib import Path

from hubctl_cli import hub_state, menu, pra_config


def _patch_parc(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(hub_state, "HUB_DIR", tmp_path / "HUB_base")
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path / "APPS")


def _fake_canvas():
    c = mock.Mock()
    c.inner_rows = 28
    c.inner_cols = 98

    def _stream_command(runner, label=""):
        return (runner(), False)

    c.stream_command = _stream_command
    return c


# ── should_offer_setup (rappel — logique de récurrence de la phase) ──

def test_setup_phase_recurs_when_no_config(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    assert pra_config.should_offer_setup() is True
    # « Plus tard » ne persiste RIEN → la phase se représente
    assert pra_config.should_offer_setup() is True


def test_setup_phase_absent_when_configured_or_declined(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    pra_config.save_config({"path": "/media/usb", "kit_id": "K"})
    assert pra_config.should_offer_setup() is False
    pra_config.save_config({"declined": True})
    assert pra_config.should_offer_setup() is False


# ── _offer_pra_setup gating ──

def test_offer_pra_setup_skips_when_declined(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    pra_config.save_config({"declined": True})
    c = _fake_canvas()
    menu._offer_pra_setup(c)
    c.render.assert_not_called()  # phase zéro interaction


def test_offer_pra_setup_skips_when_configured(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    pra_config.save_config({"path": "/media/usb", "kit_id": "K"})
    c = _fake_canvas()
    menu._offer_pra_setup(c)
    c.render.assert_not_called()


def test_offer_pra_setup_declines_definitively(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    c = _fake_canvas()
    monkeypatch.setattr(menu.pra_config, "scan_candidate_locations", lambda: [])
    monkeypatch.setattr(menu.widgets, "select",
                        lambda canvas, prompt, labels, disabled=None: "Ne plus proposer")
    monkeypatch.setattr(menu, "pause", lambda *a, **kw: None)
    menu._offer_pra_setup(c)
    assert pra_config.load_config() == {"declined": True}


def test_offer_pra_setup_manual_path_configures(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    c = _fake_canvas()
    dest = tmp_path / "nas"
    dest.mkdir()
    monkeypatch.setattr(menu.pra_config, "scan_candidate_locations", lambda: [])
    monkeypatch.setattr(menu.pra_config, "validate_location", lambda p: ("ok", ""))
    configured = []
    monkeypatch.setattr(menu.pra_replicate, "configure_location",
                        lambda p, label="": configured.append(p) or (True, "Kit PRA initialisé"))
    sel_calls = iter(["Saisir un autre chemin…"])
    monkeypatch.setattr(menu.widgets, "select",
                        lambda canvas, prompt, labels, disabled=None: next(sel_calls))
    monkeypatch.setattr(menu.widgets, "text", lambda canvas, prompt: str(dest))
    monkeypatch.setattr(menu, "pause", lambda *a, **kw: None)
    menu._offer_pra_setup(c)
    assert configured == [dest]


# ── Branchement dans _require_vault ──

def test_require_vault_offers_pra_setup_after_backup(tmp_path, monkeypatch):
    c = _fake_canvas()
    vault = mock.Mock()
    vault_file = tmp_path / "parc.vault.enc"
    vault_file.touch()
    vault.path = vault_file
    vault.is_locked = True
    vault.unlock.return_value = True
    mgr = mock.Mock()
    mgr.vault = vault
    mgr.backup_all.return_value = {"env": 1, "files": 1, "pra": None}
    monkeypatch.setattr(menu.widgets, "password", lambda canvas, prompt: "pw")
    monkeypatch.setattr(menu, "pause", lambda *a, **kw: None)
    offered = []
    monkeypatch.setattr(menu, "_offer_pra_setup", lambda canvas: offered.append(True))
    menu._require_vault(c, mgr)
    assert offered == [True]


# ── Badge dashboard ──

def _snap_with(monkeypatch, **overrides):
    base = {"docker_ok": True, "services": [], "apps_count": (0, 0),
            "pra_badge": None}
    base.update(overrides)
    monkeypatch.setattr(menu, "_snap", base)


def test_build_welcome_shows_pra_badge(monkeypatch):
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "prod")
    monkeypatch.setattr(menu.hub_state, "get_hub_version", lambda: "vX")
    _snap_with(monkeypatch, pra_badge="⚠ PRA : emplacement secondaire introuvable")
    assert "PRA" in menu._build_welcome().plain


def test_build_welcome_no_badge_when_fresh(monkeypatch):
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "prod")
    monkeypatch.setattr(menu.hub_state, "get_hub_version", lambda: "vX")
    _snap_with(monkeypatch, pra_badge=None)
    assert "PRA" not in menu._build_welcome().plain


def test_build_welcome_tolerates_missing_pra_badge_key(monkeypatch):
    # Cache _snap sans la clé (ex: seed_state_snapshot initial) → rien d'affiché.
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "prod")
    monkeypatch.setattr(menu.hub_state, "get_hub_version", lambda: "vX")
    monkeypatch.setattr(menu, "_snap",
                        {"docker_ok": None, "services": None, "apps_count": None})
    assert "PRA" not in menu._build_welcome().plain


def test_build_welcome_never_calls_staleness_badge(monkeypatch):
    # Le badge est une E/S disque/réseau : JAMAIS sur le thread de rendu.
    monkeypatch.setattr(menu.hub_state, "get_mode", lambda: "prod")
    monkeypatch.setattr(menu.hub_state, "get_hub_version", lambda: "vX")
    _snap_with(monkeypatch, pra_badge="⚠ PRA : jamais synchronisé")
    called = []
    monkeypatch.setattr("hubctl_cli.pra_config.staleness_badge",
                        lambda: called.append(True))
    text = menu._build_welcome().plain
    assert called == []
    assert "PRA" in text  # vient bien du cache, pas de l'appel direct


def test_refresh_state_snapshot_populates_pra_badge(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    monkeypatch.setattr(menu.hub_state, "check_docker", lambda: True)
    monkeypatch.setattr(menu.hub_state, "get_hub_services", lambda: [])
    monkeypatch.setattr(menu.hub_state, "get_active_apps", lambda: [])
    monkeypatch.setattr("hubctl_cli.pra_config.staleness_badge",
                        lambda: "⚠ PRA : dernière synchro il y a 12 j")
    menu._refresh_state_snapshot()
    assert menu._snap["pra_badge"] == "⚠ PRA : dernière synchro il y a 12 j"


def test_refresh_state_snapshot_badge_error_tolerated(tmp_path, monkeypatch):
    # Un montage NFS mort pendant le calcul du badge ne doit rien casser :
    # pra_badge → None, le reste du snapshot reste peuplé.
    _patch_parc(tmp_path, monkeypatch)
    monkeypatch.setattr(menu.hub_state, "check_docker", lambda: True)
    monkeypatch.setattr(menu.hub_state, "get_hub_services", lambda: [])
    monkeypatch.setattr(menu.hub_state, "get_active_apps", lambda: [])

    def boom():
        raise OSError("NFS mort")

    monkeypatch.setattr("hubctl_cli.pra_config.staleness_badge", boom)
    menu._refresh_state_snapshot()
    assert menu._snap["pra_badge"] is None
    assert menu._snap["docker_ok"] is True
    assert menu._snap["apps_count"] == (0, 0)


# ── view_coffre : action Emplacement PRA ──

def test_view_coffre_has_pra_action(monkeypatch):
    c = _fake_canvas()
    vault = mock.Mock()
    vault.is_locked = False
    vault.list_entries.return_value = []
    mgr = mock.Mock()
    mgr.vault = vault
    mgr.check_divergence.return_value = {}
    menu_called = []
    monkeypatch.setattr(menu, "_pra_location_menu",
                        lambda canvas: menu_called.append(True))
    sel_seq = iter(["🔄 Emplacement PRA", "← Retour"])

    def fake_select(canvas, prompt, labels, disabled=None):
        assert "🔄 Emplacement PRA" in labels
        return next(sel_seq)

    monkeypatch.setattr(menu.widgets, "select", fake_select)
    monkeypatch.setattr(menu, "pause", lambda *a, **kw: None)
    menu.view_coffre(c, mgr)
    assert menu_called == [True]
