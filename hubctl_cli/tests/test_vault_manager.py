# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

from hubctl_cli import hub_state
from hubctl_cli.coffre import Vault
from hubctl_cli.vault_manager import VaultManager, restore_all


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(hub_state, "HUB_DIR", tmp_path / "HUB_base")
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path / "APPS")
    # recovery_dir hermétique : isolé du paquet réel
    monkeypatch.setattr(
        hub_state, "recovery_dir",
        lambda *a, **k: tmp_path / "hubctl_cli" / "recovery")
    (tmp_path / "HUB_base" / "secrets").mkdir(parents=True)
    (tmp_path / "HUB_base" / ".env").write_text("POSTGRES_PASSWORD=sec\n")
    (tmp_path / "HUB_base" / "secrets" / "jwt_private_key.pem").write_bytes(b"KEYDATA")
    v = Vault(tmp_path / "vault" / "v.enc")
    v.unlock("pw")
    return VaultManager(v)


def test_backup_all_captures_env_and_files(tmp_path, monkeypatch):
    mgr = _setup(tmp_path, monkeypatch)
    summary = mgr.backup_all()
    assert mgr.vault.get_entry("hub") == {"POSTGRES_PASSWORD": "sec"}
    assert mgr.vault.get_file("HUB_base/secrets/jwt_private_key.pem") == b"KEYDATA"
    assert summary["env"] >= 1 and summary["files"] >= 1


def test_backup_all_does_not_modify_source(tmp_path, monkeypatch):
    mgr = _setup(tmp_path, monkeypatch)
    env_path = tmp_path / "HUB_base" / ".env"
    key_path = tmp_path / "HUB_base" / "secrets" / "jwt_private_key.pem"
    before = (env_path.read_text(), key_path.read_bytes())
    mgr.backup_all()
    assert (env_path.read_text(), key_path.read_bytes()) == before


def test_export_recovery_writes_tree_only_under_recovery(tmp_path, monkeypatch):
    mgr = _setup(tmp_path, monkeypatch)
    mgr.backup_all()
    (tmp_path / "HUB_base" / ".env").unlink()
    dest = mgr.export_recovery()
    assert dest.parent == tmp_path / "hubctl_cli" / "recovery"
    assert (dest / "HUB_base" / ".env").read_text().strip() == "POSTGRES_PASSWORD=sec"
    assert (dest / "HUB_base" / "secrets" / "jwt_private_key.pem").read_bytes() == b"KEYDATA"
    assert not (tmp_path / "HUB_base" / ".env").exists()


def test_backup_all_captures_service_accounts_subtree(tmp_path, monkeypatch):
    """Le coffre doit capturer récursément HUB_base/secrets/, y compris
    service_accounts/ (credentials SA par app)."""
    mgr = _setup(tmp_path, monkeypatch)
    sa = tmp_path / "HUB_base" / "secrets" / "service_accounts" / "sa_demo.env"
    sa.parent.mkdir(parents=True)
    sa.write_text("SA_TOKEN=xyz\n")
    summary = mgr.backup_all()
    assert mgr.vault.get_file("HUB_base/secrets/service_accounts/sa_demo.env") == b"SA_TOKEN=xyz\n"
    assert summary["files"] >= 2


def test_export_recovery_reproduces_service_accounts_subtree(tmp_path, monkeypatch):
    mgr = _setup(tmp_path, monkeypatch)
    sa = tmp_path / "HUB_base" / "secrets" / "service_accounts" / "sa_demo.env"
    sa.parent.mkdir(parents=True)
    sa.write_text("SA_TOKEN=xyz\n")
    mgr.backup_all()
    dest = mgr.export_recovery()
    assert (dest / "HUB_base" / "secrets" / "service_accounts" / "sa_demo.env").read_text() == "SA_TOKEN=xyz\n"


def test_export_recovery_files_are_not_world_readable(tmp_path, monkeypatch):
    """Les secrets déchiffrés dans recovery/ ne doivent pas être world-readable."""
    mgr = _setup(tmp_path, monkeypatch)
    mgr.backup_all()
    dest = mgr.export_recovery()
    for f in dest.rglob("*"):
        if f.is_file():
            mode = f.stat().st_mode
            assert (mode & 0o004) == 0, f"{f.relative_to(dest)} est world-readable ({oct(mode)})"
    assert (dest.stat().st_mode & 0o007) == 0, "recovery/ ne doit pas être world-accessible"


def test_managed_api_removed():
    for attr in ("push_env", "pull_env", "auto_import_missing", "toggle_managed", "managed_env"):
        assert not hasattr(VaultManager, attr)


def test_backup_all_denies_symlinked_app(tmp_path, monkeypatch):
    """backup_all ne doit PAS sauvegarder les .env de dossiers symlinks."""
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(hub_state, "HUB_DIR", tmp_path / "HUB_base")
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path / "APPS")
    (tmp_path / "HUB_base" / "secrets").mkdir(parents=True)
    (tmp_path / "HUB_base" / ".env").write_text("POSTGRES_PASSWORD=sec\n")
    external = tmp_path / "external"; external.mkdir()
    (external / ".env").write_text("OUTSIDE=1\n")
    (tmp_path / "APPS").mkdir()
    (tmp_path / "APPS" / "linked").symlink_to(external, target_is_directory=True)
    v = Vault(tmp_path / "vault" / "v.enc")
    v.unlock("pw")
    mgr = VaultManager(v)
    mgr.backup_all()
    assert "app:linked" not in v.list_entries()


def test_backup_all_cleans_stale_app_entries(tmp_path, monkeypatch):
    """backup_all doit supprimer les entrées d'apps disparues du disque."""
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(hub_state, "HUB_DIR", tmp_path / "HUB_base")
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path / "APPS")
    (tmp_path / "HUB_base" / "secrets").mkdir(parents=True)
    (tmp_path / "HUB_base" / ".env").write_text("POSTGRES_PASSWORD=sec\n")
    app = tmp_path / "APPS" / "gone"; app.mkdir(parents=True)
    (app / ".env").write_text("DEAD=1\n")
    v = Vault(tmp_path / "vault" / "v.enc")
    v.unlock("pw")
    mgr = VaultManager(v)
    mgr.backup_all()
    assert "app:gone" in v.list_entries()
    # Supprimer l'app physique puis re-sauvegarder
    (app / ".env").unlink(); app.rmdir()
    mgr.backup_all()
    assert "app:gone" not in v.list_entries()


# ── CA HTTPS (HUB_base/certs/ca) : sauvegarde + restauration ──


def _write_ca(tmp_path):
    """Pose une CA factice sur le disque (comme HUB_base/certs/ca en prod)."""
    ca_dir = tmp_path / "HUB_base" / "certs" / "ca"
    ca_dir.mkdir(parents=True)
    (ca_dir / "ca.crt").write_bytes(b"-----BEGIN CERTIFICATE-----")
    (ca_dir / "ca.key").write_bytes(b"-----BEGIN PRIVATE KEY-----")
    return ca_dir


def _mock_backup_db(monkeypatch):
    """Hermetise backup_all : sans ce mock, lancé sur un serveur HUB (docker
    + hub-db vivants), le coffre de test embarquerait un VRAI dump — et un
    restore_all rejouerait un TRUNCATE sur la base réelle."""
    from hubctl_cli import vault_manager as vm
    monkeypatch.setattr(vm, "_backup_db", lambda vault, on_log=None: False)


def test_backup_all_captures_certs_ca(tmp_path, monkeypatch):
    """backup_all doit capturer ca.crt + ca.key sous leur relpath PARC_ROOT :
    la CA est importée dans les navigateurs — un PRA sans elle rejette toute
    la flotte (SEC_ERROR_BAD_SIGNATURE)."""
    mgr = _setup(tmp_path, monkeypatch)
    _write_ca(tmp_path)
    _mock_backup_db(monkeypatch)
    logs = []
    summary = mgr.backup_all(on_log=logs.append)
    assert mgr.vault.get_file("HUB_base/certs/ca/ca.crt") == b"-----BEGIN CERTIFICATE-----"
    assert mgr.vault.get_file("HUB_base/certs/ca/ca.key") == b"-----BEGIN PRIVATE KEY-----"
    assert summary["ca"] == 2
    assert any("HUB_base/certs/ca/ca.crt" in l for l in logs)


def test_backup_all_without_certs_ca_is_graceful(tmp_path, monkeypatch):
    """HUB_base sans certs/ca (HTTPS non déployé) → pas de crash, compteur 0,
    rien d'inventé dans le coffre."""
    mgr = _setup(tmp_path, monkeypatch)
    _mock_backup_db(monkeypatch)
    summary = mgr.backup_all()
    assert summary["ca"] == 0
    assert not any(f.startswith("HUB_base/certs/ca/") for f in mgr.vault.list_files())


def test_restore_all_restores_certs_ca_with_modes(tmp_path, monkeypatch):
    """restore_all doit remettre EXACTEMENT la CA d'origine : ca.key 0600,
    ca.crt 0644, dossier 0700 — c'est ce qui préserve la confiance des
    navigateurs après une restauration PRA."""
    mgr = _setup(tmp_path, monkeypatch)
    ca_dir = _write_ca(tmp_path)
    _mock_backup_db(monkeypatch)
    mgr.backup_all()
    # Simule la perte du disque : la CA disparaît, le coffre seul la connaît.
    for f in ca_dir.iterdir():
        f.unlink()
    ca_dir.rmdir()

    restored = restore_all(mgr.vault)

    crt = ca_dir / "ca.crt"
    key = ca_dir / "ca.key"
    assert crt.read_bytes() == b"-----BEGIN CERTIFICATE-----"
    assert key.read_bytes() == b"-----BEGIN PRIVATE KEY-----"
    assert key.stat().st_mode & 0o777 == 0o600
    assert crt.stat().st_mode & 0o777 == 0o644
    assert ca_dir.stat().st_mode & 0o777 == 0o700
    assert any("certs/ca" in r for r in restored)
    assert any("CA HTTPS" in r for r in restored)


def test_restore_all_old_vault_without_certs_ca(tmp_path, monkeypatch):
    """Coffre ancien sans certs/ca → pas de crash, rien d'écrit sous certs/
    (comportement dégradé attendu sur les coffres d'avant la feature)."""
    mgr = _setup(tmp_path, monkeypatch)
    _mock_backup_db(monkeypatch)
    mgr.backup_all()
    restored = restore_all(mgr.vault)
    assert not (tmp_path / "HUB_base" / "certs").exists()
    assert not any("certs/ca" in r for r in restored)


# ── import_vault : validation + backup de l'ancien coffre ──


def test_import_vault_rejects_invalid_magic(tmp_path):
    """Un fichier externe sans magic HUBCTLV1 ne doit pas remplacer le coffre
    courant (n'importe quel binaire était auparavant accepté)."""
    from hubctl_cli.vault_manager import import_vault
    cur = tmp_path / "v.enc"
    v = Vault(cur)
    v.unlock("pw")
    v.set_entry("hub", {"K": "ORIGINAL"})
    v.save()
    original = cur.read_bytes()

    bad = tmp_path / "bad.enc"
    bad.write_bytes(b"NOT_A_VAULT_garbage")

    assert import_vault(v, bad) is False
    assert cur.read_bytes() == original, "le coffre courant ne doit pas être touché"
    assert not (tmp_path / "v.enc.bak").exists(), "rien n'a été importé → pas de backup"


def test_import_vault_valid_backs_up_current(tmp_path):
    """Un import valide préserve l'ancien coffre dans un .bak récupérable."""
    from hubctl_cli.vault_manager import import_vault
    cur = tmp_path / "v.enc"
    v = Vault(cur)
    v.unlock("pw")
    v.set_entry("hub", {"K": "OLD"})
    v.save()

    src_path = tmp_path / "new.enc"
    vsrc = Vault(src_path)
    vsrc.unlock("other")
    vsrc.set_entry("hub", {"K": "NEW"})
    vsrc.save()

    assert import_vault(v, src_path) is True

    bak = tmp_path / "v.enc.bak"
    assert bak.exists(), "un backup de l'ancien coffre doit être créé"
    vb = Vault(bak)
    assert vb.unlock("pw") is True
    assert vb.reveal("hub") == {"K": "OLD"}, "le .bak doit contenir l'ancien état"
    assert cur.read_bytes() == src_path.read_bytes(), "le courant = le nouveau"


def test_import_vault_missing_src(tmp_path):
    from hubctl_cli.vault_manager import import_vault
    v = Vault(tmp_path / "v.enc")
    v.unlock("pw")
    assert import_vault(v, tmp_path / "absent.enc") is False


# ── _restore_db : snapshot avant truncate + rollback sur échec ──


_SNAPSHOT = b"-- snapshot live --\nINSERT INTO users VALUES (1);\n"

# Dump coffre réaliste (format pg_dump --inserts capturé par _backup_db) :
# les marqueurs « -- Data for Name: » sont ce que _restore_db parse pour
# connaître les tables à tronquer et les counts attendus post-restore.
_DUMP_COFFRE = (
    b"--\n-- PostgreSQL database dump\n--\n\n"
    b"-- Data for Name: users; Type: TABLE DATA; Schema: public; Owner: hubadmin\n\n"
    b"INSERT INTO users VALUES (1, 'admin');\n\n"
    b"-- Data for Name: applications; Type: TABLE DATA; Schema: public; Owner: hubadmin\n\n"
    b"INSERT INTO applications VALUES (1, 'app1');\n"
    b"INSERT INTO applications VALUES (2, 'app2');\n"
)
_COUNTS_OK = {"users": 1, "applications": 2}


def _db_dispatch_strict(cmd, *, replay_ok=True, truncate_ok=True, counts=None):
    """Fake subprocess.run : simule pg_dump (snapshot), TRUNCATE, replay psql
    et requête de vérification count(*) — routage par contenu de la commande."""
    from unittest import mock
    joined = " ".join(str(c) for c in cmd)
    if "pg_dump" in joined:
        return mock.Mock(returncode=0, stdout=_SNAPSHOT, stderr=b"")
    if "TRUNCATE" in joined:
        return mock.Mock(returncode=0 if truncate_ok else 1,
                         stdout=b"", stderr=b"ERROR: cannot truncate table")
    if "count(*)" in joined:
        lines = "".join(f"{t}|{n}\n" for t, n in (counts or {}).items())
        return mock.Mock(returncode=0, stdout=lines.encode(), stderr=b"")
    return mock.Mock(returncode=0 if replay_ok else 1, stdout=b"",
                     stderr=b"ERROR: violates foreign key constraint")


def _is_psql_stream_cmd(cmd):
    """psql alimenté par stdin (replay dump / rollback snapshot) — ni pg_dump,
    ni TRUNCATE -c, ni requête de vérification count(*)."""
    joined = " ".join(str(x) for x in cmd)
    return ("psql" in joined and "TRUNCATE" not in joined
            and "pg_dump" not in joined and "count(*)" not in joined)


def test_restore_db_rolls_back_on_failure(monkeypatch):
    """Si le replay du dump échoue, _restore_db doit restaurer le snapshot live
    (pris AVANT truncate) pour ne pas laisser la DB vidée sans recours."""
    import subprocess
    from hubctl_cli import vault_manager

    calls = []

    def fake_run(cmd, **kw):
        calls.append({"cmd": list(cmd), "input": kw.get("input")})
        return _db_dispatch_strict(cmd, replay_ok=False, counts=_COUNTS_OK)

    monkeypatch.setattr(subprocess, "run", fake_run)
    logs = []
    ok = vault_manager._restore_db(_DUMP_COFFRE, on_log=logs.append)

    assert ok is False
    # 1) un snapshot live est pris AVANT le 1er truncate
    def joined(c):
        return " ".join(str(x) for x in c["cmd"])
    pgdump_idx = next(i for i, c in enumerate(calls) if "pg_dump" in joined(c))
    truncate_idx = next(i for i, c in enumerate(calls) if "TRUNCATE" in joined(c))
    assert pgdump_idx < truncate_idx, "le snapshot doit précéder le truncate"
    # 2) le rollback rejoue le snapshot (psql avec input=snapshot)
    assert any(c["input"] == _SNAPSHOT for c in calls), \
        "le snapshot live doit être rejoué en rollback après échec"
    # 3) un log d'erreur est émis (plus d'échec silencieux)
    assert logs, "un log doit être émis en cas d'échec"


def test_restore_db_success_takes_snapshot(monkeypatch):
    """Même en succès, un snapshot live est pris (filet de sécurité), sans rollback."""
    import subprocess
    from hubctl_cli import vault_manager

    calls = []

    def fake_run(cmd, **kw):
        calls.append({"cmd": list(cmd), "input": kw.get("input")})
        return _db_dispatch_strict(cmd, counts=_COUNTS_OK)

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok = vault_manager._restore_db(_DUMP_COFFRE)

    assert ok is True
    # snapshot pris
    assert any("pg_dump" in " ".join(str(x) for x in c["cmd"]) for c in calls)
    # pas de rollback : le snapshot n'est jamais rejoué en psql
    assert not any(c["input"] == _SNAPSHOT for c in calls)


# ── _restore_db : détection d'échec du replay (issue Hubctl#1) ──
#
# Sans ON_ERROR_STOP, psql continue sur erreur SQL (conflit FK, dup key) et
# sort 0 : un dump partiellement joué était déclaré « DB restaurée » alors que
# des applications n'étaient pas rejouées. Ces tests verrouillent le contrat :
# replay sous ON_ERROR_STOP, TRUNCATE vérifié avant replay, comptage de lignes
# post-restore comparé au dump, et rollback du snapshot au moindre écart.


def test_replay_psql_uses_on_error_stop(monkeypatch):
    """Le replay (et le rollback) doivent porter -v ON_ERROR_STOP=1 : c'est ce
    qui transforme une erreur SQL intra-dump (conflit FK) en code de sortie
    non nul, au lieu d'un « succès » partiel silencieux."""
    import subprocess
    from hubctl_cli import vault_manager

    calls = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return _db_dispatch_strict(cmd, counts=_COUNTS_OK)

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok = vault_manager._restore_db(_DUMP_COFFRE)

    assert ok is True
    streams = [c for c in calls if _is_psql_stream_cmd(c)]
    assert streams, "le replay du dump (psql + stdin) doit avoir lieu"
    for c in streams:
        assert "-v" in c and "ON_ERROR_STOP=1" in c, \
            "psql doit être lancé avec -v ON_ERROR_STOP=1 (sinon conflits FK silencieux)"


def test_truncate_failure_aborts_before_replay(monkeypatch):
    """Un TRUNCATE en échec doit arrêter le restore AVANT le replay et recharger
    le snapshot : rejouer le dump sur des tables non vidées garantit dup keys et
    conflits FK en cascade (le scénario de l'incident Hubctl#1)."""
    import subprocess
    from hubctl_cli import vault_manager

    calls = []

    def fake_run(cmd, **kw):
        calls.append({"cmd": list(cmd), "input": kw.get("input")})
        return _db_dispatch_strict(cmd, truncate_ok=False, counts=_COUNTS_OK)

    monkeypatch.setattr(subprocess, "run", fake_run)
    logs = []

    ok = vault_manager._restore_db(_DUMP_COFFRE, on_log=logs.append)

    assert ok is False
    assert not any(c["input"] == _DUMP_COFFRE for c in calls), \
        "aucun replay ne doit être tenté quand un TRUNCATE échoue"
    assert any(c["input"] == _SNAPSHOT for c in calls), \
        "le snapshot live doit être rechargé (tables partiellement tronquées)"
    assert any("TRUNCATE" in l for l in logs), "l'échec du TRUNCATE doit être loggé"


def test_row_count_mismatch_triggers_rollback(monkeypatch):
    """Replay « réussi » (psql sort 0) mais counts post-restore ≠ dump → échec
    explicite + rollback : c'est exactement le bug Hubctl#1, psql continuait sur
    les conflits FK et le restore déclarait succès avec des apps manquantes."""
    import subprocess
    from hubctl_cli import vault_manager

    calls = []

    def fake_run(cmd, **kw):
        calls.append({"cmd": list(cmd), "input": kw.get("input")})
        # DB vide après replay alors que le dump contient 3 lignes
        return _db_dispatch_strict(cmd, counts={"users": 0, "applications": 0})

    monkeypatch.setattr(subprocess, "run", fake_run)
    logs = []

    ok = vault_manager._restore_db(_DUMP_COFFRE, on_log=logs.append)

    assert ok is False, "un restore partiel (counts ≠ dump) ne doit pas rapporter succès"
    assert any(c["input"] == _SNAPSHOT for c in calls), "rollback du snapshot requis"
    assert logs, "l'écart de comptage doit être loggé"


def test_row_count_match_is_verified(monkeypatch):
    """Chemin nominal : la vérification count(*) est réellement exécutée et le
    restore réussit sans rollback quand les counts correspondent au dump."""
    import subprocess
    from hubctl_cli import vault_manager

    calls = []

    def fake_run(cmd, **kw):
        calls.append({"cmd": list(cmd), "input": kw.get("input")})
        return _db_dispatch_strict(cmd, counts=_COUNTS_OK)

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok = vault_manager._restore_db(_DUMP_COFFRE)

    assert ok is True
    assert any("count(*)" in " ".join(str(x) for x in c["cmd"]) for c in calls), \
        "les counts post-restore doivent être vérifiés"
    assert not any(c["input"] == _SNAPSHOT for c in calls), "pas de rollback en succès"


def test_dump_without_data_section_is_rejected(monkeypatch):
    """Un contenu sans section « -- Data for Name: » (pas un dump pg_dump
    --inserts) doit être refusé AVANT tout truncate : on ne vide pas la base
    pour rejouer du SQL inconnu."""
    import subprocess
    from hubctl_cli import vault_manager

    calls = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return _db_dispatch_strict(cmd, counts={})

    monkeypatch.setattr(subprocess, "run", fake_run)
    logs = []

    ok = vault_manager._restore_db(b"-- fichier quelconque, pas un dump\nSELECT 1;\n",
                                   on_log=logs.append)

    assert ok is False
    assert not any("TRUNCATE" in " ".join(c) for c in calls), "aucun truncate sans dump reconnu"
    assert not any(_is_psql_stream_cmd(c) for c in calls), "aucun replay sans dump reconnu"
    assert logs, "le refus d'un dump méconnaissable doit être loggé"


def test_restore_all_reports_db_failure(tmp_path, monkeypatch):
    """restore_all ne doit plus déclarer « DB restaurée » quand le replay a
    échoué (issue Hubctl#1 : la valeur de retour de _restore_db était ignorée)."""
    mgr = _setup(tmp_path, monkeypatch)
    mgr.vault.set_file("HUB_base/backup/hub_dump.sql", _DUMP_COFFRE)
    from hubctl_cli import vault_manager as vm
    monkeypatch.setattr(vm, "_restore_db", lambda sql, on_log=None: False)

    restored = restore_all(mgr.vault)

    assert not any("DB restaurée" in r for r in restored), \
        "un échec de replay ne doit pas être rapporté comme un succès"
    assert any("NON restaurée" in r for r in restored), \
        "l'échec DB doit apparaître explicitement dans le rapport"


def test_restore_all_reports_db_success(tmp_path, monkeypatch):
    """Pin du chemin nominal : quand le replay réussit, le rapport mentionne
    toujours la restauration DB."""
    mgr = _setup(tmp_path, monkeypatch)
    mgr.vault.set_file("HUB_base/backup/hub_dump.sql", _DUMP_COFFRE)
    from hubctl_cli import vault_manager as vm
    monkeypatch.setattr(vm, "_restore_db", lambda sql, on_log=None: True)

    restored = restore_all(mgr.vault)
    assert any("DB restaurée" in r for r in restored)


# ── Hook réplication PRA ──


def test_backup_all_triggers_pra_replicate(tmp_path, monkeypatch):
    mgr = _setup(tmp_path, monkeypatch)
    from hubctl_cli import pra_replicate
    calls = []
    monkeypatch.setattr(pra_replicate, "replicate",
                        lambda **kw: calls.append(kw) or {"status": "ok", "copied": [], "message": ""})
    summary = mgr.backup_all()
    assert calls, "backup_all doit appeler pra_replicate.replicate"
    assert summary["pra"]["status"] == "ok"


def test_backup_all_survives_replicate_crash(tmp_path, monkeypatch):
    mgr = _setup(tmp_path, monkeypatch)
    from hubctl_cli import pra_replicate

    def boom(**kw):
        raise RuntimeError("usb arrachée")

    monkeypatch.setattr(pra_replicate, "replicate", boom)
    summary = mgr.backup_all()
    assert summary["env"] >= 1          # la sauvegarde a réussi quand même
    assert summary["pra"] is None       # la réplication a échoué silencieusement
