# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Pools d'adresses du daemon Docker — F14 (saturation récurrente des pools
par défaut : ≈31 sous-réseaux pour 33 apps × 1 network chacune)."""
import json

import pytest

from hubctl_cli import docker_pools as dp


def test_merge_daemon_config_sur_dict_vide():
    out = dp.merge_daemon_config({})
    assert out == {"default-address-pools": dp.DEFAULT_POOLS}


def test_merge_daemon_config_preserve_dns_et_runtimes():
    cfg = {
        "dns": ["8.8.8.8", "1.1.1.1"],
        "runtimes": {"nvidia": {"args": [], "path": "nvidia-container-runtime"}},
    }
    out = dp.merge_daemon_config(cfg)
    assert out["dns"] == ["8.8.8.8", "1.1.1.1"]
    assert "nvidia" in out["runtimes"]
    assert out["default-address-pools"] == dp.DEFAULT_POOLS


def test_merge_daemon_config_est_idempotent():
    out1 = dp.merge_daemon_config({"dns": ["1.1.1.1"]})
    out2 = dp.merge_daemon_config(out1)
    assert out1 == out2


def test_merge_daemon_config_remplace_pools_existant_differents():
    cfg = {"default-address-pools": [{"base": "10.0.0.0/8", "size": 16}]}
    out = dp.merge_daemon_config(cfg)
    assert out["default-address-pools"] == dp.DEFAULT_POOLS


def test_check_pools_et_write_config(tmp_path):
    cfg_file = tmp_path / "daemon.json"
    assert dp.check_pools(cfg_file) is False  # absent

    cfg = dp.merge_daemon_config({"dns": ["1.1.1.1"]})
    dp.write_config(cfg, cfg_file)
    assert json.loads(cfg_file.read_text())["dns"] == ["1.1.1.1"]
    assert dp.check_pools(cfg_file) is True

    other = tmp_path / "other.json"
    other.write_text(json.dumps({"default-address-pools": [{"base": "10.0.0.0/8", "size": 16}]}))
    assert dp.check_pools(other) is False


def test_setup_pools_deja_configure_est_idempotent(tmp_path, monkeypatch):
    cfg_file = tmp_path / "daemon.json"
    dp.write_config(dp.merge_daemon_config({}), cfg_file)
    rc = dp.setup_pools(console=None, yes=True, path=cfg_file)
    assert rc == 0


def test_setup_pools_ecrit_et_retourne_zero(tmp_path, monkeypatch):
    cfg_file = tmp_path / "daemon.json"
    monkeypatch.setattr("builtins.input", lambda *a, **k: "o")
    rc = dp.setup_pools(console=None, yes=True, path=cfg_file)
    assert rc == 0
    assert json.loads(cfg_file.read_text())["default-address-pools"] == dp.DEFAULT_POOLS


def test_setup_pools_permission_denied_retourne_un(tmp_path, monkeypatch):
    cfg_file = tmp_path / "daemon.json"

    def raise_perm(cfg, path=None):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(dp, "write_config", raise_perm)
    rc = dp.setup_pools(console=None, yes=True, path=cfg_file)
    assert rc == 1


def test_network_prune_appelle_docker_prune_force(monkeypatch):
    captured = {}

    class FakeCompleted:
        returncode = 0
        stdout = "deleted networks: 2"

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeCompleted()

    monkeypatch.setattr(dp.subprocess, "run", fake_run)
    assert dp.network_prune() is True
    assert captured["cmd"] == ["docker", "network", "prune", "-f"]


def test_stop_all_appelle_le_prune_reseaux(monkeypatch):
    """F14 : `hubctl stop all` doit purger les networks orphelins en fin de
    run (sinon ils s'accumulent et saturent les pools d'adresses)."""
    from hubctl_cli import actions

    calls = []
    monkeypatch.setattr(actions.hub_state, "get_apps", lambda: [])
    monkeypatch.setattr(actions, "_hub_running", lambda: False)
    monkeypatch.setattr(actions, "network_prune", lambda: calls.append("prune") or True)
    actions.action_stop_all(console=None, target="all")
    assert calls == ["prune"]


# ── docker0_gateway : passerelle réelle pour le bind des units hôte ──


def test_docker0_gateway_lit_l_inet_de_docker0(monkeypatch):
    """Retourne l'IP inet de docker0 — pas 172.17.0.1 codé en dur (bug
    15/11/2026 : docker0 déplacé en 10.200.0.1 par les pools F14 → unit
    bindée sur une IP inexistante, « Service de mise a jour hôte
    injoignable » côté Hub)."""
    class FakeCompleted:
        stdout = ("2: docker0    inet 10.200.0.1/24 brd 10.200.0.255 scope "
                  "global docker0\\       valid_lft forever preferred_lft forever\n")
        returncode = 0

    monkeypatch.setattr(dp.subprocess, "run", lambda cmd, **k: FakeCompleted())
    assert dp.docker0_gateway() == "10.200.0.1"


def test_docker0_gateway_repli_historique_si_docker0_absent(monkeypatch):
    """Interface absente / `ip` manquant / timeout → 172.17.0.1 (comportement
    historique), JAMAIS d'exception (appelé au rendu d'unit — jamais fatal)."""
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("ip")

    monkeypatch.setattr(dp.subprocess, "run", fake_run)
    assert dp.docker0_gateway() == "172.17.0.1"


def test_docker0_gateway_repli_si_sortie_sans_inet(monkeypatch):
    class FakeCompleted:
        stdout = "2: docker0    <NO-CARRIER,BROADCAST,MULTICAST,UP> mtu 1500\n"
        returncode = 0

    monkeypatch.setattr(dp.subprocess, "run", lambda cmd, **k: FakeCompleted())
    assert dp.docker0_gateway() == "172.17.0.1"
