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

```bash
cd hubctl_src
hubctl_cli/.venv/bin/pip install pytest
hubctl_cli/.venv/bin/python -m pytest hubctl_cli/tests/ -v
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
