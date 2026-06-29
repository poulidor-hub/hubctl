# Intro animée hubctl (Node / @opentui)

Animation jouée par `./hubctl` **avant** le menu Python (mode hybride). Le launcher
ne la déclenche que si **toutes** ces conditions sont réunies — sinon il bascule
automatiquement sur l'intro Python (`fly-to-flock`), donc rien n'est jamais cassé :

1. terminal interactif (TTY) et lancement sans `--no-anim` ;
2. `node` disponible dans le PATH ;
3. dépendances installées (`hubctl_cli/intro/node_modules/.bin/tsx` présent) ;
4. fichier `ascii-motion-tui.tsx` présent dans ce dossier.

## Activation

1. **Déposer l'animation** : copier `ascii-motion-tui.tsx` (exportant `AsciiMotionTui`)
   dans `hubctl_cli/intro/`.
2. **Installer les dépendances** :
   ```bash
   cd hubctl_cli/intro && npm install
   ```
   Ajuster les versions de `@opentui/core`, `@opentui/react`, `react` dans
   `package.json` si nécessaire.
3. Lancer `./hubctl` : l'intro Node se joue (10 s, `loop=false`), puis le menu Python.

## Comportement

- **Durée** : 10 s (configurable via `HUBCTL_INTRO_MS`).
- **Skip** : n'importe quelle touche termine l'intro immédiatement.
- `main.tsx` rend `<AsciiMotionTui autoPlay loop={false} hasDarkBackground />`, puis
  `process.exit(0)` à la fin (ou au skip), rendant la main au launcher.

## Test manuel de l'intro seule

```bash
cd hubctl_cli/intro && npm run play
```
