"""E2E VM 26/11 : `hubctl init` échouait à 120 s sur le build `up-prod` du
premier boot (VM 2 vCPU : le premier `docker compose up -d` build les images,
> 120 s) → « ✗ Échec du build/démarrage du Hub » alors que le build continuait
orphelin et finissait healthy. Le nouvel utilisateur était accueilli par une
erreur sur le premier start.

Fix attendu :
- `hub_make` applique un budget généreux et surchargeable
  (HUBCTL_BUILD_TIMEOUT, défaut 1800 s) aux cibles de build du Hub ;
- l'init attend le health check avec un plafond remonté et surchargeable
  (HUBCTL_HEALTH_TIMEOUT, défaut 600 s) ;
- sur timeout (rc 124), la guidance est actionable (relancer ./hubctl init
  converge), pas une simple ligne d'échec.

Suite (même E2E) : cette guidance était fausse. `.env` existant + pas de
`--force` → `_generate_dotenv` refusait (non-TTY : « .env existe déjà …
utilisez --force » ; TTY : prompt « Écraser le .env existant ? ») → rc 1.
Même symptôme pour `install.sh` relancé Hub arrêté : « ✗ L'initialisation du
Hub a échoué », alors que le script se dit « Idempotent : relançable ».

Fix attendu :
- `.env` présent + pas de `--force` → REPRISE : `.env` inchangé (octets),
  aucun prompt même avec TTY, ni nettoyage des conteneurs ni re-sync du mot
  de passe DB ; `up-prod` (budget build de `hub_make`) + health (600 s) ;
  étapes idempotentes conservées ; résumé construit depuis le `.env` EXISTANT
  + ligne « reprise » ;
- `.env` absent → installation complète, inchangée ;
- `--force` → régénération, inchangée, précédée d'un avertissement explicite.
"""
import io

import pytest
from rich.console import Console

from hubctl_cli import actions, hub_state


def _capture_run_shell(monkeypatch, rc=0):
    calls = []

    def fake_run_shell(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        return rc

    monkeypatch.setattr(actions.utils, "run_shell", fake_run_shell)
    return calls


def _bootstrap_env(monkeypatch, tmp_path, hub_make_rc=0):
    """Environnement bootstrap déterministe (style test_main.py)."""
    hub = tmp_path / "HUB_base"
    hub.mkdir(parents=True)
    (hub / "docker-compose.yml").write_text("services:\n  api:\n    image: test\n")
    (hub / "Makefile").write_text("up-prod:\n\techo up\n")
    monkeypatch.setattr(hub_state, "HUB_DIR", hub)
    monkeypatch.setattr(hub_state, "PARC_ROOT", tmp_path)
    monkeypatch.setattr(hub_state, "APPS_DIR", tmp_path / "APPS")
    monkeypatch.setattr(actions.hub_state, "active_apps_dir", lambda: tmp_path / "APPS")
    monkeypatch.setattr(actions, "_sync_db_password_if_needed", lambda *a, **kw: None)
    monkeypatch.setattr(actions, "_install_shell_marker", lambda *a, **kw: None)
    monkeypatch.setattr(actions, "_ensure_atelier_service", lambda *a, **kw: None)
    monkeypatch.setattr(actions, "_ensure_update_service", lambda *a, **kw: None)
    monkeypatch.setattr(actions, "_cleanup_existing_hub_containers", lambda *a, **kw: None)
    make_calls = {}

    def fake_hub_make(target, *, dry_run, console=None, timeout=None):
        make_calls["target"] = target
        make_calls["timeout"] = timeout
        return hub_make_rc

    monkeypatch.setattr(actions, "hub_make", fake_hub_make)
    health_calls = {}

    def fake_health(target, console=None, timeout=60, port_override=None):
        health_calls["timeout"] = timeout
        return True

    monkeypatch.setattr(actions, "_health_check", fake_health)
    return make_calls, health_calls


def test_up_prod_gets_build_budget(monkeypatch):
    """Cible de build → budget généreux (1800 s), pas le défaut 120 s."""
    calls = _capture_run_shell(monkeypatch)
    rc = actions.hub_make("up-prod", dry_run=False, console=Console())
    assert rc == 0
    cmd, kwargs = calls[-1]
    assert cmd == ["make", "-C", str(hub_state.HUB_DIR), "up-prod"]
    assert kwargs.get("timeout") == 1800


def test_build_timeout_env_override(monkeypatch):
    monkeypatch.setenv("HUBCTL_BUILD_TIMEOUT", "60")
    calls = _capture_run_shell(monkeypatch)
    actions.hub_make("up-prod", dry_run=False, console=Console())
    assert calls[-1][1].get("timeout") == 60


def test_non_build_target_keeps_historic_default(monkeypatch):
    calls = _capture_run_shell(monkeypatch)
    actions.hub_make("status", dry_run=False, console=Console())
    assert calls[-1][1].get("timeout", 120) == 120


def test_explicit_timeout_wins_over_budget(monkeypatch):
    calls = _capture_run_shell(monkeypatch)
    actions.hub_make("build", dry_run=False, console=Console(), timeout=5)
    assert calls[-1][1].get("timeout") == 5


def test_bootstrap_health_check_gets_health_budget(monkeypatch, tmp_path):
    make_calls, health_calls = _bootstrap_env(monkeypatch, tmp_path)
    rc = actions.action_bootstrap(Console(file=io.StringIO(), width=120),
                                  force=True, dry_run=False)
    assert rc == 0
    assert make_calls["target"] == "up-prod"
    # l'init ne passe PAS de timeout explicite à hub_make : le budget build
    # s'applique à l'intérieur de hub_make (test ci-dessus)
    assert make_calls["timeout"] is None
    assert health_calls["timeout"] == 600


def test_bootstrap_build_timeout_gives_guidance(monkeypatch, tmp_path):
    make_calls, _ = _bootstrap_env(monkeypatch, tmp_path, hub_make_rc=124)
    out = io.StringIO()
    rc = actions.action_bootstrap(Console(file=out, width=120),
                                  force=True, dry_run=False)
    assert rc == 1
    output = out.getvalue()
    assert "Relancez" in output and "hubctl init" in output, (
        "sur timeout de build, la guidance doit indiquer que relancer "
        "./hubctl init converge (build orphelin continué)"
    )


# ── Reprise sans --force quand le .env existe déjà (E2E VM 26/11, suite) ──

# .env d'une installation EXISTANTE : valeurs volontairement non par défaut
# (admin, hôte, ports) — prouve que le résumé de reprise est construit depuis
# le fichier et non depuis le générateur. Tous les secrets sont fictifs.
_EXISTING_ENV = (
    "# Hub POULIDOR — généré automatiquement par hubctl init\n"
    "# 2026-11-26T10:00:00\n"
    "\n"
    "POSTGRES_USER=hubadmin\n"
    "POSTGRES_PASSWORD=KeepMePgPass1\n"
    "POSTGRES_DB=hub_auth\n"
    "DATABASE_URL=postgresql+asyncpg://hubadmin:KeepMePgPass1@db:5432/hub_auth\n"
    "\n"
    "ADMIN_USERNAME=patrice\n"
    "ADMIN_PASSWORD=KeepMeAdmin1\n"
    "ADMIN_EMAIL=patrice@local.lan\n"
    "\n"
    "HUB_API_PORT=8000\n"
    "HUB_HOST=10.0.2.15\n"
    "HUB_FRONTEND_PORT=8080\n"
    "HUB_HTTPS_PORT=8443\n"
    "\n"
    "SECRETS_ENCRYPTION_KEY=cle-fernet-existante\n"
    "HUB_INTERNAL_SA_CLIENT_SECRET=KeepMeSaSecret\n"
    "APPS_HOST_PATH=/srv/poulidor/APPS\n"
)

# Étapes de l'init traçées par `_bootstrap_steps`.
_STEPS = (
    "_ensure_jwt_keys",
    "_cleanup_existing_hub_containers",
    "_sync_db_password_if_needed",
    "_install_shell_marker",
    "_ensure_atelier_service",
    "_ensure_update_service",
)


def _bootstrap_steps(monkeypatch, tmp_path, *, env_text=None):
    """`_bootstrap_env` + traçage de chaque étape de l'init.

    `env_text` écrit un `.env` EXISTANT avant l'init (None = installation
    fraîche). Retourne `state` : compteurs par étape (`state["calls"]`), et
    des pilotes modifiables ENTRE deux runs (`state["make_rc"]`,
    `state["healthy"]`) ; `state["make"]` / `state["health"]` tracent les
    appels à `hub_make` (cible, timeout) et `_health_check` (timeout).

    Toute étape à effet de bord réel est stubbée — dont
    `_cleanup_existing_hub_containers`, dont le `docker rm -f` par nom
    toucherait les VRAIS conteneurs hub-*.
    """
    _bootstrap_env(monkeypatch, tmp_path)
    hub = hub_state.HUB_DIR
    if env_text is not None:
        (hub / ".env").write_text(env_text)
    state = {
        "calls": {name: 0 for name in _STEPS},
        "make_rc": 0,
        "healthy": True,
        "make": [],    # (cible, timeout) reçus par hub_make
        "health": [],  # timeout reçu par _health_check
    }

    def _step(name):
        def _stub(*a, **kw):
            state["calls"][name] += 1
        return _stub

    for name in _STEPS:
        monkeypatch.setattr(actions, name, _step(name))
    monkeypatch.setattr(hub_state, "check_docker", lambda: True)
    # IP détectée VOLONTAIREMENT différente du HUB_HOST du .env existant :
    # si elle apparaît dans le résumé, celui-ci n'a pas lu le fichier.
    monkeypatch.setattr(actions, "_detect_host_ip", lambda: "192.0.2.99")

    def fake_hub_make(target, *, dry_run, console=None, timeout=None):
        state["make"].append((target, timeout))
        return state["make_rc"]

    def fake_health(target, console=None, timeout=60, port_override=None):
        state["health"].append(timeout)
        return state["healthy"]

    monkeypatch.setattr(actions, "hub_make", fake_hub_make)
    monkeypatch.setattr(actions, "_health_check", fake_health)
    return state


def _run_bootstrap(**kwargs):
    out = io.StringIO()
    rc = actions.action_bootstrap(Console(file=out, width=120), **kwargs)
    return rc, out.getvalue()


def test_relaunch_after_build_timeout_converges_without_force(monkeypatch, tmp_path):
    """Le bug E2E bout en bout : 1ᵉʳ boot en timeout, puis la relance telle
    que la guidance l'indique (sans --force) doit converger, pas échouer."""
    state = _bootstrap_steps(monkeypatch, tmp_path)
    env_file = hub_state.HUB_DIR / ".env"

    state["make_rc"] = 124
    rc, output = _run_bootstrap(force=False, dry_run=False)
    assert rc == 1
    assert "Relancez" in output and "hubctl init" in output
    assert env_file.exists()
    fingerprint = env_file.read_bytes()

    state["make_rc"] = 0
    rc, output = _run_bootstrap(force=False, dry_run=False)
    assert rc == 0, f"la relance doit converger, pas refuser : {output}"
    assert env_file.read_bytes() == fingerprint


def test_init_resumes_existing_dotenv_without_force(monkeypatch, tmp_path):
    """.env existant + pas de --force → reprise : .env intact, ni nettoyage
    ni re-sync DB, up-prod + health, étapes idempotentes, résumé « reprise »."""
    state = _bootstrap_steps(monkeypatch, tmp_path, env_text=_EXISTING_ENV)
    env_file = hub_state.HUB_DIR / ".env"
    before = env_file.read_bytes()
    mtime_before = env_file.stat().st_mtime_ns

    rc, output = _run_bootstrap(force=False, dry_run=False)

    assert rc == 0
    # Empreinte strictement inchangée (octets + mtime : le fichier n'a pas
    # été réécrit, même à contenu identique).
    assert env_file.read_bytes() == before
    assert env_file.stat().st_mtime_ns == mtime_before
    # Build & health : mêmes budgets qu'une installation fraîche.
    assert state["make"] == [("up-prod", None)]  # budget build dans hub_make
    assert state["health"] == [600]
    # Pas de nettoyage des conteneurs, pas de re-sync du mot de passe DB.
    assert state["calls"]["_cleanup_existing_hub_containers"] == 0
    assert state["calls"]["_sync_db_password_if_needed"] == 0
    # Les étapes idempotentes restent exécutées.
    assert state["calls"]["_ensure_jwt_keys"] == 1
    assert state["calls"]["_install_shell_marker"] == 1
    assert state["calls"]["_ensure_atelier_service"] == 1
    assert state["calls"]["_ensure_update_service"] == 1
    # Résumé construit depuis le .env EXISTANT.
    assert "reprise" in output.lower()
    assert "conserv" in output.lower()
    assert "Login : patrice" in output
    assert "https://10.0.2.15:8443" in output
    assert "http://10.0.2.15:8080" in output
    assert "http://10.0.2.15:8000" in output
    assert "192.0.2.99" not in output  # IP détectée ≠ .env
    # Aucun secret du .env ne fuite, pas de refus ni d'avertissement --force.
    assert "KeepMe" not in output
    assert "Abandon" not in output
    assert "utilisez --force" not in output


def test_init_resume_never_prompts_even_with_tty(monkeypatch, tmp_path):
    """Même avec un TTY : aucun prompt « Écraser le .env existant ? »."""
    import builtins
    import sys
    _bootstrap_steps(monkeypatch, tmp_path, env_text=_EXISTING_ENV)
    env_file = hub_state.HUB_DIR / ".env"
    before = env_file.read_bytes()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    prompts = []
    # Si un prompt survenait, répondre « oui » : l'ancien code écraserait le
    # .env → le test échoue sur l'empreinte, pas sur un EOF.
    monkeypatch.setattr(builtins, "input",
                        lambda *a, **kw: prompts.append(a) or "o")

    rc, output = _run_bootstrap(force=False, dry_run=False)

    assert prompts == []
    assert rc == 0
    assert env_file.read_bytes() == before
    assert "Écraser" not in output


def test_init_force_regenerates_dotenv_with_explicit_warning(monkeypatch, tmp_path):
    """--force → sémantique actuelle (régénération complète, nettoyage,
    re-sync DB), précédée d'un avertissement explicite et vrai."""
    state = _bootstrap_steps(monkeypatch, tmp_path, env_text=_EXISTING_ENV)
    env_file = hub_state.HUB_DIR / ".env"
    # Espionne le générateur pour vérifier que l'avertissement est affiché
    # AVANT la régénération (sortie déjà produite au moment de l'appel).
    seen = {}
    real_generate = actions._generate_dotenv

    def spying_generate(console, **kwargs):
        seen["output_before"] = console.file.getvalue()
        seen["old_content"] = env_file.read_text()
        return real_generate(console, **kwargs)

    monkeypatch.setattr(actions, "_generate_dotenv", spying_generate)

    rc, output = _run_bootstrap(force=True, dry_run=False)

    assert rc == 0
    assert "Fernet" in seen["output_before"]
    assert "KeepMe" in seen["old_content"]  # l'ancien .env était encore en place
    content = env_file.read_text()
    # Régénération : anciens secrets partis (dont la clé Fernet), valeurs du
    # générateur en place.
    assert "KeepMe" not in content
    assert "cle-fernet-existante" not in content
    assert "ADMIN_USERNAME=admin\n" in content
    assert "HUB_HOST=192.0.2.99\n" in content
    assert state["calls"]["_cleanup_existing_hub_containers"] == 1
    assert state["calls"]["_sync_db_password_if_needed"] == 1
    assert state["calls"]["_ensure_jwt_keys"] == 1
    assert state["make"] == [("up-prod", None)]
    # Avertissement AVANT la régénération, disant vrai sur ce qui change.
    assert "Fernet" in output
    assert "régénéré" in output.lower()
    assert "JWT" in output and "conserv" in output.lower()
    assert "reprise" not in output.lower()


def test_init_without_dotenv_keeps_full_install(monkeypatch, tmp_path):
    """.env absent → installation complète, inchangée (pas de « reprise »)."""
    state = _bootstrap_steps(monkeypatch, tmp_path)
    env_file = hub_state.HUB_DIR / ".env"
    assert not env_file.exists()

    rc, output = _run_bootstrap(force=False, dry_run=False)

    assert rc == 0
    content = env_file.read_text()
    assert "ADMIN_USERNAME=admin\n" in content
    assert "POSTGRES_PASSWORD=" in content and "SECRETS_ENCRYPTION_KEY=" in content
    assert state["calls"]["_ensure_jwt_keys"] == 1
    assert state["calls"]["_cleanup_existing_hub_containers"] == 1
    assert state["calls"]["_sync_db_password_if_needed"] == 1
    assert state["calls"]["_install_shell_marker"] == 1
    assert state["calls"]["_ensure_atelier_service"] == 1
    assert state["calls"]["_ensure_update_service"] == 1
    assert state["make"] == [("up-prod", None)]
    assert state["health"] == [600]
    assert "reprise" not in output.lower()
    assert "--force :" not in output  # rien à régénérer → pas d'avertissement


def test_init_resume_fails_when_health_check_fails(monkeypatch, tmp_path):
    """Chemin reprise + health faux → rc 1 (sans retoucher le .env)."""
    state = _bootstrap_steps(monkeypatch, tmp_path, env_text=_EXISTING_ENV)
    state["healthy"] = False
    env_file = hub_state.HUB_DIR / ".env"
    before = env_file.read_bytes()

    rc, _ = _run_bootstrap(force=False, dry_run=False)

    assert rc == 1
    assert state["make"] == [("up-prod", None)]
    assert state["health"] == [600]
    assert env_file.read_bytes() == before
    # Étapes d'après le health check non jouées.
    assert state["calls"]["_install_shell_marker"] == 0
    assert state["calls"]["_ensure_atelier_service"] == 0
    assert state["calls"]["_ensure_update_service"] == 0


def test_init_docker_unreachable_exits_immediately(monkeypatch, tmp_path):
    """Docker injoignable → sortie 1 avant toute étape, .env existant ou non."""
    state = _bootstrap_steps(monkeypatch, tmp_path, env_text=_EXISTING_ENV)
    env_file = hub_state.HUB_DIR / ".env"
    before = env_file.read_bytes()
    monkeypatch.setattr(hub_state, "check_docker", lambda: False)

    rc, output = _run_bootstrap(force=False, dry_run=False)

    assert rc == 1
    assert "Docker injoignable" in output
    assert state["make"] == [] and state["health"] == []
    assert all(count == 0 for count in state["calls"].values())
    assert env_file.read_bytes() == before


def test_init_resume_dry_run_writes_nothing(monkeypatch, tmp_path):
    """Dry-run sur le chemin reprise : aperçu seul — rien d'écrit, pas de make."""
    state = _bootstrap_steps(monkeypatch, tmp_path, env_text=_EXISTING_ENV)
    env_file = hub_state.HUB_DIR / ".env"
    before = env_file.read_bytes()

    rc, output = _run_bootstrap(force=False, dry_run=True)

    assert rc == 0
    assert env_file.read_bytes() == before
    assert state["make"] == [] and state["health"] == []
    assert state["calls"]["_cleanup_existing_hub_containers"] == 0
    assert state["calls"]["_sync_db_password_if_needed"] == 0
    assert state["calls"]["_install_shell_marker"] == 0
    assert "reprise" in output.lower()


def test_existing_dotenv_credentials_reads_file_with_fallbacks(monkeypatch, tmp_path):
    """Le résumé de reprise lit le .env ; défauts du générateur si clé absente/vide."""
    _bootstrap_steps(monkeypatch, tmp_path, env_text=_EXISTING_ENV)
    assert actions._existing_dotenv_credentials() == {
        "admin_username": "patrice", "host_ip": "10.0.2.15",
    }

    (hub_state.HUB_DIR / ".env").write_text(
        "POSTGRES_PASSWORD=x\nADMIN_USERNAME=\n# HUB_HOST absent\n")
    assert actions._existing_dotenv_credentials() == {
        "admin_username": "admin", "host_ip": "192.0.2.99",
    }


# ── Services hôtes dans le flux de l'installeur (verdict VM 26/11) ──
# `install.sh` lance `hubctl init` sans TTY, puis installe lui-même les unités
# en root (phase 3c). `hubctl init` affichait pourtant « commandes à lancer à
# la main » : un avertissement trompeur pour une action faite juste après.
# L'installeur signale sa présence par HUBCTL_INSTALLER=1.

def _service_env(monkeypatch, tmp_path):
    """Services hôtes sur des chemins jetables : ni /etc, ni ~/.config, ni le dépôt."""
    from hubctl_cli import atelier_serve, hub_state

    (tmp_path / "rend").mkdir()
    monkeypatch.setattr(atelier_serve, "DEFAULT_TOKEN_FILE",
                        tmp_path / "cfg" / "atelier_token")
    monkeypatch.setattr(hub_state, "HUB_DIR", tmp_path / "HUB_base")
    monkeypatch.setattr(actions, "_ATELIER_UNIT", tmp_path / "etc" / "hubctl-atelier.service")
    monkeypatch.setattr(actions, "_UPDATE_UNIT", tmp_path / "etc" / "hubctl-update.service")
    monkeypatch.setattr(actions, "_ATELIER_UNIT_RENDERED", tmp_path / "rend" / "atelier.service")
    monkeypatch.setattr(actions, "_UPDATE_UNIT_RENDERED", tmp_path / "rend" / "update.service")


def test_services_keep_manual_reminder_outside_installer(monkeypatch, tmp_path):
    """Sans TTY et hors installeur : le rappel des commandes reste (init lancé seul)."""
    out = io.StringIO()
    _service_env(monkeypatch, tmp_path)
    monkeypatch.delenv("HUBCTL_INSTALLER", raising=False)
    con = Console(file=out, width=120)
    actions._ensure_atelier_service(con)
    actions._ensure_update_service(con)
    text = out.getvalue()
    assert "Service atelier : commandes à lancer à la main" in text
    assert "Service update : commandes à lancer à la main" in text


def test_services_do_not_ask_for_manual_steps_when_run_by_installer(monkeypatch, tmp_path):
    """`HUBCTL_INSTALLER=1` : l'installeur pose les deux unités en root juste après —
    `hubctl init` le dit au lieu de réclamer une action manuelle."""
    out = io.StringIO()
    _service_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HUBCTL_INSTALLER", "1")
    con = Console(file=out, width=120)
    actions._ensure_atelier_service(con)
    actions._ensure_update_service(con)
    text = out.getvalue()
    assert "à lancer à la main" not in text
    assert text.count("installé par l'installeur") == 2
