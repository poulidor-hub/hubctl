# Changelog — Hubctl

## [0.3.0] — 10 août 2026

### ⚡ .env généré enrichi : variables OIDC + SLO + SA interne

- **Ajouté** : les variables OIDC (`OIDC_ENABLED`, `OIDC_ISSUER`, `OIDC_EMAIL_TEMPLATE`,
  `OIDC_CODE_TTL_SECONDS`, `OIDC_ID_TOKEN_TTL_MINUTES`, `OIDC_ACCESS_TOKEN_TTL_MINUTES`,
  `OIDC_REFRESH_TOKEN_DAYS`, `OIDC_BACKCHANNEL_LOGOUT_ENABLED`) sont désormais incluses
  dans le `.env` généré par `hubctl init`.
- **Ajouté** : les variables SLO poste partagé (`OIDC_SLO_ENABLED`,
  `OIDC_SLO_IFRAME_TIMEOUT_MS`) ajoutées au `.env` généré.
- **Ajouté** : le Service Account interne `hub-internal` est auto-généré dans le `.env`
  (`HUB_INTERNAL_SA_CLIENT_ID=hub-internal`, `HUB_INTERNAL_SA_CLIENT_SECRET` aléatoire).
  Ce SA est requis par le SLO v2 et le M2M HUB→lui-même, évitant une erreur au
  premier déploiement si ces variables sont absentes.

## [0.2.0] — 2 août 2026

### ⚡ Migration ateliers APP_Workshop/ → APPS/ + PARC_ROOT robuste

- **Ateliers** : migrés de `APP_Workshop/` vers `APPS/` via `.workshop.env` et `docker compose -p X-ws`. Plus besoin de dossier séparé.
- **`auto_detect_mode()`** détecte le mode DEV via la présence de `.workshop.env` dans `APPS/`.
- **`active_apps_dir()`** retourne toujours `APPS_DIR`.
- **`get_apps_status()`** gère les conteneurs atelier (`{slug}-ws`) en mode DEV.
- **`vault_manager`** : la cible `ws:` pointe vers `.workshop.env` (plus de `.env` dupliqué).
- **`PARC_ROOT`** : nouvelle fonction `_find_parc_root()` qui remonte jusqu'à trouver `manifest.json` et `APPS/`, immunisant `hubctl` contre les symlinks (`hubctl_cli → hubctl_src/hubctl_cli`).
- **`debug_mcp.py` / `prompt.py`** utilisent `hub_state.PARC_ROOT` au lieu de `parents[2]`.
- **Tests** refondus pour la nouvelle architecture atelier.

## [0.1.0] — 31 juillet 2026

### Initial

- Extraction depuis POULIDOR (historique complet, 502 commits).
- Script wrapper bash `hubctl` + package Python `hubctl_cli/`.
- CLI TUI (menu, canvas, animations, widgets).
- Gestion des apps (start/stop/update/workshop), coffre (backup/restore), seed (export/deploy).
- Agent de debug (debug_agent) avec serveur MCP dédié (Docker/PSQL/file).
- 312 tests (pytest asyncio).
