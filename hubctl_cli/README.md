# hubctl — Menu CLI de pilotage du hub

![version](https://img.shields.io/badge/version-0.9.0-blue)

> 📦 Paquet principal du dépôt [Hubctl](../README.md) — voir le README racine pour la
> vue d'ensemble et la liste des sous-commandes. Ce document détaille le fonctionnement
> interne : menu TUI, atelier, coffre, PRA, garde-fous.

Menu interactif pour piloter le hub POULIDOR depuis le serveur : barre de
chargement réelle (Docker, services, apps, disque), puis menu de pilotage.

## Lancement

```bash
./hubctl                 # barre de chargement + menu
./hubctl --no-anim       # sans animation de chargement
./hubctl --dry-run       # imprime les commandes sans les exécuter
./hubctl --dev           # force le mode DEV (ateliers)
./hubctl --prod          # force le mode PROD
```

Le mode est **auto-détecté** : DEV dès qu'un atelier est actif
(`.workshop.env` dans `APPS/`), PROD sinon — les flags `--dev`/`--prod` (ou
`HUBCTL_MODE`) forcent l'un ou l'autre.

Au premier lancement, le launcher crée le venv `hubctl_src/hubctl_cli/.venv` et
installe les dépendances (`rich`, `cryptography`, `mcp`, `pyyaml`). En l'absence
de terminal interactif (pipe, cron, SSH non interactif), `hubctl` affiche l'état
du hub puis quitte (le menu requiert un TTY).

## Menu

Menu plat, touches différentes selon le mode courant :

| Mode | Touches |
|------|---------|
| **PROD** | `1` Démarrer tout · `2` Arrêter tout · `3` Mettre à jour · `4` Coffre · `0`/`Échap` Quitter |
| **DEV** | `1` Démarrer prod · `2` Arrêter prod · `3` Démarrer dev · `4` Arrêter dev · `5` Mettre à jour · `6` Coffre · `0`/`Échap` Quitter |

Le tableau de bord affiche en permanence les services, le disque, les apps
(version, git, statut) et une alerte `⚠ PRA` si l'emplacement de secours est
absent ou non synchronisé depuis 7+ jours.

## Atelier (workshop)

```bash
./hubctl atelier <app>                  # créer l'atelier, ou menu 4 choix s'il existe
./hubctl atelier <app> --maj            # MAJ atelier : git fetch + checkout origin/main + rebuild -ws
./hubctl atelier <app> --maj --ref R    # idem sur une branche/tag (défaut: main)
./hubctl atelier <app> --prod           # déployer origin/main validé en prod + supprimer l'atelier
./hubctl atelier --close-all          # fermer TOUS les ateliers actifs (confirmation globale O/N)
./hubctl atelier --close-all --yes    # idem, sans confirmation (scriptable)
./hubctl --dry-run atelier <app> --maj  # simuler — --dry-run est un flag GLOBAL (avant le subcommand)
```

- L'atelier teste les montées de version sur le clone de DB (migrations rejouées au boot).
- `--prod` exige que l'atelier ait testé `main` au commit courant d'`origin/main`.
- Rollback git automatique en cas d'échec ; atelier conservé si le déploiement prod échoue.

### Serveur hôte (`atelier --serve`) et service systemd

Mini-serveur HTTP que le Hub proxy pour piloter les ateliers et le coffre :
`GET /health`, `GET /atelier/list`, `POST /atelier/close-all`,
`GET /vault/status`, `POST /vault/backup`. Toute route
sauf `/health` exige le header `X-Atelier-Token` (le vrai garde-fou
d'exposition). Bind `127.0.0.1` par défaut ; `--host` permet de l'ouvrir au
strict nécessaire — ex. la passerelle docker0 (`ip -4 addr show docker0`,
souvent `172.17.0.1`, mais PAS toujours : les pools personnalisés du
daemon, cf. `docker setup-pools`, peuvent la déplacer — ex. `10.200.0.1`)
pour être joignable des conteneurs via `host.docker.internal`, sans être
visible du LAN (c'est le bind de l'unit systemd, cf. `ExecStart` :
placeholder `@DOCKER_GATEWAY@` substitué au rendu par
`docker_pools.docker0_gateway`).

```bash
./hubctl atelier --serve                                        # port 8777, token ~/.config/hubctl/atelier_token
./hubctl atelier --serve --port 8800 --token-file ~/autre_token # variantes
./hubctl atelier --serve --host 172.17.0.1                      # joignable des conteneurs (host-gateway)
```

Installation en service (unit : [`packaging/hubctl-atelier.service`](../packaging/hubctl-atelier.service),
placeholders `@REAL_USER@`/`@PARC_ROOT@` substitués à l'installation —
l'unit livrée ne suppose plus d'utilisateur ni de chemin en dur) :

```bash
# 1. Générer le token UNE seule fois (s'il n'existe pas déjà)
mkdir -p ~/.config/hubctl
[ -s ~/.config/hubctl/atelier_token ] || openssl rand -hex 32 > ~/.config/hubctl/atelier_token
chmod 600 ~/.config/hubctl/atelier_token

# 2. Rappel — le Hub lit le MÊME token depuis son dépôt (copie MANUELLE :
#    HUB_base est un autre dépôt, rien n'est copié automatiquement)
cp ~/.config/hubctl/atelier_token <parc>/HUB_base/secrets/atelier_token

# 3. Prérequis : le venv hubctl_cli/.venv doit exister (créé par n'importe
#    quel lancement de ./hubctl). L'unit, en lecture seule stricte hors
#    coffre (ReadWritePaths), ne peut pas le construire — sans lui, le
#    service échoue en boucle.

# 4. Rendre l'unit (substitution des placeholders) puis l'activer —
#    fait automatiquement par `./hubctl init` ; à la main, depuis hubctl_src :
sed -e "s|@REAL_USER@|$USER|g" \
    -e "s|@PARC_ROOT@|$(cd .. && pwd)|g" \
    packaging/hubctl-atelier.service > /tmp/hubctl-atelier.service
sudo install -m 644 /tmp/hubctl-atelier.service /etc/systemd/system/hubctl-atelier.service
sudo systemctl daemon-reload
sudo systemctl enable --now hubctl-atelier.service
journalctl -u hubctl-atelier -f     # suivi ; si le token est absent,
                                    # le service échoue visiblement (rc 2)
```

L'unit durcit l'exécution (`Restart=on-failure`, `NoNewPrivileges`,
`ProtectSystem=strict`, `PrivateTmp=true`) : le serveur n'écrit QUE dans le
coffre via `POST /vault/backup` — `ReadWritePaths` ouvre l'écriture sur
`hubctl_cli/vault` (parc.vault.enc, `.bak`, artefact dump DB), tout le reste
du système reste en lecture seule ; il lit le token et le code et interroge
Docker via son socket. `./hubctl init` fait l'étape 4 tout seul (ensure
idempotent : rendu + installation si absente, `enable --now` si inactive,
rien si active) et rappelle les commandes à lancer à la main quand aucun TTY
ne permet le sudo.

> **Note** : les anciennes docs référencent les commandes `work`, `ship`,
> `deploy`, multi-slug et `--copy-db`. **Aucune de ces commandes n'est
> exposée dans le parser actuel** — le cycle complet (création, MAJ,
> déploiement prod, suppression) est porté par `atelier` et ses flags.
> Voir le skill `app-workshop-mover` pour le détail du cycle.

Le cycle atelier se pilote en CLI (`./hubctl atelier` et ses flags) ou depuis
l'UI HUB (vue Santé des Applications).

Les opérations s'appuient sur les `Makefile` du parc : `make -C HUB_base …`
(hub central) et `make -C APPS/<nom> …` (apps, y compris pour l'atelier qui
tourne dans le même dossier via un 2e projet compose `-p X-ws`).

### Agent de mise a jour (`update --serve`) et service systemd

Serveur HTTP que le Hub proxy pour le « Centre de mises à jour » :
`GET /health`, `GET /update/status`, `POST /update/run`, `GET /update/job`.
Même auth `X-Atelier-Token` que l'atelier — le token est **réutilisé**
(`~/.config/hubctl/atelier_token`, rien à générer ni copier). Port par
défaut `8778`, bind `127.0.0.1` ; `--host` permet de l'ouvrir au strict
nécessaire — ex. la passerelle docker0 (`ip -4 addr show docker0`,
souvent `172.17.0.1`, mais PAS toujours : les pools personnalisés du
daemon, cf. `docker setup-pools`, peuvent la déplacer — ex. `10.200.0.1`)
pour être joignable des conteneurs via `host.docker.internal`,
sans être visible du LAN. L'unit systemd porte le placeholder
`@DOCKER_GATEWAY@`, substitué au rendu par `docker_pools.docker0_gateway`
(jamais d'IP en dur — un bind sur une IP absente rend le service
injoignable depuis les conteneurs, cf. CHANGELOG 15/11/2026). L'agent met
à jour le PARC ENTIER : ne pas lui passer de cible
(`update <cible> --serve` refuse, code 2 — la cible se passe
à `POST /update/run`) :

```bash
./hubctl update --serve                                       # port 8778, token atelier réutilisé
./hubctl update --serve --host 172.17.0.1                     # joignable des conteneurs (host-gateway)
./hubctl update --serve --port 8800 --token-file ~/autre_token # variantes
```

Installation en service (unit : [`packaging/hubctl-update.service`](../packaging/hubctl-update.service),
placeholders `@REAL_USER@`/`@PARC_ROOT@` substitués à l'installation,
même recette que l'atelier) — fait automatiquement par `./hubctl init` ;
à la main, depuis hubctl_src :

```bash
sed -e "s|@REAL_USER@|$USER|g" \
    -e "s|@PARC_ROOT@|$(cd .. && pwd)|g" \
    packaging/hubctl-update.service > /tmp/hubctl-update.service
sudo install -m 644 /tmp/hubctl-update.service /etc/systemd/system/hubctl-update.service
sudo systemctl daemon-reload
sudo systemctl enable --now hubctl-update.service
journalctl -u hubctl-update -f      # suivi ; si le token est absent,
                                    # le service échoue visiblement (rc 2)
```

L'unit durcit l'exécution comme l'atelier (`Restart=on-failure`,
`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp=true`), mais son
`ReadWritePaths` est plus large : git/make écrivent dans tout le parc
(pull méta + submodules, rebuild Hub + apps) et l'état du job + les logs
vivent dans `~/.config/hubctl` (`update_state.json`, `update_logs/`).
Prérequis identique : le venv `hubctl_cli/.venv` doit exister avant
l'activation (créé par n'importe quel lancement de `./hubctl`).

Toutes les confirmations sont des **choix** (Oui/Non), pas de la saisie libre.

**Ajouter une app** : via un fichier seed — `./hubctl seed apps.yaml` (clone,
détection de config, écriture du `.env`, build, démarrage, sync coffre) — ou en
clonant manuellement dans `APPS/<nom-du-dépôt>` puis en lançant
`INITIALISE.py`.

## Raccourcis clavier

| Touche | Où | Action |
|--------|-----|--------|
| **↑↓** | Menu, sous-menus | Naviguer |
| **Entrée** | Partout | Valider |
| **Échap** | Menu principal | Quitter |
| **Échap** | Sous-menu, saisie | Revenir au menu principal |
| **Ctrl-C** | Action en cours | Interrompre et revenir au menu |
| **Ctrl-C** | Menu principal | Rester au menu |

## Coffre

Secrets chiffrés (Fernet) dans `hubctl_cli/vault/parc.vault.enc`. La passphrase
maître est **auto-générée au premier besoin et mémorisée** dans
`hubctl_cli/vault/parc.vault.key` (chmod 0600) : les démarrages suivants
déverrouillent le coffre sans saisie. Rotation possible via le menu
(**Coffre → 🔑 Changer la passphrase**). Sources lues : `HUB_base/.env`,
`APPS/*/.env`, `HUB_base/secrets/`. Synchronisation manuelle, et automatique avant
toute suppression d'app.

## Chargement

Le démarrage affiche une **barre de chargement réelle** (connexion Docker,
services du hub, inventaire des apps, disque, version du hub) ; `--no-anim` la
rend instantanée. Les anciens modules d'intro animée
(`animations/hub_ascii.py`, `animations/fly_to_flock.py`) sont toujours présents
dans le dépôt mais ne sont plus joués au démarrage.

## Garde-fous

- Confirmation pour toute action mutante ; `--dry-run` global.
- Corbeille `hubctl_cli/_trash/` (pas de `rm -rf`), backup `tar.gz` dans
  `hubctl_cli/backups/` avant suppression.
- Garde disque : avertissement < 15 % libre, refus (bypass) < 5 %.

## Tests

Depuis la racine du dépôt, après un clone frais (venv dédié, l'extra `dev`
apporte pytest) :

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e hubctl_cli[dev]
pytest hubctl_cli/tests -q
```

## PRA — Kit de secours et restauration

Le coffre et le dernier snapshot du HUB sont répliqués automatiquement vers un
**emplacement secondaire** (clé USB, 2e disque, montage réseau) à chaque
sauvegarde du coffre — l'emplacement doit être **hors du disque serveur**
(validé automatiquement). Configuration : au premier lancement de hubctl, ou
dans **Coffre → 🔄 Emplacement PRA** (statut, changer, répliquer, retirer).

Contenu du kit : `parc.vault.enc` (+ `.bak`), le dernier
`snapshot_*.tar.zst.enc`, `HUB-PRA-KIT.json` (marqueur) et `PRA-README.txt`.
Le dashboard affiche `⚠ PRA` si l'emplacement est absent ou non synchronisé
depuis 7+ jours.

### Restaurer sur un serveur neuf

```bash
git clone <repo POULIDOR> && cd POULIDOR
./hubctl pra-restore          # assistant interactif (crée le venv au besoin)
```

L'assistant : détecte le kit branché → passphrase du coffre → restaure
secrets/.env → build & start de la stack → injection du snapshot →
attente du restore interne → sélection des apps à rebuilder/réinjecter.
Arguments : `kit` (positionnel, scan auto si omis) ; fanions `--snapshot`,
`--apps all|none|a,b`, `--admin-user`, `--yes`, `--force`, `--dry-run`.

### Restauration scriptable (pipeline)

Les prompts attendent un TTY — en pipeline/cron, stdin est avalé par les
commandes du processus : passer les choix en fanions et variables d'env
(`ADMIN_PASSWORD`/`ADMIN_USERNAME` pour la réinjection des données) :

```bash
# interactif : ./hubctl pra-restore
ADMIN_PASSWORD=… ./hubctl pra-restore ~/kit --yes --force --apps all
```

---

Distribué sous licence [AGPL-3.0](../LICENSE) (GNU Affero General Public License v3).
