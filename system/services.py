#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Entry point for the services module.

app.conf still imports `services`. The implementation lives in
`services_parts/` and is executed in this module namespace.
"""
from __future__ import annotations

import os
import sys

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if _MODULE_DIR not in sys.path:
    sys.path.insert(0, _MODULE_DIR)


from _module_chunks import load_module_chunks

load_module_chunks(globals(), __file__, "services_parts")