# hubctl — Menu CLI de pilotage du hub

Menu interactif pour piloter le hub POULIDOR depuis le serveur : intro animée
(oiseau → flock → « HUB ») + barre de chargement réelle, puis menu de pilotage.

## Lancement

```bash
./hubctl                 # intro animée + menu
./hubctl --no-anim       # sans intro
./hubctl --dry-run       # imprime les commandes sans les exécuter
./hubctl --anim random   # choisit une animation au hasard
```

Au premier lancement, le launcher crée le venv `hubctl_cli/.venv` et installe
`rich`, `questionary`, `cryptography`. En l'absence de terminal interactif (pipe,
cron, SSH non interactif), `hubctl` affiche l'état du hub puis quitte (le menu
requiert un TTY).

## Menu

Update · Ajouter une app · Supprimer une app · Réparer/Diagnostiquer · Logs ·
Start/Stop hub · Apps (vue + disque) · Coffre (secrets chiffrés).

Les opérations s'appuient sur les `Makefile` du parc : `make -C HUB_base …`
(hub central) et `make -C APPS/<nom> …` (apps).

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

Déposer un fichier `.py` dans `hubctl_cli/animations/` exposant :

```python
NAME = "mon-animation"
DESCRIPTION = "…"

def play(console, state):
    ...
```

Il est auto-découvert. Sélection : `--anim <NAME>`.

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
