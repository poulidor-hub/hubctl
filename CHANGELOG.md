## [0.23.0] - 27 novembre 2026
### ⚡ `app install` : une dépendance absente ne bloque plus l'installation
Retour utilisateur (27/11/2026) : pour la mise en place d'un Hub, installer une app
aux dépendances non présentes (ex. intégrations M2M référentiel/compta) doit marcher —
ces apps rejoindront le parc plus tard.
- Dépendance déclarée et absente du registre du Hub → **avertissement explicite** et
  l'app s'installe seule (« ses intégrations s'activeront quand elle rejoindra le parc ») ;
  plus jamais d'échec ni de renvoi vers `seed-apps.yaml`, qui n'en fait pas partie
  (outil de reprise d'une flotte sauvegardée — décision utilisateur).
- La résolution des dépendances passe désormais par le seul registre du Hub
  (`git_repo_url`, jeton admin).
- `seed` et `seed-apps.yaml` gardent leur rôle de reprise/convergence, inchangés.
- Tests réécrits pour le nouveau contrat (autonomie + avertissement + « aucune
  interrogation du registre sans jeton ») ; suite complète 993 verts.

## [0.22.1] - 26 novembre 2026
### 🐛 Validation VM d'une installation neuve : service atelier, doctor, dépendances, coffre
Tout vient de la validation bout-en-bout sur VM vierge du 26/11/2026 (installation puis
`hubctl app install` d'une app issue du gabarit).
- **`hubctl-atelier` démarrait en boucle (226/NAMESPACE) sur tout déploiement neuf** : le
  dossier du coffre (runtime, hors git) n'existe pas encore et l'unit est en
  `ProtectSystem=strict` avec `ReadWritePaths` sur ce dossier. `hubctl init` crée maintenant
  le dossier (700) avant l'installation du service ; l'unit tolère son absence (`-`).
- **`doctor` après `restart`** : un `restart` réussi pose un timbre ; une modification du
  `.env` qui ne fait rien recréer par compose (commentaire, valeur identique) n'est plus
  signalée « configuration non appliquée » en boucle. Toujours signalée si le `.env`
  change APRÈS le dernier restart.
- **`app install`** : `depends.yaml` vide et bien formé (celui du gabarit, `depends_on: []`)
  ne déclenche plus d'avertissement — seulement un fichier réellement illisible. Sans coffre
  encore créé (installation fraîche), une information remplace « ⚠ Coffre verrouillé… » ;
  un coffre existant verrouillé garde son rappel (concerne aussi `seed`).
- Suite complète : 993 tests verts.

## [0.22.0] - 26 novembre 2026
### ⚡ `hubctl app install <url-git>` — une commande, l'app tourne (plan « installation sans friction », phases 2-3)
- **Nouvelle commande** `hubctl app install <url-git>… [--no-deps] [--yes]` (aussi un fichier YAML au
  format seed) : clone dans `APPS/<slug>`, dépendances d'abord (`depends.yaml`), `.env`, images et
  contexte de build du socle garantis, `sync-env`, build, app **pré-créée approuvée** au Hub,
  démarrage, certificat de la CA du Hub, résumé avec l'URL d'accès. Menu PROD : touche 5.
- **Dépendances** : résolues par `seed-apps.yaml` puis le registre Hub ; introuvables → message
  clair, l'app n'est pas installée, les suivantes continuent ; cycle détecté ; `--no-deps`.
- **Secrets par défaut neutralisés** : toute variable `${VAR:-défaut}` du compose dont le défaut
  ressemble à un placeholder (`changeme`, `admin123`…) reçoit une valeur aléatoire dans le `.env`.
- **Socle garanti** : `_ensure_socle_image` construit `poulidor/socle-api:<V>` et `socle-web:<V>`
  (V = `HUBsocle/VERSION`) s'ils manquent — budget 1800 s par image — avant `sync-env` ; le lien
  de contexte `socle` est créé quand le compose de l'app passe par un lien `socle` local.
- **Approbation** : hubctl agit comme l'admin — `POST /api/auth/login` puis `POST /api/apps/`
  (ports lus dans le `.env` après `sync-env`) ; l'app naît `approved`. Mot de passe admin :
  `HUBCTL_ADMIN_PASSWORD`, puis `ADMIN_PASSWORD` de `HUB_base/.env` (valide tant qu'il n'a pas
  été changé), puis saisie masquée. Sans TTY ni mot de passe valide : l'app est installée et reste
  « en attente d'approbation », message explicite, rc 0. Rien n'est mémorisé ni journalisé. Une app
  déjà installée encore `pending` est approuvée au passage (rattrapage).
- **Budgets de premier démarrage** 1800 / 600 / 300 s (build, up, santé) — aussi pour une nouvelle
  app du `seed`, qui passe désormais par le même chemin (`_build_up_health`).
- `socle build` accepte une console et un budget ; `_health_check` nomme sa cible.
- Tests : `test_app_install.py` (133), suite complète 981 verts. **Non vérifié sur une vraie
  pile** (VM neuve) à la date de cette entrée : validation E2E à venir.

## [0.21.0] - 26 novembre 2026
### ⚡ doctor honnête sur un déploiement, `hubctl restart`, init clair — plan « installation sans friction » (phase 4)
- **`hubctl doctor` sur un clone public frais ne sort plus en rouge** (verdict VM 26/11 : deux ✗
  juste après une installation réussie). La lecture se fait par l'ÉTAT : un composant en HEAD
  détaché sur le gitlink du méta = un déploiement qui suit les releases du méta. Dans ce cas
  « HUB_base en retard sur origin/main » devient une information ℹ (« mise à jour disponible —
  mettre le méta à jour d'abord, puis `./hubctl update hub` »), la branche `main` du sous-module
  n'est plus listée comme « restante », `--fix` ne fait ni `checkout main` ni `reset --hard`
  (idempotent, il ne réaligne que sur le gitlink), et le remède `pull --ff-only` n'est plus
  proposé. Checkout de développement (HEAD attaché) : comportement inchangé. Les vrais
  problèmes (gitlink modifié, sous-module dérivé) restent rouges.
- **`hubctl update hub` n'est plus bloqué** par ces informations (seuls les problèmes bloquants bloquent).
- **Nouvelle commande `hubctl restart`** : recharge la configuration par recreate (`compose up -d`
  sur le Hub puis les apps actives) — `./hub.sh restart` et `start prod` ne relisent pas le `.env`.
- **`doctor` signale un `.env` non appliqué** (ℹ « HUB_base/.env modifié après le dernier démarrage
  du Hub — `./hubctl restart` »).
- **Fetch impossible** : la cause réelle du stderr git (pas son conseil final tronqué) + remède SSH.
- **Une information s'affiche ℹ**, plus avec un ✓ vert.
- **`hubctl init --help`** expose les trois chemins (installation complète, reprise, `--force`).
- **Sous l'installeur** (`HUBCTL_INSTALLER=1`, posé par `install.sh`) : plus de rappel « commandes
  à lancer à la main » pour les services hôtes — l'installeur les pose en root juste après. Hors
  installeur, le rappel reste.
- README : lignes `restart` et `doctor`. Tests : 821 verts (25 + 11 + 8 + 5 nouveaux).

## [0.20.8] - 26 novembre 2026
### ⚡ init : relançable sans risque — reprise quand le .env existe déjà
- `hubctl init` refusait quand `HUB_base/.env` existait sans `--force` (non-interactif :
  « utilisez --force » ; interactif : prompt « Écraser le .env existant ? »). Conséquences
  constatées en VM le 26/11 : la guidance du timeout de build (« Relancez ./hubctl init »)
  était fausse en non-interactif, et `install.sh` relancé Hub arrêté affichait « ✗
  L'initialisation du Hub a échoué » (exit 1) alors qu'il se prétend idempotent.
- **Reprise** (`.env` présent, sans `--force`) : le `.env` sert tel quel (octets inchangés,
  aucun secret régénéré, aucun prompt même avec TTY), pas de nettoyage des conteneurs ni
  de re-synchronisation du mot de passe DB ; `up-prod` (budget build 1800 s), health check
  (600 s), étapes idempotentes (marker shell, services atelier/update), résumé construit
  depuis le `.env` existant et signalé « ↻ Reprise ».
- **`--force`** : régénération inchangée, précédée d'un avertissement explicite et vrai
  (mots de passe PostgreSQL/admin, clé Fernet — les secrets chiffrés avec l'ancienne clé
  deviennent illisibles —, secret du Service Account interne sont retirés ; clés JWT,
  token atelier et volumes Docker conservés).
- Installation fraîche et Docker injoignable : inchangés. README : ligne `init` alignée.
- Tests : 7 nouveaux (rouge → vert sur l'ancien code), suite complète 784 verts.

## [0.20.7] - 26 novembre 2026
### ⚡ init : le premier boot ne doit plus échouer à 120 s sur le build des images
- E2E VM 26/11 (×2, reproductible sur VM 2 vCPU) : le 1ᵉʳ `make up-prod` build les
  images Hub (> 120 s) → timeout du défaut générique → « ✗ Échec du build/démarrage
  du Hub » alors que le build continuait orphelin et finissait healthy. Le nouvel
  utilisateur était accueilli par une erreur sur le premier start.
- `hub_make` : budget généreux (1800 s, surcharge `HUBCTL_BUILD_TIMEOUT`) pour les
  cibles de build (`up-prod`, `up`, `build`, `rebuild`) ; les autres cibles gardent le
  défaut historique (kwarg absent, pas `timeout=None` qui désactiverait la limite).
- Health check d'init : plafond 120 s → 600 s (polling conditionné inchangé),
  surcharge `HUBCTL_HEALTH_TIMEOUT`.
- Timeout de build (rc 124) : guidance actionable — « Relancez ./hubctl init : il
  convergera » (le build orphelin a continué, l'init reprise détecte un Hub
  opérationnel) — au lieu de la seule ligne d'échec.
- pyproject.toml resynchronisé sur VERSION (0.20.3 → 0.20.7, dérive constatée).
- Tests : test_init_first_boot.py (6, dont le défaut historique verrouillé) ;
  scope élargi 170 verts.
## [0.20.6] - 26 novembre 2026
### ⚡ README public mis à jour — URLs voisines vers poulidor-hub, section Mise à jour d'un déploiement
- Badge de version du README corrigé (0.9.0 -> 0.20.5) ; URLs des dépôts voisins pointées
  vers les dépôts publics poulidor-hub (POULIDOR x2, HUB_base -> HUB, HUBsocle).
- Section « Mise à jour d'un déploiement » ajoutée : la mise à jour d'un déploiement passe
  par la racine du méta-repo (fetch upstream + merge --ff-only + sous-modules) ; le dépôt
  public est en lecture seule pour les clients (garanti par GitHub).
- Aucune modification de la CLI.

## [0.20.5] - 25 novembre 2026
### ⚡ Neutralisation open source : valeurs d'exemple dans le code, les tests et la documentation
- Valeurs réelles (adresses IP, noms d'hôtes, identifiants de clients, slugs d'applications
  privées) remplacées par des valeurs d'exemple (9 fichiers). Aucune valeur du parc ne
  subsiste dans les fichiers suivis (garde `oss-guard` du méta-repo).
- Aucune modification fonctionnelle de la CLI ; tests : 769 passed, 7 skipped.
## [0.20.4] - 17 novembre 2026

### ⚡ Coffre — restore DB : timeout des flux psql proportionnel au volume rejoué (issue #1, suite)

- **Cause** : timeout fixe de 30 s sur le replay (`_restore_db`) et le rollback (`_rollback_db`) — un dump légitime volumineux en PRA (psql --inserts rejoue ligne par ligne, ~20 Ko/s réels sur VM) serait tué en plein rejeu : faux « échec psql » + rollback. Pire sur le rollback : tué en plein rejeu, il laisse la base à l'état post-truncate (le pire état possible) en rapportant « rollback échoué ».
- **Fix** : `_replay_timeout(sql)` — plancher 30 s (petits dumps), croissance proportionnelle (~20 Ko/s conservateur), plafond 900 s (garde-fou anti-hang — un PRA ne reste pas suspendu indéfiniment) ; branché sur le replay ET le rollback (snapshot de volume comparable en réel).
- **Tests** (+2, TDD) : timeout proportionnel capturé sur les deux flux psql (replay d'un gros dump strictement au-dessus du plancher ; rollback soumis au même contrat) + bornes de la formule (plancher 30 s, 1 Mo → 50 s, plafond 900 s). Suite dépôt : 769 passed, 7 skipped.
- **Validation PRA end-to-end** (labo VM `repro-hubctl1`, scénario exact de l'incident du 16/11 — Hub frais, registre propre, coffre externe 3 apps) : restore → 3 applications présentes, counts base = dump sur toutes les tables ; dump corrompu (INSERT FK invalide) → échec explicitement remonté (« ⚠ DB NON restaurée ») + base live intacte (rollback du snapshot). Journal archivé (VM détruite après validation).
- **Résidu documenté** : un dump corrompu échouant EN MILIEU de replay (des INSERTs déjà joués) peut voir le rollback best-effort buter sur des dup keys (rejeu du snapshot sans TRUNCATE préalable) et laisser la base partiellement remplie — l'échec reste toujours remonté, aucun faux succès possible. Amélioration future : TRUNCATE avant rejeu du snapshot dans `_rollback_db`.

---

## [0.20.3] - 17 novembre 2026

### ⚡ Tests — fin du flake pause (issue #3) ; MonkeyPatch manuels convertis

- **Cause racine** (lecture + confirmation à l'exécution) : trois tests créaient `pytest.MonkeyPatch()` à la main SANS `undo()` — `test_run_action_returns_true_on_success` et `test_run_action_returns_false_on_failure` laissaient `menu.pause` remplacé par un no-op pour tout le reste du process, et `test_refresh_apps_status_updates_module_var` laissait `hub_state.get_apps_status` remplacé (fuite jumelle découverte pendant l'enquête). Conséquence visible : `test_pause_waits_for_enter` (test_actions) échouait si `test_menu_loop` passait avant lui dans le même process — `pause()` rend pourtant inconditionnellement, d'où `render.call_count == 0`, signature d'un pause no-op.
- **Fix** : les 3 tests convertis à la fixture pytest `monkeypatch` (auto-undo garantie).
- **+2 gardes d'intégrité de module** (TDD, regardés rouges sur l'état fautif en lançant les fichiers complets) : `test_menu_pause_not_polluted` et `test_hub_state_get_apps_status_not_polluted` assertent que les attributs partagés restent les fonctions réelles après la suite — tout futur MonkeyPatch manuel oublié sera détecté dans le fichier fautif lui-même.
- **Vérifié** : batch historique déclencheur (`test_menu_loop` → `test_pause_waits_for_enter`) vert, ordre inverse vert ; 2 full-suites consécutives 767 passed / 7 skipped / 0 failed.

---

## [0.20.2] - 16 novembre 2026

### ⚡ Tests — fin du flake park_checks ; poll worker du menu arrêtable (issue #2)

- **Cause racine prouvée par instrumentation** : les tests de timeout de `test_park_checks` échouaient de façon intermittente en suite complète (2 échecs sur 3 runs, tests alternés). Journal `subprocess.run` de la fenêtre du spy : 14 threads résiduels `_state_poll_worker` (laissés par `test_actions`/`test_main`/`test_menu_loop` via `main_loop`) appelaient `docker compose` (timeout=20) et `git status` (timeout=120) pendant la fenêtre et écrasaient `store["timeout"]` (spy posé sur le module `subprocess` global).
- **`menu.py` — poll worker arrêtable** : `_start_poll_worker()` enregistre le thread, `stop_poll_worker()` arrête via `Event` + join ; boucle en `Event.wait(_POLL_INTERVAL)` au lieu de `time.sleep(3.0)` — le worker daemon qui sous-traitait docker/git « pour toujours » est désormais stoppé proprement (utilisé par les tests, disponible pour l'app).
- **Tests menu** : fixture autouse `stop_poll_worker()` en teardown dans les 3 fichiers pilotant `main_loop`.
- **`test_park_checks._spy_run` — défense en profondeur** : le spy ne recorder que les appels au `argv_prefix` attendu (`git rev-parse` par défaut, `git fetch` explicite pour le site « env ») — la fenêtre est immunisée contre tout appel étranger, même si un futur test laisse fuir un thread.
- **+2 tests** : contrat d'arrêt du worker (TDD, rouge avant implémentation) ; immunité du spy reproduisant le mécanisme du flake en unitaire (un appel étranger timeout=20 ne doit pas écraser store).
- **Effet de bord mesuré** : suite intégrale 440 s → 127 s (×3,5) — les workers résiduels sous-traitaient docker/git réel pendant toute la suite. 765 passed, 7 skipped, 0 failed (2 runs consécutifs).
- Flakes annexes documentés : `test_pause_waits_for_enter` (dépendance d'ordre, préexistant, ordre canonique épargné) → issue #3.

---

## [0.20.1] - 16 novembre 2026

### ⚡ Coffre — restore DB : fin du succès mensonger sur conflits FK (issue #1)

- **Contexte (run réel du 16/11/2026)** : `hubctl vault restore` déclarait « DB restaurée » alors que deux applications n'avaient pas été rejouées — psql, sans `ON_ERROR_STOP`, continuait sur les conflits FK et sortait 0 ; `restore_all` ignorait de surcroît la valeur de retour de `_restore_db`. Dump du coffre sain (replay manuel vérifié) : le problème était le replay, pas la capture.
- **`ON_ERROR_STOP=1`** sur les trois psql du chemin restore (replay, rollback snapshot, TRUNCATE) : toute erreur SQL (conflit FK, dup key) interrompt psql avec un code non nul — le filet de sécurité (rollback du snapshot live pris avant truncate), jusque-là inatteignable, joue désormais son rôle.
- **TRUNCATE vérifié** : returncode contrôlé, exceptions loggées — un échec abandonne le restore AVANT le replay et recharge le snapshot (rejouer le dump sur des tables non vidées garantissait dup keys et conflits FK en cascade, le scénario de l'incident).
- **Comptage de lignes post-restore** : counts attendus parsés des sections `-- Data for Name:` du dump, comparés à un `SELECT count(*)` par table (`-t -A -F |`, `ON_ERROR_STOP=1`) ; tout écart → échec explicite + rollback.
- **Tables parsées du dump** (fin de la liste en dur `_RESTORE_TABLES`) : truncate/snapshot/vérification couvrent exactement ce que `_backup_db` a capturé — `schema_migrations`, capturée mais jamais tronquée, ne fait plus peser de risque de dup keys sur les re-restores ; un contenu sans section DATA est refusé avant tout truncate.
- **Rapport honnête** : `restore_all` ajoute « ⚠ DB NON restaurée (replay en échec — voir logs, snapshot live rechargé) » au lieu de « DB restaurée » quand le replay a échoué ; le dry-run affiche « DB (dry-run : rien rejoué) ».
- **tests** : +7 dans `test_vault_manager.py` (ON_ERROR_STOP présent, abort sur TRUNCATE, mismatch de counts avec rollback, vérification exécutée en nominal, rejet d'un dump méconnaissable, rapport restore_all échec/succès) ; 2 tests existants adaptés au contrat de fixture (dump réaliste pg_dump --inserts). Suite : 763 collected — 762 passed + 1 flake préexistant `park_checks` (pollution `subprocess.run` global par thread résiduel, issue dédiée), 7 skipped.

---

## [0.20.0] - 16 novembre 2026

### 📣 Pour les utilisateurs
- **Ajouté** : `hubctl` mesure désormais les ressources du serveur (CPU, mémoire, disque, charge) et les met à disposition du Hub — c'est la source du nouveau « cockpit » de la page Administration.
- **Amélioré** : mesure du CPU fiable (calcul réel au lieu d'une approximation).

### ⚡ Agent hôte — route `GET /stats` (métriques serveur pour le cockpit HUB_base)

- **Nouveau module `hubctl_cli/stats.py`** : parseurs purs testables — uptime (`/proc/uptime`), mémoire (`/proc/meminfo` : total/available/used/percent + swap), charge (`/proc/loadavg`), échantillon CPU (`/proc/stat`) et `cpu_percent_between()` par différentiel idle/total (remplace l'approximation `load1 × 25` du menu TUI) ; `get_stats()` assemble le contrat JSON `cpu.percent/cores/load1`, `memory.total_mb/used_mb/available_mb/percent/swap_percent`, `disk.path/total_gb/used_gb/free_gb/percent` (via `hub_state.get_disk_usage()`), timestamp ISO local.
- **CPU honnête** : échantillon précédent conservé en module (sous `threading.Lock`, serveur ThreadingHTTPServer) ; au premier appel, 2 échantillons espacés (~0,6 s, `_cpu_delay()` monkeypatchable en test) ; valeur bornée [0, 100], Δtotal ≤ 0 → 0,0.
- **Route `GET /stats`** dans `update_serve.py` : même protection token `X-Atelier-Token` que `/update/status` (pas publique), POST → 405, docstrings/route doc à jour. Dégradation par section : capteur en échec → valeurs neutres **clés du contrat toujours présentes** + `errors: {section}` + `service: "degraded"` (le consommateur lit sans test d'existence).
- **Contrat consommé tel quel** par le nouveau proxy HUB_base `GET /api/admin/system/stats` (cockpit admin) — ne pas renommer les champs.
- **tests** : `tests/test_stats.py` — 13 tests (parseurs sur `/proc` factices, garde Δtotal=0, double échantillon 1ᵉʳ appel, dégradation isolée, route 401/403/200/405 via la recette serveur existante) ; suite ciblée 39 passed (stats + régression update_serve).
- `hubctl_cli/pyproject.toml` resynchronisé sur `VERSION` (0.16.7 → 0.20.0, drift préexistant).

---

## [0.19.0] - 16 novembre 2026

### ⚡ Coffre — sauvegarde et restauration de `certs/ca` (CA HTTPS du parc)

- **Contexte** : la CA « Hub Local CA » (`HUB_base/certs/ca/ca.crt` + `ca.key`)
  n'était sauvegardée par aucun chemin du coffre — les postes/navigateurs
  l'ont importée ; une perte de disque suivie d'une restauration PRA
  régénérait une NOUVELLE CA et remettait toute la flotte en
  `SEC_ERROR_BAD_SIGNATURE` (incident réel du 3→16 nov 2026).
- **`backup_all`** : `vault_manager._backup_certs_ca()` packe `certs/ca/`
  (rglob, skip symlinks, même pattern que `secrets/`), dégradé silencieux si
  absente ; compteur `"ca"` dédié dans le résumé.
- **Restore — chemin commun** : `Vault.restore()` (coffre.py) est le chemin
  partagé de `restore_all` ET `pra_restore.restore_files_from_kit` → la CA
  est restaurée sans duplication via `_apply_restore_modes()` :
  `ca.crt` 0644 (publique navigateurs), `ca.key` 0600, dossier `certs/ca`
  0700 ; autres secrets inchangés (0600). Vieux coffre sans `certs/ca` :
  no-op, pas de crash.
- **Sorties enrichies** : CLI `hubctl vault backup`, TUI Coffre (menu.py),
  contrat Hub `/vault/status` (`entries.ca_certs`).
- Tests : +6 (backup captures/dégradé, restore modes au chemin commun,
  vieux coffre, kit PRA, contrat atelier) ; suite complète 739 passed,
  7 skipped.

## [0.18.6] - 15 novembre 2026

### ⚡ Units hôte (atelier 8777, update 8778) : bind sur la passerelle docker0 RÉELLE, plus d'IP en dur

- **Bug constaté en prod 15/11/2026** : vue Admin → Mises à jour
  « Service de mise à jour hôte injoignable ». Cause : les gabarits
  `packaging/*.service` codeaient `--host 172.17.0.1` en dur, mais le
  daemon Docker — reconfiguré par `hubctl docker setup-pools` (0.18.4) ou
  un daemon.json local — avait déplacé docker0 en `10.200.0.1` sur
  l'atelier. Bind sur une IP inexistante → injoignable depuis hub-api
  (`host.docker.internal` résout vers la VRAIE passerelle), alors que le
  service systemd affichait « active ».
- `docker_pools.docker0_gateway()` : lit l'inet de docker0
  (`ip -4 -o addr show dev docker0`), repli 172.17.0.1 + avertissement
  (jamais fatal — appelé au rendu).
- Gabarits `hubctl-atelier.service` / `hubctl-update.service` :
  `--host @DOCKER_GATEWAY@` substitué au rendu (`actions._render_unit`),
  comme @REAL_USER@/@PARC_ROOT@. Même posture sécurité (docker0 non routé
  du LAN, auth X-Atelier-Token inchangée).
- README + aide `--host` des deux sous-commandes : « 172.17.0.1 » présenté
  comme un exemple parmi d'autres, pas LA valeur.
- Tests : +3 sur `docker0_gateway` (lecture inet, repli sans `ip`, repli
  sans inet) ; rendu d'unit testé avec passerelle mockée (plus de
  dépendance au docker0 de la machine de test) ; garde-fous packaging
  étendus (pas d'IP en dur dans ExecStart).
- Correctif runtime sur l'atelier concerné : unit installée passée à
  `--host 10.200.0.1` + restart (hubctl-update ET hubctl-atelier, les deux
  étaient touchés).

---

## [0.18.5] - 14 novembre 2026

### ⚡ Statut « sale » = fichiers suivis modifiés (fin du badge permanent sur les secrets runtime)

- `update_serve._component_status` : le champ `dirty` (GET /update/status →
  badge « arbre sale » de l'UI Mises à jour) utilise désormais
  `park_checks._dirty_tracked` — les fichiers **non suivis** ne comptent plus.
  Cas déclencheur : `secrets/atelier_token` (créé hors git par `hubctl init`)
  = statut sale permanent avec un remède « committer ou nettoyer » inapplicable
  à un secret. Définition désormais identique aux pré-checks de `update hub`.
- `hub_state._git_dirty` : aligné tracked-only pour ses deux appelants
  (`get_apps`, `get_workshop_apps` — affichage panneau Apps/Ateliers).
  Les gardes historiques (git absent / timeout → False) sont conservées.
- **Inchangé volontairement** : `actions._ws_worktree_clean` (garde atelier
  avant `--maj/--prod`) reste sur le porcelain brut — fail-closed : un
  pull/checkout ne doit jamais écraser du non-suivi.
- Tests : 3 nouveaux (untracked seul → dirty False ; tracked modifié → True) ;
  suite complète 729 passed / 7 skipped (1 flake préexistant sur
  `test_park_checks.py::test_run_timeout_*`, prouvé sur baseline avant le fix).

---

## [0.18.4] - 14 novembre 2026

### ⚡ Pools d'adresses Docker : fin de la saturation récurrente des réseaux

Les pools par défaut du daemon (≈31 sous-réseaux) saturaient avec la
flotte (33 apps × 1 network compose + hub + stacks de test) →
« invalid pool request: Pool overlaps » récurrent au démarrage des apps
(constaté au test PRA VM et en prod au start de flotte).

- Nouvelle sous-commande `hubctl docker setup-pools` : merge idempotent
  de `default-address-pools` (172.16.0.0/12 et 10.200.0.0/14 en /24,
  ~5100 réseaux /24) dans /etc/docker/daemon.json, en préservant dns et
  runtimes ; `hubctl docker check` pour l'état.
- `hubctl stop all` / `stop prod` : purge des networks Docker orphelins
  en fin de run (ils s'accumulent sinon).
- Installs récents : config posée manuellement sur le serveur et le
  labo VM pendant le test PRA (14/11/2026) — cette release l'automatise.

---

## [0.18.3] - 14 novembre 2026

### ⚡ pra-restore : restauration complète des apps opérationnelle

Fixes issus du test PRA réel sur VM Debian vierge (13-14/11/2026, restore
du snapshot réel 3,5 Go) : la phase réinjection des apps échouait en
cascade pour 4 causes distinctes.

- `admin_login` : POST sur `/api/auth/token` (le router auth du hub a le
  prefix `/api/auth` — l'ancienne URL `/api/token` rendait le login
  impossible en PRA).
- `list_apps_with_backups` : réinjection sur backups `status='success'`
  uniquement — le dernier backup d'une app peut être `failed` (aucun
  fichier ZIP produit) et faisait échouer le restore en 404.
- `rebuild_app` : dossier d'app résolu par `APP_SLUG` du `.env` — le
  slug DB et le nom du checkout divergent pour 25 apps de la flotte
  (Exemple_App vs exemple-app…).
- Timeouts couverts pour les gros restores : `HEALTH_TIMEOUT_S` 300→900,
  `RESTORE_TIMEOUT_S` 180→600 (boot hub-api post-restore multi-minutes
  sur petite machine).

| Fonctionnalité | Description |
|----------------|-------------|
| **PRA bout en bout** | Backup → snapshot → coffre → clé USB → restore sur machine neuve : hub restauré + apps rebuildées + données réinjectées |

---

## [0.18.2] - 9 novembre 2026

### 🐛 Unit systemd update : `~/.docker` dans `ReadWritePaths`

Le premier job réel échouait au build (`failed to update builder last activity
time: read-only file system`) : `ProtectHome=read-only` bloquait l'écriture du
client docker buildx (`~/.docker/buildx/activity`). Ajout de
`/home/@REAL_USER@/.docker` aux `ReadWritePaths` (parc + `.config/hubctl` +
`.docker`), asserts rendu/gabarit mis à jour. Constaté sur le premier déploiement
réel (job 21:11, rollback automatique propre — pré-checks, snapshot DB et
rollback exactement comme prévu).

## [0.18.1] - 9 novembre 2026
## [0.18.1] - 9 novembre 2026

### 🐛 Unit systemd update : home réelle dans `ReadWritePaths`

L'unit `hubctl-update.service` refusait de démarrer en prod (`status=226/NAMESPACE,
/root/.config/hubctl introuvable`) : le gabarit utilisait le raccourci home de
systemd dans `ReadWritePaths`, qui se résout à `/root` dans une unit système
indépendamment de `User=`. Remplacé par `/home/@REAL_USER@/.config/hubctl`
(substitution à l'installation). Garde-fou de packaging renforcé : plus aucun
raccourci home dans le gabarit livré ; asserts rendu/gabarit mis à jour (+2/28 tests
update_init+serve verts).

## [0.18.0] - 9 novembre 2026
## [0.18.0] - 9 novembre 2026

### ✨ Service hôte de mise à jour (`hubctl update --serve`) — centre de mises à jour du parc

Le Hub central peut désormais détecter les mises à jour disponibles et lancer la mise à jour du socle depuis son interface : ce service hôte expose le statut git du parc et exécute le chemin CLI réel `hubctl update hub` (pré-checks + rollback inchangés) en job persistant qui survit au redémarrage du Hub.

| Élément | Détail |
|---------|--------|
| **`hubctl_cli/update_serve.py`** | nouveau module stdlib (aucune dépendance nouvelle) : statut par composant (méta + submodules : `version`, `version_target` lu sur `origin/main` sans toucher l'arbre, `commits_behind`, `dirty`, `drifted` (gitlink méta ≠ HEAD submodule), `error` isolé par entrée), cache TTL 60 s + `force` |
| **Job de mise à jour** | `start_update` (ThreadPoolExecutor 1 worker + verrou, 409 si déjà en cours, `--dry-run` supporté pour smoke) ; subprocess `hubctl update hub` (log unique horodaté `~/.config/hubctl/update_logs/`, timeout 3600 s) ; état persistant disque `~/.config/hubctl/update_state.json` (json atomique, tail 50 lignes) — survit au redémarrage du service ET du Hub ; garde job obsolète (`running` > 65 min ⇒ `failed`) |
| **Serveur HTTP** | `ThreadingHTTPServer` (recette `atelier_serve`), port 8778, bind docker0, auth `X-Atelier-Token` constant-time (token atelier réutilisé), `GET /health` sans auth, `GET /update/status[?refresh=1]`, `POST /update/run`, `GET /update/job` ; jamais de 500 masqué |
| **CLI** | `hubctl update --serve [--port 8778] [--host HOST] [--token-file PATH]` ; cibles refusées avec `--serve` (code 2, parité atelier) |
| **systemd** | `packaging/hubctl-update.service` : durcissement complet du gabarit atelier + `ReadWritePaths=@PARC_ROOT@ %h/.config/hubctl` ; installation via `hubctl init` (`_ensure_update_service`, idempotent, jamais fatal) |
| **Tests** | +28 : 21 `test_update_serve.py` (faux dépôts git jetables + faux binaire hubctl, cycle job complet, 409, dry-run, garde obsolète, contrat argparse) + 7 `test_update_init.py` (miroir atelier : rendu, installation, dry-run) — suite complète 710 passed / 1 flaky préexistant (`test_park_checks`, hors périmètre, prouvé baseline) |

## [0.17.0] - 8 novembre 2026

### ⚡ Ateliers : overrides compose par app (`.workshop.overrides.env`) + create en allowlist

Parité CLI de HUB_base 3.83.0 — fix durable du bug « Task 0 » (constaté sur une app du parc) : les apps qui bindent des ports host métier depuis leur `.env` prod retombaient sur ces ports dans l'atelier (conflit → restart loop).

| Élément | Détail |
|---------|--------|
| **`_ws_override_env()` / `_ws_overrides_path()` / `_ws_merge_env()`** | nouveau fichier optionnel `APPS/<slug>/.workshop.overrides.env` (committé par l'app), fusionné dans l'env de tout `docker compose -p X-ws` ; précédence `_compose_base_env()` < overrides < `.workshop.env` (les 8 clés de l'allocateur gagnent toujours) ; absent → no-op strict ; illisible → warning + ignore ; clés réservées → warning + ignore |
| **`_ws_compose_env()`** | merge via `_ws_merge_env` (start/maj/ship/remove) ; fallbacks `or _compose_base_env()` → `or _ws_merge_env(app_slug, {})` (parité sans fichier) |
| **`_create_workshop`** | correction au passage : l'env subprocess passait `os.environ` complet au compose (contraire à la politique `_COMPOSE_ENV_ALLOWLIST`, incident 2026-09) → route `_ws_merge_env` (allowlist + overrides + contrat 8 clés ; `DATABASE_URL` hôte non transmise — testé) |
| **Tests** | +8 (`TestWsOverrideEnv`, `TestCreateWorkshopOverrides`) — workshop e2e : 94 verts ; étendu : 164 verts ; plein suite : 682 passed / 1 failed préexistant flaky (couplage d'ordre `test_park_checks`), présent aussi sur la base |

> Consommateur : le `.workshop.overrides.env` committé par une app du parc (`PHILIA_NG_PORT`, `PHILIA_NG_HTTPS_PORT`). Requiert HUB_base ≥ 3.83.0 pour la parité voie UI.

## [0.16.7] - 4 novembre 2026

### ⚡ Scrub open source (noms d'apps privées → génériques) + docs reproductibles

| Élément | Détail |
|---------|--------|
| **code/tests** | noms d'apps privées et du hub legacy remplacés par des génériques (`app-exemple`, `APP_EXEMPLE`, `APP_EXEMPLE_E`, `service-exemple`, `PARC_EXEMPLE`) : commentaires, docstrings, fixtures |
| **test_vault_path** | garde-fou d'existence du coffre local : `skipif` sans `parc.vault.enc` (le coffre n'est pas versionné — clone frais) |
| **README (×2)** | instructions de test reproductibles après clone frais (venv + `pip install -e hubctl_cli[dev]`) au lieu du `.venv` du poste ; section « Installation autonome » ; footer « Projet interne » retiré |
| **Tests** | 677 passed ; 5 échecs plein-suite analysés = interférences d'isolation préexistantes (présentes aussi sur main) + coffre local absent du worktree |

## [0.16.6] - 4 novembre 2026

### 🐛 Fix — doctor/update sur liens lents et remotes HTTPS sans identifiants

Retours serveur client (`doctor` tué après 30 s sur fetch, prompt d'identifiants sans terminal) :

- **Timeout configurable** : `HUBCTL_GIT_TIMEOUT` (secondes) surcharge le défaut des commandes git de `park_checks` — défaut relevé de **30 s à 120 s** (fetch à froid d'un gros dépôt sur lien lent)
- **`TimeoutExpired` converti en échec explicite** (rc 124, stderr actionnable) : `hubctl doctor` et les pré-checks d'`update` ne plantent plus avec `Command [...] timed out after 30 seconds`, ils reportent un Finding `fetch origin impossible`
- **Fetch non interactif** (`GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS=echo`) : un remote HTTPS sans credential helper échoue immédiatement avec un message clair au lieu d'attendre une saisie impossible en headless
- **Remède ciblé** sur ce cas : suggestion `remote set-url` en SSH ou credential helper HTTPS
- 7 nouveaux tests (`test_park_checks.py`) — régression 0.16.4 (`doctor` exécutable) toujours verte

## [0.16.5] - 4 novembre 2026

### ⚡ Licence open source AGPLv3 + en-têtes SPDX

- Ajout de `LICENSE` (**GNU AGPLv3**) — le dépôt n'avait aucune licence ; le CLI devient publiable en open source
- En-têtes SPDX (`AGPL-3.0-or-later` + copyright POULIDOR Audit & Développement) sur les 70 fichiers sources (CLI + debug_agent)
- `.gitignore` : exclusion `.worktrees/` ; registre d'agents retiré du suivi git (convention parc : registre local non versionné)

## [0.16.4] - 4 novembre 2026

### 🐛 Fix — `hubctl doctor` de nouveau exécutable

- **Corrigé** : `hubctl doctor` plantait systématiquement depuis son
  introduction (0.12.0) avec `name 'action_doctor' is not defined` — la
  fonction existe dans `actions.py` (5 tests unitaires verts) mais l'import
  nommé de `__main__.py` ne la listait pas. La première étape de la routine
  de mise à jour (`doctor` → `update hub` → `update all`) est à nouveau
  opérationnelle, `--fix`/`--yes` inclus.
- **Tests** : `test_doctor_wired_in_cli_main` — vérifie que
  `hubctl_cli.__main__` expose bien `action_doctor` ; le câblage CLI
  n'était couvert par aucun test, d'où le passage inaperçu du bug.

---

## [0.16.3] - 31 octobre 2026

### 🐛 Fix — unit systemd hubctl-atelier rendue à l'installation

- **Corrigé** : le gabarit `packaging/hubctl-atelier.service` codait en dur
  `User=admin` et le chemin canonique `%h/MyCOMPANY/HUB_stack/POULIDOR` — sur
  tout hôte où le parc n'appartient pas à « admin », l'unit copiée telle quelle
  (install.sh, `hubctl init`) ne pouvait pas démarrer (utilisateur inexistant)
  et le menu Coffre du Hub restait en « service injoignable ». Le gabarit porte
  désormais les placeholders `@REAL_USER@`/`@PARC_ROOT@`, substitués à
  l'installation : `_ensure_atelier_service` écrit un rendu persistant dans
  `packaging/.rendered/` (gitigné) puis l'installe — les commandes de repli
  manuel sans TTY référencent un chemin qui existe toujours ; `install.sh` du
  méta fait la même substitution côté shell (merge méta contemporain).
  Ensure inchangé : absente → rendu+install, inactive → `enable --now`,
  active → rien ; dry-run sans écriture ; étape jamais fatale (repli gabarit
  brut si le rendu échoue).
- **Docs** : README « Serveur hôte (atelier --serve) » réécrit — rendu des
  placeholders + ensure via `./hubctl init`, plus aucune supposition
  d'utilisateur ni de chemin.
- **Tests** : `test_atelier_init.py` porté à 13 cas — rendu réellement
  substitué (user + PARC_ROOT, zéro placeholder restant), repli gabarit brut
  si rendu KO (jamais fatal), dry-run zéro écriture, fichier rendu présent
  pour le rappel manuel sans TTY, et garde-fou anti-régression sur le gabarit
  livré (`User=admin` et `%h/MyCOMPANY` interdits).

---

## [0.16.2] - 29 octobre 2026

### 🐛 Fix — réplication PRA : le « dernier snapshot » est désigné par la DB

- **Corrigé** : `latest_snapshot()` interroge désormais `hub_snapshots` (status=success, ORDER BY created_at) au lieu de trier les fichiers par mtime — en réel, un snapshot créé le jour même portait un mtime d'août (le hub préserve un mtime ancien à l'écriture) et n'était **jamais répliqué** dans le kit
- Fallback mtime conservé si la DB est injoignable ou si le fichier désigné est absent
- Tests : 3 nouveaux (DB gagne sur mtime · fallback DB injoignable · fallback ligne sans fichier)

---
## [0.16.1] - 29 octobre 2026

### 🐛 pra-restore : fiabilisation du sinistre

Retours d'un test PRA réel sur VM vierge (28/10/2026) : le pipeline échouait
silencieusement en conditions réelles. Trois corrections + une investigation.

- **Corrigé (bloquant)** : la ligne `hub_snapshots` est désormais **vérifiée par
  re-SELECT après l'INSERT** — si elle ne peut pas être créée, `pra-restore`
  abandonne **avant** d'écrire `pending_restore.txt` (fini le « Snapshot
  introuvable » côté hub-api sur un état que le hub ne peut pas restaurer) ;
  l'INSERT passe par `psql` avec stderr capté et affiché (le returncode était
  ignoré), et le check/INSERT/vérification partagent le même `cwd` (`_psql`
  gagne un paramètre `cwd`, défaut `HUB_DIR`) ; re-test VM 29/10 : le SELECT de
  présence et la vérification sont en **tuples-only** (`-t -A`, comparaison du
  contenu « 1 ») — sans quoi l'en-tête psql (« ?column? / (0 rows) », émis même
  sur table vide) faisait croire à une « ligne déjà présente » sur une table
  vidée : jamais d'INSERT et protection de vérification neutralisée
- **Corrigé** : un échec de restauration côté hub-api est détecté —
  `wait_restore_done` scanne `docker logs --since <début de l'attente> hub-api`
  (marqueurs `Restauration ECHEC`/`RESTAURATION ECHEC`, re-scan final pour
  rattraper la course avec la suppression du pending) → « ❌ Restauration du
  snapshot en échec côté hub-api » + code ≠ 0 au lieu de « PRA terminé. » rc 0 ;
  pending parti sans marqueur → succès inchangé
- **Renforcé** (review) : re-scan post-healthy des échecs de restauration lente —
  le hub supprime `pending_restore.txt` AVANT de lancer le restore, donc un
  pg_restore lent se solde par un marqueur `Restauration ECHEC` loggé après la
  disparition du pending ; re-scané une fois hub-api healthy sur la même fenêtre
  → échec explicite au lieu de « ✓ Restore terminé » ; `wait_restore_done`
  retourne un statut tri-state (done/failed/timeout) et les messages timeout
  (« Restore non terminé ») et échec sont distincts
- **Corrigé** : prompt apps en pipeline — stdin fermé (EOF) n'est plus un
  « none » silencieux : avertissement explicite avec la relance scriptable ;
  la voie scriptable est documentée (docstring du module, aide `--apps`/
  `--force`/`--admin-user`, README) :
  `ADMIN_PASSWORD=… ./hubctl pra-restore ~/kit --yes --force --apps all`
- **Investigué** : CRITICAL « Clés backup ABSENTES — paire générée à chaud » au
  premier boot sur la VM — aucun défaut hubctl trouvé : noms (`backup_private_key.pem`)
  et chemins (coffre → `PARC_ROOT/HUB_base/secrets/` ↔ montage `./secrets:/code/secrets`)
  corrects, ordre restore-fichiers → build_stack correct, entrypoint sans
  destruction ; cause la plus probable : stack démarrée AVANT `pra-restore`
  (ex. `./hubctl init`), boot sans secrets légitime — les clés du coffre
  écrasent ensuite la paire à chaud (état final cohérent) ; à documenter côté
  procédure PRA, aucun fix code nécessaire
- Tests : 16 nouveaux (53 sur le fichier pra-restore — séquence
  SELECT→INSERT→vérif→pending, abandon avant pending sur INSERT KO,
  check tuples-only vs en-tête psql « (0 rows) », marqueurs d'échec logs
  y compris échec lent post-healthy, EOF prompt, docstring), suite verte

## [0.16.0] - 28 octobre 2026

### ✨ Service hôte hubctl-atelier automatique au init

- **Ajouté** : `hubctl init` met en place le service hôte `hubctl-atelier` tout seul (étape `_ensure_atelier_service`, ensure idempotent) — plus besoin de suivre la section manuelle du README : token `~/.config/hubctl/atelier_token` auto-généré (`secrets.token_hex`, chmod 600) s'il est absent ou vide et **jamais écrasé**, copie vers `HUB_base/secrets/atelier_token` (créée seulement si absente, jamais écrasée)
- **Ajouté** : installation/activation systemd automatiques — unit `packaging/hubctl-atelier.service` copiée + `daemon-reload` + `enable --now` si un TTY est disponible (sudo interactif, sortie streamée) ; service déjà actif → aucun sudo, simple ✓ ; unit présente mais inactive → seul `enable --now` est relancé
- Sans TTY (ou échec sudo) : simple rappel des commandes à lancer à la main, l'étape n'échoue **jamais** — l'init ne casse pas si sudo est indisponible ; `--dry-run` imprime les commandes sans rien écrire
- Tests : 8 nouveaux (génération/idempotence du token hôte, copie `HUB_base/secrets`, TTY vs non-TTY, déjà-actif noop, dry-run sans écriture) ; isolation des tests bootstrap existants durcie (token/units sous tmp_path)

## [0.15.0] - 28 octobre 2026

### ✨ Coffre pilotable par le Hub — routes vault du serveur atelier

- **Ajouté** : `GET /vault/status` — état du coffre pour le Hub : `{"status": "ok"|"locked", "locked", "last_backup" (mtime ISO UTC), "vault_size" (octets), "entries" {env, files, apps, db_dump}, "divergences", "pra"}` — lecture directe du coffre (déverrouillage via token stocké), aucune écriture
- **Ajouté** : `POST /vault/backup` — backup complet délégué à `VaultManager.backup_all()` dans un worker unique (ThreadPoolExecutor 1 thread) : 200 `{"status": "ok"|"locked", …}` / **409** si un backup est déjà en cours (verrou non-bloquant — retry sûr côté Hub) / **500** au-delà de la garde `_VAULT_BACKUP_TIMEOUT` (110 s : lecture disque + dump DB + réplication PRA, verrou laissé au worker)
- **Ajouté** : unit systemd durcie — `ReadWritePaths` restreint l'écriture du service au seul coffre (`…/hubctl_cli/vault` : parc.vault.enc + artefact dump DB), le reste du système reste en lecture seule
- Auth inchangée (header `X-Atelier-Token` constant-time contre le fichier token) ; docs mises à jour (README hubctl_cli, aide `--routes`)
- Tests : 11 nouveaux (39 sur le fichier atelier_serve — shapes status ok/locked, 409 concurrence, timeout 110 s → 500 puis récupération, échec PRA non fatal, coffre réel verrouillé), suite complète verte

## [0.14.1] - 27 octobre 2026

### 🐛 Fix — atelier serve joignable depuis hub-api

- **Corrigé** : `--host` paramètre le bind (défaut 127.0.0.1 inchangé) — l'unit systemd passe `--host 172.17.0.1` (passerelle docker0) : le conteneur hub-api atteint le service via `host.docker.internal` (E2E réelle : 503 systématique avec le bind loopback, token inchangé)
- Tests : 28 (nouveau test bind paramétré sur 127.0.0.2 — absence de hardcode prouvée)

## [0.14.0] - 27 octobre 2026

### ✨ Ateliers pilotables par le Hub — `hubctl atelier serve`

- **Ajouté** : `hubctl atelier --serve [--port 8777] [--token-file PATH]` — mini-serveur HTTP **stdlib** sur **127.0.0.1** uniquement : `GET /health`, `GET /atelier/list` (markers `.workshop.env` + état des conteneurs `-ws`), `POST /atelier/close-all` (appelle `action_atelier_close_all(oui=True)`) ; auth header `X-Atelier-Token` constant-time contre le fichier token
- **Ajouté** : unit systemd durcie `packaging/hubctl-atelier.service` (Restart=on-failure, NoNewPrivileges, ProtectSystem=strict, PrivateTmp) + génération conditionnelle du token (`openssl rand -hex 32`, jamais écrasé) et rappel de copie vers `HUB_base/secrets/atelier_token`
- **Shape close-all** : `{"closed": [...], "failed": [...], "rc", "output"}` — 200 / **207** échec partiel ; verrou anti-close-all concurrent
- Tests : 26 nouveaux (serveur réel sur port 0 + httpx), suite complète verte

## [0.13.1] - 27 octobre 2026

### 📣 Pour les utilisateurs
- **Modifié** : les options de fermeture en masse s'écrivent désormais en
  anglais : `./hubctl atelier --close-all` (au lieu de `--fermer-tous`) et
  `--yes` (au lieu de `--oui`), alignées sur le `--yes` existant de `doctor`.
  Comportement et codes retour inchangés.

### ⚡ Renommage des options atelier --close-all / --yes

- `--fermer-tous` → `--close-all`, `--oui` → `--yes` (dest `atelier_yes`,
  cohérent avec `doctor_yes`) ; dest interne `atelier_close_all` inchangé ;
  messages d'erreur et README ajustés.
- Les 16 tests de la feature passent sous les nouveaux noms (rouge vérifié
  avant renommage : 6 échecs parser/dispatch attendus).

## [0.13.0] - 27 octobre 2026

### 📣 Pour les utilisateurs
- **Ajouté** : `./hubctl atelier --fermer-tous` — ferme tous les ateliers ouverts
  en une seule commande (récapitulatif + confirmation unique), pour repartir
  d'une base propre sans fermer chaque atelier à la main. `--oui` permet de
  scripter la fermeture ; si un atelier pose problème, les autres sont
  quand même fermés. La production n'est jamais touchée.

### ⚡ `atelier --fermer-tous` : fermeture en masse des ateliers

- Nouvelle `action_atelier_close_all` (actions.py) : liste les ateliers actifs
  (`get_workshop_apps`), récapitulatif `app → X-ws (WS_REF @ WS_COMMIT, code)`,
  confirmation globale unique, puis `_remove_workshop` pour chacun (seul lieu
  légitime du `down -v`).
- Parser : `app` devient optionnel (`nargs="?"`) ; `--fermer-tous` rejoint le
  groupe exclusif `--maj`/`--prod` ; nouveau flag `--oui` (scriptable).
- Refus propre en non-TTY sans `--oui` (rc 2) ; rc propagé au shell
  (0 = ok / annulation, 1 = échecs partiels, 2 = refus/usage) ; un atelier en
  échec n'interrompt pas la boucle ; `--dry-run` global supporté.
- +16 tests (parser, dispatch, action) ; doc README.md + hubctl_cli/README.md.

## [0.12.1] - 27 octobre 2026

### ⚡ Findings de la revue finale (sécurité du rollback et fiabilité du diagnostic)

- **C1** : le rollback ne fait plus de `reset --hard` sur le méta si celui-ci
  portait des modifications non commitées tolérées par les pré-checks — le
  travail des sessions parallèles n'est plus détruit en cas d'échec d'update.
- **I1** : `doctor` détecte désormais les branches locales mergées restantes
  de HUB_base (`hub_base.branches`) — le `--fix` pouvait les supprimer mais
  rien ne les listait jamais.
- **I2** : fetch origin en échec (réseau) = diagnostic fail-closed sans
  remède destructeur — un `origin/main` périmé ne produit plus un faux
  « historique divergent → reset --hard » (Hub et apps).
- **I3** : `update <app>` pré-vérifie l'app (propreté/alignement) AVANT tout
  stop/pull — une app sale échoue proprement, sans rollback destructeur.
- **I4** : le mode d'`update` est explicite uniquement (`--dev`/`--prod`/
  `HUBCTL_MODE`, défaut **prod**) — jamais l'auto-détection atelier : un
  `.workshop.env` résiduel ne bascule plus un hub de prod en `up-dev`.
- **I5** : un autre submodule du méta dérivé (HUBCSS…) est bloquant au
  pré-check (il aurait cassé `submodule update`) au lieu d'un simple warning.
- **I6** : `doctor --fix` corrige aussi le méta détaché (checkout main) et
  le submodule HUB_base absent.
- +8 tests (rollback méta sale, branches, fetch fail-closed ×2, submodule
  dérivé, méta détaché réparé, mode update défaut prod, app sale).

## [0.12.0] - 27 octobre 2026

### ⚡ Unification des mises à jour — méta = source de vérité, doctor

- **`update hub` via le méta** : le Hub n'est plus mis à jour par un
  `git pull` direct dans HUB_base. Nouveau flux : pré-checks stricts
  (divergence/saleté git = échec propre avec remède exact) →
  `git pull --ff-only` du méta-repo POULIDOR → `submodule update` →
  build → up → health. Rollback étendu : pointeur méta + submodule +
  snapshot DB (le pointeur méta est restauré AVANT le submodule).
- **Nouveau `hubctl doctor [--fix] [--yes]`** : diagnostic des clones
  (méta, HUB_base, apps) — divergences d'historique, pointeurs dérivés,
  branches mergées restantes — avec remèdes exacts ; `--fix` n'applique
  que le sûr (jamais une branche non mergée, jamais les données non
  suivies), `--yes` pour le mode non interactif.
- **Mode dev respecté par `update`** : `--dev`/`--prod`/`HUBCTL_MODE`
  choisissent la cible `make up-dev` (Hub, apps, rollback) — avant :
  `up-prod`/`up` hardcodés.
- **`update all` : Hub bloquant** — le Hub est mis à jour en premier ;
  s'il échoue, les apps ne sont pas touchées (dépendance M2M). Les
  échecs d'apps restent comptés dans le code retour.
- +20 tests (park_checks, doctor, flux méta + rollback étendu, mode dev,
  ordre hub→apps).

## [0.11.1] - 17 octobre 2026

### ⚡ README — sous-commande update documentée avec sa cible obligatoire

- Table des sous-commandes : `update` → `update <cible…>` (`hub`, un ou
  plusieurs slugs d'app, `*`/`all` = Hub + toutes les apps ; sans cible :
  usage, rien n'exécuté). Documentation seule, aucun changement de code.

## [0.11.0] - 17 octobre 2026

### ⚡ update ciblé — hub | <slug> | * (rupture d'usage)

- **Rupture** : `hubctl update` **sans cible** n'exécute plus rien (usage
  affiché, code retour 1). La mise à jour globale devient `hubctl update "*"`
  (alias `all`) — quoter le `*`, sinon le shell développe le glob.
- **Ajouté** : ciblage par argument(s) : `hubctl update hub` (Hub seul),
  `hubctl update app-exemple` (une app), plusieurs slugs en une passe
  (`hubctl update hub app-exemple`). Cible inconnue → erreur listant les cibles
  valides. Code retour = nombre d'échecs.
- Factoring : validation dans `_resolve_update_targets()`, pool parallèle
  factorisé dans `_update_targets_pool()` ; `action_update_all()` (menu TUI)
  délègue à `action_update(["*"])` — comportement du menu inchangé.
- Le cycle par cible reste stop → git pull → build → up → health, avec
  snapshot DB (Hub) et rollback automatique (flux Lot 2 inchangé).
- +14 tests (parser, dispatch, résolution de cibles, comptage d'échecs).

## [0.10.7] - 12 octobre 2026

### ⚡ ensure_hub_env — APPS_HOST_PATH auto-réparée avant tout compose du Hub

- `ensure_hub_env()` : ajoute `APPS_HOST_PATH` (valeur = `APPS_DIR` réel du
  parc) au `.env` du Hub si absente ou vide, **de façon persistante**, avant
  chaque invocation compose du Hub (`update`, `start`, cleanup, sync-db,
  vpn). Répare automatiquement les serveurs installés avant HUB_base 3.71.1
  au prochain `hubctl update`/`start` — zéro action manuelle, et un
  `docker compose` lancé hors hubctl fonctionne ensuite aussi.
- Valeur custom existante jamais écrasée ; clé vide traitée comme absente ;
  dry-run sans écriture. Tests : ajout / conservation / remplissage / dry-run.

---

# Changelog — Hubctl

## [0.10.6] - 12 octobre 2026

### ⚡ fix atelier — subnet dédié garanti (fail-hard), plus de repli subnet prod

- `_create_workshop` : la création est **refusée** si aucun /24 du pool
  atelier (`10.210.0.0/24` … `10.210.63.0/24`) n'est libre. L'ancien
  fallback « subnet prod réutilisé » (simple warning) produisait des
  ateliers en conflit réseau avec la prod : `APP_SUBNET` absent du
  `.workshop.env` → le compose interpolait la valeur prod → « Pool
  overlaps » ou subnet partagé. `APP_SUBNET` est désormais **toujours
  écrit** (parité voie UI `workshop_service`, qui levait déjà
  `RuntimeError`).
- `_ws_used_subnets()` retourne `None` si l'état Docker est inconnu
  (`network ls`/`inspect` en échec, timeout, JSON invalide) → création
  refusée : on n'alloue jamais un subnet à l'aveugle (une liste
  partielle rend l'overlap-check muet).
- Warning à la création si le compose de l'app n'épingle pas
  `APP_SUBNET` (apps legacy) : le réseau atelier recevra un subnet
  dynamique Docker, non contrôlé — migration template recommandée.
- Menu `atelier` : les ateliers legacy sans `APP_SUBNET` sont signalés
  (« sans subnet dédié ») — recréer l'atelier pour une isolation réseau
  garantie.
- Tests : refus pool épuisé (plus d'« omission de clé »), refus état
  Docker inconnu, warnings legacy (création + menu), contrat
  `APP_SUBNET` toujours écrit ; `_ws_used_subnets` → `None` testé.

---

## [0.10.5] - 10 octobre 2026

### ⚡ fix atelier — health : accepter le format « status ok »

- `_is_healthy_body` exigeait `status == "healthy"` (format template
  courant). Les apps Node d'ancien template (ex. mon-app)
  répondent `{"status":"ok", …}` → faux négatif systématique : rollback à
  chaque création d'atelier, quel que soit le timeout (constaté après la
  montée à 90 s du 0.10.4 — la vraie cause n'était pas le délai).
- « healthy » et « ok » acceptés ; sensibilité à la casse conservée.
- +1 test (test_actions.py).

---

## [0.10.4] - 10 octobre 2026

### ⚡ fix atelier — clone DB consommé + health 90 s

- `_create_workshop` : le clone DB copie désormais vers le volume
  **réellement monté** par le projet `-ws` (résolu depuis
  `docker compose config` : `name:` explicite prioritaire, sinon naming
  compose par défaut `<projet>_<clé>`) au lieu de la convention
  historique `<ws-slug>-postgres-data`, jamais référencée par les
  composes à naming dynamique. Source : datadir PostgreSQL du conteneur
  prod en cours (`docker inspect APP_SLUG-postgres`). Fallbacks sur la
  convention historique.
  Conséquence terrain (mon-app) : l'atelier ne démarre plus sur
  une DB vide — et ne peut plus monter la DB de prod sur un compose à
  volumes épinglés dont le `name:` viserait le volume prod.
- Timeout health de création : 30 s → **90 s** (premier boot = initdb +
  migrations + seed, > 30 s sur DB vierge → faux échec + rollback).
  Surchageable : `WORKSHOP_HEALTH_TIMEOUT_CREATE`.
- Rollback health : tous les fichiers compose passés en `-f`
  (docker-compose.https.yml inclus), plus le seul docker-compose.yml.
- +9 tests unitaires des helpers de résolution (test_workshop_e2e.py).

---

## [0.10.3] - 5 octobre 2026

### 🔧 fix vpn — message reconcile en état résiduel

- `_vpn_reconcile` : quand `wg0.conf` est absente, le message renvoie
  désormais au toggle OFF→ON dans l'UI admin (auto-réparation,
  HUB_base ≥ 3.62.4) au lieu de « VPN → Initialiser » — l'init
  répond 409 quand le serveur est déjà initialisé.

---

## [0.10.2] — 24 septembre 2026

### ⚡ init : TTL du JWT dashboard aligné sur 8h

- **Fix** : le `.env` généré par `hubctl init` écrivait `ACCESS_TOKEN_EXPIRE_MINUTES=30`,
  écrasant le défaut 480 (8h) présent partout ailleurs (code HUB_base, docker-compose,
  install.sh, `.env.example`) — tout déploiement initialisé via hubctl se retrouvait avec
  des sessions de 30 min. Génère désormais `ACCESS_TOKEN_EXPIRE_MINUTES=480` avec la note
  LAN (revenir à 30 si exposition externe), aligné sur la décision HUB_base
  (`SECURITY_AUDIT.md` §9).

## [0.10.1] — 21 septembre 2026

### ⚡ park-restore : migration legacy → hub neuf fiabilisée

- **Reset de la base hub avant `pg_restore`** (`_reset_hub_db`, fatal) : un dump legacy
  restauré sur le schéma courant du hub neuf échouait (FKs de tables courantes-only
  bloquant les DROP, COPY legacy refusés sur les colonnes récentes NOT NULL). La base
  est recréée (`DROP DATABASE WITH (FORCE)` + `CREATE`, deux `-c` psql distincts —
  `DROP DATABASE` est interdit dans un bloc transactionnel).
- **Réconciliation des migrations fourchues** (`_reconcile_forked_migrations`, non
  fatal) : purge de la ligne `027` d'un dump legacy quand la table `port_allocations`
  attendue par notre 027 est absente (même sémantique que
  `HUB_base/hub/app/restore_snapshot.py::FORKED_MIGRATIONS`) — le boot rejoue notre 027.
- Tests : +7 sur `test_park_restore.py` (22 total). Prouvé de bout en bout en répétition
  VM (journal `build/vm-lab/client-mig-legacy/journal.md` du méta-repo) : restore legacy
  complet, 027 purgée puis rejouée, `port_allocations` créée, comptages hub identiques.

## [0.10.0] — 19 septembre 2026

### ⚡ HTTPS par défaut sur la flotte — init + ateliers

- `hubctl init` : HTTPS_ENABLED=true écrit explicitement dans le .env généré quand le
  .env.example de l'app ne définit pas la clé (choix app respecté sinon).
- Ateliers : HTTPS_ENABLED=true dans .workshop.env + override docker-compose.https.yml
  chargé quand le checkout l'embarque (l'atelier reste accessible, le :80 renvoie un 301).
- Spec : docs/superpowers/specs/2026-09-19-https-par-defaut-flotte-design.md (méta).

## [0.9.1] — 17 septembre 2026

### ⚡ Documentation — README racine du dépôt

- **Créé** : `README.md` à la racine (il n'existait qu'un README dans `hubctl_cli/`) —
  rôle de hubctl, place dans le parc (Mermaid), quickstart, tableau des 13 sous-commandes
  à jour du parser (dont `jwt` v0.9.0), tests, liens.
- `hubctl_cli/README.md` : bannière + badge de version + renvoi vers le README racine ;
  contenu existant (menu TUI, atelier, coffre, PRA) inchangé.

## [0.9.0] — 16 septembre 2026

### ⚡ Sous-commande `hubctl jwt` — gestion du trousseau de clés JWT

- **Ajouté** : `hubctl jwt keys|rotate|revoke|cleanup` — pilotage du trousseau multi-kid
  du Hub (HUB_base ≥ 3.59.0) via l'API `/api/auth/keys*` (rotation sans rupture,
  révocation d'urgence, nettoyage opt-in).
- **Corrigé** : login admin sur `/api/auth/token` (`/api/token` est un endpoint mort
  depuis le déplacement des routes auth) — login local à `jwt_keys.py`, l'import
  `pra_restore.admin_login` n'est plus utilisé.

---

## [0.8.2] — 14 septembre 2026

### ⚡ Update : tolérance au HEAD détaché

- **Corrigé** : `hubctl update` échouait en rollback complet sur les déploiements
  posés par copie/export — leur dépôt git est en HEAD détaché et `git pull`
  refusait (« You are not currently on a branch »). Désormais, avant chaque
  pull, `update` détecte le détachement et rattache la branche `main` au commit
  déployé (`checkout -B main` : l'état déployé fait foi), puis restaure
  l'upstream absent (repo jamais cloné, `branch --set-upstream-to
  <remote>/main`) pour que le pull sache quoi tirer. Cas nominal (branche
  rattachée) strictement inchangé. Incident prod site client 2026-09-14
  (update hub 3.54.0 → 3.54.1 en rollback).
- Tests : 4 nouveaux (repos git réels — rattachement, no-op sur branche,
  restauration d'upstream, ordre attach < pull dans `_update_target`) ;
  suite : 456 passed.

## [0.8.1] — 13 septembre 2026

### ⚡ Seed : APP_SUBNET écrit dès l'allocation

- **Corrigé** : `hubctl seed` génère désormais le `.env` avec `APP_SUBNET` — la
  configuration est allouée officiellement auprès du Hub
  (`POST /api/apps/allocate-ports` : ports préservés par slug+host **et**
  subnet), ce qui supprime l'échec de `make build`
  (« APP_SUBNET manquant ») survenant avant que le `sync-env` de l'app n'ait
  pu interroger le Hub. Fallback silencieux sur les ports locaux si le Hub est
  injoignable. Subnet affiché dans le récapitulatif de confirmation.
- Validé en réel : allocation 9008/4208 + subnet 10.50.13.0/24 écrits dans le
  `.env` généré ; 430 tests verts.

---
## [0.8.0] — 9 septembre 2026

### ⚡ Socle HUBsocle : commandes `socle build` / `socle status`

- **Ajouté** : `hubctl socle build [--force]` — construction locale (offline-first)
  des images de base du socle `poulidor/socle-api` et `poulidor/socle-web` depuis le
  checkout `HUBsocle` (sous-module méta-repo ou sibling) ; cache par tag (rebuild
  uniquement si le tag `:<VERSION>` est absent, `--force` pour forcer) ; échec
  explicite si un build échoue.
- **Ajouté** : `hubctl socle status` — tableau de drift flotte : `APP_SOCLE_VERSION`
  par app (`APPS/*/.env`, `MIGRATION` et dossiers cachés exclus) vs version courante
  du sous-module HUBsocle ; apps sans `APP_SOCLE_VERSION` marquées « fork template ».
- **Technique** : module `hubctl_cli/socle.py` (helpers purs testés :
  `socle_version`, `collect_app_socle_versions` ; `-f` Dockerfile web en chemin
  absolu — le wrapper `./hubctl` fait `cd hubctl_src`) ; sous-commandes argparse
  `socle build|status` câblées sur le pattern `vault` ; `--dry-run` global respecté.

## [0.7.4] — 6 septembre 2026

### 🔧 Atelier : allowlist env compose (parité hub-api 3.47.3)

- **Corrigé** : `_ws_compose_env` ne transmet plus l'environnement du shell
  hôte au `docker compose` atelier — allowlist `_COMPOSE_ENV_ALLOWLIST`
  (PATH/HOME/locales + DOCKER_*) + `.workshop.env`. Les 4 fallbacks
  `or dict(os.environ)` (maj, rollback, stop, remove) passent à
  `_compose_base_env()`.
- **Cause** (incident 2026-09) : `HUB_HTTPS_ENABLED` a deux sens opposés (hub =
  « le hub sert du HTTPS » ≠ app = « joindre l'API hub en HTTPS ») ; une clé
  exportée dans le shell hôte écrasait le `.env` de l'app via l'interpolation
  `${VAR:-défaut}` du compose → atelier en « Mode dégradé »
  (SSL WRONG_VERSION_NUMBER, enregistrement + JWKS en échec).
- **Note** : `run_shell(env=)` remplace l'environnement (pas de fusion) — le
  fix est localisé dans `_ws_compose_env`, sans mécanisme `env_base` comme
  côté hub-api.
- **Tests** : `test_ne_fuit_pas_env_hote` (rouge → vert) ; suite complète
  492 passed.

## [0.7.3] — 2 septembre 2026

### 🔧 Chemins runtime résolus depuis le paquet (coffre retrouvé)

- **Corrigé** : la restructuration v0.51.0 (suppression du symlink racine
  `hubctl_cli`) rendait le coffre introuvable — le code cherchait
  `<PARC_ROOT>/hubctl_cli/vault/parc.vault.enc`, chemin qui n'existe plus.
  Conséquence : le coffre existant était traité comme **neuf** (prompt
  passphrase au lieu du déverrouillage par `parc.vault.key`, et un
  `vault backup` risquait d'écrire un coffre vide en orphelinant le vrai).
- **Nouveau** : `hub_state.vault_dir()` / `vault_path()` / `backups_dir()` /
  `recovery_dir()` — résolution unique pour toutes les données runtime :
  emplacement canonique **dans le paquet** (`hubctl_src/hubctl_cli/vault|backups|recovery/`,
  gitignorés du submodule), avec **repli legacy** `<PARC_ROOT>/hubctl_cli/<name>/`
  pour les installations où `hubctl_cli/` reste un vrai dossier racine.
  Sites migrés : `__main__.py` (CLI vault + TUI), `actions.py` (`_get_vault`,
  backups avant suppression, snapshots DB), `pra_config.config_path()`,
  `pra_replicate.vault_path()`, `vault_manager.export_recovery()`.
- **Tests** : nouveau `test_vault_path.py` (7 tests — priorité paquet, repli
  legacy, défaut paquet, garde-fou installation réelle) ; tests PRA/vault
  rendus hermétiques (patch de `vault_dir`/`recovery_dir`, fin de pollution du
  coffre réel en CI/dev) ; `test_slug_with_uppercase` aligné sur la
  normalisation 0.7.2 (lowercase). Suite : **491 passed**.

## [0.7.2] — 2 septembre 2026

### 🔧 Wrapper symlink + slug atelier strict

- **Corrigé** : le wrapper `hubctl` résout les symlinks (`BASH_SOURCE` suivi jusqu'au
  fichier réel) — appelé depuis la racine du parc via le lien `./hubctl → hubctl_src/hubctl`,
  `ROOT_DIR` ne pointe plus la racine du méta-repo ; `.secrets` est désormais lu depuis
  `PARC_ROOT` (racine du parc, chemin canonique ou non).
- **Corrigé** : `_ws_project` slugifie entièrement le nom de projet compose de l'atelier
  (lowercase **et** `_`→`-`) — l'`APP_SLUG` atelier reste identique au slug enregistré
  auprès du Hub (normalisé en tirets), sinon la résolution des rôles utilisateur JWT ne
  matche plus (app vue « sans rôle »).
- **Corrigé** (commit 3bbc6be, resté non releasé) : subnet IPAM None-safe, projet compose
  lowercase, volume source basé sur `APP_SLUG`.
- **Doc** : README `hubctl_cli` resynchronisé (modes `--dev`/`--prod` + auto-détection
  atelier via `.workshop.env`, barre de chargement réelle, retrait de l'intro ASCII).

## [0.7.1] — 31 août 2026

### 🔧 Ports réels dans l'outillage

- **Corrigé** : `OIDC_ISSUER` est réécrit automatiquement quand `setup_wizard`
  change `HUB_API_PORT` (si auto-généré ; un issuer personnalisé n'est jamais
  touché) — le template initial reste cohérent (`http://{host}:{port}`).
- **Corrigé** : le résumé post-init affiche les URLs avec les ports réels du
  `.env` (`HUB_FRONTEND_PORT`/`HUB_HTTPS_PORT`/`HUB_API_PORT`) au lieu de
  `:80`/`:8000` en dur.
- **Tests** : +4 (résumé avec/sans env, cohérence issuer/port, réécriture wizard
  + préservation d'un issuer personnalisé).

## [0.7.0] — 24 août 2026

### 🔐 Coffre — passphrase auto-générée (fin de la saisie manuelle)

- **Ajouté** : à la création du coffre, la passphrase est générée automatiquement
  (`secrets.token_urlsafe`) et conservée dans `hubctl_cli/vault/parc.vault.key`
  (chmod 0600, gitignoré) — zéro saisie, déverrouillage silencieux à chaque démarrage
  (TUI, sous-menu Coffre, `hubctl vault …`).
- **Ajouté** : migration douce des coffres existants — la passphrase est demandée une
  dernière fois puis mémorisée ; « 🔑 Changer la passphrase » resynchronise la clé.
- **Ajouté** : kit PRA — `parc.vault.key` est répliqué avec le coffre (sans elle le kit
  serait indéchiffrable) ; `pra-restore` lit la clé du kit (repli saisie pour les kits
  anciens).
- **Sécurité (assumée)** : la clé vit en clair à côté du coffre — le coffre est une
  sauvegarde lecture-seule, pas une frontière (les `.env` sont déjà en clair sur disque).
- **Tests** : +19 (4 remplacés — 479 passed).

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
