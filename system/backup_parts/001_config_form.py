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


def decode_mountinfo_field(value):
    """Décode les échappements octaux employés par /proc/*/mountinfo."""
    decoded = str(value or '')
    # Le backslash est traité en dernier pour éviter un double décodage.
    for escaped, plain in (
        ('\\040', ' '),
        ('\\011', '\t'),
        ('\\012', '\n'),
        ('\\134', '\\'),
    ):
        decoded = decoded.replace(escaped, plain)
    return decoded


def parse_mountinfo(content):
    """Retourne les montages Linux décrits par /proc/self/mountinfo."""
    entries = []
    for raw_line in str(content or '').splitlines():
        fields = raw_line.split()
        try:
            separator = fields.index('-')
        except ValueError:
            continue
        if separator < 6 or len(fields) < separator + 4:
            continue
        entries.append({
            'device_id': fields[2],
            'root': decode_mountinfo_field(fields[3]),
            'mount_point': decode_mountinfo_field(fields[4]),
            'mount_options': fields[5],
            'optional_fields': fields[6:separator],
            'fs_type': decode_mountinfo_field(fields[separator + 1]),
            'mount_source': decode_mountinfo_field(fields[separator + 2]),
            'super_options': fields[separator + 3],
        })
    return entries


def read_mountinfo_entries(path='/proc/self/mountinfo'):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            return parse_mountinfo(handle.read())
    except OSError:
        return []


def mount_entry_for_path(path_value, mount_entries=None):
    """Trouve le montage le plus spécifique contenant un chemin local."""
    value = str(path_value or '').strip()
    if not value.startswith('/'):
        return None
    candidate = os.path.normpath(os.path.realpath(value))
    best = None
    for entry in mount_entries if mount_entries is not None else read_mountinfo_entries():
        mount_point = os.path.normpath(str(entry.get('mount_point') or ''))
        if not mount_point.startswith('/'):
            continue
        inside = mount_point == '/' or candidate == mount_point or candidate.startswith(mount_point.rstrip('/') + '/')
        if inside and (best is None or len(mount_point) > len(str(best.get('mount_point') or ''))):
            best = entry
    return best


def is_mergerfs_mount(entry):
    if not isinstance(entry, dict):
        return False
    signature = ' '.join((
        str(entry.get('fs_type') or ''),
        str(entry.get('mount_source') or ''),
    )).lower()
    return 'mergerfs' in signature


def parse_fstab_entries(content):
    entries = []
    for raw_line in str(content or '').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        fields = line.split()
        if len(fields) < 3:
            continue
        entries.append({
            'mount_source': decode_mountinfo_field(fields[0]),
            'mount_point': decode_mountinfo_field(fields[1]),
            'fs_type': decode_mountinfo_field(fields[2]),
            'mount_options': fields[3] if len(fields) > 3 else '',
        })
    return entries


def read_fstab_entries(path='/etc/fstab'):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            return parse_fstab_entries(handle.read())
    except OSError:
        return []


def mergerfs_branch_paths(entry, fstab_entries=None):
    """Retourne les branches physiques d'un montage mergerFS.

    mergerFS expose parfois seulement une source numérique (par ex. 1:2:3)
    dans mountinfo. Dans ce cas, /etc/fstab fournit la composition réelle.
    """
    if not is_mergerfs_mount(entry):
        return []
    mount_point = os.path.normpath(str(entry.get('mount_point') or ''))
    branch_source = ''
    rows = fstab_entries if fstab_entries is not None else read_fstab_entries()
    for row in rows:
        if os.path.normpath(str(row.get('mount_point') or '')) == mount_point and is_mergerfs_mount(row):
            branch_source = str(row.get('mount_source') or '')
            break
    if not branch_source:
        branch_source = str(entry.get('mount_source') or '')

    raw_branches = [part.strip() for part in branch_source.split(':') if part.strip()]
    if raw_branches and all(part.isdigit() for part in raw_branches):
        return []

    branches = []
    parent = os.path.dirname(mount_point.rstrip('/')) or '/'
    for raw_branch in raw_branches:
        branch = re.sub(r'=(?:RO|RW|NC)$', '', raw_branch, flags=re.IGNORECASE)
        if not branch.startswith('/'):
            branch = os.path.join(parent, branch)
        expanded = glob.glob(branch) if glob.has_magic(branch) else [branch]
        if not expanded:
            expanded = [branch]
        for candidate in expanded:
            normalized = os.path.normpath(os.path.realpath(candidate))
            if normalized not in branches:
                branches.append(normalized)
    return branches


def same_physical_mount(left_entry, right_entry):
    if not isinstance(left_entry, dict) or not isinstance(right_entry, dict):
        return False
    left_mount = os.path.normpath(str(left_entry.get('mount_point') or ''))
    right_mount = os.path.normpath(str(right_entry.get('mount_point') or ''))
    if left_mount and left_mount == right_mount:
        return True
    left_device = str(left_entry.get('device_id') or '')
    right_device = str(right_entry.get('device_id') or '')
    if left_device and right_device and left_device == right_device:
        return True
    left_source = str(left_entry.get('mount_source') or '')
    right_source = str(right_entry.get('mount_source') or '')
    return bool(
        left_source.startswith('/dev/')
        and right_source.startswith('/dev/')
        and os.path.realpath(left_source) == os.path.realpath(right_source)
    )


def mergerfs_target_source_membership(source_entry, target_entry, mount_entries=None, fstab_entries=None):
    """Retourne (vérifiable, incluse, branches, branches_correspondantes)."""
    if not isinstance(source_entry, dict):
        return False, False, [], []
    entries = mount_entries if mount_entries is not None else read_mountinfo_entries()
    branches = mergerfs_branch_paths(target_entry, fstab_entries=fstab_entries)
    if not branches:
        return False, False, [], []
    matching = []
    for branch in branches:
        branch_entry = mount_entry_for_path(branch, mount_entries=entries)
        if branch_entry is None:
            return False, False, branches, matching
        if same_physical_mount(source_entry, branch_entry):
            matching.append(branch)
    return True, bool(matching), branches, matching


def mergerfs_endpoint_physical_mounts(entry, mount_entries=None, fstab_entries=None):
    """Résout un point d'entrée en montages physiques sous-jacents."""
    if not isinstance(entry, dict):
        return False, [], []
    if not is_mergerfs_mount(entry):
        mount_point = os.path.normpath(str(entry.get('mount_point') or ''))
        return True, [{'branch': mount_point, 'entry': entry}], [mount_point]

    entries = mount_entries if mount_entries is not None else read_mountinfo_entries()
    branches = mergerfs_branch_paths(entry, fstab_entries=fstab_entries)
    if not branches:
        return False, [], []
    resolved = []
    for branch in branches:
        branch_entry = mount_entry_for_path(branch, mount_entries=entries)
        # Un mergerFS imbriqué ou une branche inconnue doit être explicitement
        # résolu avant d'autoriser un déplacement destructif.
        if branch_entry is None or is_mergerfs_mount(branch_entry):
            return False, resolved, branches
        resolved.append({'branch': branch, 'entry': branch_entry})
    return True, resolved, branches


def cache_mergerfs_mount_issues(source, target, mount_entries=None, fstab_entries=None):
    """Refuse uniquement le partage d'un disque physique via mergerFS."""
    entries = mount_entries if mount_entries is not None else read_mountinfo_entries()
    source_entry = mount_entry_for_path(source, mount_entries=entries)
    target_entry = mount_entry_for_path(target, mount_entries=entries)
    source_virtual = is_mergerfs_mount(source_entry)
    target_virtual = is_mergerfs_mount(target_entry)
    if not source_virtual and not target_virtual:
        return []

    source_verified, source_mounts, source_branches = mergerfs_endpoint_physical_mounts(
        source_entry,
        mount_entries=entries,
        fstab_entries=fstab_entries,
    )
    target_verified, target_mounts, target_branches = mergerfs_endpoint_physical_mounts(
        target_entry,
        mount_entries=entries,
        fstab_entries=fstab_entries,
    )
    if not source_verified or not target_verified:
        return [{
            'code': 'mergerfs_composition_unverifiable',
            'role': 'target' if target_virtual else 'source',
            'label': 'La destination' if target_virtual else 'La source',
            'path': str(target if target_virtual else source or '').strip(),
            'mount_point': str((target_entry if target_virtual else source_entry or {}).get('mount_point') or ''),
            'fs_type': str((target_entry if target_virtual else source_entry or {}).get('fs_type') or ''),
            'source_branches': source_branches,
            'target_branches': target_branches,
        }]

    overlaps = []
    for source_mount in source_mounts:
        for target_mount in target_mounts:
            if not same_physical_mount(source_mount.get('entry'), target_mount.get('entry')):
                continue
            overlap = {
                'source_branch': source_mount.get('branch') or '',
                'target_branch': target_mount.get('branch') or '',
                'physical_mount': str((source_mount.get('entry') or {}).get('mount_point') or ''),
                'device': str((source_mount.get('entry') or {}).get('mount_source') or ''),
            }
            if overlap not in overlaps:
                overlaps.append(overlap)
    if not overlaps:
        return []

    if target_virtual and not source_virtual:
        code = 'mergerfs_target_contains_source'
    elif source_virtual and not target_virtual:
        code = 'mergerfs_source_contains_target'
    else:
        code = 'mergerfs_endpoints_overlap'
    return [{
            'code': code,
            'role': 'target',
            'label': 'La destination',
            'path': str(target or '').strip(),
            'mount_point': str((target_entry or {}).get('mount_point') or ''),
            'fs_type': str((target_entry or {}).get('fs_type') or ''),
            'source_mount_point': str((source_entry or {}).get('mount_point') or ''),
            'source_branches': source_branches,
            'target_branches': target_branches,
            'overlaps': overlaps,
            'matching_branches': [item.get('target_branch') or item.get('source_branch') for item in overlaps],
        }]


def cache_mergerfs_error_payload(issues):
    details = list(issues or [])
    descriptions = []
    for item in details:
        if item.get('code') == 'mergerfs_target_contains_source':
            descriptions.append(
                f"La destination virtuelle {item.get('mount_point') or '—'} contient le disque physique de la source "
                f"({', '.join(item.get('matching_branches') or []) or '—'})."
            )
        elif item.get('code') == 'mergerfs_source_contains_target':
            descriptions.append(
                f"La source virtuelle {item.get('source_mount_point') or '—'} contient le disque physique de la destination "
                f"({', '.join(item.get('matching_branches') or []) or '—'})."
            )
        elif item.get('code') == 'mergerfs_endpoints_overlap':
            descriptions.append(
                "La source et la destination mergerFS partagent au moins un même disque physique."
            )
        else:
            descriptions.append(
                f"La composition du montage virtuel {item.get('mount_point') or '—'} ne peut pas être vérifiée."
            )
    return {
        'code': 'mergerfs_path_forbidden',
        'title': 'Configuration mergerFS dangereuse',
        'message': 'Configuration mergerFS impossible en mode Cache.',
        'note': ' '.join(descriptions) + ' Le déplacement est autorisé uniquement si les deux côtés ne partagent aucun disque physique via mergerFS.',
        'details': details,
    }


def cache_script_launch_safety_error(script_path, mount_entries=None, fstab_entries=None):
    """Valide MODE puis contrôle mergerFS uniquement pour le mode cache."""
    content = read_text(script_path, '')
    data = extract_script_metadata(content)
    mode = str(data.get('MODE') or '').strip().lower()
    allowed_modes = {'backup', 'mirror', 'archive', 'cache'}
    if mode not in allowed_modes:
        message = (
            f'Désolé, l’étiquette MODE est inconnue ({mode}) : lancement refusé.'
            if mode
            else 'Désolé, l’étiquette MODE du script est introuvable : lancement refusé.'
        )
        return {
            'code': 'backup_mode_marker_missing',
            'title': 'Étiquette MODE introuvable',
            'message': message,
            'note': 'Veuillez ouvrir puis réenregistrer le script pour le refaire avec une étiquette MODE reconnue : backup, mirror, archive ou cache.',
            'details': [],
        }

    # Les montages virtuels sont dangereux uniquement pour Cache, car ce mode
    # déplace puis supprime les fichiers source avec --remove-source-files.
    if mode != 'cache':
        return None

    source = str(data.get('SOURCE') or '').strip()
    target = str(data.get('TARGET') or '').strip()
    if not source or not target:
        return {
            'code': 'cache_paths_unverifiable',
            'title': 'Chemins Cache introuvables',
            'message': 'Ancien script Cache impossible à vérifier : lancement refusé.',
            'note': 'Ouvrez puis réenregistrez ce script dans le module Backup afin d’intégrer les protections actuelles.',
            'details': [],
        }
    issues = cache_mergerfs_mount_issues(
        source,
        target,
        mount_entries=mount_entries,
        fstab_entries=fstab_entries,
    )
    return cache_mergerfs_error_payload(issues) if issues else None


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
    'SKIP_UNCHANGED': '1',
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
    # En mode cache, DRY_RUN désigne la pré-analyse rsync qui calcule le
    # volume à déplacer avant le transfert réel. L'option n'est pas affichée
    # dans le formulaire cache : elle doit donc être forcée ici.
    'DRY_RUN': '1',
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
        'SKIP_UNCHANGED': '1' if form.get('skip_unchanged') else '0',
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
        'SKIP_UNCHANGED': '1',
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
