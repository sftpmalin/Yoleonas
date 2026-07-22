#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Task Manager universel - module Yoleo host
- Blueprint Flask : /system/task
- Fichiers techniques : ../conf/task par défaut
- Logs Linux : /var/log/yoleo/task par défaut

Le cron ne lance pas directement les grosses commandes.
Il lance CE fichier en mode CLI : le chemin est calculé automatiquement avec Path(__file__).
Comme ça, les logs et les statuts sont toujours centralisés dans SQLite.
"""

import argparse
import base64
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - Docker Linux fournit fcntl.
    fcntl = None
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import Blueprint, jsonify, make_response, redirect, render_template, request, url_for

try:
    from pywebpush import WebPushException, webpush
except Exception:  # L’environnement Python doit installer pywebpush pour envoyer de vraies notifications Web Push.
    WebPushException = Exception
    webpush = None

APP_DIR = Path(__file__).resolve().parent
task_bp = Blueprint("task_bp", __name__)


# ==========================================================
# 1. LE MANIFEST
# ==========================================================
@task_bp.route('/system/task/manifest.json')
def manifest():
    data = {
        "short_name": "Tasks Manager",
        "name": "Tasks Manager",
        "start_url": "/system/task",
        "display": "standalone",
        "background_color": "#000000",
        "theme_color": "#000000",
        "icons": [
            {
                "src": "/static/logo/Tasks.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable"
            },
            {
                "src": "/static/logo/Tasks.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable"
            }
        ]
    }
    resp = make_response(jsonify(data))
    resp.headers['Content-Type'] = 'application/manifest+json'
    return resp

# ==========================================================
# 2. LE SERVICE WORKER
# ==========================================================
@task_bp.route('/system/task/sw.js')
def sw():
    script = r"""
self.addEventListener('fetch', (event) => {});

self.addEventListener('push', (event) => {
    let data = {};
    try {
        data = event.data ? event.data.json() : {};
    } catch (e) {
        data = { body: event.data ? event.data.text() : '' };
    }

    const title = data.title || 'Task Manager';
    const options = {
        body: data.body || '',
        icon: data.icon || '/static/logo/Tasks.png',
        badge: data.badge || '/static/logo/Tasks.png',
        tag: data.tag || 'task-manager',
        renotify: true,
        data: {
            url: data.url || '/system/task/progress'
        }
    };

    event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    const targetUrl = event.notification && event.notification.data && event.notification.data.url
        ? event.notification.data.url
        : '/system/task/progress';

    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
            for (const client of clientList) {
                if ('focus' in client) {
                    client.navigate(targetUrl);
                    return client.focus();
                }
            }
            if (clients.openWindow) return clients.openWindow(targetUrl);
            return null;
        })
    );
});
"""
    resp = make_response(script)
    resp.headers['Content-Type'] = 'application/javascript'
    resp.headers['Cache-Control'] = 'no-store'
    return resp

# ==========================================================
# 3. LA PORTE D'ENTRÉE
# ==========================================================
@task_bp.route('/system/task/')
def home():
    return redirect(url_for("task_bp.task_home"))

# ==========================================================
# CONFIG DE BASE
# ==========================================================
def default_python_bin():
    """Retourne le Python qui exécute réellement Flask.

    Dans les images Python officielles, Flask est souvent installé dans
    /usr/local/bin/python3, alors que /usr/bin/python3 peut exister sans les
    modules pip. C'est exactement ce qui casse les tâches cron si on met
    /usr/bin/python3 en dur.
    """
    local_venv_python = APP_DIR / ".venv" / "bin" / "python"
    if local_venv_python.exists():
        return ".venv/bin/python"
    return os.environ.get("PYTHON_BIN", "python3")


def python_bin_uses_path_lookup(python_bin):
    """True quand PYTHON_BIN est un nom de commande à résoudre via PATH."""
    value = str(python_bin or "").strip()
    return bool(value) and not os.path.isabs(value) and "/" not in value and "\\" not in value


def cron_runner_file():
    """Chemin absolu du module Task réellement chargé."""
    return Path(__file__).resolve()


TASK_CONF_DIR_RAW = os.environ.get("TASK_CONF_DIR", "../conf/task")


def resolve_task_path(path_value):
    value = os.path.expanduser(os.path.expandvars(str(path_value or "").strip()))
    if os.path.isabs(value):
        return os.path.abspath(value)
    return os.path.abspath(os.path.join(str(APP_DIR), value))


def display_task_path(path_value):
    try:
        rel = os.path.relpath(str(path_value), str(APP_DIR))
        if rel.startswith(".."):
            return rel.replace(os.sep, "/")
    except Exception:
        pass
    return str(path_value)


TASK_CONF_DIR = resolve_task_path(TASK_CONF_DIR_RAW)
CONF_FILE = resolve_task_path(os.environ.get("TASK_CONF_FILE", os.path.join(TASK_CONF_DIR_RAW, "task.conf")))
STANDARD_TASK_LOG_DIR = os.environ.get("TASK_LOG_DIR", "/var/log/yoleo/task")
DEFAULTS = {
    "TASK_DB": os.environ.get("TASK_DB", os.path.join(TASK_CONF_DIR_RAW, "task.db")),
    "CRON_FILE": os.environ.get("TASK_CRON_FILE", os.path.join(TASK_CONF_DIR_RAW, "root.cron")),
    "LOG_DIR": STANDARD_TASK_LOG_DIR,
    "TASK_BACKUP_DIR": os.environ.get("TASK_BACKUP_DIR", os.path.join(TASK_CONF_DIR_RAW, "backup")),
    "TASK_BACKUP_KEEP_DAYS": os.environ.get("TASK_BACKUP_KEEP_DAYS", "30"),
    "TASK_LOCK_FILE": os.environ.get("TASK_LOCK_FILE", os.path.join(TASK_CONF_DIR_RAW, "task_runner.lock")),
    "TASK_RUNTIME_DIR": os.environ.get("TASK_RUNTIME_DIR", "/run/yoleo/task"),
    "VAPID_SUBJECT": "mailto:admin@localhost",
    "PYTHON_BIN": default_python_bin(),
    "SHELL_BIN": "/bin/bash",
    "MAX_LOG_LINES_PER_TASK": "5000",
}

PATH_CONFIG_KEYS = {"TASK_DB", "CRON_FILE", "LOG_DIR", "TASK_BACKUP_DIR", "TASK_LOCK_FILE", "TASK_RUNTIME_DIR"}
TASK_DB_LOG_BUFFER_ENV = "YOLEO_TASK_DB_LOG_BUFFER"

DAYS_LABELS = {
    "1": "Lundi",
    "2": "Mardi",
    "3": "Mercredi",
    "4": "Jeudi",
    "5": "Vendredi",
    "6": "Samedi",
    "0": "Dimanche",
}

SCHEDULE_LABELS = {
    "manual": "Manuel uniquement",
    "every_minutes": "Toutes les X minutes",
    "every_hours": "Toutes les X heures",
    "daily": "Tous les jours",
    "week_days": "Jours sélectionnés",
    "monthly": "Une fois par mois",
    "yearly": "Une fois par an",
    "custom": "Personnalisé",
}

CRON_FIELD_DEFAULTS = {
    "custom_cron_minute": "*",
    "custom_cron_hour": "*",
    "custom_cron_day": "*",
    "custom_cron_month": "*",
    "custom_cron_weekday": "*",
}

CHAIN_LABELS = {
    "and": "Stop si erreur (&&)",
    "continue": "Continuer même si erreur (;)",
}


DB_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    schedule_type TEXT NOT NULL DEFAULT 'manual',
    time_hour INTEGER NOT NULL DEFAULT 0,
    time_minute INTEGER NOT NULL DEFAULT 0,
    every_minutes INTEGER NOT NULL DEFAULT 5,
    every_hours INTEGER NOT NULL DEFAULT 1,
    week_days TEXT DEFAULT '',
    month_day INTEGER NOT NULL DEFAULT 1,
    month INTEGER NOT NULL DEFAULT 1,
    custom_cron_minute TEXT DEFAULT '*',
    custom_cron_hour TEXT DEFAULT '*',
    custom_cron_day TEXT DEFAULT '*',
    custom_cron_month TEXT DEFAULT '*',
    custom_cron_weekday TEXT DEFAULT '*',
    notify_success INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT DEFAULT '',
    chain_mode TEXT NOT NULL DEFAULT 'and',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_cron TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS task_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    command TEXT DEFAULT '',
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    UNIQUE(task_id, position)
);

CREATE TABLE IF NOT EXISTS task_status (
    task_id INTEGER PRIMARY KEY,
    running INTEGER NOT NULL DEFAULT 0,
    status TEXT DEFAULT 'Jamais lance',
    last_run TEXT DEFAULT '-',
    last_end TEXT DEFAULT '-',
    source TEXT DEFAULT '-',
    result TEXT DEFAULT '-',
    last_message TEXT DEFAULT '',
    process_pid INTEGER DEFAULT 0,
    process_pgid INTEGER DEFAULT 0,
    tmux_session TEXT DEFAULT '',
    systemd_unit TEXT DEFAULT '',
    lock_path TEXT DEFAULT '',
    stop_requested INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    source TEXT DEFAULT '',
    line TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint TEXT NOT NULL UNIQUE,
    subscription_json TEXT NOT NULL,
    user_agent TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_task_log_task_id_id ON task_log(task_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_task_commands_task_pos ON task_commands(task_id, position);
CREATE INDEX IF NOT EXISTS idx_push_subscriptions_enabled ON push_subscriptions(enabled);
"""

DB_MIGRATION_SQLS = [
    "ALTER TABLE task_status ADD COLUMN process_pid INTEGER DEFAULT 0",
    "ALTER TABLE task_status ADD COLUMN process_pgid INTEGER DEFAULT 0",
    "ALTER TABLE task_status ADD COLUMN tmux_session TEXT DEFAULT ''",
    "ALTER TABLE task_status ADD COLUMN systemd_unit TEXT DEFAULT ''",
    "ALTER TABLE task_status ADD COLUMN lock_path TEXT DEFAULT ''",
    "ALTER TABLE task_status ADD COLUMN stop_requested INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN custom_cron_minute TEXT DEFAULT '*'",
    "ALTER TABLE tasks ADD COLUMN custom_cron_hour TEXT DEFAULT '*'",
    "ALTER TABLE tasks ADD COLUMN custom_cron_day TEXT DEFAULT '*'",
    "ALTER TABLE tasks ADD COLUMN custom_cron_month TEXT DEFAULT '*'",
    "ALTER TABLE tasks ADD COLUMN custom_cron_weekday TEXT DEFAULT '*'",
    "ALTER TABLE tasks ADD COLUMN notify_success INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN archived INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN archived_at TEXT DEFAULT ''",
]

MAINTENANCE_TASKS = [
    {
        "title": "Maintenance Task DB - backup quotidien",
        "description": "Sauvegarde automatique de task.db dans le sous-dossier backup.",
        "hour": 0,
        "minute": 0,
        "action": "backup",
    },
    {
        "title": "Maintenance Task DB - check et reparation",
        "description": "Controle l'integrite SQLite et repare/restaure si necessaire.",
        "hour": 1,
        "minute": 0,
        "action": "check-repair",
    },
]

_DB_PROCESS_LOCK = threading.RLock()


def now_str():
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def parse_task_datetime(value):
    try:
        return datetime.strptime(str(value or ""), "%d/%m/%Y %H:%M:%S")
    except Exception:
        return None


def is_ajax_request():
    """Détecte une action appelée par le tableau dynamique.

    Les boutons modernes ajoutent ?ajax=1 et l’en-tête X-Requested-With=fetch.
    On garde aussi Accept: application/json pour les appels API propres.
    """
    accept = request.headers.get("Accept", "")
    return (
        request.args.get("ajax") == "1"
        or request.headers.get("X-Requested-With") == "fetch"
        or "application/json" in accept
    )


def build_task_stats(tasks=None):
    """Stats communes pour le rendu HTML et les retours AJAX."""
    tasks = tasks if tasks is not None else get_all_tasks()
    return {
        "total": len(tasks),
        "enabled": len([t for t in tasks if t.get("enabled")]),
        "disabled": len([t for t in tasks if not t.get("enabled")]),
        "running": len([t for t in tasks if safe_int(t.get("status", {}).get("running"), 0) == 1]),
    }


def ajax_tasks_payload(**extra):
    """Payload standard pour rafraîchir le tableau sans recharger la page."""
    tasks = get_all_tasks()
    payload = {
        "ok": True,
        "updated_at": now_str(),
        "stats": build_task_stats(tasks),
        "tasks": tasks,
        "archived_count": get_archived_task_count(),
    }
    payload.update(extra)
    return payload


def safe_int(value, default=0, min_value=None, max_value=None):
    try:
        result = int(str(value).strip())
    except Exception:
        result = default
    if min_value is not None and result < min_value:
        result = min_value
    if max_value is not None and result > max_value:
        result = max_value
    return result


def read_task_conf():
    """Lit task.conf dans ../conf/task par défaut et le crée si absent."""
    conf_path = Path(CONF_FILE)
    conf_path.parent.mkdir(parents=True, exist_ok=True)

    if not conf_path.exists():
        lines = [
            "# ==========================================================",
            "# TASK MANAGER - CONFIG MINIMALE",
            "# La vraie base des tâches est SQLite.",
            "# ==========================================================",
        ]
        for k, v in DEFAULTS.items():
            lines.append(f"{k}={v}")
        conf_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    config = dict(DEFAULTS)
    with conf_path.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().upper()
            value = value.strip()
            if key:
                config[key] = value

    # Auto-réparation : si un ancien task.conf contient encore /usr/bin/python3
    # alors que Flask tourne avec /usr/local/bin/python3, on remplace par le
    # Python courant. Ça évite qu'un vieux fichier conf casse tous les crons.
    configured_python = config.get("PYTHON_BIN") or ""
    preferred_python = default_python_bin()
    should_rewrite_python = False
    if preferred_python != configured_python:
        if preferred_python == ".venv/bin/python" and (python_bin_uses_path_lookup(configured_python) or configured_python in {"/usr/bin/python3", "/usr/bin/python"}):
            should_rewrite_python = True
        elif configured_python == "/usr/bin/python3" and preferred_python != "/usr/bin/python3":
            should_rewrite_python = True

    if should_rewrite_python:
        config["PYTHON_BIN"] = preferred_python
        try:
            lines = conf_path.read_text(encoding="utf-8", errors="replace").splitlines()
            changed = False
            for i, line in enumerate(lines):
                if line.startswith("PYTHON_BIN="):
                    lines[i] = f"PYTHON_BIN={config['PYTHON_BIN']}"
                    changed = True
                    break
            if not changed:
                lines.append(f"PYTHON_BIN={config['PYTHON_BIN']}")
            conf_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        except Exception:
            pass
    elif configured_python and os.path.isabs(configured_python):
        try:
            rel_python = os.path.relpath(configured_python, str(APP_DIR)).replace(os.sep, "/")
            if rel_python and not rel_python.startswith(".."):
                config["PYTHON_BIN"] = rel_python
                lines = conf_path.read_text(encoding="utf-8", errors="replace").splitlines()
                changed = False
                for i, line in enumerate(lines):
                    if line.startswith("PYTHON_BIN="):
                        lines[i] = f"PYTHON_BIN={rel_python}"
                        changed = True
                        break
                if not changed:
                    lines.append(f"PYTHON_BIN={rel_python}")
                conf_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        except Exception:
            pass

    if config.get("LOG_DIR") != STANDARD_TASK_LOG_DIR:
        config["LOG_DIR"] = STANDARD_TASK_LOG_DIR
        try:
            lines = conf_path.read_text(encoding="utf-8", errors="replace").splitlines()
            changed = False
            for i, line in enumerate(lines):
                if line.startswith("LOG_DIR="):
                    lines[i] = f"LOG_DIR={STANDARD_TASK_LOG_DIR}"
                    changed = True
                    break
            if not changed:
                lines.append(f"LOG_DIR={STANDARD_TASK_LOG_DIR}")
            conf_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        except Exception:
            pass

    for key in PATH_CONFIG_KEYS:
        config[key] = resolve_task_path(config.get(key, DEFAULTS[key]))

    Path(config["TASK_DB"]).parent.mkdir(parents=True, exist_ok=True)
    Path(config["CRON_FILE"]).parent.mkdir(parents=True, exist_ok=True)
    Path(config["LOG_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(config["TASK_BACKUP_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(config.get("TASK_LOCK_FILE", DEFAULTS["TASK_LOCK_FILE"])).parent.mkdir(parents=True, exist_ok=True)
    try:
        Path(config.get("TASK_RUNTIME_DIR", DEFAULTS["TASK_RUNTIME_DIR"])).mkdir(parents=True, exist_ok=True)
    except Exception:
        # Fallback utile sur un système transitoire où /run/yoleo ne serait pas encore prêt.
        fallback_runtime = Path(config.get("TASK_LOCK_FILE", DEFAULTS["TASK_LOCK_FILE"])).parent / "runtime"
        fallback_runtime.mkdir(parents=True, exist_ok=True)
        config["TASK_RUNTIME_DIR"] = str(fallback_runtime)
    return config

