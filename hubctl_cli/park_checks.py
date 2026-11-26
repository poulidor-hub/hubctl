# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Vérifications git partagées du parc (plan « unification des mises à jour »).

Consommé par les pré-checks de ``hubctl update`` et par ``hubctl doctor``.

Conventions :
- Chaque vérification retourne des :class:`Finding` (check, ok, detail,
  remedy) ; ``remedy`` = commande exacte prête à copier, ou ``None``.
- ``ok=False`` = un problème (sortie 1, ``update hub`` refusé). Un constat
  d'INFORMATION (``info=True``, toujours ``ok=True``) est rapporté mais ne
  compte jamais comme un problème et n'est jamais corrigé.
- Deux lectures de HUB_base, décidées par son ÉTAT et rien d'autre : HEAD
  détaché sur le gitlink du méta (ce que pose ``git clone`` + ``submodule
  update``) = un déploiement qui suit les releases du méta ; HEAD attaché =
  un checkout de développement (cf. :func:`check_hub_base`).
- Aucune fonction ne corrige quoi que ce soit d'elle-même : seule
  :func:`apply_safe_fixes` agit, et uniquement sur les catégories sûres
  (HUB_base : réalignement, pointeur submodule, branches mergées).
- Les données non suivies (``.env``, ``secrets/``, ``certs/``, ``storage/``)
  ne sont jamais évaluées ni touchées : la propreté regarde le suivi git.
- Les branches portant des commits non mergés sont structurellement
  refusées par :func:`apply_safe_fixes` (re-derivation via
  :func:`list_merged_local_branches`).
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Défaut du timeout des commandes git réseau (fetch). Le défaut historique de
# 30 s tuait les fetch à froid sur liens lents (retour serveur :
# `Command ['git', 'fetch', '--prune', 'origin'] timed out after 30 seconds`).
# Surchargeable par installation via HUBCTL_GIT_TIMEOUT (secondes).
GIT_TIMEOUT_DEFAULT = 120


def _git_timeout() -> int:
    """Timeout (secondes) des commandes git — HUBCTL_GIT_TIMEOUT, défaut 120."""
    raw = (os.environ.get("HUBCTL_GIT_TIMEOUT") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return GIT_TIMEOUT_DEFAULT
    return value if value > 0 else GIT_TIMEOUT_DEFAULT


@dataclass
class Finding:
    """Résultat d'une vérification : problème éventuel + remède exact.

    ``ok=False`` = un problème (sortie 1, ``update hub`` refusé).
    ``info=True`` = un constat d'information — toujours ``ok=True`` : il est
    rapporté (« mise à jour disponible », « impossible de vérifier les mises
    à jour ») mais ne compte jamais comme un problème et n'est jamais
    corrigé. Son message se suffit à lui-même : ``doctor`` n'affiche le
    champ ``remedy`` que d'un problème.
    """

    check: str
    ok: bool
    detail: str
    remedy: str | None = None
    info: bool = False

    def __post_init__(self) -> None:
        if self.info and not self.ok:
            raise ValueError(
                "un constat d'information est toujours ok=True — il ne compte "
                "jamais comme un problème")


def _info(check: str, detail: str) -> Finding:
    """Constat d'information : vrai constat, jamais un problème."""
    return Finding(check=check, ok=True, detail=detail, info=True)


def _run(*args: str, cwd: Path | None = None, timeout: int | None = None) -> tuple[int, str, str]:
    """Sous-processus git générique → (returncode, stdout, stderr).

    * ``timeout`` : secondes ; ``None`` → :func:`_git_timeout` (défaut 120).
    * **Non interactif** (``GIT_TERMINAL_PROMPT=0``, ``GIT_ASKPASS=echo``) :
      un remote HTTPS sans credential helper échoue immédiatement et de façon
      explicite au lieu d'attendre une saisie jusqu'au timeout (les serveurs
      headless n'ont personne au clavier).
    * Un dépassement de délai est converti en échec (rc 124, stderr
      actionnable) : ``doctor`` et les pré-checks d'``update`` ne plantent
      plus sur réseau lent.
    """
    if timeout is None:
        timeout = _git_timeout()
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_ASKPASS", "echo")
    try:
        r = subprocess.run(list(args), cwd=cwd, capture_output=True,
                           text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, "", (
            f"délai dépassé ({timeout} s) — réseau lent ou remote injoignable ; "
            "réessayez, ou augmentez HUBCTL_GIT_TIMEOUT (secondes)"
        )
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _git_out(*args: str, cwd: Path) -> str | None:
    """stdout d'une commande git, ou None si elle échoue."""
    rc, out, _ = _run(*args, cwd=cwd)
    return out if rc == 0 else None


_SSH_DENIED = re.compile(r"^(?:[\w.-]+@)?([\w.-]+): permission denied",
                         re.IGNORECASE | re.MULTILINE)
_REMOTE_PROGRESS = re.compile(
    r"^remote: (?:enumerating|counting|compressing|receiving|resolving|total"
    r"|writing|found)\b", re.IGNORECASE)


def _significant_line(stderr: str) -> str:
    """Première ligne significative d'un stderr git, ou ``""``.

    La cause d'un échec git est sa PREMIÈRE ligne parlante ; la dernière est
    un conseil générique (« Please make sure you have the correct access
    rights / and the repository exists. ») qui, seul, ne dit rien. Sont
    sautés : lignes vides, avertissements de bruit (``Warning: Permanently
    added …``, ``hint:``) et progression d'un transfert (``remote:
    Enumerating objects…``) — sur un lien lent le transfert démarre avant
    de casser, l'erreur vient après.
    """
    for line in (stderr or "").splitlines():
        line = line.strip()
        if (not line or line.lower().startswith(("warning:", "hint:"))
                or _REMOTE_PROGRESS.match(line)):
            continue
        return line
    return ""


def _fetch_failure_remedy(repo: Path, ferr: str) -> str | None:
    """Remède ciblé quand le fetch échoue sur une authentification.

    - HTTPS sans identifiants (cas réel, serveur client) : remote ``origin``
      en ``https://`` sans credential helper — git attend une saisie au
      clavier qu'un serveur headless ne peut pas fournir. On oriente vers
      SSH ou un helper.
    - SSH sans clé utilisable (``Permission denied (publickey)``) : la clé
      privée autorisée manque sur ce poste, ou n'est pas chargée.
    """
    low = ferr.lower()
    if ("terminal prompts" in low or "could not read username" in low
            or "authentication failed" in low):
        return (f"remote 'origin' en HTTPS sans identifiants : passez-le en SSH "
                f"(git -C {repo} remote set-url origin "
                f"git@github.com:<org>/<repo>.git) ou configurez un "
                f"credential helper HTTPS")
    if "permission denied" in low and "publickey" in low:
        denied = _SSH_DENIED.search(ferr)
        probe = (f"vérifiez avec `ssh -T git@{denied.group(1)}`" if denied
                 else "vérifiez votre accès au dépôt")
        return (f"clé du dépôt privé absente de ce poste : déposez la clé "
                f"privée autorisée dans ~/.ssh (ou chargez-la avec `ssh-add`), "
                f"{probe} ; à défaut passez 'origin' en HTTPS avec un "
                f"credential helper")
    return None


def _current_branch(repo: Path) -> str | None:
    """Branche courante, ou None si HEAD détaché/inexistant."""
    out = _git_out("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
    return None if out in (None, "HEAD") else out


def _detached_head(repo: Path) -> bool | None:
    """HEAD détaché ? ``None`` quand git ne sait pas répondre (dépôt illisible).

    ``True`` : l'état que pose ``git clone`` + ``submodule update`` sur tout
    déploiement ; ``False`` : checkout de développement (branche courante).
    L'indétermination n'est JAMAIS lue comme un déploiement — on retombe sur
    la lecture stricte du checkout de développement.
    """
    rc, out, _ = _run("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
    if rc != 0:
        return None
    return out.strip() in ("", "HEAD")


def _short(sha: str | None) -> str:
    return (sha or "?")[:7]


# Chemin réel de la mise à jour d'un déploiement — README, « Mise à jour d'un
# déploiement » : le MÉTA d'abord (le Hub suit le gitlink du méta), puis
# hubctl. Proposer `./hubctl update hub` seul reviendrait à proposer une
# commande qui ne fait rien tant que le méta n'a pas proposé le nouveau gitlink.
_META_UPDATE_PATH = ("`git fetch upstream && git merge --ff-only upstream/main "
                     "&& git submodule update --init --recursive`, puis "
                     "`./hubctl update hub`")


def _dirty_tracked(repo: Path, path: str | None = None) -> list[str]:
    """Fichiers SUIVIS modifiés (ignore le non-suivi ``??``)."""
    args = ["git", "status", "--porcelain"]
    if path:
        args += ["--", path]
    out = _git_out(*args, cwd=repo)
    if out is None:
        return []
    return [line for line in out.splitlines()
            if line.strip() and not line.lstrip().startswith("??")]


def _submodule_paths(parc_root: Path) -> set[str]:
    """Chemins des sous-modules déclarés dans ``.gitmodules`` du méta."""
    out = _git_out("git", "config", "--file", ".gitmodules", "--get-regexp",
                   r"submodule\..*\.path", cwd=parc_root)
    if not out:
        return set()
    return {line.split()[-1] for line in out.splitlines() if line.strip()}


def meta_unrelated_dirty_files(parc_root: Path) -> list[str]:
    """Fichiers suivis modifiés du méta sans rapport avec l'update.

    (tout sauf le gitlink ``HUB_base``) — le rollback ne doit jamais
    faire de ``reset --hard`` quand cette liste n'est pas vide (C1).
    """
    parc_root = Path(parc_root)
    return [line.split()[-1] for line in _dirty_tracked(parc_root)
            if line.split()[-1] != "HUB_base"]


def check_meta_ready(parc_root: Path) -> list[Finding]:
    """Méta : branche ``main`` + gitlink ``HUB_base`` propre pour un pull."""
    parc_root = Path(parc_root)
    findings: list[Finding] = []

    branch = _current_branch(parc_root)
    findings.append(Finding(
        check="meta.branch",
        ok=branch == "main",
        detail=f"branche courante du méta : {branch or 'HEAD détaché'}",
        remedy=None if branch == "main" else "git -C <méta> checkout main",
    ))

    dirty = _dirty_tracked(parc_root)
    subs = _submodule_paths(parc_root)
    paths = [line.split()[-1] for line in dirty]
    hub_dirty = "HUB_base" in paths
    other_subs = sorted(p for p in paths if p in subs and p != "HUB_base")

    if hub_dirty:
        findings.append(Finding(
            check="meta.gitlink_clean",
            ok=False,
            detail="HUB_base diverge du pointeur enregistré par le méta",
            remedy=f"git -C {parc_root} submodule update --init HUB_base",
        ))
    else:
        plain = [p for p in paths if p not in subs]
        detail = "gitlink HUB_base propre"
        if plain:
            detail += (" ; avertissement : fichiers suivis modifiés sans rapport "
                       "avec l'update : " + ", ".join(plain[:5]))
        findings.append(Finding(check="meta.gitlink_clean", ok=True, detail=detail))

    # I5 : un AUTRE submodule dérivé casserait `submodule update` → bloquant
    if other_subs:
        findings.append(Finding(
            check="meta.submodules_clean",
            ok=False,
            detail="sous-modules dérivés (HEAD local ≠ pointeur méta) : "
                   + ", ".join(other_subs),
            remedy=(f"git -C {parc_root} submodule update --init "
                    + " ".join(other_subs)),
        ))

    return findings


def check_hub_base(parc_root: Path) -> list[Finding]:
    """HUB_base : alignement origin/main + HEAD == gitlink du méta.

    La lecture dépend de l'ÉTAT du sous-module, jamais d'une configuration :

    - **HEAD détaché** — l'état que pose ``git clone`` + ``submodule update``
      sur tout déploiement : un déploiement qui suit les releases du méta.
      Le gitlink du méta est la source de vérité ; le rapport à
      ``origin/main`` est une *information* (« mise à jour disponible »,
      « impossible de vérifier les mises à jour »), jamais un problème : le
      composant avance entre deux releases, c'est normal.
    - **HEAD attaché** — un checkout de développement : sémantique
      historique, retard sur ``origin/main`` = problème.

    Dans les deux cas, l'alignement sur le gitlink (``hub_base.pointer``)
    et les branches de fonctionnalité mergées restent de vrais problèmes.
    """
    parc_root = Path(parc_root)
    hub = parc_root / "HUB_base"
    if not (hub / ".git").exists() and not (hub / ".git").is_file():
        return [Finding(
            check="hub_base.present",
            ok=False,
            detail="HUB_base absent ou non initialisé",
            remedy=f"git -C {parc_root} submodule update --init --recursive",
        )]

    findings: list[Finding] = []
    deployment = _detached_head(hub) is True
    local = _git_out("git", "rev-parse", "HEAD", cwd=hub)

    frc, _, ferr = _run("git", "fetch", "--prune", "origin", cwd=hub)
    if frc != 0:
        remedy = _fetch_failure_remedy(hub, ferr)
        reason = _significant_line(ferr) or "erreur réseau"
        if not deployment:
            # I2 : refs potentiellement périmées → JAMAIS de diagnostic
            # « divergent » (remède destructeur) sur un fetch en échec.
            findings.append(Finding(
                check="hub_base.fetch",
                ok=False,
                detail=f"fetch origin impossible : {reason}",
                remedy=remedy,
            ))
            return findings
        # Déploiement : le parc est cohérent avec le méta, on ignore juste
        # s'il existe plus récent. Information ; le remède, quand il est
        # connu, va dans le message (`doctor` ne montre que celui d'un
        # problème).
        detail = f"impossible de vérifier les mises à jour : {reason}"
        if remedy:
            detail += f" — {remedy}"
        findings.append(_info("hub_base.fetch", detail))
    else:
        findings.append(_aligned_origin(hub, local, deployment))

    recorded = _git_out("git", "ls-tree", "HEAD", "HUB_base", cwd=parc_root)
    recorded_sha = recorded.split()[2] if recorded else None
    findings.append(Finding(
        check="hub_base.pointer",
        ok=local is not None and local == recorded_sha,
        detail="HEAD HUB_base == pointeur méta" if local == recorded_sha
        else "HEAD HUB_base ≠ pointeur méta",
        remedy=None if local == recorded_sha
        else f"git -C {parc_root} submodule update --init HUB_base",
    ))

    # I1 : branches locales mergées restantes (le --fix sait les supprimer).
    # Sur des refs fraîches seulement : un origin/main périmé ne propose
    # aucune suppression (même prudence que I2).
    if frc == 0:
        merged = list_merged_local_branches(hub)
        if merged:
            findings.append(Finding(
                check="hub_base.branches",
                ok=False,
                detail="branches locales mergées restantes : "
                       + ", ".join(merged[:5]),
                remedy=f"hubctl doctor --fix (supprime {', '.join(merged[:5])})",
            ))
    return findings


def _aligned_origin(hub: Path, local: str | None, deployment: bool) -> Finding:
    """Rapport de HUB_base à ``origin/main`` — fetch fait, refs fraîches."""
    remote = _git_out("git", "rev-parse", "origin/main", cwd=hub)

    if local is not None and remote is not None and local == remote:
        return Finding(check="hub_base.aligned_origin", ok=True,
                       detail="HUB_base == origin/main")

    rc, _, _ = _run("git", "merge-base", "--is-ancestor", "HEAD",
                    "origin/main", cwd=hub)

    if not deployment:
        # Checkout de développement : sémantique historique, inchangée.
        if rc == 0:
            return Finding(
                check="hub_base.aligned_origin", ok=False,
                detail="HUB_base en retard sur origin/main",
                remedy=f"git -C {hub} pull --ff-only",
            )
        return Finding(
            check="hub_base.aligned_origin", ok=False,
            detail="historique HUB_base divergent d'origin/main "
                   "(historique réécrit ?)",
            remedy=(f"git -C {hub} fetch origin --prune && "
                    f"git -C {hub} checkout main && "
                    f"git -C {hub} reset --hard origin/main"),
        )

    # Déploiement : le gitlink du méta est la vérité, origin/main n'est
    # qu'un renseignement — jamais un remède qui déplacerait le composant.
    here, there = _short(local), _short(remote)
    if remote is None:
        return _info("hub_base.aligned_origin",
                     "impossible de vérifier les mises à jour : origin/main "
                     "introuvable dans HUB_base")
    if rc == 0:
        return _info("hub_base.aligned_origin",
                     f"mise à jour disponible : HUB_base {here} → {there} "
                     f"(origin/main) — mettre le méta à jour d'abord, à sa "
                     f"racine : {_META_UPDATE_PATH}")
    return _info("hub_base.aligned_origin",
                 f"HUB_base {here} est absent d'origin/main {there} "
                 "(historique du composant réécrit ?) — rien à corriger, le "
                 f"déploiement suit le méta ; pour avancer : {_META_UPDATE_PATH}")


def check_app_repo(app_path: Path) -> Finding:
    """Une app : dépôt présent, propre (suivi), aligné sur origin/main."""
    app_path = Path(app_path)
    name = app_path.name
    if not (app_path / ".git").exists():
        return Finding(check=f"app.{name}", ok=False,
                       detail="dépôt git absent (app posée par copie ?)",
                       remedy=None)

    frc, _, ferr = _run("git", "fetch", "--prune", "origin", cwd=app_path)
    if frc != 0:
        # I2 : pas de diagnostic destructeur sur des refs périmées
        return Finding(check=f"app.{name}", ok=False,
                       detail="fetch origin impossible : "
                              + (_significant_line(ferr) or "erreur réseau"),
                       remedy=_fetch_failure_remedy(app_path, ferr))

    dirty = _dirty_tracked(app_path)
    if dirty:
        return Finding(check=f"app.{name}", ok=False,
                       detail="modifications locales non commitées : "
                              + ", ".join(line.split()[-1] for line in dirty[:5]),
                       remedy=f"commit ou stash dans {app_path}")

    local = _git_out("git", "rev-parse", "HEAD", cwd=app_path)
    remote = _git_out("git", "rev-parse", "origin/main", cwd=app_path)
    if local is None or remote is None:
        return Finding(check=f"app.{name}", ok=False,
                       detail="branche main ou origin/main introuvable",
                       remedy=None)
    if local != remote:
        rc, _, _ = _run("git", "merge-base", "--is-ancestor", "HEAD",
                        "origin/main", cwd=app_path)
        return Finding(check=f"app.{name}", ok=False,
                       detail="en retard/divergent vs origin/main",
                       remedy=f"git -C {app_path} pull --ff-only" if rc == 0
                       else f"git -C {app_path} reset --hard origin/main")
    return Finding(check=f"app.{name}", ok=True, detail="propre et à jour")


def run_update_prechecks(parc_root: Path) -> list[Finding]:
    """Pré-checks du flux ``update hub`` : méta + HUB_base."""
    return check_meta_ready(Path(parc_root)) + check_hub_base(Path(parc_root))


def run_checks(parc_root: Path, apps: list[str]) -> list[Finding]:
    """Pré-checks complets : méta + HUB_base + chaque app demandée."""
    parc_root = Path(parc_root)
    findings = run_update_prechecks(parc_root)
    for slug in apps:
        findings.append(check_app_repo(parc_root / "APPS" / slug))
    return findings


def list_merged_local_branches(repo: Path) -> list[str]:
    """Branches locales (hors branche de référence) dont tous les commits
    sont dans ``origin/main`` — les seules que ``apply_safe_fixes`` accepte
    de supprimer.

    Branche de référence, jamais une « restante » : la courante quand HEAD
    est attaché (checkout de développement) ; ``main`` quand HEAD est
    détaché — un déploiement garde le miroir local d'origin/main posé par le
    clone, ce n'est pas une branche de fonctionnalité à nettoyer.
    """
    repo = Path(repo)
    remote_main = _git_out("git", "rev-parse", "origin/main", cwd=repo)
    if remote_main is None:
        return []
    reference = _current_branch(repo) or "main"
    refs = _git_out("git", "for-each-ref", "refs/heads",
                    "--format", "%(refname:short)", cwd=repo) or ""
    merged: list[str] = []
    for name in [line.strip() for line in refs.splitlines() if line.strip()]:
        if name == reference:
            continue
        rc, _, _ = _run("git", "merge-base", "--is-ancestor", name,
                        "origin/main", cwd=repo)
        if rc == 0:
            merged.append(name)
    return merged


def _confirm(console, message: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if console is None:
        return False  # non interactif : jamais d'action sans accord explicite
    try:
        answer = console.input(f"{message} [o/N] ")
    except Exception:
        return False
    return answer.strip().lower() in ("o", "y", "oui", "yes")


def apply_safe_fixes(findings: list[Finding], parc_root: Path, *,
                     assume_yes: bool, console=None) -> int:
    """Applique les remèdes sûrs des findings en échec, par catégorie.

    Catégories corrigibles : réalignement HUB_base (divergence), pointeur
    submodule (hub_base.pointer / meta.gitlink_clean), branches locales
    mergées de HUB_base (hub_base.branches). Les findings ``app.*`` ne sont
    jamais auto-corrigés (rapport seul). Retourne le nombre d'actions
    appliquées. Une branche non mergée est structurellement refusée.

    Sur un déploiement (HUB_base en HEAD détaché), ni ``checkout main`` ni
    ``reset --hard`` : ils avanceraient le composant au-delà du gitlink du
    méta. Seul le réalignement sur le gitlink s'applique.
    """
    parc_root = Path(parc_root)
    hub = parc_root / "HUB_base"
    failed = {f.check for f in findings if not f.ok}
    applied = 0

    if "meta.branch" in failed:
        # I6 : méta détaché → retour sur main (sans toucher au worktree)
        if _confirm(console, "Repasser le méta sur la branche main ?", assume_yes):
            rc, _, _ = _run("git", "checkout", "main", cwd=parc_root)
            if rc == 0:
                applied += 1

    # Checkout de développement (HEAD attaché, établi) SEULEMENT : sur un
    # déploiement (HEAD détaché, ou illisible) un checkout main + reset
    # --hard avancerait le composant au-delà du gitlink du méta — le
    # réalignement passe par le pointeur ci-dessous.
    if ("hub_base.aligned_origin" in failed and hub.exists()
            and _detached_head(hub) is False):
        if _confirm(console, "Réaligner HUB_base sur origin/main (reset --hard) ?",
                    assume_yes):
            rc, _, _ = _run("git", "checkout", "main", cwd=hub)
            if rc == 0:
                rc, _, _ = _run("git", "reset", "--hard", "origin/main", cwd=hub)
            if rc == 0:
                applied += 1

    if failed & {"hub_base.present", "hub_base.pointer", "meta.gitlink_clean"}:
        if _confirm(console, "Réaligner le submodule HUB_base sur le pointeur méta ?",
                    assume_yes):
            rc, _, _ = _run("git", "-c", "protocol.file.allow=always",
                            "submodule", "update", "--init", "--recursive",
                            cwd=parc_root)
            if rc == 0:
                applied += 1

    if "hub_base.branches" in failed and hub.exists():
        for branch in list_merged_local_branches(hub):
            if not _confirm(console, f"Supprimer la branche mergée « {branch} » ?",
                            assume_yes):
                continue
            rc, _, _ = _run("git", "branch", "-D", branch, cwd=hub)
            if rc == 0:
                applied += 1

    return applied
