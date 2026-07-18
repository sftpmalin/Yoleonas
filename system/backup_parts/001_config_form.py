#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import configparser
import glob
import fnmatch
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, make_response, redirect, render_template, request, url_for

backup_bp = Blueprint("backup_bp", __name__, url_prefix="/services/backup")

# ==========================================================
# 📁 CONF CENTRALISÉE YOLEO
# ==========================================================
_NAS_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

# Important : la conf du module backup reste dans ../conf/backup.conf.
# On résout le chemin en absolu uniquement en interne pour éviter les surprises
# avec gunicorn/systemd, mais l'interface et la conf affichent le chemin relatif.
DEFAULT_CONFIG_FILE = '../conf/backup.conf'
CONFIG_FILE_RAW = os.environ.get('BACKUP_CONF', DEFAULT_CONFIG_FILE)

def resolve_module_path(path_value: str) -> str:
    value = os.path.expanduser(os.path.expandvars(str(path_value or '').strip()))
    if os.path.isabs(value):
        return os.path.abspath(value)
    return os.path.abspath(os.path.join(_NAS_MODULE_DIR, value))

CONFIG_FILE = resolve_module_path(CONFIG_FILE_RAW)
CONFIG_FILE_DISPLAY = CONFIG_FILE_RAW if CONFIG_FILE_RAW else DEFAULT_CONFIG_FILE

# Compatibilité interne uniquement : la partie Partage réseau du vieux Docker n'est plus exposée.
NETWORK_INI_FILE_RAW = os.environ.get('NETWORK_MOUNTS_INI', '../conf/network_mounts.conf')
NETWORK_INI_FILE = resolve_module_path(NETWORK_INI_FILE_RAW)
NETWORK_LOG_FILE = os.environ.get('NETWORK_MOUNTS_LOG', '/var/log/yoleo/backup-network.log')

# Chemins Linux standards non réglables dans l'UI.
STANDARD_LOG_DIR = '/var/log/yoleo/backup'
STANDARD_STATUS_DIR = '/var/lib/yoleo/backup/status'

GLOBAL_DEFAULTS = {
    'SCRIPTS_DIR': '',
    'LOG_DIR': STANDARD_LOG_DIR,
    'JDOM_DIR': STANDARD_STATUS_DIR,
    'STATUS_DIR': STANDARD_STATUS_DIR,
    'RSYNC_BIN': '/usr/bin/rsync',
    'PYTHON_BIN': '/usr/bin/python3',
    'DEFAULT_SOURCE': '/mnt/user/',
    'DEFAULT_TARGET': '/mnt/user/Backup/',
    'TITLE': 'Gestionnaire de backup',
    'LOGO': '/static/logo/Backup.png',
    'MAX_LOG_LINES': '900',
    'BROWSE_SOURCE': '/',
    'BROWSE_CIBLE': '/',
    'BROWSE_SCRIPTS': '/',
}

METADATA_START = '# === BACKUP_UI_CONFIG_START ==='
METADATA_END = '# === BACKUP_UI_CONFIG_END ==='
SCRIPT_MARKER = '# BACKUP_RSYNC'


def parse_conf(content):
    data = {}
    for raw_line in (content or '').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        data[key.strip()] = value.strip()
    return data


def force_standard_runtime_paths(settings):
    """Les seuls réglages utilisateur sont les scripts.

    Logs et statuts/JDOM restent sur les chemins Linux standards du module,
    même si un vieux backup.conf issu du Docker ou d'un ancien patch contient
    encore un chemin local/projet.
    """
    data = dict(settings or {})
    data['LOG_DIR'] = STANDARD_LOG_DIR
    data['JDOM_DIR'] = STANDARD_STATUS_DIR
    data['STATUS_DIR'] = STANDARD_STATUS_DIR
    return data


def load_settings():
    settings = dict(GLOBAL_DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as handle:
                settings.update(parse_conf(handle.read()))
        except Exception:
            pass
    return force_standard_runtime_paths(settings)


def backup_conf_exists() -> bool:
    return os.path.exists(CONFIG_FILE)


def backup_is_configured(settings=None) -> bool:
    data = settings if settings is not None else load_settings()
    scripts_dir = str((data or {}).get('SCRIPTS_DIR') or '').strip()
    return backup_conf_exists() and bool(scripts_dir)


def backup_config_error_message() -> str:
    return 'Veuillez configurer d’abord le dossier des scripts dans Réglages. backup.conf sera créé à ce moment-là.'


def ensure_ini_slash(path_value):
    value = str(path_value or '').strip() or '/'
    if not value.startswith('/') and ':' not in value:
        value = '/' + value.lstrip('/')
    if value.endswith('/'):
        return value
    return value + '/'


def _settings_with_compat(settings):
    data = dict(settings or {})
    if data.get('JDOM_DIR') and not data.get('STATUS_DIR'):
        data['STATUS_DIR'] = data.get('JDOM_DIR')
    if data.get('STATUS_DIR') and not data.get('JDOM_DIR'):
        data['JDOM_DIR'] = data.get('STATUS_DIR')
    return force_standard_runtime_paths(data)


def write_settings(settings):
    """Écrit backup.conf en key=value simple, sans backup.ini séparé."""
    data = _settings_with_compat({**GLOBAL_DEFAULTS, **(settings or {})})
    order = [
        'SCRIPTS_DIR', 'LOG_DIR', 'JDOM_DIR', 'STATUS_DIR',
        'RSYNC_BIN', 'PYTHON_BIN', 'DEFAULT_SOURCE', 'DEFAULT_TARGET',
        'TITLE', 'LOGO', 'MAX_LOG_LINES',
        'BROWSE_SOURCE', 'BROWSE_CIBLE', 'BROWSE_SCRIPTS',
    ]
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    tmp = CONFIG_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        handle.write('# ============================================================\n')
        handle.write('# Module Yoleo Backup - conf unique\n')
        handle.write('# Route principale : /services/backup\n')
        handle.write('# Les scripts sont réglables dans l\'onglet Réglage.\n')
        handle.write('# Les logs et statuts/JDOM restent sur des chemins Linux standards.\n')
        handle.write('# ============================================================\n')
        for key in order:
            handle.write(f'{key}={data.get(key, GLOBAL_DEFAULTS.get(key, ""))}\n')
        extra_keys = sorted(k for k in data if k not in set(order))
        for key in extra_keys:
            handle.write(f'{key}={data.get(key, "")}\n')
    os.replace(tmp, CONFIG_FILE)
    try:
        os.chmod(CONFIG_FILE, 0o644)
    except OSError:
        pass
    return data

def load_browse_state():
    # Conservé uniquement pour compatibilité avec l'ancien module Docker.
    # Le nouveau Yoleo utilise /browser/folder en window.open.
    settings = load_settings()
    return {
        'source': ensure_ini_slash(settings.get('BROWSE_SOURCE') or '/'),
        'cible': ensure_ini_slash(settings.get('BROWSE_CIBLE') or settings.get('BROWSE_TARGET') or '/'),
        'scripts': ensure_ini_slash(settings.get('BROWSE_SCRIPTS') or settings.get('SCRIPTS_DIR') or '/'),
    }


def save_browse_state(kind, path_value):
    # Ne doit jamais créer backup.conf : la conf naît seulement quand l'utilisateur
    # enregistre le dossier scripts dans l'onglet Réglages.
    if not backup_conf_exists():
        return load_browse_state()
    kind = str(kind or '').strip().lower()
    if kind in {'left', 'last_left'}:
        kind = 'source'
    elif kind in {'right', 'target', 'last_right'}:
        kind = 'cible'
    elif kind in {'script', 'scripts_dir', 'script_dir', 'settings'}:
        kind = 'scripts'
    if kind not in {'source', 'cible', 'scripts'}:
        return load_browse_state()

    settings = load_settings()
    value = ensure_ini_slash(path_value)
    if kind == 'source':
        settings['BROWSE_SOURCE'] = value
    elif kind == 'cible':
        settings['BROWSE_CIBLE'] = value
    elif kind == 'scripts':
        settings['BROWSE_SCRIPTS'] = value
    write_settings(settings)
    return load_browse_state()


def ensure_layout():
    """Charge l'environnement backup sans créer backup.conf par surprise.

    Règle Yoleo : backup.conf est créé uniquement quand l'utilisateur choisit
    et enregistre le dossier des scripts dans Réglages. Avant cela, le module
    peut s'afficher, mais aucune écriture de conf ni dossier scripts imposé.
    """
    settings = _settings_with_compat(load_settings())
    conf_dir = os.path.dirname(CONFIG_FILE)
    os.makedirs(conf_dir, exist_ok=True)

    if not backup_conf_exists():
        settings['SCRIPTS_DIR'] = ''
        settings['BROWSE_SCRIPTS'] = '/'
        return settings

    if settings.get('JDOM_DIR') and settings.get('STATUS_DIR') != settings.get('JDOM_DIR'):
        settings['STATUS_DIR'] = settings.get('JDOM_DIR')
        settings = write_settings(settings)

    scripts_dir = str(settings.get('SCRIPTS_DIR') or '').strip()
    if scripts_dir:
        settings['SCRIPTS_DIR'] = os.path.abspath(os.path.expanduser(os.path.expandvars(scripts_dir)))
        for key in ('SCRIPTS_DIR', 'LOG_DIR', 'STATUS_DIR'):
            path_value = str(settings.get(key) or '').strip()
            if path_value:
                os.makedirs(path_value, exist_ok=True)
    return settings


def now_label():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def slugify_filename(filename):
    filename = (filename or '').strip().replace(' ', '_')
    filename = filename.replace('/', '').replace('\\', '')
    filename = re.sub(r'[^A-Za-z0-9_.-]+', '_', filename)
    if not filename:
        filename = 'backup_rsync.py'
    if not filename.endswith('.py'):
        root, _ext = os.path.splitext(filename)
        filename = (root or filename) + '.py'
    return filename


def safe_script_path(filename):
    settings = ensure_layout()
    scripts_dir_raw = str(settings.get('SCRIPTS_DIR') or '').strip()
    if not scripts_dir_raw:
        raise ValueError(backup_config_error_message())
    safe_name = os.path.basename(slugify_filename(filename))
    scripts_dir = os.path.abspath(os.path.expanduser(os.path.expandvars(scripts_dir_raw)))
    full_path = os.path.abspath(os.path.join(scripts_dir, safe_name))
    if not full_path.startswith(scripts_dir + os.sep):
        raise ValueError('Chemin script invalide')
    return full_path


def read_text(path, default=''):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            return handle.read()
    except Exception:
        return default


def tail_text(path, max_lines=900):
    text = read_text(path, '')
    if not text:
        return ''
    lines = text.splitlines()
    return '\n'.join(lines[-int(max_lines):])


def extract_script_metadata(content):
    match = re.search(
        re.escape(METADATA_START) + r'(.*?)' + re.escape(METADATA_END),
        content or '',
        flags=re.DOTALL,
    )
    if not match:
        return {}
    raw_lines = []
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith('#'):
            stripped = stripped[1:].lstrip()
        if stripped:
            raw_lines.append(stripped)
    try:
        return json.loads('\n'.join(raw_lines))
    except Exception:
        return {}


def has_script_marker(content):
    first_lines = '\n'.join((content or '').splitlines()[:12])
    return SCRIPT_MARKER in first_lines


def build_metadata_block(data):
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    lines = [METADATA_START]
    lines.extend(f'# {line}' if line else '#' for line in payload.splitlines())
    lines.append(METADATA_END)
    return '\n'.join(lines)



def ensure_folder_slash(path_value):
    """Rsync dossier : slash final obligatoire pour copier le contenu du dossier.
    Exemple : /source/Autres/ vers /backup/Autres/ évite /backup/Autres/Autres.
    """
    value = str(path_value or '').strip()
    if not value:
        return value
    if value.endswith('/'):
        return value
    return value + '/'


def browse_response_path(path_value):
    return ensure_folder_slash(path_value)


RSYNC_MODE_KEYS = (
    'ARCHIVE',
    'VERBOSE',
    'HUMAN',
    'PROGRESS',
    'DELETE',
    'DELETE_EXCLUDED',
    'DRY_RUN',
    'COMPRESS',
    'CHECKSUM',
    'HARD_LINKS',
    'NO_OWNER',
    'NO_GROUP',
    'NO_PERMS',
    'NUMERIC_IDS',
    'ONE_FILE_SYSTEM',
    'PARTIAL',
    'INPLACE',
    'WHOLE_FILE',
    'STATS',
)


ARCHIVE_MODE_DEFAULTS = {
    'ARCHIVE_FORMAT': 'tar.7z',
    'ARCHIVE_CHILDREN': '1',
    'ARCHIVE_NAME': '',
    'COMPRESSION_LEVEL': '7',
    'REPLACE_EXISTING': '1',
    'DATE_SUFFIX': '0',
    'COMMAND_START': '',
    'COMMAND_END': '',
}


CACHE_MODE_RSYNC_DEFAULTS = {
    'ARCHIVE': '1',
    'VERBOSE': '1',
    'HUMAN': '1',
    'PROGRESS': '1',
    'DELETE': '0',
    'DELETE_EXCLUDED': '0',
    'DRY_RUN': '0',
    'COMPRESS': '0',
    'CHECKSUM': '0',
    'HARD_LINKS': '1',
    'NO_OWNER': '0',
    'NO_GROUP': '0',
    'NO_PERMS': '0',
    'NUMERIC_IDS': '0',
    'ONE_FILE_SYSTEM': '0',
    'PARTIAL': '0',
    'INPLACE': '0',
    'WHOLE_FILE': '0',
    'STATS': '1',
    'EXTRA_ARGS': '',
}


def archive_format_badge_label(value):
    fmt = str(value or 'tar.7z').strip().lower()
    labels = {
        'tar': 'TAR',
        '7z': '7Z',
        'tar.7z': 'TAR 7Z',
        'tar.gz': 'TAR GZ',
        'tgz': 'TAR GZ',
        'gz': 'TAR GZ',
    }
    return labels.get(fmt, fmt.upper())


def normalize_form(form):
    settings = ensure_layout()
    title = (form.get('title') or '').strip() or 'Backup rsync'
    data = {
        'TITLE': title,
        'LOGO': (form.get('logo') or settings.get('LOGO') or '/static/logo.png').strip(),
        'SOURCE': ensure_folder_slash(form.get('source') or settings['DEFAULT_SOURCE']),
        'TARGET': ensure_folder_slash(form.get('target') or settings['DEFAULT_TARGET']),
        'MODE': (form.get('mode') or 'backup').strip(),
        'ARCHIVE': '1' if form.get('archive') else '0',
        'VERBOSE': '1' if form.get('verbose') else '0',
        'HUMAN': '1' if form.get('human') else '0',
        'PROGRESS': '1' if form.get('progress') else '0',
        'DELETE': '1' if form.get('delete') else '0',
        'DELETE_EXCLUDED': '1' if form.get('delete_excluded') else '0',
        'DRY_RUN': '1' if form.get('dry_run') else '0',
        'COMPRESS': '1' if form.get('compress') else '0',
        'CHECKSUM': '1' if form.get('checksum') else '0',
        'HARD_LINKS': '1' if form.get('hard_links') else '0',
        'NO_OWNER': '1' if form.get('no_owner') else '0',
        'NO_GROUP': '1' if form.get('no_group') else '0',
        'NO_PERMS': '1' if form.get('no_perms') else '0',
        'NUMERIC_IDS': '1' if form.get('numeric_ids') else '0',
        'ONE_FILE_SYSTEM': '1' if form.get('one_file_system') else '0',
        'PARTIAL': '1' if form.get('partial') else '0',
        'INPLACE': '1' if form.get('inplace') else '0',
        'WHOLE_FILE': '1' if form.get('whole_file') else '0',
        'STATS': '1' if form.get('stats') else '0',
        'EXCLUDES': (form.get('excludes') or '').strip(),
        'EXTRA_ARGS': (form.get('extra_args') or '').strip(),
        'AUTO_MKDIR_TARGET': '1' if form.get('auto_mkdir_target') else '0',
        # Mode Archive : options reprises du module Archive, mais stockées dans
        # le script Backup généré pour garder le lancement/stop/logs du module Backup.
        'ARCHIVE_FORMAT': (form.get('archive_format') or 'tar.7z').strip().lower(),
        'ARCHIVE_CHILDREN': '1' if form.get('archive_children') else '0',
        # Nom d'archive volontairement masqué dans le formulaire Backup :
        # le moteur génère le nom automatiquement depuis le dossier source.
        'ARCHIVE_NAME': '',
        'COMPRESSION_LEVEL': (form.get('compression_level') or '7').strip(),
        'REPLACE_EXISTING': '1' if form.get('replace_existing') else '0',
        'DATE_SUFFIX': '1' if form.get('date_suffix') else '0',
        'COMMAND_START': (form.get('command_start') or '').strip(),
        'COMMAND_END': (form.get('command_end') or '').strip(),
    }
    if data['MODE'] not in {'backup', 'mirror', 'archive', 'cache'}:
        data['MODE'] = 'backup'
    if data['MODE'] == 'mirror':
        data['DELETE'] = '1'
    if data['ARCHIVE_FORMAT'] not in {'tar', 'tar.7z', '7z', 'tar.gz', 'tgz', 'gz'}:
        data['ARCHIVE_FORMAT'] = 'tar.7z'
    if data['COMPRESSION_LEVEL'] not in {'0', '1', '2', '3', '4', '5', '6', '7', '8', '9'}:
        data['COMPRESSION_LEVEL'] = '7'
    if data['MODE'] == 'archive':
        # Quand un ancien script rsync est bascule en archive, les champs rsync
        # caches dans le formulaire peuvent encore etre postes. On les neutralise
        # pour que le moteur genere soit strictement archive, soit strictement rsync.
        for key in RSYNC_MODE_KEYS:
            data[key] = '0'
        data['EXTRA_ARGS'] = ''
    elif data['MODE'] == 'cache':
        data.update(ARCHIVE_MODE_DEFAULTS)
        data.update(CACHE_MODE_RSYNC_DEFAULTS)
        data['EXCLUDES'] = ''
        data['COMMAND_START'] = (form.get('command_start') or '').strip()
        data['COMMAND_END'] = (form.get('command_end') or '').strip()
    else:
        data.update(ARCHIVE_MODE_DEFAULTS)
    return data


def default_form_data():
    settings = ensure_layout()
    return {
        'TITLE': 'Backup rsync',
        'LOGO': settings.get('LOGO', '/static/logo.png'),
        'SOURCE': ensure_folder_slash(settings.get('DEFAULT_SOURCE', '/mnt/user/')),
        'TARGET': ensure_folder_slash(settings.get('DEFAULT_TARGET', '/mnt/user/Backup/')),
        'MODE': 'backup',
        'ARCHIVE': '1',
        'VERBOSE': '1',
        'HUMAN': '1',
        'PROGRESS': '0',
        'DELETE': '0',
        'DELETE_EXCLUDED': '0',
        'DRY_RUN': '0',
        'COMPRESS': '0',
        'CHECKSUM': '0',
        'HARD_LINKS': '0',
        'NO_OWNER': '1',
        'NO_GROUP': '1',
        'NO_PERMS': '1',
        'NUMERIC_IDS': '0',
        'ONE_FILE_SYSTEM': '0',
        'PARTIAL': '1',
        'INPLACE': '0',
        'WHOLE_FILE': '0',
        'STATS': '1',
        'EXCLUDES': '',
        'EXTRA_ARGS': '',
        'AUTO_MKDIR_TARGET': '1',
        'ARCHIVE_FORMAT': 'tar.7z',
        'ARCHIVE_CHILDREN': '1',
        'ARCHIVE_NAME': '',
        'COMPRESSION_LEVEL': '7',
        'REPLACE_EXISTING': '1',
        'DATE_SUFFIX': '0',
        'COMMAND_START': '',
        'COMMAND_END': '',
    }


def runtime_config():
    settings = ensure_layout()
    return {
        'rsync_bin': settings.get('RSYNC_BIN', '/usr/bin/rsync'),
        'log_dir': settings.get('LOG_DIR', STANDARD_LOG_DIR),
        'status_dir': settings.get('STATUS_DIR', STANDARD_STATUS_DIR),
    }
