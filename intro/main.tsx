// Point d'entrée de l'intro animée hubctl.
// Joue l'animation <AsciiMotionTui /> (10 s par défaut, loop=false),
// puis rend la main (process.exit 0) — soit à la fin de la durée, soit dès
// qu'une touche est pressée (skip). Le launcher ./hubctl enchaîne ensuite
// sur le menu Python.
//
// REQUIS : déposer le fichier `ascii-motion-tui.tsx` (fourni par l'utilisateur)
// dans ce dossier, exportant `AsciiMotionTui`.
import { createCliRenderer } from '@opentui/core';
import { createRoot } from '@opentui/react';
import React from 'react';
import { AsciiMotionTui } from './ascii-motion-tui';

const DURATION_MS = Number(process.env.HUBCTL_INTRO_MS ?? 10000);

async function main() {
  const renderer = await createCliRenderer();
  const root = createRoot(renderer);

  let done = false;
  const finish = (code = 0) => {
    if (done) return;
    done = true;
    try {
      (renderer as any).destroy?.();
    } catch {
      /* ignore */
    }
    process.exit(code);
  };

  // Skip : n'importe quelle touche termine l'intro immédiatement.
  if (process.stdin.isTTY) {
    try {
      process.stdin.setRawMode(true);
    } catch {
      /* ignore */
    }
    process.stdin.resume();
    process.stdin.once('data', () => finish(0));
  }

  root.render(
    React.createElement(AsciiMotionTui, {
      autoPlay: true,
      loop: false,
      hasDarkBackground: true,
    }),
  );

  // Fin garantie après la durée de l'animation.
  setTimeout(() => finish(0), DURATION_MS);
}

main().catch(() => process.exit(0));
