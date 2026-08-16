"""Tests pra_restore — assistant PRA (étapes unitaires, tout mocké)."""
import pytest
from pathlib import Path

from hubctl_cli import hub_state, pra_config, pra_restore


def _patch_parc(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(hub_state, "HUB_DIR", tmp_path / "HUB_base")
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path / "APPS")


def _kit(tmp_path):
    kit = tmp_path / "usb"
    kit.mkdir(exist_ok=True)
    (kit / pra_config.KIT_MARKER).write_text('{"kit_id": "K1"}')
    (kit / pra_config.VAULT_FILENAME).write_bytes(b"VAULT")
    (kit / "snapshot_abc123.tar.zst.enc").write_bytes(b"S1")
    (kit / "snapshot_def456.tar.zst.enc").write_bytes(b"S2-older")
    import os
    older = 1700000000
    os.utime(kit / "snapshot_def456.tar.zst.enc", (older, older))
    return kit


def test_pick_snapshot_arg_wins(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    snap = pra_restore.pick_snapshot(kit, "snapshot_def456.tar.zst.enc")
    assert snap.name == "snapshot_def456.tar.zst.enc"
    snap2 = pra_restore.pick_snapshot(kit, str(kit / "snapshot_def456.tar.zst.enc"))
    assert snap2.name == "snapshot_def456.tar.zst.enc"


def test_pick_snapshot_latest_by_mtime(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    assert pra_restore.pick_snapshot(kit, None).name == "snapshot_abc123.tar.zst.enc"


def test_pick_snapshot_none_available(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    empty = tmp_path / "emptykit"
    empty.mkdir()
    with pytest.raises(SystemExit):
        pra_restore.pick_snapshot(empty, None)


def test_find_kit_explicit_arg(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    assert pra_restore.find_kit(str(kit)) == kit


def test_find_kit_explicit_arg_without_vault(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    (kit / pra_config.VAULT_FILENAME).unlink()
    with pytest.raises(SystemExit):
        pra_restore.find_kit(str(kit))


def test_find_kit_discover(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    monkeypatch.setattr(pra_restore.pra_config, "load_config", lambda: None)
    monkeypatch.setattr(pra_restore.pra_config, "discover_kits", lambda: [kit])
    assert pra_restore.find_kit(None) == kit


def test_find_kit_none_found(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    monkeypatch.setattr(pra_restore.pra_config, "load_config", lambda: None)
    monkeypatch.setattr(pra_restore.pra_config, "discover_kits", lambda: [])
    with pytest.raises(SystemExit):
        pra_restore.find_kit(None)


def test_check_parc_empty_refuses_without_force(monkeypatch):
    from hubctl_cli import park_restore
    monkeypatch.setattr(park_restore, "_check_hub_is_empty", lambda: False)
    with pytest.raises(SystemExit):
        pra_restore.check_parc_empty(False)


def test_check_parc_empty_passes_empty(monkeypatch):
    from hubctl_cli import park_restore
    monkeypatch.setattr(park_restore, "_check_hub_is_empty", lambda: True)
    pra_restore.check_parc_empty(False)  # ne lève pas


def test_check_parc_empty_force_overrides(monkeypatch, capsys):
    from hubctl_cli import park_restore
    monkeypatch.setattr(park_restore, "_check_hub_is_empty", lambda: False)
    pra_restore.check_parc_empty(True)  # ne lève pas
    assert "force" in capsys.readouterr().out.lower()


def test_env_lookup_and_api_base_url(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    assert pra_restore._api_base_url() == "http://localhost:8000"
    hub_state.HUB_DIR.mkdir(parents=True)
    (hub_state.HUB_DIR / ".env").write_text("HUB_API_PORT=8010\nADMIN_USERNAME=boss\n")
    assert pra_restore._api_base_url() == "http://localhost:8010"
    assert pra_restore._env_lookup("ADMIN_USERNAME") == "boss"
    assert pra_restore._env_lookup("ABSENT") is None


def test_build_stack_missing_compose_exits(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        pra_restore.build_stack(None, dry_run=True)


def test_build_stack_dry_run_no_subprocess(tmp_path, monkeypatch):
    import subprocess as sp
    _patch_parc(tmp_path, monkeypatch)
    hub_state.HUB_DIR.mkdir(parents=True)
    (hub_state.HUB_DIR / "docker-compose.yml").write_text("x: 1\n")
    called = []
    monkeypatch.setattr(sp, "run", lambda *a, **kw: called.append(a) or None)
    pra_restore.build_stack(None, dry_run=True)
    assert called == []  # dry-run : zéro subprocess


def test_wait_healthy_polls_docker_inspect(monkeypatch):
    import subprocess as sp
    from hubctl_cli.pra_restore import wait_healthy
    results = iter([("1", "starting"), ("0", "healthy")])

    class FakeProc:
        def __init__(self, code, out):
            self.returncode = int(code)
            self.stdout = out

    monkeypatch.setattr(sp, "run", lambda *a, **kw: FakeProc(*next(results)))
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert wait_healthy(None, timeout_s=5) is True


def test_wait_healthy_timeout(monkeypatch):
    import subprocess as sp
    from hubctl_cli.pra_restore import wait_healthy

    class FakeProc:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(sp, "run", lambda *a, **kw: FakeProc())
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert wait_healthy(None, timeout_s=0) is False


# ── Task 6 : sélection, injection, HTTP, flux ──

SLUGS = ["app-a", "app-b", "app-c"]


def test_snapshot_id_from():
    assert pra_restore.snapshot_id_from("snapshot_abc.tar.zst.enc") == "abc"


def test_parse_selection_all_none_empty():
    assert pra_restore.parse_selection("all", SLUGS) == SLUGS
    assert pra_restore.parse_selection("none", SLUGS) == []
    assert pra_restore.parse_selection("", SLUGS) == []
    assert pra_restore.parse_selection(None, SLUGS) == []


def test_parse_selection_numbers():
    assert pra_restore.parse_selection("1,3", SLUGS) == ["app-a", "app-c"]


def test_parse_selection_slugs_and_dedupe():
    assert pra_restore.parse_selection("app-b,app-b, app-a", SLUGS) == ["app-b", "app-a"]


def test_parse_selection_invalid_ignored():
    assert pra_restore.parse_selection("9,xx,2", SLUGS) == ["app-b"]


def test_list_apps_with_backups_parses_psql(monkeypatch):
    import subprocess as sp

    class FakeProc:
        returncode = 0
        stdout = "app-a|bk-111\napp-b|\n\n"

    monkeypatch.setattr(sp, "run", lambda *a, **kw: FakeProc())
    apps = pra_restore.list_apps_with_backups()
    assert apps == [{"slug": "app-a", "backup_id": "bk-111"},
                    {"slug": "app-b", "backup_id": None}]


def test_admin_login_posts_form_and_returns_token(monkeypatch):
    import json as _json
    import urllib.request as ur

    captured = {}

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return _json.dumps({"access_token": "TOK", "refresh_token": "R"}).encode()

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data.decode()
        captured["ctype"] = req.headers.get("Content-type")
        return FakeResp()

    monkeypatch.setattr(ur, "urlopen", fake_urlopen)
    token = pra_restore.admin_login("http://localhost:8000", "admin", "pw")
    assert token == "TOK"
    assert captured["url"] == "http://localhost:8000/api/token"
    assert "username=admin" in captured["data"] and "grant_type=password" in captured["data"]
    assert "form" in (captured["ctype"] or "")


def test_restore_app_data_http_error_returns_false(monkeypatch):
    import urllib.error as ue
    import urllib.request as ur

    def fake_urlopen(req, timeout=None):
        raise ue.HTTPError(req.full_url, 500, "boom", {}, None)

    monkeypatch.setattr(ur, "urlopen", fake_urlopen)
    assert pra_restore.restore_app_data("http://x", "TOK", "app", "bk-1") is False


def test_inject_snapshot_dry_run_writes_nothing(tmp_path, monkeypatch):
    import subprocess as sp
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    snap = kit / "snapshot_abc123.tar.zst.enc"
    called = []
    monkeypatch.setattr(sp, "run", lambda *a, **kw: called.append(a) or None)
    sid = pra_restore.inject_snapshot(kit, snap, None, dry_run=True)
    assert sid == "abc123"
    assert called == []
    assert not (hub_state.HUB_DIR / "storage" / "snapshots").exists()


def test_inject_snapshot_copies_and_inserts(tmp_path, monkeypatch):
    import subprocess as sp
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    snap = kit / "snapshot_abc123.tar.zst.enc"
    commands = []

    class FakeProc:
        def __init__(self):
            self.returncode = 0
            self.stdout = ""  # SELECT 1 → vide = ligne absente

    monkeypatch.setattr(sp, "run", lambda *a, **kw: commands.append(a) or FakeProc())
    sid = pra_restore.inject_snapshot(kit, snap, None, dry_run=False)
    assert sid == "abc123"
    dest = hub_state.HUB_DIR / "storage" / "snapshots" / snap.name
    assert dest.exists() and dest.read_bytes() == b"S1"
    joined = [" ".join(str(x) for x in c) for c in commands]
    assert any("INSERT INTO hub_snapshots" in c and "abc123" in c for c in joined)
    assert any("pending_restore.txt" in c for c in joined)


def test_wait_restore_done_when_file_gone(monkeypatch):
    import subprocess as sp

    class FakeProc:
        returncode = 1  # test -f → absent

    monkeypatch.setattr(sp, "run", lambda *a, **kw: FakeProc())
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert pra_restore.wait_restore_done("abc", None, timeout_s=1) is True


def test_wait_restore_done_timeout(monkeypatch):
    import subprocess as sp

    class FakeProc:
        returncode = 0  # fichier toujours présent

    monkeypatch.setattr(sp, "run", lambda *a, **kw: FakeProc())
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert pra_restore.wait_restore_done("abc", None, timeout_s=0) is False


def test_run_pra_restore_dry_run_full_flow(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    steps = []

    monkeypatch.setattr(pra_restore, "check_parc_empty", lambda f: steps.append("check"))
    monkeypatch.setattr(pra_restore, "find_kit", lambda a: steps.append("kit") or kit)
    monkeypatch.setattr(pra_restore, "pick_snapshot",
                        lambda k, s: steps.append("snap") or k / "snapshot_abc123.tar.zst.enc")
    monkeypatch.setattr(pra_restore, "restore_files_from_kit",
                        lambda k, c, dry_run=False: steps.append("files"))
    monkeypatch.setattr(pra_restore, "build_stack",
                        lambda c, dry_run=False: steps.append("stack"))
    monkeypatch.setattr(pra_restore, "inject_snapshot",
                        lambda k, s, c, dry_run=False: steps.append("inject") or "abc123")
    monkeypatch.setattr(pra_restore, "restart_hub_api",
                        lambda c, dry_run=False: steps.append("restart"))
    code = pra_restore.run_pra_restore(str(kit), dry_run=True, yes=True, apps="none")
    assert code == 0
    assert steps == ["check", "kit", "snap", "files", "stack", "inject", "restart"]


def test_argparse_has_pra_restore():
    from hubctl_cli.__main__ import build_parser
    args = build_parser().parse_args(
        ["pra-restore", "/mnt/kit", "--snapshot", "s.tar.zst.enc",
         "--apps", "all", "--yes", "--force", "--dry-run"])
    assert args.command == "pra-restore"
    assert args.kit == "/mnt/kit" and args.snapshot == "s.tar.zst.enc"
    assert args.apps == "all" and args.yes and args.force and args.dry_run
