#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
services.py - module Flask fusionné pour Services.

Contient : MiniDLNA, ProFTPD, SFTP et Backup intégré.
MDNS reste volontairement séparé pour aller dans Système/Réseau.
"""

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for, g
import argparse
import configparser
import fcntl
import glob
import sys
import threading
import uuid
from pathlib import Path
import os
import pwd
import grp
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sqlite3
import urllib.request
import html
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, Iterable
import ipaddress
import json
from io import StringIO
from typing import Any, Dict, List, Optional, Tuple
import copy

services_bp = Blueprint("services_bp", __name__)

# ==========================================================
# 📁 CONF CENTRALISÉE
# ==========================================================
# app.py pose NAS_CONF_DIR. Les modules le lisent sans importer app.py
# pour éviter les imports circulaires pendant le chargement des blueprints.
_NAS_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_NAS_DEFAULT_CONF_DIR = os.path.abspath(os.path.join(_NAS_MODULE_DIR, "..", "conf"))
NAS_CONF_DIR = os.path.abspath(os.path.expanduser(os.path.expandvars(os.environ.get("NAS_CONF_DIR", _NAS_DEFAULT_CONF_DIR))))
NAS_ROOT_DIR = os.path.abspath(os.path.join(NAS_CONF_DIR, ".."))

def nas_conf_file(name: str) -> str:
    return os.path.join(NAS_CONF_DIR, name)

def nas_root_path(*parts: str) -> str:
    return os.path.join(NAS_ROOT_DIR, *parts)


# Un seul fichier de configuration pour le module fusionné.
SERVICES_CONFIG_FILE = os.environ.get("SERVICES_MODULE_CONF", nas_conf_file("services.conf"))


# Le module Services ne doit pas inventer de dossier YAML pour les services Docker.
# ProFTPD et SFTP lisent le dossier YAML déclaré dans le module Docker : dockers.conf / YML_FOLDER.
DOCKERS_CONFIG_FILE = nas_conf_file("dockers.conf")


def svc_read_kv_file(path: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not path or not os.path.exists(path):
        return data
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().upper()
            if key:
                data[key] = _svc_strip_quotes(value)
    return data


def svc_resolve_path_from_conf(value: str, base_dir: Optional[str] = None) -> str:
    raw = _svc_strip_quotes(str(value or "")).strip()
    if not raw:
        return ""
    raw = os.path.expanduser(os.path.expandvars(raw))
    if os.path.isabs(raw):
        return os.path.abspath(raw)
    return os.path.abspath(os.path.join(base_dir or NAS_CONF_DIR, raw))


def svc_docker_yml_folder_status() -> Tuple[str, str]:
    """Retourne (dossier_yml_absolu, erreur). Aucune valeur inventée ici."""
    if not os.path.exists(DOCKERS_CONFIG_FILE):
        return "", (
            "Veuillez d'abord configurer le module Docker.\n"
            "Le fichier dockers.conf n'existe pas encore, donc Services ne connaît pas le dossier YAML."
        )
    docker_conf = svc_read_kv_file(DOCKERS_CONFIG_FILE)
    raw_folder = (
        docker_conf.get("YML_FOLDER")
        or docker_conf.get("YML_DIR")
        or docker_conf.get("PATH_DOCKER_YML")
        or ""
    ).strip()
    if not raw_folder:
        return "", (
            "Veuillez d'abord configurer le module Docker.\n"
            "Dans le module Docker, renseigne le dossier YAML avant d'utiliser ce service."
        )
    return svc_resolve_path_from_conf(raw_folder, NAS_CONF_DIR).rstrip("/"), ""


def svc_docker_yaml_error(service_label: str, yaml_name: str, yaml_path: str = "") -> str:
    yml_folder, setup_error = svc_docker_yml_folder_status()
    expected = yaml_path or (os.path.join(yml_folder, yaml_name) if yml_folder else yaml_name)
    if setup_error:
        return (
            f"{setup_error}\n\n"
            f"{service_label} fonctionne via Docker : son YAML doit venir du dossier YAML configuré dans le module Docker.\n"
            f"Fichier YAML attendu ensuite : {expected}\n\n"
            "Veuillez d'abord configurer le module Docker et vous assurer que dans ce module, vous mettez bien le fichier YAML correspondant."
        )
    return (
        "Veuillez d'abord configurer le module Docker et vous assurer que dans ce module, "
        "vous mettez bien le fichier YAML correspondant.\n\n"
        f"Service : {service_label}\n"
        f"Dossier YAML Docker : {yml_folder}\n"
        f"Fichier YAML attendu : {expected}"
    )


def svc_docker_yaml_path(service_label: str, yaml_name: str) -> Tuple[str, str]:
    yml_folder, setup_error = svc_docker_yml_folder_status()
    yaml_path = os.path.join(yml_folder, yaml_name) if yml_folder else ""
    if setup_error:
        return "", svc_docker_yaml_error(service_label, yaml_name, yaml_path)
    if not os.path.exists(yaml_path):
        return yaml_path, svc_docker_yaml_error(service_label, yaml_name, yaml_path)
    return yaml_path, ""


def svc_env_file_from_docker_yaml(conf: Dict[str, str]) -> str:
    yaml_path = str(conf.get("YAML") or "").strip()
    if yaml_path:
        return os.path.join(os.path.dirname(yaml_path), ".env")
    yml_folder, _err = svc_docker_yml_folder_status()
    return os.path.join(yml_folder, ".env") if yml_folder else ""

archive_DEFAULT_CONFIG: Dict[str, str] = {
    "PATH_CONF": "../conf/archive.conf",
    "BROWSE_ROOTS": "/",
    "BACKUP_DIR": "../backups/archive",
}
archive_CONFIG_ORDER = ["PATH_CONF", "BROWSE_ROOTS", "BACKUP_DIR"]

cache_DEFAULT_CONFIG: Dict[str, str] = {
    "PATH_CONF": "../conf/cache.conf",
    # Conservé derrière pour la sécurité du navigateur de dossiers, mais plus affiché dans l'UI Cache.
    "BROWSE_ROOTS": "/",
    "BACKUP_DIR": "../backups/cache",
    "LOG_DIR": "/var/log/cache",
}
cache_CONFIG_ORDER = ["PATH_CONF", "BROWSE_ROOTS", "BACKUP_DIR", "LOG_DIR"]


def _svc_strip_quotes(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _svc_read_lines() -> List[str]:
    if not os.path.exists(SERVICES_CONFIG_FILE):
        return []
    with open(SERVICES_CONFIG_FILE, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read().splitlines()


def _svc_read_values() -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    values: Dict[str, str] = {}
    multi: Dict[str, List[str]] = {}
    for raw in _svc_read_lines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().upper()
        value = _svc_strip_quotes(value)
        if not key:
            continue
        if key in {"MINIDLNA_SHARE", "PROFTPD_SHARE"}:
            multi.setdefault(key, []).append(value)
        else:
            values[key] = value
    return values, multi


def _svc_write_all(values: Dict[str, str], multi: Optional[Dict[str, List[str]]] = None) -> None:
    multi = multi or {}
    os.makedirs(os.path.dirname(SERVICES_CONFIG_FILE) or ".", exist_ok=True)
    def val(key: str, default: str = "") -> str:
        return str(values.get(key, default))
    lines: List[str] = []
    lines += [
        "# ============================================================",
        "# services.conf - Configuration du module Flask Services",
        "# Modules fusionnés : MiniDLNA, ProFTPD, SFTP, Backup",
        "# MDNS n'est pas ici : à garder dans Système/Réseau.",
        "# ============================================================",
        "",
        "# ---------------- MiniDLNA host ----------------",
    ]
    for key in mini_CONFIG_ORDER:
        full = "MINIDLNA_" + key
        lines.append(f"{full}={val(full, mini_DEFAULT_CONFIG.get(key, ''))}")
    lines += ["", "# Dossiers médias exposés par MiniDLNA"]
    shares = multi.get("MINIDLNA_SHARE", [])
    for share in shares:
        lines.append(f"MINIDLNA_SHARE={share}")
    lines += ["", "# ---------------- ProFTPD host ----------------"]
    lines.append("# Serveur FTP host : les accès reposent sur les utilisateurs et droits Linux.")
    for key in pro_CONFIG_ORDER:
        full = "PROFTPD_" + key
        lines.append(f"{full}={val(full, pro_DEFAULT_CONFIG.get(key, ''))}")
    lines += ["", "# Dossiers FTP host : chemin|mode. Le premier dossier devient la racine chroot DefaultRoot."]
    for share in multi.get("PROFTPD_SHARE", []):
        lines.append(f"PROFTPD_SHARE={share}")
    lines += ["", "# ---------------- SFTP Docker ----------------"]
    lines.append("# YAML dérivé depuis dockers.conf / YML_FOLDER : pas de chemin inventé dans services.conf.")
    for key in ("SERVICE", "BROWSE_ROOTS", "BACKUP_DIR", "DOCKER_BIN", "ENABLE_DOCKER_COMPOSE_CHECK"):
        full = "SFTP_" + key
        lines.append(f"{full}={val(full, sftp_DEFAULT_CONFIG.get(key, ''))}")
    lines += ["", "# ---------------- Archive intégrée ----------------"]
    for key in archive_CONFIG_ORDER:
        full = "ARCHIVE_" + key
        lines.append(f"{full}={val(full, archive_DEFAULT_CONFIG.get(key, ''))}")
    lines += ["", "# ---------------- Cache integre ----------------"]
    for key in cache_CONFIG_ORDER:
        full = "CACHE_" + key
        lines.append(f"{full}={val(full, cache_DEFAULT_CONFIG.get(key, ''))}")
    with open(SERVICES_CONFIG_FILE, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines).rstrip() + "\n")


def _svc_update_prefixed(prefix: str, data: Dict[str, str], order: List[str]) -> None:
    values, multi = _svc_read_values()
    # initialise depuis les defaults si le fichier n'existe pas encore
    if not values and not multi:
        values = _svc_default_values()
        multi = _svc_default_multi()
    for key in order:
        values[prefix + key] = str(data.get(key, ""))
    _svc_write_all(values, multi)


def _svc_default_values() -> Dict[str, str]:
    values: Dict[str, str] = {}
    for key in mini_CONFIG_ORDER:
        values["MINIDLNA_" + key] = str(mini_DEFAULT_CONFIG.get(key, ""))
    for key, value in pro_DEFAULT_CONFIG.items():
        values["PROFTPD_" + key] = str(value)
    for key, value in sftp_DEFAULT_CONFIG.items():
        values["SFTP_" + key] = str(value)
    for key, value in archive_DEFAULT_CONFIG.items():
        values["ARCHIVE_" + key] = str(value)
    for key, value in cache_DEFAULT_CONFIG.items():
        values["CACHE_" + key] = str(value)
    return values


def _svc_default_multi() -> Dict[str, List[str]]:
    # MiniDLNA ne doit exposer aucun dossier média par défaut.
    # Si l'utilisateur veut des médias, il les ajoute explicitement dans l'UI.
    return {}


def _svc_ensure_file() -> None:
    if not os.path.exists(SERVICES_CONFIG_FILE):
        _svc_write_all(_svc_default_values(), _svc_default_multi())


def _svc_prefixed_config(prefix: str, defaults: Dict[str, str], order: List[str]) -> Dict[str, str]:
    _svc_ensure_file()
    values, _multi = _svc_read_values()
    out = defaults.copy()
    for key in order:
        full = prefix + key
        if full in values:
            out[key] = values[full]
    return out


def _svc_prefixed_mini() -> Tuple[Dict[str, str], List["mini_MiniShare"]]:
    _svc_ensure_file()
    values, multi = _svc_read_values()
    conf = mini_DEFAULT_CONFIG.copy()
    for key in mini_CONFIG_ORDER:
        full = "MINIDLNA_" + key
        if full in values:
            conf[key] = values[full]
    shares: List[mini_MiniShare] = []
    for idx, raw in enumerate(multi.get("MINIDLNA_SHARE", []), 1):
        share = mini_parse_share_line("MINIDLNA_SHARE=" + raw, idx)
        if share:
            shares.append(share)
    return conf, shares


def _svc_write_mini(conf: Dict[str, str], shares: List["mini_MiniShare"]) -> None:
    values, multi = _svc_read_values()
    if not values and not multi:
        values = _svc_default_values()
        multi = _svc_default_multi()
    for key in mini_CONFIG_ORDER:
        values["MINIDLNA_" + key] = str(conf.get(key, mini_DEFAULT_CONFIG.get(key, "")))
    multi["MINIDLNA_SHARE"] = [f"{s.media_type}|{mini_safe_share_field(s.name)}|{mini_safe_share_field(s.host_path)}" for s in shares]
    _svc_write_all(values, multi)


# Les anciennes constantes CONFIG_FILE pointent maintenant vers le conf fusionné.

# ============================================================
# MINI MERGED MODULE
# ============================================================
mini_CONFIG_FILE = nas_conf_file("minidnla.conf")
mini_DEFAULT_CONFIG: Dict[str, str] = {
    "SERVICE_NAME": "minidnla-host.service",
    "MINIDLNA_BIN": "/usr/sbin/minidlnad",
    "MINIDLNA_CONF": "../conf/minidlnad.conf",
    "DB_DIR": "/var/cache/minidlna",
    "LOG_DIR": "/var/log/minidlna",
    "SERVICE_USER": "minidlna",
    "SERVICE_GROUP": "users",
    "BROWSE_ROOTS": "/",
    "FRIENDLY_NAME": "Minidnla",
    "ENABLE_SUBTITLES": "yes",
    "PORT": "8200",
    "NETWORK_INTERFACE": "",
    "LOG_LEVEL": "off",
    "SERIAL": "12345678",
    "MODEL_NUMBER": "1",
    "MAX_CONNECTIONS": "50",
    "ALBUM_ART_NAMES": "Cover.jpg/cover.jpg/AlbumArt.jpg/albumart.jpg/Folder.jpg/folder.jpg/Thumb.jpg/thumb.jpg",
    "INOTIFY": "yes",
    "ENABLE_TIVO": "no",
    "TIVO_DISCOVERY": "bonjour",
    "STRICT_DLNA": "no",
    "NOTIFY_INTERVAL": "900",
}
mini_CONFIG_ORDER = [
    "SERVICE_NAME", "MINIDLNA_BIN", "MINIDLNA_CONF", "DB_DIR", "LOG_DIR",
    "SERVICE_USER", "SERVICE_GROUP", "BROWSE_ROOTS", "FRIENDLY_NAME",
    "ENABLE_SUBTITLES", "PORT", "NETWORK_INTERFACE", "LOG_LEVEL", "SERIAL",
    "MODEL_NUMBER", "MAX_CONNECTIONS", "ALBUM_ART_NAMES", "INOTIFY",
    "ENABLE_TIVO", "TIVO_DISCOVERY", "STRICT_DLNA", "NOTIFY_INTERVAL",
]
mini_MEDIA_TYPES = {'mixed', 'video', 'audio', 'photo'}
mini_MEDIA_PREFIX = {'mixed': '', 'video': 'V,', 'audio': 'A,', 'photo': 'P,'}
mini_MEDIA_LABEL = {'mixed': 'Tout', 'video': 'Vidéos', 'audio': 'Musique', 'photo': 'Photos'}
mini_SHARE_REJECT_RE = re.compile('[\\r\\n|]')

@dataclass
class mini_MiniShare:
    index: int
    name: str
    host_path: str
    media_type: str = 'mixed'

    @property
    def media_dir_line(self) -> str:
        prefix = mini_MEDIA_PREFIX.get(self.media_type, '')
        return f'media_dir={prefix}{self.host_path}'

    @property
    def label(self) -> str:
        return mini_MEDIA_LABEL.get(self.media_type, 'Tout')

def mini_normalize_path(value: str) -> str:
    value = (value or '').strip().replace('\\', '/')
    if not value:
        return ''
    return os.path.normpath(value)

def mini_abs_path(path: str) -> str:
    path = mini_normalize_path(path)
    if not path:
        return ''
    if os.path.isabs(path):
        return path
    # Les chemins MiniDLNA du services.conf restent portables :
    # ../conf, ../cache, ../logs sont résolus depuis le dossier du module
    # Flask (/dockers/system), et non depuis le cwd du service.
    return os.path.abspath(os.path.join(_NAS_MODULE_DIR, path))

def mini_yes_no(value: str, default: str='no') -> str:
    v = str(value or default).strip().lower()
    return 'yes' if v in {'1', 'true', 'yes', 'on', 'oui'} else 'no'

def mini_read_lines(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    with open(path, 'r', encoding='utf-8', errors='replace') as handle:
        return handle.read().splitlines()

def mini_parse_share_line(raw: str, index: int) -> Optional[mini_MiniShare]:
    value = raw.split('=', 1)[1].strip() if '=' in raw else raw.strip()
    parts = value.split('|', 2)
    if len(parts) != 3:
        return None
    media_type, name, host_path = (p.strip() for p in parts)
    media_type = media_type if media_type in mini_MEDIA_TYPES else 'mixed'
    host_path = mini_normalize_path(host_path)
    name = name or os.path.basename(host_path.rstrip('/')) or f'Media{index}'
    return mini_MiniShare(index=index, name=name, host_path=host_path, media_type=media_type)

def mini_read_module_config() -> Tuple[Dict[str, str], List[mini_MiniShare]]:
    conf = mini_DEFAULT_CONFIG.copy()
    shares: List[mini_MiniShare] = []
    for raw in mini_read_lines(mini_CONFIG_FILE):
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip().upper()
        value = value.strip().strip('"').strip("'")
        if key == 'SHARE':
            share = mini_parse_share_line(line, len(shares) + 1)
            if share:
                shares.append(share)
        elif key:
            conf[key] = value
    if not os.path.exists(mini_CONFIG_FILE):
        mini_write_module_config(conf, shares)
    return (conf, shares)

def mini_safe_conf_value(value: str) -> str:
    return str(value or '').replace('\r', ' ').replace('\n', ' ').strip()

def mini_safe_share_field(value: str) -> str:
    return mini_SHARE_REJECT_RE.sub('_', str(value or '').strip())

def mini_write_module_config(conf: Dict[str, str], shares: List[mini_MiniShare]) -> None:
    os.makedirs(os.path.dirname(mini_CONFIG_FILE), exist_ok=True)
    merged = mini_DEFAULT_CONFIG.copy()
    merged.update({k.upper(): mini_safe_conf_value(v) for k, v in conf.items()})
    lines = ['# Configuration du module Flask MiniDLNA en host', "# Ce fichier remplace l'ancien pilotage Docker/YAML pour MiniDLNA.", '# Les lignes SHARE utilisent le format : SHARE=type|nom|chemin_hote', '# type = mixed, video, audio ou photo', '']
    for key in mini_CONFIG_ORDER:
        lines.append(f"{key}={merged.get(key, mini_DEFAULT_CONFIG.get(key, ''))}")
    lines.append('')
    lines.append('# Dossiers médias exposés par MiniDLNA')
    if not shares:
        lines.append('# SHARE=video|Films|/mnt/user/Films')
        lines.append('# SHARE=audio|Musique|/mnt/user/Musique')
        lines.append('# SHARE=photo|Photos|/mnt/user/Photos')
    for share in shares:
        lines.append('SHARE=' + mini_safe_share_field(share.media_type) + '|' + mini_safe_share_field(share.name) + '|' + mini_safe_share_field(share.host_path))
    with open(mini_CONFIG_FILE, 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(lines).rstrip() + '\n')

def mini_allowed_roots(conf: Dict[str, str]) -> List[str]:
    roots: List[str] = []
    for raw in str(conf.get('BROWSE_ROOTS', '/')).split(','):
        path = mini_abs_path(raw)
        if path:
            roots.append(os.path.realpath(path))
    return roots or ['/']

def mini_is_under_allowed_root(path: str, roots: List[str]) -> bool:
    if not path:
        return False
    real = os.path.realpath(path)
    for root in roots:
        if root == '/':
            return True
        root = root.rstrip('/')
        if real == root or real.startswith(root + '/'):
            return True
    return False

def mini_collect_settings_from_form(conf: Dict[str, str]) -> Dict[str, str]:
    out = conf.copy()

    # Interface simplifiée : l'utilisateur ne règle que le nom visible et le port.
    # Les options avancées sont forcées ici à des valeurs NAS cohérentes à chaque
    # sauvegarde, même si un ancien conf/minidnla.conf contenait d'autres valeurs.
    for key in ['FRIENDLY_NAME', 'PORT']:
        if key in request.form:
            out[key] = mini_safe_conf_value(request.form.get(key, ''))

    out.update({
        'ENABLE_SUBTITLES': 'yes',
        'NETWORK_INTERFACE': '',
        'LOG_LEVEL': 'off',
        'SERIAL': '12345678',
        'MODEL_NUMBER': '1',
        'MAX_CONNECTIONS': '50',
        'ALBUM_ART_NAMES': mini_DEFAULT_CONFIG['ALBUM_ART_NAMES'],
        'INOTIFY': 'yes',
        'ENABLE_TIVO': 'no',
        'TIVO_DISCOVERY': 'bonjour',
        'STRICT_DLNA': 'no',
        'NOTIFY_INTERVAL': '900',
    })
    return out

def mini_collect_module_conf_from_form(conf: Dict[str, str]) -> Dict[str, str]:
    out = conf.copy()
    for key in ['SERVICE_NAME', 'MINIDLNA_BIN', 'MINIDLNA_CONF', 'DB_DIR', 'LOG_DIR', 'SERVICE_USER', 'SERVICE_GROUP', 'BROWSE_ROOTS']:
        if key in request.form:
            out[key] = mini_safe_conf_value(request.form.get(key, ''))
    if not out.get('SERVICE_NAME', '').endswith('.service'):
        out['SERVICE_NAME'] = (out.get('SERVICE_NAME') or 'minidnla-host') + '.service'
    return out

def mini_collect_shares_from_form() -> List[mini_MiniShare]:
    names = request.form.getlist('share_name[]')
    paths = request.form.getlist('share_path[]')
    types = request.form.getlist('share_type[]')
    total = max(len(names), len(paths), len(types))
    shares: List[mini_MiniShare] = []
    for i in range(total):
        host_path = mini_normalize_path(paths[i]) if i < len(paths) else ''
        name = names[i].strip() if i < len(names) else ''
        media_type = types[i].strip().lower() if i < len(types) else 'mixed'
        if not any([host_path, name]):
            continue
        if media_type not in mini_MEDIA_TYPES:
            media_type = 'mixed'
        if not name:
            name = os.path.basename(host_path.rstrip('/')) or f'Media{i + 1}'
        shares.append(mini_MiniShare(index=len(shares) + 1, name=name, host_path=host_path, media_type=media_type))
    return shares

def mini_validate_config(conf: Dict[str, str]) -> List[str]:
    errors: List[str] = []
    try:
        port = int(conf.get('PORT', '8200'))
        if not 1 <= port <= 65535:
            errors.append('Le port MiniDLNA doit être entre 1 et 65535.')
    except ValueError:
        errors.append('Le port MiniDLNA doit être un nombre.')
    for key in ['MAX_CONNECTIONS', 'NOTIFY_INTERVAL']:
        try:
            value = int(conf.get(key, mini_DEFAULT_CONFIG[key]))
            if value < 0:
                errors.append(f'{key} doit être positif.')
        except ValueError:
            errors.append(f'{key} doit être un nombre.')
    for key in ['MINIDLNA_CONF', 'DB_DIR', 'LOG_DIR']:
        if not conf.get(key, '').strip():
            errors.append(f'{key} ne doit pas être vide.')
    if '/' in conf.get('SERVICE_NAME', '') or not conf.get('SERVICE_NAME', '').endswith('.service'):
        errors.append('SERVICE_NAME doit être un nom systemd simple, par exemple minidnla-host.service.')
    return errors

def mini_validate_shares(conf: Dict[str, str], shares: List[mini_MiniShare]) -> List[str]:
    errors: List[str] = []
    roots = mini_allowed_roots(conf)
    seen = set()
    for share in shares:
        if mini_SHARE_REJECT_RE.search(share.name):
            errors.append(f'Nom invalide pour le partage {share.index}.')
        if not share.host_path.startswith('/'):
            errors.append(f'Chemin invalide pour {share.name} : {share.host_path}')
            continue
        if not mini_is_under_allowed_root(share.host_path, roots):
            errors.append(f'Chemin refusé pour {share.name} : hors BROWSE_ROOTS.')
        if not os.path.isdir(share.host_path):
            errors.append(f"Le dossier n'existe pas ou n'est pas un dossier : {share.host_path}")
        real = os.path.realpath(share.host_path)
        if real in seen:
            errors.append(f'Dossier en double : {share.host_path}')
        seen.add(real)
        if share.media_type not in mini_MEDIA_TYPES:
            errors.append(f'Type média invalide pour {share.name}.')
    return errors

def mini_generate_minidlna_config_text(conf: Dict[str, str], shares: List[mini_MiniShare]) -> str:
    db_dir = mini_abs_path(conf.get('DB_DIR', mini_DEFAULT_CONFIG['DB_DIR']))
    log_dir = mini_abs_path(conf.get('LOG_DIR', mini_DEFAULT_CONFIG['LOG_DIR']))
    service_user = conf.get('SERVICE_USER', mini_DEFAULT_CONFIG['SERVICE_USER']).strip() or mini_DEFAULT_CONFIG['SERVICE_USER']
    lines = ['# Fichier généré par Flask System - module MiniDLNA host', "# Ne pas modifier à la main : modifie plutôt conf/minidnla.conf ou l'interface Flask.", f"friendly_name={conf.get('FRIENDLY_NAME', mini_DEFAULT_CONFIG['FRIENDLY_NAME'])}", f"enable_subtitles={mini_yes_no(conf.get('ENABLE_SUBTITLES', 'yes'), 'yes')}", f'user={service_user}', f"log_level={conf.get('LOG_LEVEL', 'off') or 'off'}", f'log_dir={log_dir}', f"port={conf.get('PORT', '8200') or '8200'}", f"serial={conf.get('SERIAL', '12345678') or '12345678'}", f"model_number={conf.get('MODEL_NUMBER', '1') or '1'}", f"max_connections={conf.get('MAX_CONNECTIONS', '50') or '50'}", f"album_art_names={conf.get('ALBUM_ART_NAMES', mini_DEFAULT_CONFIG['ALBUM_ART_NAMES'])}", f"inotify={mini_yes_no(conf.get('INOTIFY', 'yes'), 'yes')}", f"enable_tivo={mini_yes_no(conf.get('ENABLE_TIVO', 'no'), 'no')}", f"tivo_discovery={conf.get('TIVO_DISCOVERY', 'bonjour') or 'bonjour'}", f"strict_dlna={mini_yes_no(conf.get('STRICT_DLNA', 'no'), 'no')}", f"notify_interval={conf.get('NOTIFY_INTERVAL', '900') or '900'}", f'db_dir={db_dir}']
    iface = conf.get('NETWORK_INTERFACE', '').strip()
    if iface:
        lines.append(f'network_interface={iface}')
    lines.append('')
    lines.append('# Dossiers médias')
    if shares:
        for share in shares:
            prefix = mini_MEDIA_PREFIX.get(share.media_type, '')
            lines.append(f'media_dir={prefix}{share.host_path}')
    else:
        lines.append('# Aucun media_dir configuré pour le moment.')
    return '\n'.join(lines).rstrip() + '\n'

def mini_write_daemon_config(conf: Dict[str, str], shares: List[mini_MiniShare]) -> str:
    conf_path = mini_abs_path(conf.get('MINIDLNA_CONF', mini_DEFAULT_CONFIG['MINIDLNA_CONF']))
    os.makedirs(os.path.dirname(conf_path), exist_ok=True)
    text = mini_generate_minidlna_config_text(conf, shares)
    with open(conf_path, 'w', encoding='utf-8') as handle:
        handle.write(text)
    return conf_path

def mini_service_unit_path(conf: Dict[str, str]) -> str:
    return os.path.join('/etc/systemd/system', conf.get('SERVICE_NAME', mini_DEFAULT_CONFIG['SERVICE_NAME']))

def mini_detect_minidlna_bin(conf: Dict[str, str]) -> str:
    configured = conf.get('MINIDLNA_BIN', '').strip()
    if configured and os.path.exists(configured):
        return configured
    return shutil.which('minidlnad') or configured or '/usr/sbin/minidlnad'

def mini_generate_service_unit_text(conf: Dict[str, str]) -> str:
    binary = mini_detect_minidlna_bin(conf)
    conf_path = mini_abs_path(conf.get('MINIDLNA_CONF', mini_DEFAULT_CONFIG['MINIDLNA_CONF']))
    return f'[Unit]\nDescription=MiniDLNA host - managed by Flask System\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nExecStart={binary} -d -f {conf_path}\nRestart=on-failure\nRestartSec=3\nKillSignal=SIGTERM\nTimeoutStopSec=20\n\n[Install]\nWantedBy=multi-user.target\n'

def mini_run_cmd(cmd: List[str], timeout: int=30) -> Tuple[int, str]:
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = ((completed.stdout or '') + (completed.stderr or '')).strip()
        return (completed.returncode, out or 'OK')
    except FileNotFoundError:
        return (127, f'Commande introuvable : {cmd[0]}')
    except subprocess.TimeoutExpired:
        return (124, 'Timeout : commande trop longue.')
    except Exception as exc:
        return (1, str(exc))

def mini_run_shell(script: str, timeout: int=120) -> Tuple[int, str]:
    return mini_run_cmd(['bash', '-lc', script], timeout=timeout)


def mini_linux_user_exists(name: str) -> bool:
    name = str(name or '').strip()
    if not name:
        return False
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def mini_linux_group_exists(name: str) -> bool:
    name = str(name or '').strip()
    if not name:
        return False
    try:
        grp.getgrnam(name)
        return True
    except KeyError:
        return False


def mini_user_in_group(user: str, group: str) -> bool:
    try:
        user_info = pwd.getpwnam(user)
        group_info = grp.getgrnam(group)
    except KeyError:
        return False
    if user_info.pw_gid == group_info.gr_gid:
        return True
    return user in group_info.gr_mem


def mini_ensure_robot_user(conf: Dict[str, str]) -> Tuple[bool, str]:
    """
    Prépare l'utilisateur robot MiniDLNA dès l'ouverture de la page.

    On ne tente pas de deviner les droits médias ici : on crée seulement le compte
    système sans shell et on l'ajoute au groupe commun configuré. Les dossiers médias
    restent contrôlés par l'admin via les permissions Linux habituelles.
    """
    user = str(conf.get('SERVICE_USER') or mini_DEFAULT_CONFIG['SERVICE_USER']).strip()
    group = str(conf.get('SERVICE_GROUP') or mini_DEFAULT_CONFIG['SERVICE_GROUP']).strip()

    # Sécurité : l'auto-création ne gère que le compte robot prévu par défaut.
    # Si l'admin choisit un autre utilisateur dans la conf, on ne le modifie pas.
    if user != mini_DEFAULT_CONFIG['SERVICE_USER']:
        return True, ''

    if os.geteuid() != 0:
        return False, "MiniDLNA : création automatique de l'utilisateur ignorée, Flask ne tourne pas en root."

    if not re.match(r'^[a-z_][a-z0-9_-]{0,31}$', user):
        return False, f"MiniDLNA : nom utilisateur invalide : {user}"

    messages: List[str] = []
    create_group = group if mini_linux_group_exists(group) else ''
    if not create_group and mini_linux_group_exists('users'):
        create_group = 'users'

    if not mini_linux_user_exists(user):
        cmd = ['useradd', '-r', '-M', '-s', '/usr/sbin/nologin']
        if create_group:
            cmd.extend(['-g', create_group])
        cmd.append(user)
        rc, out = mini_run_cmd(cmd, timeout=20)
        if rc != 0:
            return False, f"MiniDLNA : impossible de créer l'utilisateur système {user}.\n{out}"
        messages.append(f"Utilisateur système créé : {user}")

    # Même si le compte vient du paquet Debian, on force un shell non interactif,
    # mais sans afficher un message à chaque rechargement si c'est déjà bon.
    try:
        current_shell = pwd.getpwnam(user).pw_shell
    except KeyError:
        current_shell = ''
    if current_shell not in {'/usr/sbin/nologin', '/sbin/nologin'}:
        rc, out = mini_run_cmd(['usermod', '-s', '/usr/sbin/nologin', user], timeout=20)
        if rc != 0:
            return False, f"MiniDLNA : impossible de passer {user} en nologin.\n{out}"
        messages.append(f"Shell désactivé pour {user}")

    if group and mini_linux_group_exists(group) and not mini_user_in_group(user, group):
        rc, out = mini_run_cmd(['usermod', '-aG', group, user], timeout=20)
        if rc != 0:
            return False, f"MiniDLNA : impossible d'ajouter {user} au groupe {group}.\n{out}"
        messages.append(f"Groupe OK : {user} ajouté à {group}")

    return True, '\n'.join(messages)

def mini_chown_tree(path: str, user: str, group: str) -> str:
    if not user:
        return 'Utilisateur vide, chown ignoré.'
    user_group = f'{user}:{group}' if group else user
    rc, out = mini_run_cmd(['chown', '-R', user_group, path], timeout=60)
    if rc != 0:
        return f'chown {user_group} {path} : {out}'
    return f'chown OK : {user_group} {path}'

def mini_ensure_runtime_dirs(conf: Dict[str, str]) -> List[str]:
    messages: List[str] = []
    for key in ['DB_DIR', 'LOG_DIR']:
        path = mini_abs_path(conf.get(key, mini_DEFAULT_CONFIG[key]))
        os.makedirs(path, exist_ok=True)
        messages.append(f'Dossier OK : {path}')
        messages.append(mini_chown_tree(path, conf.get('SERVICE_USER', mini_DEFAULT_CONFIG['SERVICE_USER']), conf.get('SERVICE_GROUP', mini_DEFAULT_CONFIG['SERVICE_GROUP'])))
    conf_path = mini_abs_path(conf.get('MINIDLNA_CONF', mini_DEFAULT_CONFIG['MINIDLNA_CONF']))
    os.makedirs(os.path.dirname(conf_path), exist_ok=True)
    return messages

def mini_add_mode_bits(path: str, bits: int) -> bool:
    if os.path.islink(path):
        return False
    current = stat.S_IMODE(os.stat(path).st_mode)
    wanted = current | bits
    if wanted == current:
        return False
    os.chmod(path, wanted)
    return True

def mini_ensure_media_share_rights(shares: List[mini_MiniShare]) -> List[str]:
    if not shares:
        return ['Droits medias : aucun dossier configure.']
    if os.geteuid() != 0:
        return ['Droits medias ignores : Flask ne tourne pas en root.']

    messages: List[str] = []
    for share in shares:
        root = os.path.realpath(share.host_path)
        if not os.path.isdir(root):
            messages.append(f'Droits medias ignores pour {share.host_path} : dossier introuvable.')
            continue

        changed_dirs = 0
        changed_files = 0
        errors: List[str] = []

        parent = root
        while parent and parent != os.path.dirname(parent):
            try:
                if os.path.isdir(parent) and mini_add_mode_bits(parent, stat.S_IROTH | stat.S_IXOTH):
                    changed_dirs += 1
            except Exception as exc:
                if len(errors) < 5:
                    errors.append(f'{parent}: {exc}')
            parent = os.path.dirname(parent)

        try:
            if mini_add_mode_bits(root, stat.S_IROTH | stat.S_IXOTH):
                changed_dirs += 1
        except Exception as exc:
            if len(errors) < 5:
                errors.append(f'{root}: {exc}')

        for current_dir, dirnames, filenames in os.walk(root, followlinks=False):
            for dirname in dirnames:
                path = os.path.join(current_dir, dirname)
                try:
                    if mini_add_mode_bits(path, stat.S_IROTH | stat.S_IXOTH):
                        changed_dirs += 1
                except Exception as exc:
                    if len(errors) < 5:
                        errors.append(f'{path}: {exc}')
            for filename in filenames:
                path = os.path.join(current_dir, filename)
                try:
                    if mini_add_mode_bits(path, stat.S_IROTH):
                        changed_files += 1
                except Exception as exc:
                    if len(errors) < 5:
                        errors.append(f'{path}: {exc}')

        msg = f'Droits medias OK : {share.host_path} ({changed_dirs} dossiers, {changed_files} fichiers ajustes).'
        if errors:
            msg += '\nErreurs limitees aux 5 premieres : ' + '; '.join(errors)
        messages.append(msg)
    return messages

def mini_install_package_if_needed(conf: Dict[str, str]) -> Tuple[int, str]:
    binary = mini_detect_minidlna_bin(conf)
    if binary and os.path.exists(binary):
        return (0, f'MiniDLNA déjà installé : {binary}')
    if shutil.which('apt-get') is None:
        return (1, 'apt-get introuvable : installation automatique impossible sur ce système.')
    script = 'export DEBIAN_FRONTEND=noninteractive; apt-get update && apt-get install -y minidlna'
    return mini_run_shell(script, timeout=300)

def mini_apply_host_service(conf: Dict[str, str], shares: List[mini_MiniShare]) -> Tuple[int, str]:
    logs: List[str] = []
    ok_user, user_msg = mini_ensure_robot_user(conf)
    if user_msg:
        logs.append(user_msg)
    if not ok_user:
        return (1, '\n'.join(logs))
    logs.extend(mini_ensure_runtime_dirs(conf))
    logs.extend(mini_ensure_media_share_rights(shares))
    conf_path = mini_write_daemon_config(conf, shares)
    logs.append(f'Conf MiniDLNA écrite : {conf_path}')
    unit_path = mini_service_unit_path(conf)
    os.makedirs(os.path.dirname(unit_path), exist_ok=True)
    with open(unit_path, 'w', encoding='utf-8') as handle:
        handle.write(mini_generate_service_unit_text(conf))
    logs.append(f'Service systemd écrit : {unit_path}')
    rc, out = mini_run_cmd(['systemctl', 'daemon-reload'], timeout=30)
    logs.append(out)
    return (rc, '\n'.join(logs))

def mini_systemctl(conf: Dict[str, str], action: str, timeout: int=30) -> Tuple[int, str]:
    service = conf.get('SERVICE_NAME', mini_DEFAULT_CONFIG['SERVICE_NAME'])
    return mini_run_cmd(['systemctl', action, service], timeout=timeout)

def mini_systemctl_value(conf: Dict[str, str], action: str) -> str:
    rc, out = mini_systemctl(conf, action, timeout=8)
    return out.strip() if rc == 0 else 'non'

def mini_get_service_status(conf: Dict[str, str]) -> Dict[str, str]:
    service = conf.get('SERVICE_NAME', mini_DEFAULT_CONFIG['SERVICE_NAME'])
    binary = mini_detect_minidlna_bin(conf)
    unit = mini_service_unit_path(conf)
    status = {'service': service, 'active': mini_systemctl_value(conf, 'is-active'), 'enabled': mini_systemctl_value(conf, 'is-enabled'), 'binary': binary, 'binary_exists': 'oui' if binary and os.path.exists(binary) else 'non', 'unit_path': unit, 'unit_exists': 'oui' if os.path.exists(unit) else 'non', 'conf_path': mini_abs_path(conf.get('MINIDLNA_CONF', mini_DEFAULT_CONFIG['MINIDLNA_CONF'])), 'conf_exists': 'oui' if os.path.exists(mini_abs_path(conf.get('MINIDLNA_CONF', mini_DEFAULT_CONFIG['MINIDLNA_CONF']))) else 'non', 'db_dir': mini_abs_path(conf.get('DB_DIR', mini_DEFAULT_CONFIG['DB_DIR'])), 'log_dir': mini_abs_path(conf.get('LOG_DIR', mini_DEFAULT_CONFIG['LOG_DIR']))}
    rc, show = mini_run_cmd(['systemctl', 'show', service, '--property=MainPID,SubState,LoadState,Result', '--no-pager'], timeout=8)
    status['show'] = show if rc == 0 else show
    rc, journal = mini_run_cmd(['journalctl', '-u', service, '-n', '35', '--no-pager'], timeout=10)
    status['journal'] = journal if rc == 0 else journal
    return status

def mini_clear_database(conf: Dict[str, str]) -> str:
    db_dir = mini_abs_path(conf.get('DB_DIR', mini_DEFAULT_CONFIG['DB_DIR']))
    removed: List[str] = []
    if not os.path.isdir(db_dir):
        return f'DB_DIR introuvable : {db_dir}'
    for name in ['files.db', 'files.db-journal', 'files.db-shm', 'files.db-wal']:
        path = os.path.join(db_dir, name)
        if os.path.exists(path):
            os.remove(path)
            removed.append(path)
    art_cache = os.path.join(db_dir, 'art_cache')
    if os.path.isdir(art_cache):
        shutil.rmtree(art_cache)
        removed.append(art_cache)
    return 'Base supprimée :\n' + '\n'.join(removed) if removed else 'Aucune base à supprimer.'

mini_VIDEO_EXTENSIONS = {
    '.3g2', '.3gp', '.asf', '.avi', '.divx', '.dv', '.f4v', '.flv',
    '.iso', '.m1v', '.m2t', '.m2ts', '.m2v', '.m4v', '.mkv', '.mov',
    '.mp4', '.mpeg', '.mpg', '.mts', '.ogm', '.ogv', '.rm', '.rmvb',
    '.ts', '.vob', '.webm', '.wmv',
}

mini_AUDIO_EXTENSIONS = {
    '.aac', '.aif', '.aiff', '.alac', '.ape', '.dsf', '.flac', '.m4a',
    '.mka', '.mp2', '.mp3', '.mpc', '.oga', '.ogg', '.opus', '.wav',
    '.wma',
}

mini_PHOTO_EXTENSIONS = {
    '.avif', '.bmp', '.gif', '.heic', '.heif', '.jpeg', '.jpg', '.jpe',
    '.png', '.tif', '.tiff', '.webp',
}


def mini_empty_media_counts() -> Dict[str, int]:
    return {'video': 0, 'audio': 0, 'photo': 0, 'mixed': 0}


def mini_detect_media_kind(path: str) -> str:
    ext = os.path.splitext(str(path or ''))[1].lower()
    if ext in mini_VIDEO_EXTENSIONS:
        return 'video'
    if ext in mini_AUDIO_EXTENSIONS:
        return 'audio'
    if ext in mini_PHOTO_EXTENSIONS:
        return 'photo'
    return ''


def mini_counts_from_status_page(conf: Dict[str, str]) -> Tuple[Dict[str, int], bool]:
    """
    Source prioritaire : la page de statut MiniDLNA elle-même.

    C'est volontairement la plus simple et la plus exacte pour l'interface :
    si http://127.0.0.1:8200 indique Video files = 4, le Flask doit afficher 4.
    """
    counts = mini_empty_media_counts()
    try:
        port = int(str(conf.get('PORT', mini_DEFAULT_CONFIG.get('PORT', '8200')) or '8200').strip())
    except Exception:
        port = 8200

    urls = [f'http://127.0.0.1:{port}/', f'http://localhost:{port}/']
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Flask-System-MiniDLNA'})
            with urllib.request.urlopen(req, timeout=2) as response:
                page = response.read(256 * 1024).decode('utf-8', errors='replace')

            rows = re.findall(
                r'<td[^>]*>\s*([^<]+?)\s*</td>\s*<td[^>]*>\s*([0-9]+)\s*</td>',
                page,
                flags=re.IGNORECASE | re.DOTALL,
            )
            found = False
            for label, value in rows:
                label = html.unescape(label).strip().lower()
                label = re.sub(r'\s+', ' ', label)
                number = int(value)
                if label == 'audio files':
                    counts['audio'] = number
                    found = True
                elif label == 'video files':
                    counts['video'] = number
                    found = True
                elif label in {'image files', 'photo files'}:
                    counts['photo'] = number
                    found = True

            if found:
                counts['mixed'] = counts['video'] + counts['audio'] + counts['photo']
                return counts, True
        except Exception:
            continue

    return counts, False


def mini_counts_from_database(conf: Dict[str, str]) -> Tuple[Dict[str, int], bool]:
    """
    Compte depuis files.db sans compter les doublons internes de MiniDLNA.

    Important : la table OBJECTS contient plusieurs lignes possibles pour un même fichier
    physique, parce qu'un média peut apparaître dans plusieurs vues DLNA. C'était la cause
    du 12 côté Flask alors que la page MiniDLNA officielle affichait 4 vidéos.
    """
    counts = mini_empty_media_counts()
    db_path = os.path.join(mini_abs_path(conf.get('DB_DIR', mini_DEFAULT_CONFIG['DB_DIR'])), 'files.db')
    if not os.path.isfile(db_path):
        return counts, False

    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=2)
        try:
            cur = conn.cursor()
            tables = {str(row[0]).upper() for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}

            # Meilleur cas : DETAILS = une ligne par fichier réellement indexé.
            if 'DETAILS' in tables:
                cols = {str(row[1]).upper() for row in cur.execute('PRAGMA table_info(DETAILS)')}
                mime_col = 'MIME' if 'MIME' in cols else ('MIME_TYPE' if 'MIME_TYPE' in cols else '')
                distinct_col = 'PATH' if 'PATH' in cols else ('ID' if 'ID' in cols else '')
                if mime_col:
                    select_expr = f'COUNT(DISTINCT {distinct_col})' if distinct_col else 'COUNT(*)'
                    counts['video'] = int(cur.execute(f"SELECT {select_expr} FROM DETAILS WHERE lower({mime_col}) LIKE 'video/%'").fetchone()[0] or 0)
                    counts['audio'] = int(cur.execute(f"SELECT {select_expr} FROM DETAILS WHERE lower({mime_col}) LIKE 'audio/%'").fetchone()[0] or 0)
                    counts['photo'] = int(cur.execute(f"SELECT {select_expr} FROM DETAILS WHERE lower({mime_col}) LIKE 'image/%'").fetchone()[0] or 0)
                    counts['mixed'] = counts['video'] + counts['audio'] + counts['photo']
                    return counts, True

            # Filet de sécurité : OBJECTS existe, mais on déduplique par DETAIL_ID.
            if 'OBJECTS' in tables:
                cols = {str(row[1]).upper() for row in cur.execute('PRAGMA table_info(OBJECTS)')}
                if 'CLASS' in cols:
                    distinct_expr = 'COUNT(DISTINCT DETAIL_ID)' if 'DETAIL_ID' in cols else 'COUNT(*)'
                    counts['video'] = int(cur.execute(f"SELECT {distinct_expr} FROM OBJECTS WHERE lower(CLASS) LIKE 'item.videoitem%'").fetchone()[0] or 0)
                    counts['audio'] = int(cur.execute(f"SELECT {distinct_expr} FROM OBJECTS WHERE lower(CLASS) LIKE 'item.audioitem%'").fetchone()[0] or 0)
                    counts['photo'] = int(cur.execute(f"SELECT {distinct_expr} FROM OBJECTS WHERE lower(CLASS) LIKE 'item.imageitem%'").fetchone()[0] or 0)
                    counts['mixed'] = counts['video'] + counts['audio'] + counts['photo']
                    return counts, True
        finally:
            conn.close()
    except Exception:
        return mini_empty_media_counts(), False

    return counts, False


def mini_read_daemon_media_dirs(conf: Dict[str, str]) -> List[mini_MiniShare]:
    """
    Petit filet de sécurité : si la conf du module ne contient pas encore les lignes SHARE,
    on sait aussi relire les media_dir du fichier minidlnad.conf généré.
    """
    conf_path = mini_abs_path(conf.get('MINIDLNA_CONF', mini_DEFAULT_CONFIG['MINIDLNA_CONF']))
    shares: List[mini_MiniShare] = []
    for raw in mini_read_lines(conf_path):
        line = raw.strip()
        if not line or line.startswith('#') or not line.lower().startswith('media_dir='):
            continue
        value = line.split('=', 1)[1].strip()
        media_type = 'mixed'
        if value.startswith('V,'):
            media_type = 'video'
            value = value[2:].strip()
        elif value.startswith('A,'):
            media_type = 'audio'
            value = value[2:].strip()
        elif value.startswith('P,'):
            media_type = 'photo'
            value = value[2:].strip()
        host_path = mini_normalize_path(value)
        if host_path:
            name = os.path.basename(host_path.rstrip('/')) or f'Media{len(shares) + 1}'
            shares.append(mini_MiniShare(index=len(shares) + 1, name=name, host_path=host_path, media_type=media_type))
    return shares


def mini_counts_from_filesystem(shares: List[mini_MiniShare]) -> Dict[str, int]:
    """
    Fallback simple : compte les fichiers par extension dans les dossiers déclarés.
    Pas de symlinks, pas d'erreur bloquante, pas de dépendance externe.
    """
    counts = mini_empty_media_counts()
    counted_files = set()

    for share in shares:
        base_path = mini_abs_path(share.host_path)
        if not base_path or not os.path.isdir(base_path):
            continue

        if share.media_type == 'mixed':
            allowed_kinds = {'video', 'audio', 'photo'}
        elif share.media_type in {'video', 'audio', 'photo'}:
            allowed_kinds = {share.media_type}
        else:
            allowed_kinds = {'video', 'audio', 'photo'}

        for root, _dirs, files in os.walk(base_path, topdown=True, onerror=lambda _err: None, followlinks=False):
            for filename in files:
                kind = mini_detect_media_kind(filename)
                if kind not in allowed_kinds:
                    continue

                full_path = os.path.realpath(os.path.join(root, filename))
                if full_path in counted_files:
                    continue

                counted_files.add(full_path)
                counts[kind] += 1
                counts['mixed'] += 1

    return counts


def mini_counts_by_type(conf_or_shares, shares: Optional[List[mini_MiniShare]] = None) -> Dict[str, int]:
    """
    Retourne les compteurs affichés dans l'onglet MiniDLNA.

    Priorité sobre :
    1. page officielle MiniDLNA http://127.0.0.1:PORT ;
    2. base MiniDLNA files.db, avec dédoublonnage ;
    3. scan simple des dossiers seulement si MiniDLNA ne répond pas et si la DB est absente.
    """
    if shares is None:
        conf = mini_DEFAULT_CONFIG.copy()
        shares = conf_or_shares or []
    else:
        conf = conf_or_shares or mini_DEFAULT_CONFIG.copy()
        shares = shares or []

    status_counts, status_ok = mini_counts_from_status_page(conf)
    if status_ok:
        return status_counts

    db_counts, db_ok = mini_counts_from_database(conf)
    if db_ok:
        return db_counts

    scan_shares = shares or mini_read_daemon_media_dirs(conf)
    fs_counts = mini_counts_from_filesystem(scan_shares)
    if fs_counts.get('mixed', 0) > 0:
        return fs_counts

    return mini_empty_media_counts()

SERVICES_MAIN_TABS = {'minidnla', 'proftpd', 'sftp', 'archive', 'cache', 'ffmpeg'}
MINIDLNA_SUBTABS = {'main', 'info', 'logs'}
MINIDLNA_LEGACY_SUBTAB_ALIASES = {'status': 'main', 'system': 'main', 'preview': 'logs', 'log': 'logs'}


def services_url(tab: str = 'minidnla', subtab: Optional[str] = None, **extra) -> str:
    """URL canonique du module Services : vraies routes /services/<service>/<subtab>.

    Les anciens liens /services?tab=...&subtab=... restent acceptés en lecture,
    mais les sauvegardes et actions ne doivent plus y renvoyer : sinon l'ancien
    bandeau horizontal peut réapparaître et le menu cranté haut perd son état.
    """
    tab = str(tab or 'minidnla').strip().lower()
    if tab not in SERVICES_MAIN_TABS:
        tab = 'minidnla'
    values: Dict[str, str] = {'service': tab}
    if subtab:
        values['subtab'] = str(subtab).strip().lower()
    for key, value in extra.items():
        if value is not None:
            values[key] = str(value)
    return url_for('services_bp.services_section', **values)


def services_redirect(tab: str = 'minidnla', subtab: Optional[str] = None, **extra):
    return redirect(services_url(tab=tab, subtab=subtab, **extra))


def services_requested_tab() -> str:
    forced = str(getattr(g, 'services_forced_tab', '') or '').strip().lower()
    if forced in SERVICES_MAIN_TABS:
        return forced
    requested = str(request.args.get('tab') or 'minidnla').strip().lower()
    # Compatibilité : les anciens liens MiniDLNA utilisaient ?tab=status/system/preview.
    if requested in MINIDLNA_SUBTABS or requested in MINIDLNA_LEGACY_SUBTAB_ALIASES:
        return 'minidnla'
    return requested if requested in SERVICES_MAIN_TABS else 'minidnla'


def mini_requested_subtab() -> str:
    forced = str(getattr(g, 'services_forced_subtab', '') or '').strip().lower()
    if forced in MINIDLNA_LEGACY_SUBTAB_ALIASES:
        return MINIDLNA_LEGACY_SUBTAB_ALIASES[forced]
    if forced in MINIDLNA_SUBTABS:
        return forced
    subtab = str(request.args.get('subtab') or '').strip().lower()
    if subtab in MINIDLNA_LEGACY_SUBTAB_ALIASES:
        return MINIDLNA_LEGACY_SUBTAB_ALIASES[subtab]
    if subtab in MINIDLNA_SUBTABS:
        return subtab
    legacy = str(request.args.get('tab') or '').strip().lower()
    if legacy in MINIDLNA_LEGACY_SUBTAB_ALIASES:
        return MINIDLNA_LEGACY_SUBTAB_ALIASES[legacy]
    if legacy in MINIDLNA_SUBTABS:
        return legacy
    return 'main'


def services_requested_subtab(allowed: Iterable[str], default: str) -> str:
    allowed_set = {str(item).strip().lower() for item in allowed}
    forced = str(getattr(g, 'services_forced_subtab', '') or '').strip().lower()
    if forced in allowed_set:
        return forced
    subtab = str(request.args.get('subtab') or '').strip().lower()
    return subtab if subtab in allowed_set else default


def mini_redirect_tab(tab: str) -> str:
    tab = str(tab or 'main').strip().lower()
    tab = MINIDLNA_LEGACY_SUBTAB_ALIASES.get(tab, tab)
    if tab not in MINIDLNA_SUBTABS:
        tab = 'main'
    return services_redirect('minidnla', subtab=tab)


@services_bp.route('/services', methods=['GET'])
def services_index():
    service_tab = services_requested_tab()
    # Compatibilité seulement : si une ancienne URL /services?tab=... arrive ici,
    # on la redirige immédiatement vers la vraie route propre. Cela évite de
    # réactiver les anciens bandeaux horizontaux et garde le menu cranté actif.
    if not str(getattr(g, 'services_forced_tab', '') or '').strip() and (request.args.get('tab') or request.args.get('subtab')):
        legacy_subtab = str(request.args.get('subtab') or '').strip().lower()
        if service_tab == 'minidnla':
            legacy_subtab = mini_requested_subtab()
        return services_redirect(service_tab, subtab=legacy_subtab or None)
    if service_tab == 'proftpd':
        return _render_proftpd()
    if service_tab == 'sftp':
        return _render_sftp()
    if service_tab == 'archive':
        return _render_archive()
    if service_tab == 'cache':
        return _render_cache()
    if service_tab == 'ffmpeg':
        return _render_ffmpeg()
    return _render_minidnla()


@services_bp.route('/services/<service>', methods=['GET'])
@services_bp.route('/services/<service>/<subtab>', methods=['GET'])
def services_section(service: str, subtab: str = ''):
    service = str(service or '').strip().lower()
    subtab = str(subtab or '').strip().lower()
    if service not in SERVICES_MAIN_TABS:
        return services_redirect('minidnla')
    g.services_forced_tab = service
    g.services_forced_subtab = subtab
    return services_index()




def _render_minidnla():
    conf, shares = mini_read_module_config()
    ok_user, user_msg = mini_ensure_robot_user(conf)
    if user_msg:
        flash(('✅ ' if ok_user else '⚠️ ') + user_msg, 'success' if ok_user else 'error')
    status = mini_get_service_status(conf)
    generated_config = mini_generate_minidlna_config_text(conf, shares)
    unit_preview = mini_generate_service_unit_text(conf)
    return render_template('services_minidlna.html', conf=conf, shares=shares, counts=mini_counts_by_type(conf, shares), status=status, generated_config=generated_config, unit_preview=unit_preview, config_file=mini_CONFIG_FILE, allowed_roots=mini_allowed_roots(conf), active_tab=mini_requested_subtab(), media_types=mini_MEDIA_LABEL, service_active='minidnla')

@services_bp.route('/services/minidnla/save', methods=['POST'])
def mini_minidnla_save():
    conf, _old_shares = mini_read_module_config()
    conf = mini_collect_settings_from_form(conf)
    shares = mini_collect_shares_from_form()
    errors = mini_validate_config(conf) + mini_validate_shares(conf, shares)
    if errors:
        for err in errors:
            flash('❌ ' + err, 'error')
        return mini_redirect_tab('main')
    try:
        mini_write_module_config(conf, shares)
        path = mini_write_daemon_config(conf, shares)
        rights = mini_ensure_media_share_rights(shares)
        rc, out = mini_apply_host_service(conf, shares)
        if rc == 0:
            rc2, out2 = mini_systemctl(conf, 'restart', timeout=30)
            if rc2 == 0:
                flash('✅ Configuration MiniDLNA sauvegardée, appliquée et service redémarré. Conf générée : ' + path + '\n' + '\n'.join(rights) + '\n' + out + '\n' + out2, 'success')
            else:
                flash('⚠️ Configuration sauvegardée et appliquée, mais redémarrage du service en erreur. Conf générée : ' + path + '\n' + '\n'.join(rights) + '\n' + out + '\n' + out2, 'error')
        else:
            flash('⚠️ Configuration sauvegardée, mais application systemd en erreur. Conf générée : ' + path + '\n' + '\n'.join(rights) + '\n' + out, 'error')
    except Exception as exc:
        flash(f'❌ Erreur sauvegarde MiniDLNA : {exc}', 'error')
    return mini_redirect_tab('main')

@services_bp.route('/services/minidnla/system/save', methods=['POST'])
def mini_minidnla_system_save():
    conf, shares = mini_read_module_config()
    conf = mini_collect_module_conf_from_form(conf)
    errors = mini_validate_config(conf)
    if errors:
        for err in errors:
            flash('❌ ' + err, 'error')
        return mini_redirect_tab('status')
    try:
        mini_write_module_config(conf, shares)
        flash('✅ Configuration système du module enregistrée.', 'success')
    except Exception as exc:
        flash(f'❌ Erreur écriture configuration système : {exc}', 'error')
    return mini_redirect_tab('status')

@services_bp.route('/services/minidnla/service/action', methods=['POST'])
def mini_minidnla_service_action():
    conf, shares = mini_read_module_config()
    action = request.form.get('action', '').strip().lower()
    try:
        if action == 'start':
            rc, out = mini_systemctl(conf, 'start')
        elif action == 'stop':
            rc, out = mini_systemctl(conf, 'stop')
        elif action == 'restart':
            mini_apply_host_service(conf, shares)
            rc, out = mini_systemctl(conf, 'restart')
        elif action == 'enable':
            rc, out = mini_systemctl(conf, 'enable')
        elif action == 'disable':
            rc, out = mini_systemctl(conf, 'disable')
        else:
            rc, out = (1, f'Action inconnue : {action}')
        flash(('✅ ' if rc == 0 else '❌ ') + out, 'success' if rc == 0 else 'error')
    except Exception as exc:
        flash(f'❌ Erreur action service : {exc}', 'error')
    return mini_redirect_tab('status')

@services_bp.route('/services/minidnla/system/action', methods=['POST'])
def mini_minidnla_system_action():
    conf, shares = mini_read_module_config()
    action = request.form.get('action', '').strip().lower()
    try:
        if action == 'install':
            rc1, out1 = mini_install_package_if_needed(conf)
            if rc1 != 0:
                flash('❌ Installation paquet MiniDLNA en erreur :\n' + out1, 'error')
                return mini_redirect_tab('status')
            rc2, out2 = mini_apply_host_service(conf, shares)
            rc3, out3 = mini_systemctl(conf, 'enable') if rc2 == 0 else (rc2, '')
            rc4, out4 = mini_systemctl(conf, 'restart') if rc2 == 0 else (rc2, '')
            ok = rc1 == rc2 == rc3 == rc4 == 0
            flash(('✅ Installation terminée.\n' if ok else '⚠️ Installation partielle.\n') + '\n'.join([out1, out2, out3, out4]), 'success' if ok else 'error')
        elif action == 'apply':
            rc, out = mini_apply_host_service(conf, shares)
            flash(('✅ Service systemd appliqué.\n' if rc == 0 else '❌ Application en erreur.\n') + out, 'success' if rc == 0 else 'error')
        elif action == 'rebuild_db':
            mini_apply_host_service(conf, shares)
            mini_systemctl(conf, 'stop', timeout=20)
            clean = mini_clear_database(conf)
            rc, out = mini_systemctl(conf, 'start', timeout=30)
            flash(('✅ Base MiniDLNA reconstruite.\n' if rc == 0 else '⚠️ Base nettoyée mais start en erreur.\n') + clean + '\n' + out, 'success' if rc == 0 else 'error')
        elif action == 'fix_runtime_rights':
            logs = mini_ensure_runtime_dirs(conf)
            logs.extend(mini_ensure_media_share_rights(shares))
            flash('✅ Droits runtime et médias corrigés.\n' + '\n'.join(logs), 'success')
        elif action == 'remove_service':
            mini_systemctl(conf, 'stop', timeout=20)
            mini_systemctl(conf, 'disable', timeout=20)
            unit = mini_service_unit_path(conf)
            if os.path.exists(unit):
                os.remove(unit)
            rc, out = mini_run_cmd(['systemctl', 'daemon-reload'], timeout=30)
            flash('✅ Service systemd supprimé. Le paquet MiniDLNA et les confs restent en place.\n' + out, 'success' if rc == 0 else 'error')
        else:
            flash(f'❌ Action inconnue : {action}', 'error')
    except Exception as exc:
        flash(f'❌ Erreur action système : {exc}', 'error')
    return mini_redirect_tab('status')

@services_bp.route('/services/minidnla/api/status', methods=['GET'])
def mini_minidnla_api_status():
    conf, _shares = mini_read_module_config()
    return jsonify(mini_get_service_status(conf))

@services_bp.route('/services/minidnla/api/browse', methods=['GET'])
def mini_minidnla_browse():
    conf, _shares = mini_read_module_config()
    roots = mini_allowed_roots(conf)
    requested = mini_normalize_path(request.args.get('path') or roots[0])
    if not mini_is_under_allowed_root(requested, roots):
        requested = roots[0]
    real = os.path.realpath(requested)
    if not os.path.isdir(real):
        return (jsonify({'ok': False, 'path': real, 'error': 'Dossier introuvable ou non accessible.', 'items': []}), 404)
    items = []
    try:
        if real not in roots:
            parent = os.path.dirname(real.rstrip('/')) or '/'
            if mini_is_under_allowed_root(parent, roots):
                items.append({'name': '..', 'path': parent, 'type': 'parent'})
        for name in sorted(os.listdir(real), key=str.lower):
            path = os.path.join(real, name)
            if os.path.isdir(path):
                items.append({'name': name, 'path': path, 'type': 'dir'})
    except PermissionError:
        return (jsonify({'ok': False, 'path': real, 'error': 'Permission refusée.', 'items': items}), 403)
    except Exception as exc:
        return (jsonify({'ok': False, 'path': real, 'error': str(exc), 'items': items}), 500)
    return jsonify({'ok': True, 'path': real, 'roots': roots, 'items': items})

# ============================================================
# PRO MERGED MODULE
# ============================================================
pro_CONFIG_FILE = nas_conf_file("proftpd.conf")
pro_CONFIG_ORDER = ['SERVICE_NAME', 'CONF_FILE', 'PORT', 'PASSIVE_PORTS', 'FTP_ROOT', 'BROWSE_ROOTS', 'BACKUP_DIR']
pro_DEFAULT_CONFIG = {
    'SERVICE_NAME': 'proftpd',
    'CONF_FILE': '/etc/proftpd/conf.d/yoleo.conf',
    'PORT': '21',
    'PASSIVE_PORTS': '30000 30100',
    'FTP_ROOT': '/mnt/user',
    'BROWSE_ROOTS': '/mnt/user,/mnt/ramdisk,/boot,/mnt',
    'BACKUP_DIR': '../backups/proftpd',
    # Anciennes clés Docker conservées uniquement pour compatibilité avec les vieux services.conf.
    'YAML': '',
    'SERVICE': '',
    'DOCKER_BIN': 'docker',
    'ENABLE_DOCKER_COMPOSE_CHECK': '0',
}
pro_USER_RE = re.compile('^[a-z_][a-z0-9_-]{0,31}$')
pro_SHARE_RE = re.compile('^[^:/\\\\\\x00]+$')
pro_RESERVED_TARGETS = {'/data', '/config', '/app', '/tmp', '/run', '/etc', '/root', '/var', '/usr', '/bin', '/sbin', '/lib', '/lib64', '/proc', '/sys', '/dev'}
pro_DEFAULT_ENV = {'TZ': 'Europe/Paris'}
