# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

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


# ── Étape 0 : preflight Docker ──

class _Proc:
    def __init__(self, code=0):
        self.returncode = code
        self.stdout = ""


def test_preflight_docker_ok(monkeypatch):
    import shutil
    import subprocess as sp
    monkeypatch.setattr(shutil, "which", lambda c: "/usr/bin/docker")
    monkeypatch.setattr(sp, "run", lambda *a, **kw: _Proc(0))
    pra_restore.preflight_docker()  # ne lève pas


def test_preflight_docker_missing_binary(monkeypatch):
    import shutil
    import subprocess as sp
    monkeypatch.setattr(shutil, "which", lambda c: None)
    with pytest.raises(SystemExit) as e:
        pra_restore.preflight_docker()
    assert "installé" in str(e.value)


def test_preflight_docker_daemon_down(monkeypatch):
    import shutil
    import subprocess as sp
    monkeypatch.setattr(shutil, "which", lambda c: "/usr/bin/docker")
    monkeypatch.setattr(sp, "run", lambda *a, **kw: _Proc(1))
    with pytest.raises(SystemExit) as e:
        pra_restore.preflight_docker()
    assert "daemon docker injoignable" in str(e.value).lower()


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


def _hub_with_compose(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    hub_state.HUB_DIR.mkdir(parents=True)
    (hub_state.HUB_DIR / "docker-compose.yml").write_text("x: 1\n")


def test_build_stack_build_failure_exits(tmp_path, monkeypatch):
    _hub_with_compose(tmp_path, monkeypatch)
    monkeypatch.setattr(pra_restore, "_run", lambda cmd, **kw: False)
    with pytest.raises(SystemExit) as e:
        pra_restore.build_stack(None, dry_run=False)
    assert "make build" in str(e.value)


def test_build_stack_up_prod_failure_exits(tmp_path, monkeypatch):
    _hub_with_compose(tmp_path, monkeypatch)
    # make build OK, make up-prod KO
    monkeypatch.setattr(pra_restore, "_run",
                        lambda cmd, **kw: cmd[1] == "build")
    with pytest.raises(SystemExit) as e:
        pra_restore.build_stack(None, dry_run=False)
    assert "up-prod" in str(e.value)


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
    # F7 (PRA VM run 9) : le hub expose /api/auth/token (prefix router auth),
    # pas /api/token → l'ancienne URL rendait le login impossible en PRA.
    assert captured["url"] == "http://localhost:8000/api/auth/token"
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
    state = {"selects": 0}

    class FakeProc:
        def __init__(self, code=0, out="", err=""):
            self.returncode, self.stdout, self.stderr = code, out, err

    def fake_run(cmd, *a, **kw):
        joined = " ".join(str(x) for x in cmd)
        commands.append(joined)
        if "SELECT 1 FROM hub_snapshots" in joined:
            state["selects"] += 1
            # 1er SELECT : absent ; SELECT de vérification : présent
            return FakeProc(out="1" if state["selects"] >= 2 else "")
        return FakeProc()  # INSERT ok, echo pending

    monkeypatch.setattr(sp, "run", fake_run)
    sid = pra_restore.inject_snapshot(kit, snap, None, dry_run=False)
    assert sid == "abc123"
    dest = hub_state.HUB_DIR / "storage" / "snapshots" / snap.name
    assert dest.exists() and dest.read_bytes() == b"S1"
    assert any("INSERT INTO hub_snapshots" in c and "abc123" in c for c in commands)
    assert any("pending_restore.txt" in c for c in commands)


# ── BUG A : ligne hub_snapshots vérifiée AVANT pending_restore ──


def test_inject_snapshot_insert_then_verify_before_pending(tmp_path, monkeypatch):
    """Ligne absente → INSERT → re-SELECT de vérification → PUIS seulement
    pending_restore (séquence stricte, cf. incident VM 28/10/2026)."""
    import subprocess as sp
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    snap = kit / "snapshot_abc123.tar.zst.enc"
    commands = []
    state = {"selects": 0}

    class FakeProc:
        def __init__(self, code=0, out="", err=""):
            self.returncode, self.stdout, self.stderr = code, out, err

    def fake_run(cmd, *a, **kw):
        joined = " ".join(str(x) for x in cmd)
        commands.append(joined)
        if "SELECT 1 FROM hub_snapshots" in joined:
            state["selects"] += 1
            return FakeProc(out="1" if state["selects"] >= 2 else "")
        return FakeProc()

    monkeypatch.setattr(sp, "run", fake_run)
    sid = pra_restore.inject_snapshot(kit, snap, None, dry_run=False)
    assert sid == "abc123"
    assert len(commands) == 4  # SELECT → INSERT → SELECT vérif → echo pending
    assert "SELECT 1 FROM hub_snapshots" in commands[0]
    assert "INSERT INTO hub_snapshots" in commands[1]
    assert "SELECT 1 FROM hub_snapshots" in commands[2]
    pending_idx = next(i for i, c in enumerate(commands) if "pending_restore.txt" in c)
    assert pending_idx == 3  # pending écrit APRÈS la vérification


def test_inject_snapshot_insert_failure_exits_before_pending(tmp_path, monkeypatch, capsys):
    """INSERT en échec → SystemExit AVANT toute écriture de pending_restore,
    stderr SQL affiché."""
    import subprocess as sp
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    snap = kit / "snapshot_abc123.tar.zst.enc"
    commands = []

    class FakeProc:
        def __init__(self, code=0, out="", err=""):
            self.returncode, self.stdout, self.stderr = code, out, err

    def fake_run(cmd, *a, **kw):
        joined = " ".join(str(x) for x in cmd)
        commands.append(joined)
        if "SELECT 1 FROM hub_snapshots" in joined:
            return FakeProc()  # ligne absente
        if "INSERT INTO hub_snapshots" in joined:
            return FakeProc(code=1, err='ERROR: relation "hub_snapshots" does not exist')
        return FakeProc()

    monkeypatch.setattr(sp, "run", fake_run)
    with pytest.raises(SystemExit) as e:
        pra_restore.inject_snapshot(kit, snap, None, dry_run=False)
    assert "hub_snapshots" in str(e.value)
    assert "pending_restore" in str(e.value)
    assert all("pending_restore.txt" not in c for c in commands)  # JAMAIS écrit
    assert "does not exist" in capsys.readouterr().err


def test_inject_snapshot_unverifiable_row_exits_before_pending(tmp_path, monkeypatch):
    """INSERT « ok » mais la ligne reste absente au re-SELECT → SystemExit,
    pending jamais écrit (l'état serait irreversible côté hub)."""
    import subprocess as sp
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    snap = kit / "snapshot_abc123.tar.zst.enc"
    commands = []

    class FakeProc:
        returncode = 0
        stdout = ""  # SELECT toujours vide : la ligne n'apparaît jamais
        stderr = ""

    monkeypatch.setattr(sp, "run", lambda *a, **kw: commands.append(a) or FakeProc())
    with pytest.raises(SystemExit) as e:
        pra_restore.inject_snapshot(kit, snap, None, dry_run=False)
    assert "hub_snapshots" in str(e.value)
    assert all("pending_restore.txt" not in " ".join(str(x) for x in c) for c in commands)


def test_inject_snapshot_already_present_skips_insert(tmp_path, monkeypatch):
    """Ligne déjà présente (SELECT tuples-only → stdout « 1 ») → aucun INSERT,
    pending écrit."""
    import subprocess as sp
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    snap = kit / "snapshot_abc123.tar.zst.enc"
    commands = []

    class FakeProc:
        returncode = 0
        stdout = "1"  # SELECT tuples-only : « 1 » = ligne présente
        stderr = ""

    monkeypatch.setattr(sp, "run", lambda *a, **kw: commands.append(a) or FakeProc())
    sid = pra_restore.inject_snapshot(kit, snap, None, dry_run=False)
    assert sid == "abc123"
    joined = [" ".join(str(x) for x in c) for c in commands]
    assert not any("INSERT INTO hub_snapshots" in c for c in joined)
    assert any("pending_restore.txt" in c for c in joined)


_PSQL_HEADER_0ROWS = " ?column? \n----------\n(0 rows)\n\n"


def test_inject_snapshot_header_zero_rows_takes_insert_path(tmp_path, monkeypatch):
    """LE test qui aurait attrapé le bug VM 29/10/2026 : psql SANS tuples-only
    renvoie l'en-tête « ?column? / (0 rows) » même sur table VIDE — le check
    doit comparer le CONTENU (« 1 » tuples-only) et prendre le chemin INSERT,
    pas croire à une « ligne déjà présente » sur un stdout non vide."""
    import subprocess as sp
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    snap = kit / "snapshot_abc123.tar.zst.enc"
    commands = []
    state = {"selects": 0}

    class FakeProc:
        def __init__(self, code=0, out="", err=""):
            self.returncode, self.stdout, self.stderr = code, out, err

    def fake_run(cmd, *a, **kw):
        joined = " ".join(str(x) for x in cmd)
        commands.append(joined)
        if "SELECT 1 FROM hub_snapshots" in joined:
            state["selects"] += 1
            if state["selects"] == 1:
                return FakeProc(out=_PSQL_HEADER_0ROWS)  # check : table vide
            return FakeProc(out="1")  # vérification : ligne bien créée
        return FakeProc()  # INSERT ok, echo pending

    monkeypatch.setattr(sp, "run", fake_run)
    sid = pra_restore.inject_snapshot(kit, snap, None, dry_run=False)
    assert sid == "abc123"
    joined = commands
    select_cmds = [c for c in joined if "SELECT 1 FROM hub_snapshots" in c]
    # le check passe bien par tuples-only (mécanisme qui rend stdout vide à 0 ligne)
    assert select_cmds and all("-t -A" in c for c in select_cmds)
    assert any("INSERT INTO hub_snapshots" in c for c in joined)  # chemin INSERT
    assert any("pending_restore.txt" in c for c in joined)


def test_inject_snapshot_verification_header_zero_rows_exits(tmp_path, monkeypatch):
    """Vérification post-INSERT : en-tête « (0 rows) » = ligne TOUJOURS
    absente → SystemExit. Avant le fix, l'en-tête rendait le stdout non vide
    et la protection ne pouvait JAMAIS se déclencher."""
    import subprocess as sp
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    snap = kit / "snapshot_abc123.tar.zst.enc"
    commands = []

    class FakeProc:
        returncode = 0
        stdout = _PSQL_HEADER_0ROWS  # tous les SELECT : en-tête + (0 rows)
        stderr = ""

    monkeypatch.setattr(sp, "run", lambda *a, **kw: commands.append(a) or FakeProc())
    with pytest.raises(SystemExit) as e:
        pra_restore.inject_snapshot(kit, snap, None, dry_run=False)
    assert "hub_snapshots" in str(e.value)
    assert all("pending_restore.txt" not in " ".join(str(x) for x in c)
               for c in commands)


def test_wait_restore_done_when_file_gone(monkeypatch):
    import subprocess as sp

    class FakeProc:
        returncode = 1  # test -f → absent
        stdout = ""
        stderr = ""

    monkeypatch.setattr(sp, "run", lambda *a, **kw: FakeProc())
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert pra_restore.wait_restore_done("abc", None, timeout_s=1) \
        is pra_restore.RestoreWaitStatus.DONE


def test_wait_restore_done_timeout(monkeypatch):
    import subprocess as sp

    class FakeProc:
        returncode = 0  # fichier toujours présent
        stdout = ""
        stderr = ""

    monkeypatch.setattr(sp, "run", lambda *a, **kw: FakeProc())
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert pra_restore.wait_restore_done("abc", None, timeout_s=0) \
        is pra_restore.RestoreWaitStatus.TIMEOUT


# ── BUG C : détection de l'échec de restauration dans les logs hub-api ──


def _fake_run_recording(logs_output: str, *, pending_code: int):
    """Fake sp.run : docker logs → logs_output ; test -f pending → pending_code."""
    calls = []

    class FakeProc:
        def __init__(self, code=0, out=""):
            self.returncode, self.stdout, self.stderr = code, out, ""

    def fake_run(cmd, *a, **kw):
        calls.append([str(x) for x in cmd])
        if cmd[:2] == ["docker", "logs"]:
            return FakeProc(out=logs_output)
        return FakeProc(code=pending_code)

    return fake_run, calls


def test_wait_restore_done_failure_marker_returns_failed(monkeypatch):
    """Pending parti + marqueur « Restauration ECHEC » dans les logs → FAILED."""
    import subprocess as sp
    fake_run, calls = _fake_run_recording(
        "… Restauration ECHEC — snapshot abc introuvable …", pending_code=1)
    monkeypatch.setattr(sp, "run", fake_run)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert pra_restore.wait_restore_done("abc", None, timeout_s=5) \
        is pra_restore.RestoreWaitStatus.FAILED
    # le scan part de --since <début du wait> et vise hub-api
    assert any(c[:3] == ["docker", "logs", "--since"] and c[-1] == "hub-api"
               for c in calls)


def test_wait_restore_done_failure_marker_case_insensitive(monkeypatch):
    import subprocess as sp
    fake_run, _ = _fake_run_recording(
        "hub-api | RESTAURATION ECHEC — données incohérentes", pending_code=1)
    monkeypatch.setattr(sp, "run", fake_run)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert pra_restore.wait_restore_done("abc", None, timeout_s=5) \
        is pra_restore.RestoreWaitStatus.FAILED


def test_wait_restore_done_pending_gone_no_marker_done(monkeypatch):
    """Faux-négatif écarté : pending parti, aucun marqueur → DONE."""
    import subprocess as sp
    fake_run, _ = _fake_run_recording("2026-10-28 Restore termine ✓", pending_code=1)
    monkeypatch.setattr(sp, "run", fake_run)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert pra_restore.wait_restore_done("abc", None, timeout_s=5) \
        is pra_restore.RestoreWaitStatus.DONE


def test_wait_restore_done_failure_while_pending_still_there(monkeypatch):
    """Marqueur détecté PENDANT l'attente (pending encore présent) → FAILED."""
    import subprocess as sp
    fake_run, _ = _fake_run_recording(
        "Restauration ECHEC — pg_restore exit 3", pending_code=0)
    monkeypatch.setattr(sp, "run", fake_run)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert pra_restore.wait_restore_done("abc", None, timeout_s=5) \
        is pra_restore.RestoreWaitStatus.FAILED


def _mock_pra_flow(tmp_path, monkeypatch, kit):
    """Mocke le flux run_pra_restore jusqu'à (hors) l'attente du restore."""
    monkeypatch.setattr(pra_restore, "preflight_docker", lambda: None)
    monkeypatch.setattr(pra_restore, "check_parc_empty", lambda f: None)
    monkeypatch.setattr(pra_restore, "find_kit", lambda a: kit)
    monkeypatch.setattr(pra_restore, "pick_snapshot",
                        lambda k, s: k / "snapshot_abc123.tar.zst.enc")
    monkeypatch.setattr(pra_restore, "restore_files_from_kit",
                        lambda k, c, dry_run=False: None)
    monkeypatch.setattr(pra_restore, "build_stack", lambda c, dry_run=False: None)
    monkeypatch.setattr(pra_restore, "wait_healthy", lambda c, timeout_s=300: True)
    monkeypatch.setattr(pra_restore, "inject_snapshot",
                        lambda k, s, c, dry_run=False: "abc123")
    monkeypatch.setattr(pra_restore, "restart_hub_api", lambda c, dry_run=False: None)
    monkeypatch.setattr(pra_restore, "_run", lambda cmd, **kw: True)


def test_run_pra_restore_wait_failure_exits_before_apps(tmp_path, monkeypatch):
    """wait_restore_done FAILED → SystemExit (échec) AVANT l'étape apps ;
    « PRA terminé. » réservé au succès."""
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    _mock_pra_flow(tmp_path, monkeypatch, kit)
    monkeypatch.setattr(pra_restore, "wait_restore_done",
                        lambda sid, c, timeout_s=180, since=None:
                        pra_restore.RestoreWaitStatus.FAILED)

    def _must_not_run():
        raise AssertionError("étape apps ne doit pas être atteinte après un échec")
    monkeypatch.setattr(pra_restore, "list_apps_with_backups", _must_not_run)

    with pytest.raises(SystemExit) as e:
        pra_restore.run_pra_restore(str(kit), yes=True)
    assert "échec" in str(e.value).lower()
    assert "hub-api" in str(e.value).lower()


def test_run_pra_restore_wait_timeout_message_distinct(tmp_path, monkeypatch):
    """TIMEOUT d'attente → message « non terminé », distinct du message d'échec."""
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    _mock_pra_flow(tmp_path, monkeypatch, kit)
    monkeypatch.setattr(pra_restore, "wait_restore_done",
                        lambda sid, c, timeout_s=180, since=None:
                        pra_restore.RestoreWaitStatus.TIMEOUT)

    def _must_not_run():
        raise AssertionError("étape apps ne doit pas être atteinte après un timeout")
    monkeypatch.setattr(pra_restore, "list_apps_with_backups", _must_not_run)

    with pytest.raises(SystemExit) as e:
        pra_restore.run_pra_restore(str(kit), yes=True)
    assert "non terminé" in str(e.value).lower()
    assert "échec" not in str(e.value).lower()


def test_run_pra_restore_slow_failure_detected_after_healthy(tmp_path, monkeypatch):
    """Échec lent : le hub supprime pending_restore.txt AVANT run_restore
    (HUB_base/hub/app/restore_snapshot.py) → l'attente voit DONE pendant que
    le pg_restore tourne encore, et le marqueur d'échec n'apparaît dans les
    logs qu'APRÈS wait_healthy. Le re-scan post-healthy doit SystemExit avec
    un message distinct AVANT « ✓ Restore terminé » / « PRA terminé. »."""
    import subprocess as sp
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    state = {"phase": "wait"}

    class FakeProc:
        def __init__(self, code=0, out="", err=""):
            self.returncode, self.stdout, self.stderr = code, out, err

    def fake_run(cmd, *a, **kw):
        if cmd[:2] == ["docker", "logs"]:
            if state["phase"] == "post-healthy":
                return FakeProc(out="… Restauration ECHEC — pg_restore exit 3 …")
            return FakeProc(out="")  # pendant l'attente : aucun marqueur
        return FakeProc(code=1)  # test -f : pending déjà parti (début du restore)

    monkeypatch.setattr(sp, "run", fake_run)
    monkeypatch.setattr("time.sleep", lambda s: None)
    _mock_pra_flow(tmp_path, monkeypatch, kit)
    # wait_healthy est appelé 2× (post-build étape 3, post-restore étape 5) :
    # la bascule de phase simule le pg_restore lent qui échoue PENDANT le
    # second healthy — les logs ne gagnent le marqueur qu'après.
    healthy_calls = {"n": 0}

    def _healthy(console, timeout_s=300):
        healthy_calls["n"] += 1
        if healthy_calls["n"] >= 2:
            state["phase"] = "post-healthy"
        return True
    monkeypatch.setattr(pra_restore, "wait_healthy", _healthy)

    def _must_not_run():
        raise AssertionError("succès déclaré malgré le marqueur d'échec post-healthy")
    monkeypatch.setattr(pra_restore, "count_apps", _must_not_run)

    with pytest.raises(SystemExit) as e:
        pra_restore.run_pra_restore(str(kit), yes=True)
    assert "échec" in str(e.value).lower()
    assert "hub-api" in str(e.value).lower()
    assert "après" in str(e.value).lower()  # message distinct du cas immédiat


def test_run_pra_restore_dry_run_full_flow(tmp_path, monkeypatch):
    _patch_parc(tmp_path, monkeypatch)
    kit = _kit(tmp_path)
    steps = []

    monkeypatch.setattr(pra_restore, "preflight_docker",
                        lambda: steps.append("preflight"))
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
    assert steps == ["preflight", "check", "kit", "snap", "files",
                     "stack", "inject", "restart"]


def test_run_pra_restore_preflight_before_parc_check(tmp_path, monkeypatch):
    # Docker absent/daemon down → échec immédiat, même en dry-run, avant tout.
    _patch_parc(tmp_path, monkeypatch)
    called = []

    def _boom():
        called.append("preflight")
        raise SystemExit("❌ Docker n'est pas installé")

    monkeypatch.setattr(pra_restore, "preflight_docker", _boom)
    monkeypatch.setattr(pra_restore, "check_parc_empty",
                        lambda f: called.append("check"))
    with pytest.raises(SystemExit):
        pra_restore.run_pra_restore(dry_run=True, yes=True)
    assert called == ["preflight"]


def test_argparse_has_pra_restore():
    from hubctl_cli.__main__ import build_parser
    args = build_parser().parse_args(
        ["pra-restore", "/mnt/kit", "--snapshot", "s.tar.zst.enc",
         "--apps", "all", "--yes", "--force", "--dry-run"])
    assert args.command == "pra-restore"
    assert args.kit == "/mnt/kit" and args.snapshot == "s.tar.zst.enc"
    assert args.apps == "all" and args.yes and args.force and args.dry_run


# ── BUG B : stdin fermé au prompt apps (pipeline) + voie scriptable documentée ──


def test_select_apps_interactive_eof_warns_and_selects_nothing(tmp_path, monkeypatch):
    """stdin avalé par le pipeline → EOF au prompt : avertissement explicite
    (voie scriptable) + sélection vide — jamais de « none » silencieux."""
    import io
    from rich.console import Console
    buf = io.StringIO()
    console = Console(file=buf)
    apps = [{"slug": "app-a", "backup_id": "bk1"},
            {"slug": "app-b", "backup_id": None}]

    def _eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    selection = pra_restore._select_apps_interactive(console, apps, None)
    assert selection == []
    out = buf.getvalue()
    assert "stdin fermé" in out
    assert "--apps all" in out
    assert "ADMIN_PASSWORD" in out


def test_select_apps_interactive_apps_arg_skips_prompt(tmp_path, monkeypatch):
    """--apps fourni → aucun prompt (voie scriptable primordiale en pipeline)."""
    import io
    from rich.console import Console

    def _no_prompt(prompt=""):
        raise AssertionError("prompt appelé alors que --apps est fourni")
    monkeypatch.setattr("builtins.input", _no_prompt)
    apps = [{"slug": "app-a", "backup_id": None},
            {"slug": "app-b", "backup_id": None}]
    assert pra_restore._select_apps_interactive(Console(file=io.StringIO()),
                                                apps, "all") == ["app-a", "app-b"]
    assert pra_restore._select_apps_interactive(Console(file=io.StringIO()),
                                                apps, "none") == []


def test_pra_restore_docstring_documents_scriptable_path():
    """La docstring d'en-tête documente la restauration scriptable (pipeline)."""
    doc = pra_restore.__doc__ or ""
    assert "Restauration scriptable" in doc
    assert "--apps all" in doc
    assert "ADMIN_PASSWORD" in doc
    assert "--force" in doc


# ── restore_files_from_kit : clé du kit d'abord ──


def _kit_with_real_vault(tmp_path, monkeypatch, *, with_key: bool):
    _patch_parc(tmp_path, monkeypatch)
    from hubctl_cli.coffre import Vault
    kit = tmp_path / "usb"
    kit.mkdir(exist_ok=True)
    (tmp_path / "HUB_base").mkdir(exist_ok=True)
    v = Vault(kit / pra_config.VAULT_FILENAME)
    v.unlock("kit-passphrase")
    v.set_entry("hub", {"POSTGRES_PASSWORD": "restored"})
    v.save()
    if with_key:
        v.store_passphrase("kit-passphrase")
    return kit


def test_restore_files_uses_kit_key_without_prompt(tmp_path, monkeypatch):
    """Clé du kit présente et valide → aucun prompt getpass."""
    kit = _kit_with_real_vault(tmp_path, monkeypatch, with_key=True)

    def _no_prompt(*a, **k):
        raise AssertionError("getpass ne doit pas être appelé (clé du kit valide)")
    monkeypatch.setattr("getpass.getpass", _no_prompt)

    from rich.console import Console
    pra_restore.restore_files_from_kit(kit, Console())
    assert (tmp_path / "HUB_base" / ".env").exists()


def test_restore_files_kit_without_key_prompts(tmp_path, monkeypatch):
    """Kit sans clé (kit ancien) → repli sur la saisie manuelle."""
    kit = _kit_with_real_vault(tmp_path, monkeypatch, with_key=False)
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "kit-passphrase")

    from rich.console import Console
    pra_restore.restore_files_from_kit(kit, Console())
    assert (tmp_path / "HUB_base" / ".env").exists()


def test_restore_files_from_kit_restores_ca_with_modes(tmp_path, monkeypatch):
    """Le chemin PRA (coffre DU KIT) doit lui aussi remettre la CA avec ses
    permissions (ca.crt 0644, ca.key 0600, dossier 0700) : même coffre, même
    chemin commun Vault.restore() — la confiance navigateurs survit au PRA."""
    from rich.console import Console
    from hubctl_cli.coffre import Vault
    _patch_parc(tmp_path, monkeypatch)
    kit = tmp_path / "usb"
    kit.mkdir(exist_ok=True)
    (tmp_path / "HUB_base").mkdir(exist_ok=True)
    v = Vault(kit / pra_config.VAULT_FILENAME)
    v.unlock("kit-passphrase")
    v.set_file("HUB_base/certs/ca/ca.crt", b"CERT")
    v.set_file("HUB_base/certs/ca/ca.key", b"KEY")
    v.save()
    v.store_passphrase("kit-passphrase")

    def _no_prompt(*a, **k):
        raise AssertionError("getpass ne doit pas être appelé (clé du kit valide)")
    monkeypatch.setattr("getpass.getpass", _no_prompt)

    pra_restore.restore_files_from_kit(kit, Console())

    ca_dir = tmp_path / "HUB_base" / "certs" / "ca"
    assert (ca_dir / "ca.crt").read_bytes() == b"CERT"
    assert (ca_dir / "ca.key").read_bytes() == b"KEY"
    assert (ca_dir / "ca.crt").stat().st_mode & 0o777 == 0o644
    assert (ca_dir / "ca.key").stat().st_mode & 0o777 == 0o600
    assert ca_dir.stat().st_mode & 0o777 == 0o700


def test_admin_login_poste_sur_api_auth_token():
    """F7 (PRA VM run 9, 13/11/2026) : admin_login appelait /api/token → 404.
    Le hub expose POST /api/auth/token (prefix router auth = /api/auth)."""
    import http.server
    import json as _json
    import threading

    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            if self.path == "/api/auth/token":
                body = _json.dumps({"access_token": "tok"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):  # silence
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        token = pra_restore.admin_login(f"http://127.0.0.1:{port}", "admin", "pw")
        assert token == "tok"
        assert hits == ["/api/auth/token"]
    finally:
        server.shutdown()


def test_list_apps_with_backups_ne_retient_que_les_success(monkeypatch):
    """F9 (PRA VM run 11, 13/11/2026) : le dernier backup d'une app peut être
    en status 'failed' (aucun fichier ZIP sur le serveur) → réinjection 404.
    La sélection ne doit retenir que les backups 'success'."""
    captured = {}

    class FakeResult:
        stdout = "app-a|6dfd9d90\napp-b|"

    def fake_psql(sql, **kwargs):
        captured["sql"] = sql
        return FakeResult()

    monkeypatch.setattr(pra_restore, "_psql", fake_psql)
    apps = pra_restore.list_apps_with_backups()
    assert "status = 'success'" in captured["sql"].lower()
    assert apps == [{"slug": "app-a", "backup_id": "6dfd9d90"},
                    {"slug": "app-b", "backup_id": None}]


def test_wait_healthy_boucle_et_expire(monkeypatch):
    """F10 (PRA VM run 12) : après un restore multi-minutes, hub-api n'est
    healthy que longtemps après le restart — wait_healthy doit poller
    jusqu'au timeout (et pas rendre la main trop tôt)."""
    state = {"checks": 0, "t": 0.0}
    monkeypatch.setattr(pra_restore.time, "monotonic", lambda: state["t"])

    def fake_sleep(s):
        state["t"] += s

    monkeypatch.setattr(pra_restore.time, "sleep", fake_sleep)

    class FakeCompleted:
        def __init__(self, status):
            self.returncode = 0
            self.stdout = status

    def fake_run(cmd, **k):
        state["checks"] += 1
        return FakeCompleted("healthy" if state["checks"] >= 3 else "starting")

    monkeypatch.setattr(pra_restore.subprocess, "run", fake_run)
    assert pra_restore.wait_healthy(None, timeout_s=10) is True
    state["checks"] = 0
    state["t"] = 0.0
    assert pra_restore.wait_healthy(None, timeout_s=3) is False


def test_timeouts_couverts_pour_gros_restores():
    """F10 : après un restore de snapshot multi-Go sur petite machine,
    le boot hub-api + le healthcheck dépassent largement 2-5 minutes."""
    assert pra_restore.HEALTH_TIMEOUT_S >= 600
    assert pra_restore.RESTORE_TIMEOUT_S >= 600


def test_rebuild_app_resout_le_dossier_par_app_slug(monkeypatch, tmp_path):
    """F12 (PRA VM run 13) : le slug DB ne correspond pas toujours au nom du
    dossier APPS (Exemple_App vs exemple-app). Le dossier réel est
    résolu via APP_SLUG dans le .env de chaque app ; fallback sur APPS/<slug>."""
    apps = tmp_path / "APPS"
    camel = apps / "Exemple_App"
    camel.mkdir(parents=True)
    (camel / "Makefile").write_text("build: up\nup:\n\techo ok\n")
    (camel / ".env").write_text("APP_SLUG=exemple-app\n")
    (apps / "mon-app").mkdir(parents=True)
    (apps / "mon-app" / "Makefile").write_text("build: up\nup:\n\techo ok\n")
    (apps / "mon-app" / ".env").write_text("APP_SLUG=mon-app\n")
    monkeypatch.setattr(hub_state, "APPS_DIR", apps)

    assert pra_restore.rebuild_app("exemple-app", None) is True
    assert pra_restore.rebuild_app("mon-app", None) is True


def test_rebuild_app_sans_dossier_echoue_proprement(monkeypatch, tmp_path):
    apps = tmp_path / "APPS"
    apps.mkdir(parents=True)
    monkeypatch.setattr(hub_state, "APPS_DIR", apps)
    assert pra_restore.rebuild_app("inconnu", None) is False
