#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Entry point for the Task Manager module.

Flask and cron still load `task.py`. The implementation lives in `task_parts/`
and is executed in this module namespace to preserve every public symbol.
"""
from __future__ import annotations

import os
import sys

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if _MODULE_DIR not in sys.path:
    sys.path.insert(0, _MODULE_DIR)

from _module_chunks import load_module_chunks as _load_module_chunks

_load_module_chunks(globals(), __file__, "task_parts")

del _load_module_chunks
del _MODULE_DIR
