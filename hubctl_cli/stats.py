# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""``hubctl_cli.stats`` — métriques de l'hôte pour GET /stats (cockpit Hub).

Collecte stdlib pure (aucune dépendance nouvelle, même ligne que
``update_serve``) : uptime, CPU, RAM, disque, charge — consommée par la
route GET /stats du serveur ``update_serve`` (auth ``X-Atelier-Token``,
PAS publique) et proxifiée telle quelle par le backend HUB_base pour
l'admin du Hub.

Contrat JSON (route GET /stats — les noms de champs sont figés, le
consommateur les lit tels quels) :

.. code-block:: json

    {"service": "ok" | "degraded",
     "timestamp": "2026-11-16T15:04:05+01:00",
     "uptime_seconds": 2073600.0,
     "cpu": {"percent": 23.4, "cores": 8, "load1": 0.84},
     "memory": {"total_mb": 10240, "used_mb": 4198, "available_mb": 6042,
                "percent": 41.0, "swap_percent": 3.2},
     "disk": {"path": "<PARC_ROOT>", "total_gb": 466.7, "used_gb": 289.4,
              "free_gb": 177.3, "percent": 62.0}}

Choix d'implémentation :
- parseurs purs (``read_*``) paramétrés par chemin de fichier : testables
  avec des textes ``/proc`` factices, sans lecture réelle du système ;
- CPU : VRAI calcul différentiel ``/proc/stat`` (ticks idle+ iowait vs
  total de ticks entre deux échantillons) — PAS l'approximation
  ``load1 × 25`` de la TUI (``menu._cpu_pct``). Le précédent échantillon
  vit en mémoire (``_last_cpu_sample``, sous ``_CPU_LOCK``) ; au premier
  appel (mémoire vide), deux échantillons espacés d'un court sleep
  (``_CPU_SAMPLE_DELAY``) suffisent ;
- disque : réutilise ``hub_state.get_disk_usage()`` (``PARC_ROOT``) ;
  un ``parc_root`` explicite (tests) passe par ``shutil.disk_usage`` ;
- une section en échec ne fait JAMAIS échouer la collecte (même
  convention que ``collect_update_status``) : la réponse garde TOUTES
  les clés du contrat (valeurs neutres — le proxy HUB_base lit les
  champs sans test d'existence), ``errors[section]`` porte la cause et
  le service global passe ``degraded``.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from hubctl_cli import hub_state

# Écart (s) entre les deux échantillons CPU du premier appel : assez long
# pour que les compteurs de ticks avancent, assez court pour ne pas retarder
# la première requête /stats du serveur.
_CPU_SAMPLE_DELAY: float = 0.6

# Dernier échantillon (idle_total, total_all) lu sur /proc/stat : le pour-
# centage CPU est différentiel, un seul échantillon suffit aux appels
# suivants. Thread-safe via _CPU_LOCK (ThreadingHTTPServer côte à côte).
_last_cpu_sample: tuple[int, int] | None = None
_CPU_LOCK = threading.Lock()

# Valeurs neutres RAM (clés du contrat, réponse stable même dégradée).
_ZERO_MEMORY: dict = {"total_mb": 0, "used_mb": 0, "available_mb": 0,
                      "percent": 0.0, "swap_percent": 0.0}


def _cpu_delay() -> None:
    """Attente entre deux échantillons CPU (surchargeable en test)."""
    time.sleep(_CPU_SAMPLE_DELAY)


def read_uptime_seconds(path: str | Path = "/proc/uptime") -> float:
    """Uptime de l'hôte en secondes (1er champ de ``/proc/uptime``).

    Laisse passer ``OSError``/``ValueError`` : ``get_stats`` isole la
    section (``errors`` + ``degraded``), ne lève jamais vers la route.
    """
    with open(path, encoding="utf-8") as f:
        return float(f.read().split()[0])


def read_memory(path: str | Path = "/proc/meminfo") -> dict:
    """RAM/swap de l'hôte (contrat sous-section ``memory``).

    ``/proc/meminfo`` en kB : total/available convertis en MiB entiers
    (``// 1024``), ``used = total - available`` (``MemAvailable`` à
    défaut ``MemFree``, même lecture que ``menu._mem_usage``),
    ``percent = used / total × 100`` (1 décimale) ; ``swap_percent``
    depuis SwapTotal/SwapFree, ``0.0`` sans swap.
    """
    info: dict[str, int] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            key, _, value = line.partition(":")
            fields = value.split()
            if not fields:
                continue
            try:
                info[key.strip()] = int(fields[0])
            except ValueError:
                continue

    total_kb = info.get("MemTotal", 0)
    avail_kb = info.get("MemAvailable", info.get("MemFree", 0))
    swap_total_kb = info.get("SwapTotal", 0)
    swap_free_kb = info.get("SwapFree", 0)

    total_mb = total_kb // 1024
    available_mb = avail_kb // 1024
    used_mb = total_mb - available_mb
    return {
        "total_mb": total_mb,
        "available_mb": available_mb,
        "used_mb": used_mb,
        "percent": round(used_mb / total_mb * 100, 1) if total_mb > 0 else 0.0,
        "swap_percent": (round((swap_total_kb - swap_free_kb)
                               / swap_total_kb * 100, 1)
                         if swap_total_kb > 0 else 0.0),
    }


def read_cpu_sample(path: str | Path = "/proc/stat") -> tuple[int, int]:
    """Échantillon CPU brut : ``(idle_total, total_all)`` en ticks.

    Première ligne ``cpu`` de ``/proc/stat`` : ``idle_total = idle +
    iowait`` (champs 4 et 5), ``total_all`` = somme de tous les champs
    (user, nice, system, idle, iowait, irq, softirq, steal…). Ligne
    absente ou tronquée → ``ValueError`` (section isolée par
    ``get_stats``).
    """
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if not parts or parts[0] != "cpu":
                continue
            ticks = [int(v) for v in parts[1:]]
            if len(ticks) < 4:
                raise ValueError("ligne « cpu » tronquée : champs manquants")
            idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
            return idle, sum(ticks)
    raise ValueError("ligne « cpu » introuvable dans /proc/stat")


def cpu_percent_between(prev: tuple[int, int],
                        cur: tuple[int, int]) -> float:
    """Pourcentage CPU entre deux échantillons : ``100 × (1 - Δidle/Δtotal)``.

    Borrné à [0, 100] et arrondi à 1 décimale (contrat). ``Δtotal <= 0``
    (échantillons identiques, compteur remis à zéro, horloge CPU…) →
    ``0.0`` : jamais de division par zéro ni de pourcentage incohérent.
    """
    idle_delta = cur[0] - prev[0]
    total_delta = cur[1] - prev[1]
    if total_delta <= 0:
        return 0.0
    return round(max(0.0, min(100.0, 100.0 * (1.0 - idle_delta / total_delta))),
                 1)


def read_load1(path: str | Path = "/proc/loadavg") -> float:
    """Charge 1 minute (1er champ de ``/proc/loadavg``)."""
    with open(path, encoding="utf-8") as f:
        return float(f.read().split()[0])


def get_stats(parc_root: str | Path | None = None) -> dict:
    """Métriques de l'hôte (contrat complet GET /stats). Ne lève jamais.

    ``parc_root=None`` → défauts du service réel : disque via
    ``hub_state.get_disk_usage()`` (``PARC_ROOT``). Un ``parc_root``
    explicite (tests) cible ``shutil.disk_usage`` sur ce chemin, sans
    toucher au global. CPU : échantillon précédent réutilisé ; premier
    appel → deux échantillons espacés de ``_CPU_SAMPLE_DELAY``. Toute
    section en échec → valeurs neutres + ``errors[section]`` + service
    ``degraded`` (clé ``errors`` absente quand tout va bien).
    """
    global _last_cpu_sample
    errors: dict[str, str] = {}
    root = Path(parc_root) if parc_root is not None else None
    disk_path = str(root) if root is not None else str(hub_state.PARC_ROOT)

    try:
        uptime = read_uptime_seconds()
    except Exception as e:
        uptime = 0.0
        errors["uptime"] = str(e)

    cpu_percent = 0.0
    try:
        with _CPU_LOCK:
            sample = read_cpu_sample()
            previous = _last_cpu_sample
            if previous is None:
                _cpu_delay()  # premier appel : 2 échantillons espacés
                previous, sample = sample, read_cpu_sample()
            _last_cpu_sample = sample
            cpu_percent = cpu_percent_between(previous, sample)
    except Exception as e:
        errors["cpu"] = str(e)

    try:
        load1 = read_load1()
    except Exception as e:
        load1 = 0.0
        errors["cpu"] = errors.get("cpu") or str(e)

    memory = _ZERO_MEMORY.copy()
    try:
        memory = read_memory()
    except Exception as e:
        errors["memory"] = str(e)

    disk: dict = {"path": disk_path, "total_gb": 0.0, "used_gb": 0.0,
                  "free_gb": 0.0, "percent": 0.0}
    try:
        if root is None:
            usage = hub_state.get_disk_usage()
        else:
            u = shutil.disk_usage(root)
            usage = hub_state.DiskInfo(
                total=u.total, used=u.used, free=u.free,
                percent_free=round(u.free / u.total * 100, 1)
                if u.total else 0.0)
        disk = {
            "path": disk_path,
            "total_gb": round(usage.total / 1024 ** 3, 1),
            "used_gb": round(usage.used / 1024 ** 3, 1),
            "free_gb": round(usage.free / 1024 ** 3, 1),
            "percent": (round(usage.used / usage.total * 100, 1)
                        if usage.total else 0.0),
        }
    except Exception as e:
        errors["disk"] = str(e)

    payload: dict = {
        "service": "degraded" if errors else "ok",
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "uptime_seconds": uptime,
        "cpu": {"percent": cpu_percent, "cores": os.cpu_count() or 0,
                "load1": load1},
        "memory": memory,
        "disk": disk,
    }
    if errors:
        payload["errors"] = errors
    return payload
