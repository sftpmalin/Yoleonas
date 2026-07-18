#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Chunk loader for large Flask modules.

The historical module files stay as app.conf entry points. Chunks are compiled
with their own filenames and executed in the parent module globals, so Flask
routes, blueprints, constants, and helper functions keep the same namespace.
"""
from __future__ import annotations

from pathlib import Path
from typing import MutableMapping


def load_module_chunks(target_globals: MutableMapping[str, object], parent_file: str, parts_dir_name: str) -> None:
    parent_path = Path(parent_file).resolve()
    parts_dir = parent_path.with_name(parts_dir_name)
    if not parts_dir.is_dir():
        raise RuntimeError(f"Chunk directory not found: {parts_dir}")

    target_globals.setdefault("__chunk_parent_file__", str(parent_path))
    for chunk_path in sorted(parts_dir.glob("[0-9][0-9][0-9]_*.py")):
        source = chunk_path.read_text(encoding="utf-8-sig")
        code = compile(source, str(chunk_path), "exec")
        target_globals["__chunk_file__"] = str(chunk_path)
        exec(code, target_globals)
    target_globals.pop("__chunk_file__", None)
