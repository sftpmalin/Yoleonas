#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Entry point for the system module.

app.conf still imports `system`. The implementation lives in
`system_parts/` and is executed in this module namespace.
"""
from __future__ import annotations

import os
import sys

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if _MODULE_DIR not in sys.path:
    sys.path.insert(0, _MODULE_DIR)

# Route declarations stay here so app.conf can be regenerated as name=route.
SYSTEM_APP_CONF_REQUIRED_MODULES = [
    "system=system",
    "idex=index",
    "browser=browser",
    "builds=builds",
    "file=file",
    "vm=vm",
    "disk=disk",
    "dockers=dockers",
    "users=users",
    "partage=partage",
    "services=services",
    "backup=backup",
    "task=task",
    "scripts=scripts",
    "meteo=meteo",
    "terminal=terminal",
]


def _system_stub_conf_file(name: str) -> str:
    module_dir = os.path.dirname(os.path.abspath(__file__))
    default_conf_dir = os.path.abspath(os.path.join(module_dir, "..", "conf"))
    conf_dir = os.path.abspath(os.path.expanduser(os.path.expandvars(os.environ.get("NAS_CONF_DIR", default_conf_dir))))
    return os.path.join(conf_dir, name)


def ensure_app_conf_backup_module(path: str = "") -> list[str]:
    target = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path or _system_stub_conf_file("app.conf")).strip())))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if not os.path.exists(target):
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("\n".join(SYSTEM_APP_CONF_REQUIRED_MODULES).rstrip() + "\n")
        return ["app.conf", "backup"]

    with open(target, "r", encoding="utf-8", errors="replace") as handle:
        lines = handle.read().splitlines()

    loaded = set()
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        loaded.add((line.split("=", 1)[1] if "=" in line else line).strip())

    added = []
    for declaration in SYSTEM_APP_CONF_REQUIRED_MODULES:
        module_name = declaration.split("=", 1)[1]
        if module_name not in loaded:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(declaration)
            added.append(module_name)

    if added:
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines).rstrip() + "\n")
    return added


try:
    ensure_app_conf_backup_module(_system_stub_conf_file("app.conf"))
except Exception:
    pass

from _module_chunks import load_module_chunks

load_module_chunks(globals(), __file__, "system_parts")
