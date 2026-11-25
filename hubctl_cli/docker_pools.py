# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Pools d'adresses du daemon Docker — F14.

Sans `default-address-pools`, le daemon n'offre que ≈31 sous-réseaux
(15 × /16 dans 172.16.0.0/12 + 16 × /20 dans 192.168.0.0/16) pour 33 apps
qui créent chacune leur network compose + hub + stacks de test →
« invalid pool request: Pool overlaps with other one on this address
space » de façon récurrente (test PRA VM 13-14/11/2026,
exemple-app).

Plages choisies sans conflit parc :
- 172.16.0.0/12 en /24 (4096 réseaux) — docker0 (172.17.0.0/16) est
  exclu automatiquement du pool par le daemon ;
- 10.200.0.0/14 en /24 (1024 réseaux) — réserve hors des sous-réseaux
  déjà occupés (hub 10.42/16, customs 10.50.x/24, pouly-test 10.99/16,
  SLIRP VM 10.0.2/24, LAN 192.168.x).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

DEFAULT_POOLS = [
    {"base": "172.16.0.0/12", "size": 24},
    {"base": "10.200.0.0/14", "size": 24},
]


def daemon_json_path() -> Path:
    return Path("/etc/docker/daemon.json")


def merge_daemon_config(cfg: dict, pools: list[dict] | None = None) -> dict:
    """Retourne `cfg` avec `default-address-pools` positionné, en préservant
    tout le reste (dns, runtimes, log-driver…). Idempotent."""
    out = dict(cfg)
    out["default-address-pools"] = pools or DEFAULT_POOLS
    return out


def read_config(path: Path | None = None) -> dict:
    """Lit le daemon.json ; {} si absent ou illisible (JSON invalide)."""
    p = Path(path) if path else daemon_json_path()
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def check_pools(path: Path | None = None) -> bool:
    """True si les pools recommandés sont déjà en place."""
    return merge_daemon_config(read_config(path)) == read_config(path)


def write_config(cfg: dict, path: Path | None = None) -> None:
    """Écrit la config (JSON indenté). Nécessite root sur /etc/docker."""
    p = Path(path) if path else daemon_json_path()
    p.write_text(json.dumps(cfg, indent=4) + "\n")


def setup_pools(console=None, *, yes: bool = False, path: Path | None = None) -> int:
    """`hubctl docker setup-pools` : merge les pools recommandés dans le
    daemon.json. Écriture directe si possible ; sinon (root requis)
    affiche la commande à relancer et retourne 1. Idempotent."""
    from rich.console import Console

    con = console or Console()
    cfg_file = Path(path) if path else daemon_json_path()
    current = read_config(cfg_file)
    if check_pools(cfg_file):
        con.print("[green]✓ Pools d'adresses Docker déjà configurés[/green]")
        return 0

    merged = merge_daemon_config(current)
    con.print("Config actuelle :")
    con.print(json.dumps(current, indent=2, sort_keys=True))
    con.print("Config cible :")
    con.print(json.dumps(merged, indent=2, sort_keys=True))
    if not yes:
        try:
            answer = input("Appliquer ? [o/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("o", "y", "oui"):
            con.print("[dim]Abandon.[/dim]")
            return 0

    try:
        write_config(merged, cfg_file)
    except PermissionError:
        con.print("[yellow]Écriture impossible sans root — relancez avec sudo :[/yellow]")
        con.print("[bold]sudo hubctl docker setup-pools[/bold]")
        return 1
    con.print("[green]✓ daemon.json mis à jour — redémarrez Docker pour appliquer :[/green]")
    con.print("[bold]sudo systemctl restart docker[/bold]")
    return 0


def network_prune() -> bool:
    """Purge les networks Docker orphelins (sans conteneur). Appelé en fin
    de `hubctl stop all` : les networks d'apps s'accumulent sinon."""
    proc = subprocess.run(["docker", "network", "prune", "-f"], capture_output=True, text=True)
    return proc.returncode == 0


def docker0_gateway(console=None) -> str:
    """IP de docker0 — la passerelle que les conteneurs voient via
    `host.docker.internal` (extra_hosts host-gateway).

    Le réflexe « 172.17.0.1 » est faux dès que les pools personnalisés
    (DEFAULT_POOLS ci-dessus, F14) ou un daemon.json local déplacent le
    bridge par défaut (ex. 10.200.0.1 sur les ateliers POULIDOR) : l'unit
    rendue se bindait alors sur une IP inexistante → « Service de mise a
    jour hôte injoignable » côté Hub (15/11/2026). Les gabarits packaging/
    portent le placeholder @DOCKER_GATEWAY@, substitué au rendu de l'unit
    (`actions._render_unit`) par cette valeur.

    Lit l'inet de docker0 (`ip -4 -o addr show dev docker0`) ; repli
    172.17.0.1 (comportement historique) avec avertissement si docker0 est
    introuvable — jamais d'exception (le rendu d'unit ne doit pas casser).
    """
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "dev", "docker0"],
            capture_output=True, text=True, timeout=5)
        m = re.search(r"inet (\d+(?:\.\d+){3})", out.stdout)
        if m:
            return m.group(1)
    except Exception:
        pass
    if console is not None:
        try:
            console.print(
                "[yellow]⚠ docker0 introuvable — bind 172.17.0.1 historique "
                "(à ajuster si le bridge par défaut utilise un autre subnet)[/yellow]")
        except Exception:
            pass
    return "172.17.0.1"
