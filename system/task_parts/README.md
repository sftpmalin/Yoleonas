# task_parts

DÃ©coupage mÃ©canique de `task.py`, exÃ©cutÃ© dans un espace global commun par
`_module_chunks.load_module_chunks`. L'ordre numÃ©rique est contractuel : les
fonctions d'un fichier peuvent utiliser les symboles des fichiers prÃ©cÃ©dents.

- `001`: imports, blueprint, manifest, service worker et configuration
- `002`: schÃ©ma, connexions et maintenance SQLite
- `003`: accÃ¨s aux tÃ¢ches, statuts et mÃ©tadonnÃ©es runtime
- `004`: processus, tmux, Docker exec et arrÃªt forcÃ©
- `005`: journaux SQLite et notifications Web Push
- `006`: planification cron et synchronisation au dÃ©marrage
- `007`: exÃ©cution des tÃ¢ches et worker
- `008`: formulaires, validation et rendu des pages
- `009`: routes Flask et point d'entrÃ©e CLI

Ne pas importer ces fichiers directement : importer `task`, comme auparavant.
