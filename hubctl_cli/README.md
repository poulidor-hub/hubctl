# hubctl — Menu CLI de pilotage du hub

Menu interactif pour piloter le hub POULIDOR depuis le serveur : intro animée
(animation HUB en ASCII art, ~10 s) + barre de chargement réelle, puis menu de pilotage.

## Lancement

```bash
./hubctl                 # intro animée + menu
./hubctl --no-anim       # sans intro
./hubctl --dry-run       # imprime les commandes sans les exécuter
./hubctl --anim random   # choisit une animation au hasard
```

Au premier lancement, le launcher crée le venv `hubctl_cli/.venv` et installe
`rich` et `cryptography`. En l'absence de terminal interactif (pipe,
cron, SSH non interactif), `hubctl` affiche l'état du hub puis quitte (le menu
requiert un TTY).

## Menu

Update · Ajouter une app · Supprimer une app · Logs · **Atelier (workshop)*** ·
Réparer/Diagnostiquer · Start/Stop hub · Apps (vue + disque) · Quick setup · Réseau ·
Admin · Coffre (secrets chiffrés) · Scénarios · Bascule DEV/PROD.

<small>* L'atelier (mode DEV) permet de mettre une app en atelier (clone DB) et
de supprimer l'atelier.</small>

## Atelier (workshop)

```bash
./hubctl atelier X           # mettre X en atelier (clone DB) — ou supprimer si déjà en atelier (toggle)
./hubctl atelier X --dry-run # simuler sans rien exécuter
```

> **Note** : les anciennes docs référencent les commandes `work`, `ship`,
> `deploy`, multi-slug et `--copy-db`. **Aucune de ces commandes n'est
> exposée dans le parser actuel** — seul `atelier` l'est. Le déploiement
> atelier → prod se fait manuellement (`cd APPS/X && git pull && make build && make up`).
> Voir le skill `app-workshop-mover` pour le détail du cycle.

Depuis le menu TUI (mode DEV) : **Apps → Atelier**.

Les opérations s'appuient sur les `Makefile` du parc : `make -C HUB_base …`
(hub central) et `make -C APPS/<nom> …` (apps, y compris pour l'atelier qui
tourne dans le même dossier via un 2e projet compose `-p X-ws`).

Toutes les confirmations sont des **choix** (Oui/Non), pas de la saisie libre.

**Ajouter une app** = cloner un dépôt : tu saisis l'URL git
(ex: `git@github.com:Poutchouli/exemple-app-2.git`), `hubctl` fait `git clone` dans
`APPS/<nom-du-dépôt>` (le clone hérite des droits SSH du serveur), puis propose de
lancer `INITIALISE.py`.

## Raccourcis clavier

| Touche | Où | Action |
|--------|-----|--------|
| **↑↓** | Menu, sous-menus | Naviguer |
| **Entrée** | Partout | Valider |
| **Échap** | Menu principal | Quitter |
| **Échap** | Sous-menu, saisie | Revenir au menu principal |
| **Ctrl-C** | Action en cours | Interrompre et revenir au menu |
| **Ctrl-C** | Menu principal | Rester au menu |
| Toute touche | Intro animée | Passer l'intro |

## Coffre

Secrets chiffrés (Fernet) dans `hubctl_cli/vault/parc.vault.enc`, déverrouillé par
une passphrase maître (jamais stockée). Sources lues : `HUB_base/.env`,
`APPS/*/.env`, `HUB_base/secrets/`. Synchronisation manuelle, et automatique avant
toute suppression d'app.

## Animations

L'intro par défaut est **`hub-ascii`** : une animation HUB en ASCII art (~240 frames,
~10 s) rejouée avec Rich. Les frames sont précalculées et stockées compressées dans
`hubctl_cli/animations/hub_ascii.frames.gz`.

Les animations sont **pluggables** : déposer un fichier `.py` dans
`hubctl_cli/animations/` exposant :

```python
NAME = "mon-animation"
DESCRIPTION = "…"

def play(console, state):
    # state["sleep"](secondes) -> True si l'utilisateur a pressé une touche (skip)
    ...
```

Il est auto-découvert. Sélection : `--anim <NAME>` (ou `--anim random`).
Animations fournies : `hub-ascii` (défaut), `fly-to-flock`.

## Garde-fous

- Confirmation pour toute action mutante ; `--dry-run` global.
- Corbeille `hubctl_cli/_trash/` (pas de `rm -rf`), backup `tar.gz` dans
  `hubctl_cli/backups/` avant suppression.
- Garde disque : avertissement < 15 % libre, refus (bypass) < 5 %.

## Tests

```bash
hubctl_cli/.venv/bin/pip install pytest
hubctl_cli/.venv/bin/python -m pytest hubctl_cli/tests/ -v
```
