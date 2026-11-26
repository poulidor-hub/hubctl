# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Tests de park_checks — vérifications git partagées (plan unification updates, Task 1).

Chaque scénario monte de vrais dépôts git jetables dans tmp_path : un « méta »
POULIDOR factice contenant HUB_base en submodule, plus des dépôts origin locaux.
"""
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hubctl_cli import park_checks
from hubctl_cli.park_checks import Finding


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", cwd=path)
    _git("config", "user.email", "test@test.local", cwd=path)
    _git("config", "user.name", "test", cwd=path)
    # les dépôts « origin » de test acceptent les push (worktree mis à jour)
    _git("config", "receive.denyCurrentBranch", "updateInstead", cwd=path)
    return path


def _commit(path: Path, name: str, msg: str) -> None:
    (path / name).write_text(f"{msg}\n")
    _git("add", "-A", cwd=path)
    _git("commit", "-m", msg, cwd=path)


def _branch_exists(repo: Path, name: str) -> bool:
    r = subprocess.run(["git", "-C", str(repo), "branch", "--list", name],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())


# ── Timeout configurable & fetch non-interactif (retours lien lent) ─────────


def _spy_run(captured_key: str, monkeypatch, store: dict, *,
             argv_prefix: tuple[str, ...] = ("git", "rev-parse")) -> None:
    """Spy subprocess.run : ne recorder QUE les appels au argv_prefix attendu.

    Hubctl#2 : subprocess.run est le module GLOBAL du process — pendant la
    fenêtre du spy, tout appel venant d'un autre thread (thread résiduel laissé
    par un test antérieur) écrasait store et faisait échouer les tests de
    timeout de façon intermittente en suite complète. Le filtrage par argv
    immunise les tests contre toute pollution étrangère. Passer argv_prefix
    explicite pour un espionnage d'une autre commande (ex. (« git », « fetch »)).
    """
    real_run = park_checks.subprocess.run

    def spy(args, **kwargs):
        if tuple(str(a) for a in args[:len(argv_prefix)]) == argv_prefix:
            store[captured_key] = kwargs.get(captured_key)
        return real_run(args, **kwargs)

    monkeypatch.setattr(park_checks.subprocess, "run", spy)


def test_run_timeout_default_120(monkeypatch, tmp_path):
    """Sans HUBCTL_GIT_TIMEOUT, le défaut est 120 s (et non plus 30 s : lien lent)."""
    repo = _init_repo(tmp_path / "r")
    monkeypatch.delenv("HUBCTL_GIT_TIMEOUT", raising=False)
    store: dict = {}
    _spy_run("timeout", monkeypatch, store)
    park_checks._run("git", "rev-parse", "HEAD", cwd=repo)
    assert store["timeout"] == 120


def test_run_timeout_env_override(monkeypatch, tmp_path):
    repo = _init_repo(tmp_path / "r")
    monkeypatch.setenv("HUBCTL_GIT_TIMEOUT", "300")
    store: dict = {}
    _spy_run("timeout", monkeypatch, store)
    park_checks._run("git", "rev-parse", "HEAD", cwd=repo)
    assert store["timeout"] == 300


def test_run_timeout_env_invalide_retombe_sur_defaut(monkeypatch, tmp_path):
    repo = _init_repo(tmp_path / "r")
    monkeypatch.setenv("HUBCTL_GIT_TIMEOUT", "pas-un-nombre")
    store: dict = {}
    _spy_run("timeout", monkeypatch, store)
    park_checks._run("git", "rev-parse", "HEAD", cwd=repo)
    assert store["timeout"] == 120


def test_run_catches_timeout(monkeypatch, tmp_path):
    """TimeoutExpired → échec explicite (124 + stderr actionnable), jamais crash."""
    repo = _init_repo(tmp_path / "r")

    def slow(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(park_checks.subprocess, "run", slow)
    rc, out, err = park_checks._run("git", "fetch", "--prune", "origin", cwd=repo)
    assert rc != 0
    assert "HUBCTL_GIT_TIMEOUT" in err


def test_run_non_interactif(monkeypatch, tmp_path):
    """GIT_TERMINAL_PROMPT=0 : jamais d'attente de saisie d'identifiants HTTPS."""
    repo = _init_repo(tmp_path / "r")
    store: dict = {}
    _spy_run("env", monkeypatch, store, argv_prefix=("git", "fetch"))
    park_checks._run("git", "fetch", "--prune", "origin", cwd=repo)
    assert (store["env"] or {}).get("GIT_TERMINAL_PROMPT") == "0"


def test_spy_run_ignores_foreign_subprocess_calls(monkeypatch):
    """Hubctl#2 : _spy_run patche subprocess.run GLOBAL du process. Un thread
    résiduel (ex. _state_poll_worker du menu : « docker compose » timeout=20,
    « git status » timeout=120) qui appelle subprocess.run pendant la fenêtre
    écrasait store → échecs intermittents des tests de timeout en suite
    complète. Contrat : seuls les appels au argv_prefix attendu alimentent
    store, tout appel étranger est ignoré."""
    store: dict = {}

    _spy_run("timeout", monkeypatch, store)

    # appel « étranger » pendant la fenêtre (type poll worker docker)
    subprocess.run(["python3", "-c", "pass"], capture_output=True, timeout=20)
    assert store.get("timeout") is None, \
        "un appel étranger (hors argv_prefix) ne doit pas écraser store"
    # l'appel attendu est bien enregistré
    subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, timeout=120)
    assert store.get("timeout") == 120


def test_check_hub_base_timeout_donner_finding(monkeypatch, tmp_path):
    """Fetch en timeout → Finding 'hub_base.fetch', pas d'exception brute."""
    meta, _hub_sub, _hub_origin = _make_meta_with_submodule(tmp_path)

    def slow(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(park_checks.subprocess, "run", slow)
    findings = park_checks.check_hub_base(meta)
    f = [x for x in findings if x.check == "hub_base.fetch"]
    assert f and f[0].ok is False
    assert "délai" in f[0].detail


def test_check_hub_base_https_sans_credentials_remedy(monkeypatch, tmp_path):
    """Remote HTTPS sans identifiants : remède explicite (SSH / credential helper)."""
    meta, _hub_sub, _hub_origin = _make_meta_with_submodule(tmp_path)

    def denied(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 128, "",
            "fatal: could not read Username for 'https://github.com': "
            "terminal prompts disabled")

    monkeypatch.setattr(park_checks.subprocess, "run", denied)
    findings = park_checks.check_hub_base(meta)
    f = [x for x in findings if x.check == "hub_base.fetch"]
    assert f and f[0].ok is False
    assert f[0].remedy, "un remède doit être proposé"
    assert "set-url" in f[0].remedy or "credential helper" in f[0].remedy


# stderr réel d'un fetch SSH sans clé utilisable : la cause est la PREMIÈRE
# ligne, git la fait suivre d'un conseil générique (les deux dernières).
_SSH_KEY_STDERR = (
    "git@github.com: Permission denied (publickey).\n"
    "fatal: Could not read from remote repository.\n"
    "\n"
    "Please make sure you have the correct access rights\n"
    "and the repository exists.\n"
)


def _fetch_fails_with(monkeypatch, stderr: str) -> None:
    """Seul ``git fetch`` échoue (rc 128 + stderr) ; tout autre git est réel."""
    real_run = park_checks.subprocess.run

    def denied(args, **kwargs):
        if tuple(str(a) for a in args[:2]) == ("git", "fetch"):
            return subprocess.CompletedProcess(args, 128, "", stderr)
        return real_run(args, **kwargs)

    monkeypatch.setattr(park_checks.subprocess, "run", denied)


def test_significant_line_is_first_line_that_says_something():
    """La cause d'un échec git est la 1ʳᵉ ligne parlante : on saute les
    lignes vides et les avertissements de bruit, jamais le conseil final."""
    stderr = ("\nWarning: Permanently added 'github.com' (ED25519) to the list "
              "of known hosts.\n"
              + _SSH_KEY_STDERR)
    assert park_checks._significant_line(stderr) == \
        "git@github.com: Permission denied (publickey)."
    assert park_checks._significant_line("") == ""


def test_significant_line_skips_transfer_progress():
    """Lien lent : le transfert commence (progression « remote: ») puis
    casse — la cause est l'erreur, pas la 1ʳᵉ ligne de progression."""
    stderr = ("remote: Enumerating objects: 12, done.\n"
              "remote: Counting objects: 100% (12/12), done.\n"
              "remote: Compressing objects: 100% (6/6), done.\n"
              "error: RPC failed; curl 18 transfer closed with outstanding "
              "read data remaining\n"
              "fatal: expected flush after ref listing\n")
    assert park_checks._significant_line(stderr) == \
        "error: RPC failed; curl 18 transfer closed with outstanding read data remaining"
    # une ligne « remote: » qui n'est pas de la progression reste la cause
    assert park_checks._significant_line(
        "remote: HTTP Basic: Access denied.\nfatal: Authentication failed"
    ) == "remote: HTTP Basic: Access denied."


def test_check_hub_base_fetch_ssh_without_key_remedy(monkeypatch, tmp_path):
    """SSH « Permission denied (publickey) » : message = la cause (pas le
    conseil final « correct access rights ») + remède clé privée."""
    meta, _hub_sub, _hub_origin = _make_meta_with_submodule(tmp_path)
    _fetch_fails_with(monkeypatch, _SSH_KEY_STDERR)
    findings = park_checks.check_hub_base(meta)
    f = [x for x in findings if x.check == "hub_base.fetch"]
    assert f and f[0].ok is False
    assert "Permission denied (publickey)" in f[0].detail
    assert "correct access rights" not in f[0].detail
    assert f[0].remedy, "un remède doit être proposé"
    assert "clé du dépôt privé absente de ce poste" in f[0].remedy
    assert "ssh -T git@github.com" in f[0].remedy


def test_check_hub_base_fetch_failure_shows_real_cause(tmp_path):
    """Vrai fetch en échec (origin inexistant) : la cause métier, pas la
    dernière ligne générique du stderr."""
    meta, hub, _ = _make_meta_with_submodule(tmp_path)
    _git("remote", "set-url", "origin", "/tmp/origin-inexistante-cause-xyz",
         cwd=hub)
    findings = park_checks.check_hub_base(meta)
    f = [x for x in findings if x.check == "hub_base.fetch"]
    assert f and f[0].ok is False
    assert "does not appear to be a git repository" in f[0].detail
    assert "and the repository exists" not in f[0].detail


def test_check_app_repo_fetch_ssh_without_key_remedy(monkeypatch, tmp_path):
    """Même remède SSH et même 1ʳᵉ ligne significative pour une app."""
    meta, _, _ = _make_meta_with_submodule(tmp_path)
    app = _init_repo(meta / "APPS" / "app-exemple")
    app_origin = _init_repo(tmp_path / "origin_inv")
    _commit(app_origin, "README.md", "app")
    _git("remote", "add", "origin", str(app_origin), cwd=app)
    _git("fetch", "origin", cwd=app)
    _git("checkout", "-b", "main", "origin/main", cwd=app)
    _fetch_fails_with(monkeypatch, _SSH_KEY_STDERR)
    finding = park_checks.check_app_repo(app)
    assert finding.ok is False
    assert "Permission denied (publickey)" in finding.detail
    assert "correct access rights" not in finding.detail
    assert finding.remedy
    assert "clé du dépôt privé absente de ce poste" in finding.remedy


def test_information_finding_is_never_a_problem():
    """Un constat d'information porte info=True ET ok=True (contrat) : il
    ne peut pas exister d'information qui compte comme un problème."""
    with pytest.raises(ValueError):
        Finding(check="hub_base.aligned_origin", ok=False, detail="x", info=True)


# Bloc « Mise à jour d'un déploiement » du README : le MÉTA d'abord — un
# déploiement suit le gitlink du méta, `./hubctl update hub` seul ne ferait
# rien tant que le méta ne propose pas le nouveau gitlink.
_META_UPDATE_CMD = ("git fetch upstream && git merge --ff-only upstream/main && "
                    "git submodule update --init --recursive")


def _readme_text() -> str:
    """README du dépôt, blancs normalisés (le bloc y est replié sur 2 lignes)."""
    readme = Path(__file__).resolve().parents[2] / "README.md"
    return " ".join(readme.read_text(encoding="utf-8").split())


def test_meta_update_path_is_the_readme_one(tmp_path):
    """Le chemin proposé est celui du README, et il est verrouillé contre la
    dérive : si le bloc du README change, ce test échoue."""
    assert _META_UPDATE_CMD in _readme_text()
    meta, _hub, hub_origin = _make_deployment(tmp_path)
    _commit(hub_origin, "release.txt", "release du composant")
    detail = next(f for f in park_checks.run_update_prechecks(meta)
                  if f.check == "hub_base.aligned_origin").detail
    assert detail.count("./hubctl update hub") == 1, "une seule commande hubctl, à la fin"
    assert detail.index("./hubctl update hub") > detail.index(_META_UPDATE_CMD)


def _rev_parse(repo: Path, ref: str = "HEAD") -> str:
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", ref],
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _head_branch(repo: Path) -> str | None:
    """Branche courante, None si HEAD détaché (même convention que park_checks)."""
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref",
                        "HEAD"], capture_output=True, text=True, check=True)
    out = r.stdout.strip()
    return None if out == "HEAD" else out


def _make_meta_with_submodule(tmp_path: Path) -> tuple[Path, Path, Path]:
    """origin_hub + origin_meta + clone méta avec HUB_base en submodule aligné.

    Retourne (meta, hub_submodule, hub_origin) — le submodule à manipuler est
    ``meta/HUB_base`` (le checkout réel du méta), pas un clone séparé.
    """
    hub_origin = _init_repo(tmp_path / "origin_hub")
    _commit(hub_origin, "README.md", "hub origin")
    meta_origin = _init_repo(tmp_path / "origin_meta")
    _commit(meta_origin, "README.md", "meta origin")

    meta = _init_repo(tmp_path / "meta")
    _git("remote", "add", "origin", str(meta_origin), cwd=meta)
    _git("fetch", "origin", cwd=meta)
    _git("checkout", "-b", "main", "origin/main", cwd=meta)
    _git("-c", "protocol.file.allow=always", "submodule", "add",
         str(hub_origin), "HUB_base", cwd=meta)
    _git("config", "protocol.file.allow", "always", cwd=meta)
    _git("commit", "-am", "add submodule", cwd=meta)
    return meta, meta / "HUB_base", hub_origin


def _make_deployment(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Déploiement réel : ``git clone`` du méta + ``submodule update --init``.

    Reproduit un clone public frais (VM 26/11) : HUB_base est en HEAD
    DÉTACHÉ sur le gitlink du méta, avec une branche locale ``main`` laissée
    par le clone (miroir d'origin/main). C'est l'état « déploiement qui suit
    les releases du méta » — l'opposé du HEAD ATTACHÉ de
    :func:`_make_meta_with_submodule` (checkout de développement).

    origin_hub compte deux commits (le gitlink est le second) pour pouvoir
    détacher HUB_base sur un commit plus ancien, et porte le
    ``docker-compose.yml`` qu'``update hub`` exige. Retourne
    (méta, HUB_base, hub_origin).
    """
    hub_origin = _init_repo(tmp_path / "origin_hub")
    _commit(hub_origin, "README.md", "hub origin v1")
    _commit(hub_origin, "docker-compose.yml", "hub origin v2")
    meta_origin = _init_repo(tmp_path / "origin_meta")
    _commit(meta_origin, "README.md", "meta origin")
    _git("-c", "protocol.file.allow=always", "submodule", "add",
         str(hub_origin), "HUB_base", cwd=meta_origin)
    _git("commit", "-am", "add submodule", cwd=meta_origin)

    meta = tmp_path / "meta"
    _git("clone", str(meta_origin), str(meta), cwd=tmp_path)
    _git("config", "protocol.file.allow", "always", cwd=meta)
    _git("submodule", "update", "--init", "--recursive", cwd=meta)
    return meta, meta / "HUB_base", hub_origin


class TestCheckMetaReady:
    def test_ok_when_clean(self, tmp_path):
        meta, _, _ = _make_meta_with_submodule(tmp_path)
        findings = park_checks.check_meta_ready(meta)
        assert findings, "au moins un finding attendu"
        assert all(f.ok for f in findings), findings

    def test_fails_on_dirty_hub_gitlink(self, tmp_path):
        meta, hub, hub_origin = _make_meta_with_submodule(tmp_path)
        # commit local au submodule, non enregistré dans le pointeur du méta
        _commit(hub, "new.txt", "drift du submodule")
        findings = park_checks.check_meta_ready(meta)
        gitlink = next(f for f in findings if f.check == "meta.gitlink_clean")
        assert gitlink.ok is False
        assert gitlink.remedy and "submodule update" in gitlink.remedy

    def test_warns_only_on_unrelated_dirty_files(self, tmp_path):
        meta, _, _ = _make_meta_with_submodule(tmp_path)
        (meta / "README.md").write_text("modifié hors update\n")
        findings = park_checks.check_meta_ready(meta)
        gitlink = next(f for f in findings if f.check == "meta.gitlink_clean")
        assert gitlink.ok is True
        assert "avertissement" in gitlink.detail.lower()

    def test_fails_when_detached(self, tmp_path):
        meta, _, _ = _make_meta_with_submodule(tmp_path)
        _git("checkout", "--detach", cwd=meta)
        findings = park_checks.check_meta_ready(meta)
        branch = next(f for f in findings if f.check == "meta.branch")
        assert branch.ok is False


class TestCheckHubBase:
    def test_ok_when_aligned(self, tmp_path):
        meta, _, _ = _make_meta_with_submodule(tmp_path)
        findings = park_checks.check_hub_base(meta)
        assert findings
        assert all(f.ok for f in findings), findings

    def test_detects_divergent_history(self, tmp_path):
        meta, hub, hub_origin = _make_meta_with_submodule(tmp_path)
        # divergence : un commit de chaque côté (cas « historique réécrit »)
        _commit(hub, "local.txt", "côté local")
        _commit(hub_origin, "origin.txt", "côté origin")
        findings = park_checks.check_hub_base(meta)
        aligned = next(f for f in findings if f.check == "hub_base.aligned_origin")
        assert aligned.ok is False
        assert aligned.remedy and "reset --hard origin/main" in aligned.remedy

    def test_detects_pointer_drift(self, tmp_path):
        meta, hub, hub_origin = _make_meta_with_submodule(tmp_path)
        _commit(hub, "new.txt", "drift du submodule")
        findings = park_checks.check_hub_base(meta)
        drift = next(f for f in findings if f.check == "hub_base.pointer")
        assert drift.ok is False
        assert drift.remedy and "submodule update" in drift.remedy

    def test_attached_behind_origin_main_stays_a_problem(self, tmp_path):
        """Checkout de développement (HEAD attaché) : sémantique inchangée —
        retard sur origin/main = problème, remède pull --ff-only, --fix =
        checkout main + reset --hard origin/main."""
        meta, hub, hub_origin = _make_meta_with_submodule(tmp_path)
        _commit(hub_origin, "release.txt", "release du composant")
        assert _head_branch(hub) == "main", "précondition : HEAD attaché"

        findings = park_checks.check_hub_base(meta)
        aligned = next(f for f in findings if f.check == "hub_base.aligned_origin")
        assert aligned.ok is False and aligned.info is False
        assert aligned.detail == "HUB_base en retard sur origin/main"
        assert aligned.remedy == f"git -C {hub} pull --ff-only"

        applied = park_checks.apply_safe_fixes(
            [aligned], parc_root=meta, assume_yes=True, console=None)
        assert applied == 1
        assert _head_branch(hub) == "main"
        assert _rev_parse(hub, "HEAD") == _rev_parse(hub, "origin/main")


class TestDeploymentDetachedHead:
    """Submodule en HEAD détaché sur le gitlink du méta = DÉPLOIEMENT qui
    suit les releases du méta (clone + ``submodule update``). origin/main en
    avance est la situation NORMALE entre deux releases : une information,
    jamais un problème (VM 26/11 : doctor rouge, update hub refusé, --fix en
    boucle sur un clone public frais pourtant sain)."""

    def test_fresh_deployment_is_healthy(self, tmp_path):
        meta, hub, _ = _make_deployment(tmp_path)
        assert _head_branch(hub) is None, "précondition : HEAD détaché"
        assert all(f.ok for f in park_checks.run_update_prechecks(meta))

    def test_origin_main_ahead_is_information(self, tmp_path):
        meta, hub, hub_origin = _make_deployment(tmp_path)
        gitlink = _rev_parse(hub, "HEAD")
        _commit(hub_origin, "release.txt", "release du composant")

        findings = park_checks.run_update_prechecks(meta)
        aligned = next(f for f in findings if f.check == "hub_base.aligned_origin")
        assert aligned.ok is True, "information : n'affecte pas le code de sortie"
        assert aligned.info is True
        assert "mise à jour disponible" in aligned.detail
        assert f"HUB_base {gitlink[:7]} → {_rev_parse(hub, 'origin/main')[:7]}" \
            in aligned.detail
        # le vrai chemin d'un déploiement : le méta d'abord — `update hub`
        # seul ne ferait rien tant que le méta ne propose pas le nouveau
        # gitlink (HUB_base suit les releases du méta)
        assert _META_UPDATE_CMD in aligned.detail
        assert aligned.detail.index(_META_UPDATE_CMD) \
            < aligned.detail.index("./hubctl update hub")
        assert "pull --ff-only" not in (aligned.remedy or "")
        assert all(f.ok for f in findings), findings
        assert all("reset --hard" not in (f.remedy or "") for f in findings), findings

    def test_gitlink_outside_origin_main_is_information(self, tmp_path):
        """Historique du composant réécrit : sur un déploiement le gitlink du
        méta reste la vérité — information avec le même chemin de mise à jour
        (méta d'abord), jamais le remède destructeur « checkout main && reset
        --hard origin/main » du checkout dev."""
        meta, hub, hub_origin = _make_deployment(tmp_path)
        gitlink = _rev_parse(hub, "HEAD")
        _git("commit", "--amend", "-m", "hub origin v2 (réécrit)", cwd=hub_origin)

        findings = park_checks.run_update_prechecks(meta)
        aligned = next(f for f in findings if f.check == "hub_base.aligned_origin")
        assert aligned.ok is True and aligned.info is True
        assert gitlink[:7] in aligned.detail and "réécrit" in aligned.detail
        assert _META_UPDATE_CMD in aligned.detail
        assert aligned.remedy is None
        assert all(f.ok for f in findings), findings

    def test_local_main_left_by_clone_is_not_a_leftover_branch(self, tmp_path):
        meta, hub, hub_origin = _make_deployment(tmp_path)
        _commit(hub_origin, "release.txt", "release du composant")
        assert _branch_exists(hub, "main"), "précondition : miroir posé par le clone"

        assert park_checks.list_merged_local_branches(hub) == []
        assert not [f for f in park_checks.check_hub_base(meta)
                    if f.check == "hub_base.branches"]

    def test_merged_feature_branch_still_reported_when_detached(self, tmp_path):
        meta, hub, hub_origin = _make_deployment(tmp_path)
        # feat-old : mergée dans main côté origin, puis rapportée localement
        _git("checkout", "-b", "feat-old", cwd=hub_origin)
        _commit(hub_origin, "feat.txt", "feat-old")
        _git("checkout", "main", cwd=hub_origin)
        _git("merge", "--no-ff", "feat-old", "-m", "merge feat-old", cwd=hub_origin)
        _git("fetch", "origin", cwd=hub)
        _git("branch", "feat-old", "origin/feat-old", cwd=hub)
        assert _head_branch(hub) is None, "précondition : toujours détaché"

        assert park_checks.list_merged_local_branches(hub) == ["feat-old"]
        findings = park_checks.check_hub_base(meta)
        branches = next(f for f in findings if f.check == "hub_base.branches")
        assert branches.ok is False and "feat-old" in branches.detail
        assert "main" not in branches.detail

        applied = park_checks.apply_safe_fixes(
            [branches], parc_root=meta, assume_yes=True, console=None)
        assert applied == 1
        assert not _branch_exists(hub, "feat-old")
        assert _branch_exists(hub, "main"), \
            "la branche main d'un déploiement n'est jamais supprimée"

    def test_fetch_failure_is_information_not_failure(self, monkeypatch, tmp_path):
        """Fetch impossible sur un déploiement : on ne sait juste pas s'il
        existe plus récent — information (remède inclus), le parc reste
        diagnostiqué sur les faits locaux."""
        meta, hub, _ = _make_deployment(tmp_path)
        _fetch_fails_with(monkeypatch, _SSH_KEY_STDERR)

        findings = park_checks.run_update_prechecks(meta)
        fetch = next(f for f in findings if f.check == "hub_base.fetch")
        assert fetch.ok is True and fetch.info is True
        assert "impossible de vérifier les mises à jour" in fetch.detail
        assert "Permission denied (publickey)" in fetch.detail
        # doctor n'affiche le champ remedy que d'un problème : le remède
        # d'une information doit être lisible dans son message
        assert "clé du dépôt privé absente de ce poste" in fetch.detail
        assert all(f.ok for f in findings), findings
        pointer = next(f for f in findings if f.check == "hub_base.pointer")
        assert pointer.ok is True

    def test_unreadable_head_is_never_read_as_a_deployment(self, monkeypatch,
                                                           tmp_path):
        """État indéterminable (git ne répond pas) ≠ déploiement : on garde
        la lecture stricte (problème), et aucun reset n'est tenté."""
        meta, _hub, _ = _make_deployment(tmp_path)

        def broken(args, **kwargs):
            return subprocess.CompletedProcess(args, 1, "", "git illisible")

        monkeypatch.setattr(park_checks.subprocess, "run", broken)
        findings = park_checks.check_hub_base(meta)
        fetch = next(f for f in findings if f.check == "hub_base.fetch")
        assert fetch.ok is False and fetch.info is False

        stale = Finding(check="hub_base.aligned_origin", ok=False, detail="x")
        assert park_checks.apply_safe_fixes(
            [stale], parc_root=meta, assume_yes=True, console=None) == 0

    def test_apply_safe_fixes_never_resets_a_detached_submodule(self, tmp_path):
        """Garde-fou structurel : un finding « aligned_origin » en échec ne
        déclenche jamais checkout main / reset --hard sur un sous-module
        détaché (il avancerait le composant au-delà du gitlink du méta)."""
        meta, hub, _ = _make_deployment(tmp_path)
        gitlink = _rev_parse(hub, "HEAD")
        stale = Finding(check="hub_base.aligned_origin", ok=False,
                        detail="HUB_base en retard sur origin/main",
                        remedy=f"git -C {hub} pull --ff-only")

        applied = park_checks.apply_safe_fixes(
            [stale], parc_root=meta, assume_yes=True, console=None)
        assert applied == 0
        assert _head_branch(hub) is None
        assert _rev_parse(hub, "HEAD") == gitlink

    def test_apply_safe_fixes_realigns_drifted_deployment_on_gitlink(self, tmp_path):
        """Sous-module détaché qui a dérivé du gitlink : --fix se contente de
        ``submodule update`` (1 action) et un 2ᵉ passage n'a plus rien à faire."""
        meta, hub, hub_origin = _make_deployment(tmp_path)
        gitlink = _rev_parse(hub, "HEAD")
        _commit(hub_origin, "release.txt", "release du composant")
        _git("checkout", "--detach", "HEAD~1", cwd=hub)  # dérive : plus ancien

        problems = [f for f in park_checks.run_update_prechecks(meta) if not f.ok]
        assert {f.check for f in problems} == {"meta.gitlink_clean", "hub_base.pointer"}

        applied = park_checks.apply_safe_fixes(
            problems, parc_root=meta, assume_yes=True, console=None)
        assert applied == 1, "le seul remède est le réalignement sur le gitlink"
        assert _head_branch(hub) is None, "un déploiement reste en HEAD détaché"
        assert _rev_parse(hub, "HEAD") == gitlink

        again = [f for f in park_checks.run_update_prechecks(meta) if not f.ok]
        assert park_checks.apply_safe_fixes(
            again, parc_root=meta, assume_yes=True, console=None) == 0
        assert _rev_parse(hub, "HEAD") == gitlink


class TestCheckAppRepo:
    def test_ok_when_clean_and_aligned(self, tmp_path):
        meta, _, _ = _make_meta_with_submodule(tmp_path)
        app = _init_repo(tmp_path / "meta" / "APPS" / "app-exemple")
        app_origin = _init_repo(tmp_path / "origin_inv")
        _commit(app_origin, "README.md", "app origin")
        _git("remote", "add", "origin", str(app_origin), cwd=app)
        _git("fetch", "origin", cwd=app)
        _git("checkout", "-b", "main", "origin/main", cwd=app)
        finding = park_checks.check_app_repo(app)
        assert finding.ok is True

    def test_fails_on_local_modifications(self, tmp_path):
        meta, _, _ = _make_meta_with_submodule(tmp_path)
        app = _init_repo(tmp_path / "meta" / "APPS" / "app-exemple")
        app_origin = _init_repo(tmp_path / "origin_inv")
        _commit(app_origin, "README.md", "app origin")
        _git("remote", "add", "origin", str(app_origin), cwd=app)
        _git("fetch", "origin", cwd=app)
        _git("checkout", "-b", "main", "origin/main", cwd=app)
        (app / "README.md").write_text("modifié\n")
        finding = park_checks.check_app_repo(app)
        assert finding.ok is False
        assert finding.remedy


class TestMergedBranchesAndFixes:
    def test_list_merged_local_branches_excludes_unmerged(self, tmp_path):
        hub_origin = _init_repo(tmp_path / "origin_hub2")
        _commit(hub_origin, "a.txt", "a")
        repo = _init_repo(tmp_path / "repo_branches")
        _git("remote", "add", "origin", str(hub_origin), cwd=repo)
        _git("fetch", "origin", cwd=repo)
        _git("checkout", "-b", "main", "origin/main", cwd=repo)

        # feat-a : créée, mergée dans main, poussée → contenue dans origin/main
        _git("checkout", "-b", "feat-a", cwd=repo)
        _commit(repo, "feat-a.txt", "feat-a")
        _git("checkout", "main", cwd=repo)
        _git("merge", "--no-ff", "feat-a", "-m", "merge feat-a", cwd=repo)
        _git("push", "origin", "main", cwd=repo)
        # feat-b : commit non poussé → PAS contenue dans origin/main
        _git("checkout", "-b", "feat-b", cwd=repo)
        _commit(repo, "feat-b.txt", "feat-b")
        _git("checkout", "main", cwd=repo)

        merged = park_checks.list_merged_local_branches(repo)
        assert "feat-a" in merged
        assert "feat-b" not in merged
        assert "main" not in merged  # branche courante jamais listée

    def test_apply_safe_fixes_deletes_merged_branch(self, tmp_path):
        meta, hub, hub_origin = _make_meta_with_submodule(tmp_path)
        # feat-a dans HUB_base : créée, mergée dans main, poussée → contenue
        _git("checkout", "-b", "feat-a", cwd=hub)
        _commit(hub, "feat-a.txt", "feat-a")
        _git("checkout", "main", cwd=hub)
        _git("merge", "--no-ff", "feat-a", "-m", "merge feat-a", cwd=hub)
        _git("push", "origin", "main", cwd=hub)

        findings = [Finding(check="hub_base.branches", ok=False,
                            detail="branches locales mergées restantes",
                            remedy="supprimer les branches locales mergées")]
        applied = park_checks.apply_safe_fixes(findings, parc_root=meta,
                                               assume_yes=True, console=None)
        assert applied == 1
        assert not _branch_exists(hub, "feat-a")

    def test_apply_safe_fixes_refuses_unmerged_branch(self, tmp_path):
        meta, hub, hub_origin = _make_meta_with_submodule(tmp_path)
        # feat-b : commit local non poussé → PAS contenue dans origin/main
        _git("checkout", "-b", "feat-b", cwd=hub)
        _commit(hub, "feat-b.txt", "feat-b NON mergée")
        _git("checkout", "main", cwd=hub)

        findings = [Finding(check="hub_base.branches", ok=False,
                            detail="branches locales mergées restantes",
                            remedy="supprimer les branches locales mergées")]
        applied = park_checks.apply_safe_fixes(findings, parc_root=meta,
                                               assume_yes=True, console=None)
        assert applied == 0
        assert _branch_exists(hub, "feat-b"), "branche non mergée intacte"


class TestRunChecks:
    def test_run_update_prechecks_combines_meta_and_hub(self, tmp_path):
        meta, _, _ = _make_meta_with_submodule(tmp_path)
        findings = park_checks.run_update_prechecks(meta)
        checks = {f.check for f in findings}
        assert "meta.branch" in checks
        assert "hub_base.aligned_origin" in checks

    def test_run_checks_includes_apps(self, tmp_path):
        meta, _, _ = _make_meta_with_submodule(tmp_path)
        app = _init_repo(meta / "APPS" / "app-exemple")
        _commit(app, "x.txt", "app sans remote")
        findings = park_checks.run_checks(meta, apps=["app-exemple"])
        assert any(f.check == "app.app-exemple" for f in findings)


class TestReviewFixes:
    """Findings de la revue finale (C1, I1, I2, I5, I6)."""

    def test_check_hub_base_flags_merged_branches(self, tmp_path):
        meta, hub, _ = _make_meta_with_submodule(tmp_path)
        _git("checkout", "-b", "feat-a", cwd=hub)
        _commit(hub, "fa.txt", "feat-a")
        _git("checkout", "main", cwd=hub)
        _git("merge", "--no-ff", "feat-a", "-m", "merge feat-a", cwd=hub)
        _git("push", "origin", "main", cwd=hub)

        findings = park_checks.check_hub_base(meta)
        branches = [f for f in findings if f.check == "hub_base.branches"]
        assert branches and branches[0].ok is False
        assert "feat-a" in branches[0].detail

    def test_check_hub_base_fetch_failure_is_fail_closed(self, tmp_path):
        """I2 : fetch impossible → pas de diagnostic « divergent » destructeur."""
        meta, hub, _ = _make_meta_with_submodule(tmp_path)
        _git("remote", "set-url", "origin", "/tmp/origin-inexistante-xyz", cwd=hub)
        findings = park_checks.check_hub_base(meta)
        fetch = [f for f in findings if f.check == "hub_base.fetch"]
        assert fetch and fetch[0].ok is False and fetch[0].remedy is None
        assert all("reset --hard" not in (f.remedy or "") for f in findings), findings

    def test_check_app_repo_fetch_failure_is_fail_closed(self, tmp_path):
        meta, _, _ = _make_meta_with_submodule(tmp_path)
        app = _init_repo(meta / "APPS" / "app-exemple")
        app_origin = _init_repo(tmp_path / "origin_inv")
        _commit(app_origin, "README.md", "app")
        _git("remote", "add", "origin", str(app_origin), cwd=app)
        _git("fetch", "origin", cwd=app)
        _git("checkout", "-b", "main", "origin/main", cwd=app)
        _git("remote", "set-url", "origin", "/tmp/origin-inexistante-inv", cwd=app)
        finding = park_checks.check_app_repo(app)
        assert finding.ok is False
        assert finding.remedy is None
        assert "fetch" in finding.detail.lower()

    def test_check_meta_ready_fails_on_other_submodule_dirty(self, tmp_path):
        """I5 : un autre submodule (HUBCSS…) dérivé doit bloquer, pas avertir."""
        meta, _, _ = _make_meta_with_submodule(tmp_path)
        css_origin = _init_repo(tmp_path / "origin_css")
        _commit(css_origin, "README.md", "css")
        _git("-c", "protocol.file.allow=always", "submodule", "add",
             str(css_origin), "HUBCSS", cwd=meta)
        _git("commit", "-am", "add HUBCSS", cwd=meta)
        _commit(meta / "HUBCSS", "drift.txt", "dérive locale HUBCSS")

        findings = park_checks.check_meta_ready(meta)
        sub = [f for f in findings if f.check == "meta.submodules_clean"]
        assert sub and sub[0].ok is False
        assert "HUBCSS" in sub[0].detail

    def test_apply_safe_fixes_restores_detached_meta(self, tmp_path):
        """I6 : méta détaché → --fix ramène sur main."""
        meta, _, _ = _make_meta_with_submodule(tmp_path)
        _git("checkout", "--detach", cwd=meta)
        findings = park_checks.check_meta_ready(meta)
        applied = park_checks.apply_safe_fixes(findings, parc_root=meta,
                                               assume_yes=True, console=None)
        assert applied >= 1
        branch = subprocess.run(
            ["git", "-C", str(meta), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True).stdout.strip()
        assert branch == "main"

    def test_meta_unrelated_dirty_files_lists_them(self, tmp_path):
        meta, _, _ = _make_meta_with_submodule(tmp_path)
        (meta / "README.md").write_text("modifié\n")
        assert park_checks.meta_unrelated_dirty_files(meta) == ["README.md"]


# ── Configuration non appliquée : HUB_base/.env plus récent que hub-api ─────
# `compose restart` (hub.sh restart) relance les processus SANS relire le
# .env (VM 26/11) : un .env modifié après le dernier démarrage du Hub n'est
# jamais appliqué, et rien ne le signalait.

def _env_findings(meta: Path) -> list[Finding]:
    # Par run_checks (chemin de `doctor`) : le contrôle lit le Docker de la
    # machine, il n'entre pas dans les préchecks d'`update hub`.
    return [f for f in park_checks.run_checks(meta, [])
            if f.check == "hub_base.env_applied"]


def test_env_newer_than_hub_start_is_an_information(tmp_path, monkeypatch):
    meta, hub, _origin = _make_deployment(tmp_path)
    env = hub / ".env"
    env.write_text("HUB_HOST=x\n")
    monkeypatch.setattr(park_checks, "_hub_api_started_at",
                        lambda: datetime.now(timezone.utc) - timedelta(hours=1))
    future = time.time() + 300
    os.utime(env, (future, future))

    findings = _env_findings(meta)
    assert len(findings) == 1
    f = findings[0]
    assert f.ok and f.info, "une configuration non appliquée est une information, pas un problème"
    assert "./hubctl restart" in f.detail
    assert f.remedy is None


def test_env_older_than_hub_start_is_silent(tmp_path, monkeypatch):
    meta, hub, _origin = _make_deployment(tmp_path)
    env = hub / ".env"
    env.write_text("HUB_HOST=x\n")
    monkeypatch.setattr(park_checks, "_hub_api_started_at", lambda: datetime.now(timezone.utc))
    old = time.time() - 7200
    os.utime(env, (old, old))
    assert _env_findings(meta) == []


def test_env_check_skipped_when_hub_not_running(tmp_path, monkeypatch):
    meta, hub, _origin = _make_deployment(tmp_path)
    (hub / ".env").write_text("HUB_HOST=x\n")
    monkeypatch.setattr(park_checks, "_hub_api_started_at", lambda: None)  # Docker absent / Hub arrêté
    assert _env_findings(meta) == []


def test_env_newer_than_container_but_older_than_last_restart_is_silent(tmp_path, monkeypatch):
    """.env modifié après le démarrage du conteneur, mais AVANT le dernier
    `hubctl restart` : la configuration est appliquée (rien à recréer) — silence."""
    meta, hub, _origin = _make_deployment(tmp_path)
    env = hub / ".env"
    env.write_text("HUB_HOST=x\n")
    now = time.time()
    monkeypatch.setattr(park_checks, "_hub_api_started_at",
                        lambda: datetime.fromtimestamp(now - 3600, tz=timezone.utc))
    monkeypatch.setattr(park_checks, "_last_restart_at",
                        lambda: datetime.fromtimestamp(now - 60, tz=timezone.utc))
    before = now - 1800
    os.utime(env, (before, before))
    assert _env_findings(meta) == []


def test_env_newer_than_last_restart_still_reported(tmp_path, monkeypatch):
    """.env modifié APRÈS le dernier restart : toujours signalé."""
    meta, hub, _origin = _make_deployment(tmp_path)
    env = hub / ".env"
    env.write_text("HUB_HOST=x\n")
    now = time.time()
    monkeypatch.setattr(park_checks, "_hub_api_started_at",
                        lambda: datetime.fromtimestamp(now - 3600, tz=timezone.utc))
    monkeypatch.setattr(park_checks, "_last_restart_at",
                        lambda: datetime.fromtimestamp(now - 1800, tz=timezone.utc))
    future = now + 300
    os.utime(env, (future, future))
    assert len(_env_findings(meta)) == 1
