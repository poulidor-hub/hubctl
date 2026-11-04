# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Tests de park_checks — vérifications git partagées (plan unification updates, Task 1).

Chaque scénario monte de vrais dépôts git jetables dans tmp_path : un « méta »
POULIDOR factice contenant HUB_base en submodule, plus des dépôts origin locaux.
"""
import subprocess
from pathlib import Path

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


def _spy_run(captured_key: str, monkeypatch, store: dict) -> None:
    real_run = park_checks.subprocess.run

    def spy(args, **kwargs):
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
    _spy_run("env", monkeypatch, store)
    park_checks._run("git", "fetch", "--prune", "origin", cwd=repo)
    assert (store["env"] or {}).get("GIT_TERMINAL_PROMPT") == "0"


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


class TestCheckAppRepo:
    def test_ok_when_clean_and_aligned(self, tmp_path):
        meta, _, _ = _make_meta_with_submodule(tmp_path)
        app = _init_repo(tmp_path / "meta" / "APPS" / "exemple-app")
        app_origin = _init_repo(tmp_path / "origin_inv")
        _commit(app_origin, "README.md", "app origin")
        _git("remote", "add", "origin", str(app_origin), cwd=app)
        _git("fetch", "origin", cwd=app)
        _git("checkout", "-b", "main", "origin/main", cwd=app)
        finding = park_checks.check_app_repo(app)
        assert finding.ok is True

    def test_fails_on_local_modifications(self, tmp_path):
        meta, _, _ = _make_meta_with_submodule(tmp_path)
        app = _init_repo(tmp_path / "meta" / "APPS" / "exemple-app")
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
        app = _init_repo(meta / "APPS" / "exemple-app")
        _commit(app, "x.txt", "app sans remote")
        findings = park_checks.run_checks(meta, apps=["exemple-app"])
        assert any(f.check == "app.exemple-app" for f in findings)


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
        app = _init_repo(meta / "APPS" / "exemple-app")
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
