# Changelog — Hubctl

## [0.6.1] — 23 août 2026

### ⚡ Revue finale — sûreté dry-run + garde-fous

- **Corrigé** : Ctrl-C pendant un `--maj`/`--prod` en dry-run déclenchait un rollback RÉEL
  (rebuild/up docker) — `dry_run` désormais propagé aux rollbacks des deux séquences.
- **Sécurisé** : `_ws_worktree_clean` fail-closed sans upstream (repli `origin/<branche>`,
  refus si irrésolvable) — un repo non-tracking avec commits non poussés ne passe plus.
- **Corrigé** : échec d'une commande SYNC du ship (ex. reset KO) restaure désormais le repo
  (`_ws_git_restore`) au lieu de laisser un tree orphelin sur main.
- **Corrigé** : `_find_free_subnet` parse les CIDRs par élément — un CIDR malformé ne
  discarde plus la totalité des subnets utilisés (risque d'allocation en overlap).
- **Tests** : +6 (464 passed).

## [0.6.0] — 23 août 2026

### 🚀 Atelier — test de mise à jour (`--maj`) et déploiement prod (`--prod`)

- **Ajouté** : `./hubctl atelier X --maj [--ref R]` — l'atelier récupère la nouvelle version du code
  (git fetch + checkout, `origin/main` par défaut, branche/tag via `--ref`) et la rebuild sur le clone
  de DB : les migrations sont rejouées au boot, la montée de version est testée sur le chemin réel
  sans risque prod. Idempotent (« déjà à jour » sans rebuild), aperçu des commits récupérés, rollback
  git automatique sur échec (build/up/health, Ctrl-C) avec hint « DB du clone possiblement migrée ».
- **Ajouté** : `./hubctl atelier X --prod` — déploie `origin/main` en prod une fois validé en atelier
  (garde-fous : atelier sain, ref `main`, commit testé == `origin/main`), puis supprime l'atelier
  (ports + disque + subnet libérés). Échec prod → rollback, atelier conservé pour diagnostic.
- **Ajouté** : menu interactif 4 choix (mettre à jour / déployer / supprimer / annuler) avec ligne
  d'état (`WS_REF @ WS_COMMIT`, version testée vs prod).
- **Ajouté** : subnet dédié au projet atelier — allocation d'un /24 dans le pool `10.210.0.0/24` –
  `10.210.63.0/24` (overlap vérifié contre les réseaux docker existants et les autres ateliers),
  persisté dans `APP_SUBNET` de `.workshop.env`. Corrige l'impossibilité de coexister prod + atelier
  sur les apps du template actuel (subnet épinglé → `Pool overlaps`).
- **Ajouté** : tracing `WS_REF`/`WS_COMMIT` dans `.workshop.env` ; injection `APP_VERSION` au build
  atelier (corrigeait une version affichée périmée — hubctl bypassait `make sync-env`).
- **Sécurisé** : pré-checks d'hygiène git (working tree propre hors `.workshop.env`, commits poussés)
  avant toute opération `--maj`/`--prod`.
- **Corrigé** : timeout du build de création d'atelier 120 s → 600 s (build template à froid > 120 s
  → création rollbackée).
- **Tests** : ~48 nouveaux (helpers git, `_update_workshop`, `_ship_workshop`, tracing création,
  dispatch + parser, subnet) — 458 passed. Validé E2E sur parc réel (app jetable template v2.16.0) :
  8/8 scénarios (refus sans remote / tree sale, idempotence, maj nominale, échec build + rollback,
  ship nominal + suppression atelier, garde-fous ref et commit, échec ship + atelier conservé).

## [0.5.2] — 17 août 2026

### ⚡ init — whitelist réseau réelle + résumé exact

- **Corrigé** : `APP_WHITELIST_NETWORKS` était généré en dur (`10.42.0.0/16,192.168.1.0/24`) — sur tout serveur hors `192.168.1.0/24`, `allocate-ports` et l'auto-enregistrement des apps renvoyaient 403 dès l'installation. Le `/24` du subnet LAN détecté est désormais inclus (fallback legacy si IP loopback). Constaté en test fresh-install Debian 12 (VM QEMU).
- **Corrigé** : le résumé de `hubctl init` affichait l'API en `https://…:8000` alors qu'elle écoute en HTTP — aligné sur `http://`.
- **Tests** : 2 nouveaux (`whitelist inclut le subnet détecté`, `fallback loopback`) — 410 passed.

## [0.5.1] — 16 août 2026

### 🔧 Kit PRA — durcissements de la revue finale

- **Corrigé** : le badge `⚠ PRA` était recalculé à chaque frame du dashboard
  (scan des montages dans le thread de rendu) — un emplacement NFS mort gelait
  l'UI. Le badge est désormais calculé par le poll worker (3 s) et lu depuis
  le cache `_snap` ; le rendu ne touche plus au disque.
- **Corrigé** : `pra-restore` vérifie Docker avant toute action (`preflight_docker` :
  binaire présent + daemon joignable) — fini les `FileNotFoundError` bruts et les
  faux « parc vierge » sur daemon down ; `build_stack` s'arrête proprement si
  `make build`/`make up-prod` échoue.
- **Corrigé** : la réplication ne copie plus un snapshot en cours d'écriture par
  le HUB (garde fraîcheur mtime < 120 s) et vérifie la taille après copie —
  un kit partiel ne peut plus être marqué synchronisé (retenté au cycle suivant).
- **Tests** : +12 (badge/poll, preflight, garde fraîcheur/taille) — 408 au total.


## [0.5.0] — 16 août 2026

### ⚡ Kit PRA : emplacement secondaire automatique + assistant pra-restore

- **Ajouté** : réplication automatique du coffre (`parc.vault.enc` + `.bak`) et du
  dernier snapshot du HUB vers l'emplacement PRA à chaque sauvegarde du coffre —
  diff checksum, copie atomique, non-bloquant, clé `pra` dans le résumé `backup_all`.
- **Ajouté** : emplacement générique (clé USB, 2e disque, montage réseau) avec
  validation « ailleurs » en 2 niveaux (refus du disque serveur) et marqueur
  `HUB-PRA-KIT.json` ; le kit embarque aussi `PRA-README.txt`.
- **Ajouté** : phase de setup initial récurrente au 1er lancement de hubctl
  (configurer / plus tard / refus définitif), action **Coffre → 🔄 Emplacement PRA**
  (statut, changer, répliquer, retirer) et badge `⚠ PRA` sur le dashboard si
  l'emplacement est absent ou non synchronisé depuis 7+ jours.
- **Ajouté** : assistant `python -m hubctl_cli pra-restore` — localisation du kit,
  coffre restauré depuis le kit, build & start de la stack, injection du snapshot,
  attente du restore interne, sélection des apps à rebuilder/réinjecter.
  Idempotent/resumable ; arguments `kit` (positionnel, scan auto si omis) et
  fanions `--snapshot`, `--apps all|none|a,b`, `--admin-user`, `--yes`,
  `--force` (garde-fou parc non vide), `--dry-run`.
- **Ajouté** : section PRA dans `hubctl_cli/README.md`.
- **Tests** : `test_pra_config` (24), `test_pra_replicate` (9), `test_pra_restore`
  (29), `test_setup_phase` (10), +2 hooks `vault_manager` — 396 au total.

## [0.4.0] — 12 août 2026

### ⚡ Shell marker portable + auto-source .secrets + cleanup legacy ws

- **Ajouté** : `_install_shell_marker()` dans `actions.py` — créer `~/.config/poulidor/parc.sh` et l'ajoute idempotemment à `~/.bashrc` pour un sourcing portable de `.secrets`.
- **Ajouté** : le wrapper `hubctl` auto-source `.secrets` si présent (indépendance du shell appelant).
- **Ajouté** : la variable `HOST_DOCKER_GID` est détectée et injectée dans le `.env` généré (mount socket Docker hub-api).
- **Supprimé** : gestion legacy `APP_Workshop/` (`ws:` entries) dans `coffre.py` et `vault_manager.py`.
- **Modifié** : `README.md` et `debug_agent` adaptés au nouveau schéma atelier.
- **Tests** : synchronisés avec les changements de `coffre.py` et `vault_manager.py`.

## [0.3.2] — 11 août 2026

### ⚡ Fix psql sync DB : utilisait le user comme nom de base

- **Corrigé** : la commande `psql -d <user>` dans `_sync_db_password_if_needed()`
  utilisait `POSTGRES_USER` comme nom de base (`-d hubadmin`), ce qui échouait
  car la base s'appelle `POSTGRES_DB` (`hub_auth`). Corrigé en `-d <db>`.

## [0.3.1] — 10 août 2026

### ⚡ Fix détection PARC_ROOT sur fresh clone sans APPS/

- **Corrigé** : `_find_parc_root()` n'exige plus la présence du dossier `APPS/`
  (seulement `manifest.json`). Sur un clone frais sans apps déployées, `APPS/`
  n'existe pas — la détection retombait sur le fallback pointant vers `hubctl_src/`,
  ce qui empêchait `make -C HUB_base up-prod` de trouver le Makefile.

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
