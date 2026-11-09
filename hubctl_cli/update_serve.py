# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""`hubctl update serve` — collecte du statut de mise à jour du parc.

Brique hôte du feature « Centre de mises à jour » : état git par composant
(méta + submodules déclarés dans ``.gitmodules``), consommé par la route
GET /update/status (Task 3 — shapes JSON = contrat, spec §5.1).

Contraintes (spec §5.1) :
- stdlib uniquement : réutilise ``park_checks._run`` (git non-interactif
  ``GIT_TERMINAL_PROMPT=0``, timeout HUBCTL_GIT_TIMEOUT) et
  ``park_checks._submodule_paths`` par import — aucune dépendance nouvelle,
  aucune logique git recopiée ;
- un composant en échec ne fait JAMAIS échouer la collecte globale : son
  entrée porte ``error`` et les champs calculables sur les refs locales ;
- cache mémoire TTL ``_STATUS_TTL`` (60 s) : le rafraîchissement UI est
  gratuit ; ``force=True`` (équivalent ``?refresh=1``) contourne le cache.

Shape (contrat) :
- :func:`collect_update_status` → ``{"service": "ok", "checked_at":
  <ISO8601>, "meta": <component>, "components": {rel_path: <component>}}`` ;
- ``<component>`` = ``{"name", "path", "version", "version_target",
  "branch", "commits_behind", "dirty", "update_available", "error"}`` ;
  ``version`` = ``VERSION`` local (à défaut champ ``version`` du
  ``package.json`` pour les composants JS, sinon ``None``) ;
  ``version_target`` = ``git show origin/main:<fichier>`` (lisible sans
  toucher à l'arbre local, ``None`` si absent).
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from hubctl_cli import park_checks

# Durée de vie (secondes) du cache statut : le front rafraîchit souvent,
# un seul cycle de fetch par minute suffit (spec §5.1).
_STATUS_TTL: float = 60.0
_STATUS_CACHE: dict | None = None
_STATUS_TS: float = 0.0


def _read_version(repo: Path, ref: str = "HEAD") -> str | None:
    """Version d'un composant : ``VERSION`` brut, sinon ``package.json``,
    sinon ``None``.

    ``ref="HEAD"`` lit l'arbre local ; toute autre ref (``origin/main``)
    passe par ``git show ref:<fichier>`` — sans toucher à l'arbre local.
    """
    for name in ("VERSION", "package.json"):
        if ref == "HEAD":
            path = repo / name
            if not path.is_file():
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
        else:
            rc, raw, _err = park_checks._run("git", "show", f"{ref}:{name}",
                                             cwd=repo)
            if rc != 0:
                continue
        if name == "package.json":
            try:
                version = json.loads(raw).get("version")
            except (json.JSONDecodeError, AttributeError):
                continue
            if isinstance(version, str) and version:
                return version
            continue
        raw = raw.strip()
        if raw:
            return raw
    return None


def _component_status(parc_root: Path, rel: str) -> dict:
    """Statut git d'un composant (``rel="."`` → méta). Ne lève jamais.

    Échecs isolés dans ``error`` : fetch impossible → on continue sur les
    refs locales (le fetch précédent, jamais de diagnostic destructeur) ;
    sous-repo non initialisé → ``error="not initialized"``.
    """
    repo = parc_root if rel == "." else parc_root / rel
    entry: dict = {
        "name": parc_root.name if rel == "." else Path(rel).name,
        "path": rel,
        "version": None,
        "version_target": None,
        "branch": None,
        "commits_behind": 0,
        "dirty": False,
        "update_available": False,
        "error": None,
    }
    if not (repo / ".git").exists() and not (repo / ".git").is_file():
        entry["error"] = "not initialized"
        return entry

    frc, _fout, _ferr = park_checks._run("git", "fetch", "--prune", "origin",
                                         cwd=repo)
    if frc != 0:
        entry["error"] = "fetch failed"

    rc, out, _ = park_checks._run("git", "rev-parse", "--abbrev-ref", "HEAD",
                                  cwd=repo)
    if rc == 0 and out and out != "HEAD":
        entry["branch"] = out  # HEAD détaché → None (convention park_checks)

    rc, out, _ = park_checks._run("git", "rev-list", "--count",
                                  "HEAD..origin/main", cwd=repo)
    if rc == 0 and out.isdigit():
        entry["commits_behind"] = int(out)
    else:
        # refs absentes (jamais fetché, repo vide) → 0 + error
        entry["error"] = entry["error"] or "origin/main introuvable"

    rc, out, _ = park_checks._run("git", "status", "--porcelain", cwd=repo)
    entry["dirty"] = rc == 0 and bool(out.strip())

    entry["version"] = _read_version(repo)
    entry["version_target"] = _read_version(repo, "origin/main")
    entry["update_available"] = entry["commits_behind"] > 0
    return entry


def collect_update_status(parc_root: Path, *, force: bool = False) -> dict:
    """Statut complet du parc (méta + submodules initialisés), cache TTL.

    ``force=True`` ignore le cache et refait le cycle fetch (route
    ``?refresh=1``). Les submodules déclarés mais jamais initialisés
    (répertoire absent) sont exclus de ``components``.
    """
    global _STATUS_CACHE, _STATUS_TS
    parc_root = Path(parc_root)
    now = time.monotonic()
    if not force and _STATUS_CACHE is not None and (now - _STATUS_TS) < _STATUS_TTL:
        return _STATUS_CACHE

    meta = _component_status(parc_root, ".")
    components: dict[str, dict] = {}
    for rel in sorted(park_checks._submodule_paths(parc_root)):
        if not (parc_root / rel).is_dir():
            continue
        components[rel] = _component_status(parc_root, rel)

    status = {
        "service": "ok",
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "meta": meta,
        "components": components,
    }
    _STATUS_CACHE = status
    _STATUS_TS = now
    return status
