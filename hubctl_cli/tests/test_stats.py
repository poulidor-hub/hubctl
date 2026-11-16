# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Tests de hubctl_cli.stats + route GET /stats (cockpit hôte Hub).

Parseurs purs : textes /proc factices écrits dans tmp_path (aucune
lecture réelle requise). Contrat get_stats (consumé tel quel par le
proxy HUB_base — ne pas changer les noms de champs) : {"service",
"timestamp", "uptime_seconds", "cpu": {percent, cores, load1},
"memory": {total_mb, used_mb, available_mb, percent, swap_percent},
"disk": {path, total_gb, used_gb, free_gb, percent}} + "errors" (et
service "degraded") si une section échoue. Route HTTP : même auth
X-Atelier-Token que /update/status (401 absent / 403 invalide), 405 en
POST — recette serveur port 0 de test_update_serve.
"""
import json
import os
import shutil
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest

from hubctl_cli import stats, update_serve


@pytest.fixture(autouse=True)
def _reset_cpu_sample():
    """_last_cpu_sample est module-level : chaque test repart à froid
    (le « premier appel » à deux échantillons doit être observable)."""
    stats._last_cpu_sample = None
    yield
    stats._last_cpu_sample = None


# ── Parseurs purs (fixtures /proc factices) ─────────────────────────────────


def _proc(tmp_path: Path, name: str, content: str) -> str:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_read_uptime_seconds(tmp_path):
    """1er champ de /proc/uptime → float (le 2e champ est ignoré)."""
    path = _proc(tmp_path, "uptime", "2073600.52 8342100.13\n")
    assert stats.read_uptime_seconds(path) == 2073600.52


def test_read_uptime_seconds_errors(tmp_path):
    """Fichier absent → OSError, contenu illisible → ValueError : les
    parseurs laissent passer, get_stats isole (jamais la route)."""
    with pytest.raises(OSError):
        stats.read_uptime_seconds(str(tmp_path / "absent"))
    path = _proc(tmp_path, "uptime", "pas-un-nombre\n")
    with pytest.raises(ValueError):
        stats.read_uptime_seconds(path)


MEMINFO = """MemTotal:       16384000 kB
MemFree:         2000000 kB
MemAvailable:    6186000 kB
Buffers:          300000 kB
Cached:          3500000 kB
SwapTotal:       2097148 kB
SwapFree:        2031616 kB
HugePages_Total:       0
"""


def test_read_memory(tmp_path):
    """Contrat memory : kB → MiB entiers, used = total - available,
    percent 1 décimale, swap depuis SwapTotal/SwapFree."""
    path = _proc(tmp_path, "meminfo", MEMINFO)
    mem = stats.read_memory(path)
    assert mem == {
        "total_mb": 16000,        # 16384000 kB // 1024
        "available_mb": 6041,     # 6186000 kB // 1024
        "used_mb": 9959,          # total - available
        "percent": 62.2,          # round(9959 / 16000 × 100, 1)
        "swap_percent": 3.1,      # round(65532 / 2097148 × 100, 1)
    }


def test_read_memory_no_swap(tmp_path):
    """Pas de lignes Swap* → swap_percent 0.0 (jamais de division par 0)."""
    content = "\n".join(l for l in MEMINFO.splitlines()
                        if not l.startswith("Swap"))
    mem = stats.read_memory(_proc(tmp_path, "meminfo", content + "\n"))
    assert mem["swap_percent"] == 0.0


def test_read_memory_memfree_fallback(tmp_path):
    """MemAvailable absent → repli MemFree (même lecture que menu._mem_usage)."""
    content = "MemTotal:  2048000 kB\nMemFree:  512000 kB\n"
    mem = stats.read_memory(_proc(tmp_path, "meminfo", content))
    assert mem["available_mb"] == 500
    assert mem["used_mb"] == 1500


def test_read_cpu_sample(tmp_path):
    """Première ligne cpu → (idle+ iowait, somme des ticks) ; cpu0… ignorés."""
    content = ("cpu  100 0 200 7000 500 0 100 0 0 0\n"
               "cpu0 50 0 100 3500 250 0 50 0 0 0\n"
               "intr 123456\n")
    assert stats.read_cpu_sample(_proc(tmp_path, "stat", content)) \
        == (7500, 7900)  # idle 7000+500, total 100+0+200+7000+500+0+100


def test_read_cpu_sample_errors(tmp_path):
    """Ligne cpu absente ou tronquée (< 4 champs) → ValueError."""
    with pytest.raises(ValueError):
        stats.read_cpu_sample(_proc(tmp_path, "stat", "intr 42\n"))
    with pytest.raises(ValueError):
        stats.read_cpu_sample(_proc(tmp_path, "stat", "cpu  1 2 3\n"))


def test_cpu_percent_between():
    """100 × (1 - Δidle/Δtotal) ; Δtotal = 0 → 0.0 ; borné à [0, 100]."""
    assert stats.cpu_percent_between((7500, 7900), (7600, 8100)) == 50.0
    assert stats.cpu_percent_between((7500, 7900), (7500, 7900)) == 0.0
    # Δidle > Δtotal (compteur incohérent) → borné, jamais négatif
    assert stats.cpu_percent_between((0, 100), (200, 100)) == 0.0


def test_read_load1(tmp_path):
    path = _proc(tmp_path, "loadavg", "0.84 0.52 0.40 2/1234 5678\n")
    assert stats.read_load1(path) == 0.84


# ── get_stats (assemblage contrat + dégradation) ────────────────────────────


def test_get_stats_first_call_two_samples(monkeypatch):
    """Premier appel : 2 échantillons espacés d'un délai, % différentiel ;
    appels suivants : 1 seul échantillon (_last_cpu_sample réutilisé) et
    Δtotal = 0 → 0.0 (PAS l'approximation load × 25 de la TUI)."""
    samples = [(100, 1000), (200, 1200), (200, 1200)]
    calls: list[str] = []

    def fake_sample(path="/proc/stat"):
        calls.append(path)
        return samples[len(calls) - 1]

    delays: list[float] = []
    monkeypatch.setattr(stats, "read_cpu_sample", fake_sample)
    monkeypatch.setattr(stats, "_cpu_delay", lambda: delays.append(1))
    monkeypatch.setattr(stats, "read_load1", lambda path="/proc/loadavg": 0.0)

    data = stats.get_stats()
    assert len(calls) == 2
    assert delays  # un délai entre les deux échantillons du 1er appel
    assert data["cpu"]["percent"] == 50.0  # Δidle 100 / Δtotal 200

    first_delays = len(delays)
    data = stats.get_stats()
    assert len(calls) == 3  # échantillon précédent réutilisé
    assert len(delays) == first_delays  # plus aucun délai
    assert data["cpu"]["percent"] == 0.0  # Δtotal = 0 → garde


def test_get_stats_contract_live():
    """Contrat exact sur la machine réelle (Linux : /proc présent) —
    clés et types consommés tels quels par le proxy HUB_base."""
    data = stats.get_stats()
    assert data["service"] == "ok"
    assert "errors" not in data
    datetime.fromisoformat(data["timestamp"])  # ISO8601 parsable
    assert data["uptime_seconds"] > 0
    assert set(data["cpu"]) == {"percent", "cores", "load1"}
    assert data["cpu"]["cores"] == os.cpu_count()
    assert 0 <= data["cpu"]["percent"] <= 100
    assert data["cpu"]["load1"] >= 0
    assert set(data["memory"]) == {"total_mb", "used_mb", "available_mb",
                                   "percent", "swap_percent"}
    assert data["memory"]["total_mb"] > 0
    assert (data["memory"]["used_mb"]
            == data["memory"]["total_mb"] - data["memory"]["available_mb"])
    assert set(data["disk"]) == {"path", "total_gb", "used_gb", "free_gb",
                                 "percent"}
    assert data["disk"]["path"]  # PARC_ROOT (str non vide)
    assert data["disk"]["total_gb"] > 0


def test_get_stats_explicit_parc_root(tmp_path):
    """parc_root explicite → disque mesuré sur CE chemin (tests), pas le
    global PARC_ROOT ; secteurs → Go 1 décimale, percent utilisé."""
    data = stats.get_stats(parc_root=tmp_path)
    assert data["disk"]["path"] == str(tmp_path)
    u = shutil.disk_usage(tmp_path)
    assert data["disk"]["total_gb"] == round(u.total / 1024 ** 3, 1)
    assert data["disk"]["used_gb"] == round(u.used / 1024 ** 3, 1)
    assert data["disk"]["free_gb"] == round(u.free / 1024 ** 3, 1)
    assert data["disk"]["percent"] == round(u.used / u.total * 100, 1)


def test_get_stats_degraded_section_isolated(monkeypatch, tmp_path):
    """Une section en échec ne fait JAMAIS échouer la collecte : clés du
    contrat à zéro + errors[section] + service "degraded" ; les autres
    sections restent correctes."""
    monkeypatch.setattr(stats, "read_memory",
                        lambda path="/proc/meminfo":
                        (_ for _ in ()).throw(OSError("meminfo boom")))

    data = stats.get_stats(parc_root=tmp_path)
    assert data["service"] == "degraded"
    assert data["errors"] == {"memory": "meminfo boom"}
    assert data["memory"] == {"total_mb": 0, "used_mb": 0, "available_mb": 0,
                              "percent": 0.0, "swap_percent": 0.0}
    # sections saines intactes
    assert data["uptime_seconds"] > 0
    assert data["disk"]["path"] == str(tmp_path)
    assert data["disk"]["total_gb"] > 0


def test_get_stats_degraded_multiple_sections(monkeypatch, tmp_path):
    """Plusieurs sections en échec → errors cumulées, service degraded."""
    def boom(path="/proc/x"):
        raise RuntimeError("kaput")

    monkeypatch.setattr(stats, "read_uptime_seconds", boom)
    monkeypatch.setattr(stats, "read_cpu_sample", boom)
    monkeypatch.setattr(stats, "read_load1", boom)
    monkeypatch.setattr(stats, "_cpu_delay", lambda: None)

    data = stats.get_stats(parc_root=tmp_path)
    assert data["service"] == "degraded"
    assert set(data["errors"]) == {"uptime", "cpu"}
    assert data["uptime_seconds"] == 0.0
    assert data["cpu"]["percent"] == 0.0
    assert data["cpu"]["load1"] == 0.0
    assert data["memory"]["total_mb"] > 0  # /proc/meminfo réel intact


# ── Route HTTP GET /stats (recette serveur port 0 de test_update_serve) ─────

TEST_TOKEN = "tok-stats-123"


@contextmanager
def _running_server(tmp_path: Path):
    """Serveur update_serve sur port 0 (thread daemon) avec un parc
    factice : /stats mesure le disque de tmp_path (path vérifiable)."""
    token_file = tmp_path / "atelier_token"
    token_file.write_text(TEST_TOKEN + "\n", encoding="utf-8")
    parc = tmp_path / "parc"
    parc.mkdir()
    server = update_serve.build_server(
        0, token_file, parc_root=parc,
        state_file=tmp_path / "update_state.json", log_dir=tmp_path / "logs")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"port": server.server_address[1], "parc": parc}
    finally:
        server.shutdown()
        server.server_close()


def _request(port: int, method: str, path: str, *,
             token: str | None = TEST_TOKEN) -> tuple[int, dict]:
    headers = {}
    if token is not None:
        headers["X-Atelier-Token"] = token
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_http_stats_auth(tmp_path):
    """Même protection token que /update/status : header absent → 401 ;
    token invalide → 403 (PAS publique, contrairement à /health)."""
    with _running_server(tmp_path) as srv:
        code, payload = _request(srv["port"], "GET", "/stats", token=None)
        assert code == 401
        assert payload  # erreur JSON propre, pas de connexion coupée
        code, payload = _request(srv["port"], "GET", "/stats",
                                 token="mauvais-token")
        assert code == 403


def test_http_stats_contract(tmp_path):
    """GET /stats + bon token → 200, contrat complet : disk.path = parc
    servi, service ok, aucune clé du contrat manquante."""
    with _running_server(tmp_path) as srv:
        code, data = _request(srv["port"], "GET", "/stats")
    assert code == 200
    assert data["service"] == "ok"
    datetime.fromisoformat(data["timestamp"])
    assert set(data) == {"service", "timestamp", "uptime_seconds", "cpu",
                         "memory", "disk"}
    assert set(data["cpu"]) == {"percent", "cores", "load1"}
    assert set(data["memory"]) == {"total_mb", "used_mb", "available_mb",
                                   "percent", "swap_percent"}
    assert set(data["disk"]) == {"path", "total_gb", "used_gb", "free_gb",
                                 "percent"}
    assert data["disk"]["path"] == str(srv["parc"])
    assert data["uptime_seconds"] > 0


def test_http_stats_post_405(tmp_path):
    """POST /stats → 405 méthode interdite (route GET only)."""
    with _running_server(tmp_path) as srv:
        code, payload = _request(srv["port"], "POST", "/stats")
    assert code == 405
    assert payload  # erreur JSON propre
