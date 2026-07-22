#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibilite : ancien chemin scripts/docker.py.

Le moteur Docker maintenu vit dans system/docker_cli.py. Ce wrapper evite de
laisser l'ancien script diverger tout en conservant les appels historiques.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main() -> int:
    target = Path(__file__).resolve().parent.parent / "system" / "docker_cli.py"
    if not target.is_file():
        print(f"ERREUR : moteur Docker introuvable : {target}", file=sys.stderr)
        return 1
    sys.argv[0] = str(target)
    runpy.run_path(str(target), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
