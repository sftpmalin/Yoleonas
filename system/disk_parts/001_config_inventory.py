#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Module DISK - cockpit disques lecture seule pour Flask System.

Version "anti-réveil HDD" :
  - pas de parted
  - pas de lsblk dans l'API
  - inventaire via /sys/block + /proc/self/mountinfo
  - état veille via hdparm -C
  - si un HDD est en standby :
      * aucun smartctl -A
      * aucun shutil.disk_usage/statvfs
      * température affichée en "*"
      * usage affiché depuis le dernier cache JSON connu
      * point gris
  - si un HDD est actif :
      * température lue avec smartctl -n standby pour éviter le réveil accidentel
      * usage filesystem lu seulement si autorisé

Dépendances utiles côté host/container :
  - hdparm : lecture d'état active/standby des HDD
  - smartmontools : température/SMART uniquement quand le disque est actif
  - aucun besoin de parted
  - aucun besoin de lsblk pour le fonctionnement normal

Si lancé dans Docker :
  - privileged: true conseillé
  - /dev:/dev
  - /sys:/sys:ro
  - monter les chemins hôte à afficher (/mnt, /srv...) dans le container
"""

from __future__ import annotations

import json
import os
import re
import shutil
import shlex
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, redirect, render_template, request

disk_bp = Blueprint("disk_bp", __name__)
bp = disk_bp
blueprint = disk_bp

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


SAFE_DISK_RE = re.compile(r"^/dev/(sd[a-z]+|hd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+|mmcblk\d+|md\d+|md/[A-Za-z0-9_.-]+)$")
SAFE_BLOCK_NAME_RE = re.compile(r"^(sd[a-z]+|hd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+|mmcblk\d+)$")
SAFE_PART_RE = re.compile(r"^/dev/(sd[a-z]+\d+|hd[a-z]+\d+|vd[a-z]+\d+|xvd[a-z]+\d+|nvme\d+n\d+p\d+|mmcblk\d+p\d+)$")
SAFE_BLOCK_PATH_RE = re.compile(r"^/dev/(sd[a-z]+\d*|hd[a-z]+\d*|vd[a-z]+\d*|xvd[a-z]+\d*|nvme\d+n\d+(p\d+)?|mmcblk\d+(p\d+)?|md\d+|md/[A-Za-z0-9_.-]+)$")
SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9_. -]{0,32}$")

CONFIG_FILE = os.environ.get("DISK_CONF", nas_conf_file("disk.conf"))

DEFAULT_CONFIG = {
    "REFRESH_SECONDS": "10",
    "SMARTCTL_BIN": "smartctl",
    "HDPARM_BIN": "hdparm",
    "SYS_BLOCK_PATH": "/sys/block",
    "MOUNTINFO_FILE": "/proc/self/mountinfo",
    "SHOW_LOOP": "0",
    "SHOW_ROM": "0",
    "SHOW_UNMOUNTED": "1",
    "TEMP_WARN": "45",
    "TEMP_CRIT": "55",

    # Anti-réveil :
    # 1 = lit SMART/température seulement quand hdparm dit actif.
    # 0 = jamais de lecture SMART/température, utile pour test extrême.
    "READ_SMART_ON_ACTIVE": "1",

    # Ne jamais lire SMART si hdparm dit standby, sauf demande explicite.
    # À laisser à 0 pour ton besoin.
    "READ_SMART_ON_STANDBY": "0",

    # Lire l'espace libre uniquement si le HDD est actif.
    # En standby on garde le disque tranquille.
    "READ_USAGE_ON_ACTIVE": "1",

    # Caches seulement pour éviter de lancer smartctl à chaque refresh actif.
    # Attention : en standby, la température affichée reste "••" même s'il y a un cache.
    "TEMP_CACHE_SECONDS": "300",
    "SMART_CACHE_SECONDS": "900",

    # Chemin du petit cache/log persistant.
    # Important : quand un HDD dort, on NE met PAS ce fichier à jour.
    # On relit seulement les dernières valeurs connues.
    "DISK_LOG_FILE": "/var/log/yoleo/disk/disk.log",

    # Affichage demandé : en veille, température = *.
    "TEMP_STANDBY_LABEL": "*",

    # Si hdparm ne sait pas répondre, on utilise smartctl -n standby
    # comme le moniteur : safe, car smartctl abandonne si le disque dort.
    "POWER_STATE_SMART_FALLBACK": "1",

    # Maintenance : par sécurité, les montages créés depuis l'UI sont limités
    # à ces racines. Tu peux ajouter /data ou autre dans conf/disk.conf.
    "MOUNT_ALLOWED_PREFIXES": "/mnt,/media,/srv,/data",
    "FSTAB_FILE": "/etc/fstab",
    "MOUNT_DEFAULT_OPTIONS": "defaults,nofail,noatime",

    # mergerfs : gestion graphique host. Les lignes sont écrites dans fstab
    # en fuse.mergerfs, systemd crée ensuite les unités .mount automatiquement.
    "MERGERFS_DEFAULT_OPTIONS": "defaults,use_ino,cache.files=partial,category.create=mfs,allow_other",

    # État UI persistant des profils MargeFS.
    # Donnée applicative, donc chemin Linux standard sous /var/lib/yoleo.
    "MERGERFS_STATE_FILE": "/var/lib/yoleo/disk/mergerfs_profiles.json",

    # Veille HDD : Flask pilote directement le vrai service Linux hd-idle.service.
    # hdd.sh peut rester disponible côté terminal, mais l'UI ne dépend plus de lui.
    "DISK_SLEEP_SERVICE": "hd-idle.service",
    "DISK_SLEEP_CONF": "/etc/default/hd-idle",
    "DISK_SLEEP_LEGACY_SERVICE": "flask-disk-spindown.service",
    "DISK_SLEEP_OLD_SERVICE": "hdd-veille.service",

    # RAID / ZFS / BTRFS : état et logs des créations lancées depuis l'UI.
    "RAID_JOB_DIR": "/var/lib/yoleo/disk/raid_jobs",
    "RAID_LOG_DIR": "/var/log/yoleo/disk/raid",

    # SnapRAID : état applicatif + fichier de configuration Linux réel.
    "SNAPRAID_BIN": "snapraid",
    "SNAPRAID_CONFIG_FILE": "/etc/snapraid.conf",
    "SNAPRAID_STATE_FILE": "/var/lib/yoleo/disk/snapraid.json",

    # RAMDrive / tmpfs : disques temporaires en mémoire vive.
    "RAMDRIVE_BASE_PATH": "/mnt/ramdrive",
    "RAMDRIVE_DEFAULT_OPTIONS": "mode=0777,nosuid,nodev,noatime",
    "RAMDRIVE_MIN_GB": "0.1",
    "RAMDRIVE_MAX_GB": "256",

    # Virtual Disk : montage ponctuel de fichiers disque VM via qemu-nbd.
    "VIRTUAL_DISK_BASE_PATH": "/mnt/virtual_disk",
    "VIRTUAL_DISK_STATE_FILE": "/var/lib/yoleo/disk/virtual_disks.json",
    "VIRTUAL_DISK_ALLOWED_EXTENSIONS": ".qcow2,.qcow,.qed,.raw,.img,.vdi,.vmdk,.vhd,.vhdx",
    "VIRTUAL_DISK_DEFAULT_OPTIONS": "nosuid,nodev,noatime",
}

DISK_DEFAULT_CONF_TEXT = """# disk.conf - Module Disk Flask System
# Fichier cree automatiquement si absent.
# Chemins relatifs : ce fichier est prevu dans <base>/conf/disk.conf.
# Les logs/cache persistants utilisent des chemins Linux standards sous /var/log/yoleo et /var/lib/yoleo.

REFRESH_SECONDS=10
SMARTCTL_BIN=smartctl
HDPARM_BIN=hdparm
SYS_BLOCK_PATH=/sys/block
MOUNTINFO_FILE=/proc/self/mountinfo

# Petit cache/log persistant des dernieres valeurs connues.
# Quand un HDD dort, le module RELIT ce fichier mais ne le met PAS a jour.
DISK_LOG_FILE=/var/log/yoleo/disk/disk.log

# Anti-reveil HDD
READ_SMART_ON_ACTIVE=1
READ_SMART_ON_STANDBY=0
READ_USAGE_ON_ACTIVE=1
POWER_STATE_SMART_FALLBACK=1

# Affichage demande : temperature en veille = *
TEMP_STANDBY_LABEL=*

TEMP_WARN=45
TEMP_CRIT=55
TEMP_CACHE_SECONDS=300
SMART_CACHE_SECONDS=900

SHOW_LOOP=0
SHOW_ROM=0
SHOW_UNMOUNTED=1

# MargeFS / mergerfs
MERGERFS_DEFAULT_OPTIONS=defaults,use_ino,cache.files=partial,category.create=mfs,allow_other
# Etat UI des profils MargeFS : donnees applicatives persistantes, pas dans le dossier app.
MERGERFS_STATE_FILE=/var/lib/yoleo/disk/mergerfs_profiles.json

# Veille HDD
# Flask pilote directement le vrai service Linux hd-idle.service et son PID systemd.
# L'UI ne depend plus de hdd.sh. hdd.sh peut rester l'outil CLI cote terminal.
DISK_SLEEP_SERVICE=hd-idle.service
DISK_SLEEP_CONF=/etc/default/hd-idle
DISK_SLEEP_LEGACY_SERVICE=flask-disk-spindown.service
DISK_SLEEP_OLD_SERVICE=hdd-veille.service

# RAID / ZFS / BTRFS
RAID_JOB_DIR=/var/lib/yoleo/disk/raid_jobs
RAID_LOG_DIR=/var/log/yoleo/disk/raid

# SnapRAID
SNAPRAID_BIN=snapraid
SNAPRAID_CONFIG_FILE=/etc/snapraid.conf
SNAPRAID_STATE_FILE=/var/lib/yoleo/disk/snapraid.json

# RAMDrive / tmpfs
RAMDRIVE_BASE_PATH=/mnt/ramdrive
RAMDRIVE_DEFAULT_OPTIONS=mode=0777,nosuid,nodev,noatime
RAMDRIVE_MIN_GB=0.1
RAMDRIVE_MAX_GB=256

# Virtual Disk / qemu-nbd
VIRTUAL_DISK_BASE_PATH=/mnt/virtual_disk
VIRTUAL_DISK_STATE_FILE=/var/lib/yoleo/disk/virtual_disks.json
VIRTUAL_DISK_ALLOWED_EXTENSIONS=.qcow2,.qcow,.qed,.raw,.img,.vdi,.vmdk,.vhd,.vhdx
VIRTUAL_DISK_DEFAULT_OPTIONS=nosuid,nodev,noatime
"""

DISK_CONF_PATH_KEYS = {
    "DISK_LOG_FILE",
    "MERGERFS_STATE_FILE",
    "RAID_JOB_DIR",
    "RAID_LOG_DIR",
    "SNAPRAID_CONFIG_FILE",
    "SNAPRAID_STATE_FILE",
    "VIRTUAL_DISK_STATE_FILE",
}

def ensure_disk_conf_file(path: str) -> bool:
    """Cree disk.conf si absent, sans jamais ecraser un fichier existant."""
    if os.path.exists(path):
        return False
    parent = os.path.dirname(path.rstrip("/")) or "."
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(DISK_DEFAULT_CONF_TEXT.rstrip() + "\n")
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass
    return True

def disk_conf_resolve_path(value: str, base_dir: Optional[str] = None) -> str:
    """Resout les chemins relatifs de disk.conf depuis le dossier conf officiel."""
    raw = strip_quotes(str(value or "")).strip()
    if not raw:
        return ""
    raw = os.path.expanduser(os.path.expandvars(raw))
    if os.path.isabs(raw):
        return os.path.abspath(raw)
    return os.path.abspath(os.path.join(base_dir or NAS_CONF_DIR, raw))

_TEMP_CACHE: Dict[str, Dict[str, Any]] = {}
_HEALTH_CACHE: Dict[str, Dict[str, Any]] = {}
_USAGE_CACHE: Dict[str, Dict[str, Any]] = {}

DISABLED_MERGERFS_PREFIX = "# flask-system-disabled-mergerfs "


def strip_quotes(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def read_config_file(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key:
                    out[key] = strip_quotes(value)
    except OSError:
        pass
    return out


def get_config() -> Dict[str, str]:
    created = ensure_disk_conf_file(CONFIG_FILE)
    conf = DEFAULT_CONFIG.copy()
    conf.update(read_config_file(CONFIG_FILE))
    conf["DISK_CONFIG_FILE"] = CONFIG_FILE
    conf["DISK_CONFIG_CREATED"] = "1" if created else "0"

    # Les chemins relatifs du disk.conf sont relatifs au dossier conf central.
    for key in DISK_CONF_PATH_KEYS:
        if conf.get(key):
            conf[key] = disk_conf_resolve_path(conf[key])

    return conf


def conf_bool(conf: Dict[str, str], key: str, default: str = "0") -> bool:
    return str(conf.get(key, default)).strip().lower() in {"1", "true", "yes", "on", "oui"}


def conf_int(conf: Dict[str, str], key: str, default: int) -> int:
    try:
        return int(str(conf.get(key, default)).strip())
    except Exception:
        return default


def run_cmd(cmd: List[str], timeout: int = 8) -> Tuple[int, str]:
    try:
        completed = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return completed.returncode, completed.stdout or ""
    except subprocess.TimeoutExpired:
        return 124, "Timeout"
    except FileNotFoundError:
        return 127, f"Commande absente: {cmd[0]}"
    except Exception as exc:
        return 1, str(exc)


def which_or_config(conf: Dict[str, str], key: str, fallback: str) -> str:
    value = str(conf.get(key, fallback)).strip() or fallback
    if os.path.isabs(value):
        return value
    return shutil.which(value) or value


def read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except Exception:
        return ""


def read_int(path: str, default: int = 0) -> int:
    try:
        return int(read_text(path))
    except Exception:
        return default


def human_bytes(value: Any) -> str:
    try:
        n = float(value)
    except Exception:
        return "—"
    units = ["o", "Ko", "Mo", "Go", "To", "Po"]
    idx = 0
    while n >= 1024 and idx < len(units) - 1:
        n /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(n)} {units[idx]}"
    return f"{n:.1f} {units[idx]}"


def disk_log_file(conf: Dict[str, str]) -> str:
    path = str(conf.get("DISK_LOG_FILE") or "/var/log/yoleo/disk/disk.log").strip()
    return path or "/var/log/yoleo/disk/disk.log"


def read_disk_log(conf: Dict[str, str]) -> Dict[str, Any]:
    """Lit le cache/log persistant des dernières valeurs connues.

    Le fichier peut s'appeler .log, mais son contenu est volontairement du JSON :
    c'est plus sûr et plus simple à relire par Python.
    """
    path = disk_log_file(conf)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"usage": {}, "updated_at": ""}


def write_disk_log(conf: Dict[str, str], data: Dict[str, Any]) -> None:
    """Écrit le cache/log persistant.

    Règle importante : cette fonction est appelée seulement quand le disque est
    actif et qu'on vient de lire des infos live. Jamais quand le disque dort.
    """
    path = disk_log_file(conf)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except Exception:
        pass


def usage_cache_keys(device: str, mountpoint: str) -> List[str]:
    keys: List[str] = []
    for key in (device, mountpoint):
        key = str(key or "").strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def load_persistent_usage(conf: Dict[str, str], device: str, mountpoint: str) -> Optional[Dict[str, Any]]:
    data = read_disk_log(conf)
    usage_map = data.get("usage") if isinstance(data, dict) else {}
    if not isinstance(usage_map, dict):
        return None
    for key in usage_cache_keys(device, mountpoint):
        item = usage_map.get(key)
        if isinstance(item, dict):
            out = dict(item)
            out["cached"] = True
            out["sleeping"] = True
            out["cache_source"] = disk_log_file(conf)
            return out
    return None


def save_persistent_usage(conf: Dict[str, str], device: str, mountpoint: str, usage: Dict[str, Any]) -> None:
    data = read_disk_log(conf)
    if not isinstance(data, dict):
        data = {}
    usage_map = data.get("usage")
    if not isinstance(usage_map, dict):
        usage_map = {}
        data["usage"] = usage_map

    clean = dict(usage)
    # Ces champs sont des marqueurs d'affichage, pas des valeurs réelles à garder.
    for transient in ("cached", "sleeping", "cache_source"):
        clean.pop(transient, None)
    clean["last_seen"] = time.strftime("%Y-%m-%d %H:%M:%S")
    clean["device"] = device
    clean["mountpoint"] = mountpoint

    for key in usage_cache_keys(device, mountpoint):
        usage_map[key] = dict(clean)
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    write_disk_log(conf, data)


def normalize_block_meta(meta: Dict[str, Any]) -> Dict[str, str]:
    """Normalise les métadonnées FS stockées en cache JSON/udev.

    Important anti-réveil : ces infos servent à l'onglet Général quand une
    partition est démontée ou quand le HDD dort. Elles évitent d'appeler
    blkid dans /disk/api.
    """
    out: Dict[str, str] = {}
    aliases = {
        "type": ("type", "fstype", "blkid_fstype", "id_fs_type", "ID_FS_TYPE"),
        "uuid": ("uuid", "id_fs_uuid", "ID_FS_UUID"),
        "label": ("label", "id_fs_label", "ID_FS_LABEL"),
        "partuuid": ("partuuid", "id_part_entry_uuid", "ID_PART_ENTRY_UUID"),
    }
    for final_key, keys in aliases.items():
        for key in keys:
            value = meta.get(key)
            if value is not None and str(value).strip():
                out[final_key] = str(value).strip()
                break
    return out


def block_meta_cache_keys(target: str) -> List[str]:
    keys: List[str] = []
    target = str(target or "").strip()
    if target and target not in keys:
        keys.append(target)

    name = block_name_from_path(target) if target.startswith("/dev/") else target
    name = os.path.basename(str(name or "").strip())
    if name:
        for key in (f"/dev/{name}", name):
            if key not in keys:
                keys.append(key)
        dev_id = sysfs_dev_id(name)
        if dev_id and f"dev:{dev_id}" not in keys:
            keys.append(f"dev:{dev_id}")
    return keys


def udev_block_meta(target: str) -> Dict[str, str]:
    """Lit le cache udev (/run/udev/data), sans toucher au disque.

    Ça donne souvent ID_FS_TYPE/UUID/LABEL même pour une partition démontée.
    Contrairement à blkid, cette lecture ne sonde pas le périphérique bloc.
    """
    name = block_name_from_path(target) if str(target or "").startswith("/dev/") else os.path.basename(str(target or ""))
    if not name:
        return {}
    dev_id = sysfs_dev_id(name)
    if not dev_id:
        return {}

    path = f"/run/udev/data/b{dev_id}"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except Exception:
        return {}

    raw: Dict[str, str] = {}
    for line in lines:
        if not line.startswith("E:") or "=" not in line:
            continue
        key, value = line[2:].split("=", 1)
        key = key.strip()
        value = value.strip()
        if key in {"ID_FS_TYPE", "ID_FS_UUID", "ID_FS_LABEL", "ID_PART_ENTRY_UUID"}:
            raw[key] = value

    meta = normalize_block_meta(raw)
    if meta:
        meta["cache_source"] = "udev"
    return meta


def load_persistent_block_meta(conf: Dict[str, str], target: str) -> Dict[str, str]:
    data = read_disk_log(conf)
    if not isinstance(data, dict):
        return {}
    meta_map = data.get("block_meta")
    if not isinstance(meta_map, dict):
        # Compatibilité si une ancienne version/test a utilisé une autre clé.
        meta_map = data.get("meta")
    if not isinstance(meta_map, dict):
        return {}

    for key in block_meta_cache_keys(target):
        item = meta_map.get(key)
        if isinstance(item, dict):
            out = normalize_block_meta(item)
            if out:
                out["cached"] = "1"
                out["cache_source"] = disk_log_file(conf)
                return out
    return {}


def save_persistent_block_meta(conf: Dict[str, str], target: str, meta: Dict[str, Any]) -> None:
    clean = normalize_block_meta(meta)
    if not clean:
        return

    data = read_disk_log(conf)
    if not isinstance(data, dict):
        data = {}
    meta_map = data.get("block_meta")
    if not isinstance(meta_map, dict):
        meta_map = {}
        data["block_meta"] = meta_map

    clean["last_seen"] = time.strftime("%Y-%m-%d %H:%M:%S")
    clean["target"] = target
    for key in block_meta_cache_keys(target):
        meta_map[key] = dict(clean)
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    write_disk_log(conf, data)


def cached_block_meta(conf: Dict[str, str], target: str, allow_live_read: bool = False) -> Dict[str, str]:
    """Retourne type/uuid/label sans réveiller les HDD dans l'onglet Général.

    Ordre en mode lecture froide (/disk/api) :
      1. cache udev en RAM (/run/udev/data) ;
      2. cache JSON persistant DISK_LOG_FILE ;
      3. rien.

    blkid n'est autorisé que si allow_live_read=True, utilisé par la
    maintenance ou par une action explicite de montage/formatage.
    """
    if not SAFE_BLOCK_PATH_RE.match(target or ""):
        return {}

    # Safe : lecture du cache udev, pas du périphérique bloc.
    # On complète avec le JSON si udev ne donne qu'une partie des infos.
    meta = udev_block_meta(target)
    persistent = load_persistent_block_meta(conf, target)
    merged: Dict[str, str] = {}
    if persistent:
        merged.update(persistent)
    if meta:
        merged.update(meta)

    if merged:
        # Si on est en maintenance/action explicite, on peut alimenter le JSON.
        # En onglet Général, on évite toute écriture qui pourrait toucher le support
        # contenant le fichier de cache.
        if allow_live_read and meta:
            save_persistent_block_meta(conf, target, merged)
        return merged

    if not allow_live_read:
        return {}

    live = parse_blkid_export(target)
    if live:
        save_persistent_block_meta(conf, target, live)
        out = normalize_block_meta(live)
        out["cache_source"] = "blkid"
        return out
    return {}


def mountinfo_unescape(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        try:
            return chr(int(match.group(1), 8))
        except Exception:
            return match.group(0)
    return re.sub(r"\\([0-7]{3})", repl, value or "")


def read_mountinfo(conf: Dict[str, str]) -> Dict[str, List[Dict[str, str]]]:
    """
    Lit /proc/self/mountinfo. C'est une lecture mémoire/kernel, pas un scan disque.
    Clé = major:minor du périphérique bloc.
    """
    path = str(conf.get("MOUNTINFO_FILE") or "/proc/self/mountinfo")
    out: Dict[str, List[Dict[str, str]]] = {}

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except Exception:
        return out

    for raw in lines:
        parts = raw.strip().split()
        if len(parts) < 10 or "-" not in parts:
            continue
        try:
            sep = parts.index("-")
            major_minor = parts[2]
            mountpoint = mountinfo_unescape(parts[4])
            fstype = parts[sep + 1] if len(parts) > sep + 1 else ""
            source = mountinfo_unescape(parts[sep + 2]) if len(parts) > sep + 2 else ""
            mount_options = parts[5] if len(parts) > 5 else ""
            super_options = parts[sep + 3] if len(parts) > sep + 3 else ""
        except Exception:
            continue

        if not major_minor or not mountpoint:
            continue

        out.setdefault(major_minor, []).append({
            "dev_id": major_minor,
            "mount": mountpoint,
            "fstype": fstype,
            "source": source,
            "opts": mount_options,
            "super_options": super_options,
        })

    return out


def symlink_map_by_devname(directory: str) -> Dict[str, str]:
    """
    Lit uniquement des symlinks (/dev/disk/by-uuid, by-label, by-id).
    Ne touche pas au contenu des disques.
    """
    result: Dict[str, str] = {}
    try:
        names = os.listdir(directory)
    except Exception:
        return result

    for name in names:
        full = os.path.join(directory, name)
        try:
            target = os.path.realpath(full)
            devname = os.path.basename(target)
            if devname:
                result.setdefault(devname, name)
        except Exception:
            continue

    return result


def by_id_map_by_devname() -> Dict[str, str]:
    raw = symlink_map_by_devname("/dev/disk/by-id")
    preferred: Dict[str, str] = {}

    # On préfère un identifiant parlant, mais sans se battre si le système n'en donne pas.
    prefixes = ("ata-", "nvme-", "usb-", "scsi-", "wwn-", "eui-")
    for devname, value in raw.items():
        current = preferred.get(devname)
        if not current:
            preferred[devname] = value
            continue
        cur_score = prefixes.index(tuple_prefix(current, prefixes)) if tuple_prefix(current, prefixes) in prefixes else 99
        val_score = prefixes.index(tuple_prefix(value, prefixes)) if tuple_prefix(value, prefixes) in prefixes else 99
        if val_score < cur_score:
            preferred[devname] = value

    return preferred


def tuple_prefix(value: str, prefixes: Tuple[str, ...]) -> str:
    for prefix in prefixes:
        if value.startswith(prefix):
            return prefix
    return ""


def partition_name_matches(disk_name: str, child_name: str) -> bool:
    if child_name == disk_name:
        return False
    if disk_name.startswith("nvme") or disk_name.startswith("mmcblk"):
        return re.fullmatch(re.escape(disk_name) + r"p\d+", child_name) is not None
    return re.fullmatch(re.escape(disk_name) + r"\d+", child_name) is not None


def list_partitions(sys_block_path: str, disk_name: str) -> List[str]:
    disk_sys = os.path.join(sys_block_path, disk_name)
    try:
        names = os.listdir(disk_sys)
    except Exception:
        return []

    parts: List[str] = []
    for name in names:
        if partition_name_matches(disk_name, name) and os.path.exists(os.path.join("/sys/class/block", name, "dev")):
            parts.append(name)

    def part_num(name: str) -> int:
        m = re.search(r"(\d+)$", name)
        return int(m.group(1)) if m else 0

    return sorted(parts, key=part_num)


def sysfs_block_size_bytes(block_name: str) -> int:
    # Linux expose le nombre de secteurs 512 octets dans /sys/class/block/*/size.
    sectors = read_int(os.path.join("/sys/class/block", block_name, "size"), 0)
    return sectors * 512


def sysfs_dev_id(block_name: str) -> str:
    return read_text(os.path.join("/sys/class/block", block_name, "dev"))


def sysfs_disk_base(sys_block_path: str, disk_name: str) -> str:
    return os.path.join(sys_block_path, disk_name)



def devname_by_dev_id() -> Dict[str, str]:
    """
    Mappe major:minor -> nom bloc (/sys/class/block). Lecture sysfs uniquement.
    Utile pour relier /proc/self/mountinfo aux disques sans lsblk/parted.
    """
    out: Dict[str, str] = {}
    try:
        names = os.listdir("/sys/class/block")
    except Exception:
        return out
    for name in names:
        dev_id = sysfs_dev_id(name)
        if dev_id:
            out[dev_id] = name
    return out


def btrfs_groups_by_device() -> Dict[str, List[str]]:
    """
    Pour un BTRFS multi-device/RAID1, /proc/self/mountinfo peut ne montrer
    le montage que sur un seul membre. /sys/fs/btrfs expose les devices du FS.
    On s'en sert pour afficher /mnt/raid1 sur les deux disques sans scan disque.
    """
    groups: Dict[str, List[str]] = {}
    root = "/sys/fs/btrfs"
    try:
        fsids = os.listdir(root)
    except Exception:
        return groups

    for fsid in fsids:
        devices_dir = os.path.join(root, fsid, "devices")
        try:
            entries = os.listdir(devices_dir)
        except Exception:
            continue

        devs: List[str] = []
        for entry in entries:
            full = os.path.join(devices_dir, entry)
            try:
                target = os.path.realpath(full)
                name = os.path.basename(target)
                if name and os.path.exists(os.path.join("/sys/class/block", name, "dev")):
                    devs.append(name)
            except Exception:
                continue

        devs = sorted(set(devs))
        if not devs:
            continue
        for dev in devs:
            groups[dev] = devs
    return groups


def btrfs_mounts_by_devname(mountinfo: Dict[str, List[Dict[str, str]]]) -> Dict[str, List[Dict[str, str]]]:
    """
    Étend les montages BTRFS à tous les membres du FS.
    Exemple : /mnt/raid1 monté depuis /dev/sdc sera aussi visible sur /dev/sde
    si /sys/fs/btrfs indique que sdc+sde appartiennent au même BTRFS.
    """
    groups = btrfs_groups_by_device()
    if not groups:
        return {}

    id_to_name = devname_by_dev_id()
    out: Dict[str, List[Dict[str, str]]] = {}

    for entries in mountinfo.values():
        for item in entries:
            if str(item.get("fstype") or "").lower() != "btrfs":
                continue

            source = str(item.get("source") or "")
            source_name = ""
            if source.startswith("/dev/"):
                source_name = os.path.basename(os.path.realpath(source))
            if not source_name:
                dev_id = str(item.get("dev_id") or "")
                source_name = id_to_name.get(dev_id, "")

            if not source_name:
                continue

            for dev in groups.get(source_name, [source_name]):
                out.setdefault(dev, []).append(item)

    return out


def dedupe_mount_items(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen: set[Tuple[str, str, str]] = set()
    for item in items:
        key = (item.get("mount", ""), item.get("fstype", ""), item.get("source", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def sysfs_model(sys_block_path: str, disk_name: str) -> Tuple[str, str, str]:
    base = sysfs_disk_base(sys_block_path, disk_name)
    vendor = read_text(os.path.join(base, "device", "vendor"))
    model = read_text(os.path.join(base, "device", "model"))
    serial = read_text(os.path.join(base, "device", "serial"))

    # NVMe expose souvent ces infos directement sous device/.
    if not model:
        model = read_text(os.path.join(base, "device", "model"))
    if not serial:
        serial = read_text(os.path.join(base, "device", "serial"))

    return vendor, model, serial


def sysfs_transport(disk_name: str) -> str:
    if disk_name.startswith("nvme"):
        return "nvme"
    if disk_name.startswith("mmcblk"):
        return "mmc"
    # Pour SATA/USB, l'info exacte dépend beaucoup du contrôleur.
    # On évite udevadm/lsblk pour ne pas rajouter de scans.
    return ""


def disk_mounts_for_names(
    mountinfo: Dict[str, List[Dict[str, str]]],
    names: List[str],
    btrfs_by_name: Optional[Dict[str, List[Dict[str, str]]]] = None,
) -> Dict[str, List[Dict[str, str]]]:
    out: Dict[str, List[Dict[str, str]]] = {}
    btrfs_by_name = btrfs_by_name or {}
    for name in names:
        dev_id = sysfs_dev_id(name)
        items: List[Dict[str, str]] = []
        if dev_id:
            items.extend(mountinfo.get(dev_id, []))
        items.extend(btrfs_by_name.get(name, []))
        out[name] = dedupe_mount_items(items)
    return out


def first_mount(mounts: List[Dict[str, str]]) -> str:
    for item in mounts:
        mount = item.get("mount") or ""
        if mount:
            return mount
    return ""


def first_fstype(mounts: List[Dict[str, str]]) -> str:
    for item in mounts:
        fstype = item.get("fstype") or ""
        if fstype:
            return fstype
    return ""


def best_mount_for_disk(disk_name: str, part_names: List[str], mounts_by_name: Dict[str, List[Dict[str, str]]]) -> str:
    mount = first_mount(mounts_by_name.get(disk_name, []))
    if mount:
        return mount

    preferred: List[str] = []
    for part in part_names:
        preferred.extend(m.get("mount") or "" for m in mounts_by_name.get(part, []))

    # Préfère les chemins simples /mnt/... quand il y a aussi les alias OMV /srv/dev-disk-by-uuid...
    for mount in preferred:
        if mount.startswith("/mnt/"):
            return mount
    for mount in preferred:
        if mount:
            return mount
    return ""


def all_mounts_for_disk(disk_name: str, part_names: List[str], mounts_by_name: Dict[str, List[Dict[str, str]]]) -> List[str]:
    out: List[str] = []
    for name in [disk_name] + part_names:
        for item in mounts_by_name.get(name, []):
            mount = item.get("mount") or ""
            if mount and mount not in out:
                out.append(mount)
    return out




def mount_kind(path: str, primary: str) -> str:
    """Libellé court pour la colonne Montage."""
    if not path:
        return ""
    if path == primary:
        return "principal"
    if path.startswith("/mnt/user/") or path.startswith("/mnt/user0/") or path in {"/mnt/user", "/mnt/user0"}:
        return "bind"
    if path.startswith("/srv/dev-disk-by-"):
        return "omv"
    if path == "/" or path.startswith("/boot"):
        return "système"
    if path.startswith("/mnt/"):
        return "bind"
    return "montage"


def display_mounts_for_disk(disk_name: str, part_names: List[str], mounts_by_name: Dict[str, List[Dict[str, str]]], primary: str) -> List[Dict[str, str]]:
    """
    Tous les points de montage du disque, un par ligne côté HTML.
    Ça inclut les bind mounts, car /proc/self/mountinfo expose le même major:minor.
    Lecture kernel uniquement : pas de scan du disque.
    """
    mounts = all_mounts_for_disk(disk_name, part_names, mounts_by_name)

    def order(path: str) -> Tuple[int, str]:
        if path == primary:
            return (0, path)
        if path.startswith("/mnt/user/") or path.startswith("/mnt/user0/") or path in {"/mnt/user", "/mnt/user0"}:
            return (1, path)
        if path.startswith("/mnt/"):
            return (2, path)
        if path.startswith("/srv/dev-disk-by-"):
            return (3, path)
        if path == "/" or path.startswith("/boot"):
            return (4, path)
        return (5, path)

    rows: List[Dict[str, str]] = []
    for mount in sorted(mounts, key=order):
        rows.append({
            "path": mount,
            "kind": mount_kind(mount, primary),
            "primary": "1" if mount == primary else "0",
        })
    return rows


def flatten_mountinfo(mountinfo: Dict[str, List[Dict[str, str]]]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    seen: set[Tuple[str, str, str]] = set()
    for entries in mountinfo.values():
        for item in entries:
            key = (item.get("mount", ""), item.get("fstype", ""), item.get("source", ""))
            if key in seen:
                continue
            seen.add(key)
            rows.append(item)
    return rows


def mergerfs_detected_mounts(mountinfo: Dict[str, List[Dict[str, str]]]) -> List[Dict[str, Any]]:
    detected: List[Dict[str, Any]] = []
    for item in flatten_mountinfo(mountinfo):
        fstype = str(item.get("fstype") or "")
        if "mergerfs" not in fstype.lower():
            continue
        mount = item.get("mount") or ""
        source = item.get("source") or ""
        opts = item.get("super_options") or item.get("opts") or "defaults"
        command = ""
        # Quand le source contient déjà les branches, c'est la commande la plus lisible.
        if source and source != "mergerfs" and ":" in source:
            command = f"mergerfs -o {opts} {source} {mount}"
        elif mount:
            command = f"mount -t fuse.mergerfs -o {opts} mergerfs {mount}"
        detected.append({
            "mount": mount,
            "source": source,
            "fstype": fstype,
            "options": opts,
            "command": command,
        })
    return sorted(detected, key=lambda x: str(x.get("mount") or ""))


def mergerfs_branch_order(path: str) -> Tuple[int, int, str]:
    base = os.path.basename(path.rstrip("/"))
    if base == "cache":
        return (0, 0, path)
    m = re.fullmatch(r"disk(\d+)", base)
    if m:
        return (1, int(m.group(1)), path)
    m = re.fullmatch(r"raid(\d+)", base)
    if m:
        return (2, int(m.group(1)), path)
    return (3, 0, path)


def build_mergerfs_summary(disks: List[Dict[str, Any]], mountinfo: Dict[str, List[Dict[str, str]]]) -> Dict[str, Any]:
    """
    Résumé affiché sous le tableau :
      - montages mergerfs réellement détectés si présents
      - commandes user/user0 reconstruites depuis les montages /mnt/cache, /mnt/disk*, /mnt/raid*
    Ne lit que les données déjà collectées, donc pas de réveil HDD.
    """
    detected = mergerfs_detected_mounts(mountinfo)

    cache_mounts: List[str] = []
    data_mounts: List[str] = []
    for row in disks:
        mount = str(row.get("mountpoint") or "")
        if not mount.startswith("/mnt/"):
            continue
        base = os.path.basename(mount.rstrip("/"))
        if base == "cache":
            cache_mounts.append(mount)
        elif re.fullmatch(r"disk\d+", base) or re.fullmatch(r"raid\d+", base):
            data_mounts.append(mount)

    cache_mounts = sorted(set(cache_mounts), key=mergerfs_branch_order)
    data_mounts = sorted(set(data_mounts), key=mergerfs_branch_order)

    opts = "defaults,use_ino,cache.files=partial,category.create=mfs"
    suggested: List[Dict[str, Any]] = []

    user_sources = cache_mounts + data_mounts
    if user_sources:
        source_text = ":".join(user_sources)
        suggested.append({
            "name": "user",
            "target": "/mnt/user",
            "sources": user_sources,
            "source_text": source_text,
            "options": opts,
            "command": f"mergerfs -o {opts} {source_text} /mnt/user",
        })

    if data_mounts:
        source_text = ":".join(data_mounts)
        suggested.append({
            "name": "user0",
            "target": "/mnt/user0",
            "sources": data_mounts,
            "source_text": source_text,
            "options": opts,
            "command": f"mergerfs -o {opts} {source_text} /mnt/user0",
        })

    return {
        "detected": detected,
        "suggested": suggested,
    }


def is_system_or_boot_disk(disk_name: str, part_names: List[str], mounts_by_name: Dict[str, List[Dict[str, str]]]) -> bool:
    for mount in all_mounts_for_disk(disk_name, part_names, mounts_by_name):
        if mount == "/" or mount.startswith("/boot"):
            return True
    return False


def disk_usage(mountpoint: str) -> Optional[Dict[str, Any]]:
    try:
        usage = shutil.disk_usage(mountpoint)
        used_pct = round((usage.used / usage.total) * 100, 1) if usage.total else 0
        return {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "total_h": human_bytes(usage.total),
            "used_h": human_bytes(usage.used),
            "free_h": human_bytes(usage.free),
            "used_pct": used_pct,
            "cached": False,
        }
    except Exception:
        return None


def cached_disk_usage(conf: Dict[str, str], mountpoint: str, device: str, allow_live_read: bool) -> Optional[Dict[str, Any]]:
    if not mountpoint:
        return None

    cached = _USAGE_CACHE.get(device)

    if not allow_live_read:
        # Anti-réveil strict : pas de statvfs sur un HDD en veille.
        # Règle demandée : si spindown/veille, on NE met PAS le cache à jour.
        # On affiche seulement les dernières valeurs connues.
        if cached:
            out = dict(cached.get("usage") or {})
            out["cached"] = True
            out["sleeping"] = True
            return out

        persistent = load_persistent_usage(conf, device, mountpoint)
        if persistent:
            _USAGE_CACHE[device] = {"time": time.time(), "usage": dict(persistent)}
            return persistent

        return {
            "total": None,
            "used": None,
            "free": None,
            "total_h": "—",
            "used_h": "—",
            "free_h": "—",
            "used_pct": None,
            "cached": False,
            "sleeping": True,
        }

    usage = disk_usage(mountpoint)
    if usage:
        _USAGE_CACHE[device] = {"time": time.time(), "usage": usage}
        save_persistent_usage(conf, device, mountpoint, usage)
    return usage


def smartctl_standby_probe(conf: Dict[str, str], device: str) -> Tuple[str, str]:
    """
    Fallback inspiré du moniteur : smartctl -n standby permet de savoir
    qu'un disque dort sans le réveiller. On l'utilise seulement quand hdparm
    est absent ou ambigu, pour éviter de déclarer "unknown" alors que le HDD
    est simplement en veille.
    """
    if not SAFE_DISK_RE.match(device or ""):
        return "unknown", "périphérique refusé"

    smartctl = which_or_config(conf, "SMARTCTL_BIN", "smartctl")
    rc, out = run_cmd([smartctl, "-n", "standby", "-A", device], timeout=6)
    low = (out or "").lower()

    standby_patterns = (
        "device is in standby",
        "standby mode",
        "please try again",
        "in standby",
    )
    if "standby" in low and any(pattern in low for pattern in standby_patterns):
        return "standby", "veille"

    if rc == 127:
        return "unknown", "smartctl absent"

    # Si smartctl a réussi à lire les attributs, le disque est actif.
    if rc in (0, 2, 4, 64) and out.strip():
        return "active", "actif"

    return "unknown", "état inconnu"


def read_power_state(conf: Dict[str, str], device: str, is_hdd: bool) -> Tuple[str, str]:
    if not SAFE_DISK_RE.match(device or ""):
        return "unknown", "périphérique refusé"

    if not is_hdd:
        return "ssd", "SSD/NVMe"

    hdparm = which_or_config(conf, "HDPARM_BIN", "hdparm")
    rc, out = run_cmd([hdparm, "-C", device], timeout=5)
    low = (out or "").lower()

    if "standby" in low or "sleeping" in low:
        return "standby", "veille"
    if "active/idle" in low or "active" in low or "idle" in low:
        return "active", "actif"

    if conf_bool(conf, "POWER_STATE_SMART_FALLBACK", "1"):
        smart_state, smart_label = smartctl_standby_probe(conf, device)
        if smart_state in {"standby", "active"}:
            return smart_state, smart_label

    if rc == 127:
        return "unknown", "hdparm absent"
    return "unknown", "état inconnu"


def read_temperature_raw(conf: Dict[str, str], device: str) -> Tuple[Optional[int], str]:
    smartctl = which_or_config(conf, "SMARTCTL_BIN", "smartctl")
    if not SAFE_DISK_RE.match(device or ""):
        return None, "périphérique refusé"

    # -n standby est le garde-fou : si le disque s'est endormi entre hdparm et smartctl,
    # smartctl doit abandonner au lieu de le réveiller.
    rc, out = run_cmd([smartctl, "-n", "standby", "-A", "-j", device], timeout=10)
    low = (out or "").lower()
    if "standby" in low and ("please try again" in low or "device is in standby" in low):
        return None, "veille"

    if rc not in (0, 2, 4, 64):
        if rc == 127:
            return None, "smartctl absent"
        return None, "smartctl indisponible"

    try:
        import json
        data = json.loads(out)
        temp = data.get("temperature", {}).get("current")
        if isinstance(temp, int):
            return temp, "smart"
        # Certains disques mettent la température dans les attributs ATA.
        for item in data.get("ata_smart_attributes", {}).get("table", []) or []:
            name = str(item.get("name") or "")
            if name in {"Temperature_Celsius", "Airflow_Temperature_Cel"}:
                raw = item.get("raw", {})
                value = raw.get("value")
                if isinstance(value, int):
                    return value, "smart"
    except Exception:
        pass

    # Fallback texte, toujours avec -n standby.
    rc2, text = run_cmd([smartctl, "-n", "standby", "-A", device], timeout=10)
    low2 = (text or "").lower()
    if "standby" in low2 and ("please try again" in low2 or "device is in standby" in low2):
        return None, "veille"
    if rc2 not in (0, 2, 4, 64):
        return None, "smartctl indisponible"

    patterns = [
        r"Temperature_Celsius\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+(\d+)",
        r"Airflow_Temperature_Cel\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+(\d+)",
        r"Current Drive Temperature:\s+(\d+)",
        r"Temperature:\s+(\d+)\s+Celsius",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                return int(match.group(1)), "smart"
            except Exception:
                pass

    return None, "température inconnue"


def cached_temperature(conf: Dict[str, str], device: str, is_hdd: bool, power_state: str) -> Tuple[Optional[int], str]:
    # Règle demandée : si standby, on affiche des points, pas une ancienne température.
    if is_hdd and power_state == "standby" and not conf_bool(conf, "READ_SMART_ON_STANDBY", "0"):
        return None, "veille"

    if not conf_bool(conf, "READ_SMART_ON_ACTIVE", "1"):
        return None, "SMART désactivé"

    now = time.time()
    ttl = max(30, conf_int(conf, "TEMP_CACHE_SECONDS", 300))
    cached = _TEMP_CACHE.get(device)

    if cached and (now - float(cached.get("time", 0))) < ttl:
        return cached.get("temp"), str(cached.get("source") or "cache")

    temp, source = read_temperature_raw(conf, device)
    _TEMP_CACHE[device] = {"time": now, "temp": temp, "source": source}
    return temp, source


def smart_health_raw(conf: Dict[str, str], device: str) -> Tuple[str, str]:
    smartctl = which_or_config(conf, "SMARTCTL_BIN", "smartctl")
    if not SAFE_DISK_RE.match(device or ""):
        return "unknown", "périphérique refusé"

    rc, out = run_cmd([smartctl, "-n", "standby", "-H", "-j", device], timeout=8)
    low = (out or "").lower()
    if "standby" in low and ("please try again" in low or "device is in standby" in low):
        return "unknown", "SMART non lu en veille"

    if rc == 127:
        return "unknown", "smartctl absent"

    try:
        import json
        data = json.loads(out)
        passed = data.get("smart_status", {}).get("passed")
        if passed is True:
            return "ok", "SMART OK"
        if passed is False:
            return "bad", "SMART ALERTE"
    except Exception:
        pass

    if "passed" in low or "ok" in low:
        return "ok", "SMART OK"
    if "failed" in low or "failing" in low:
        return "bad", "SMART ALERTE"
    return "unknown", "SMART inconnu"


def cached_smart_health(conf: Dict[str, str], device: str, is_hdd: bool, power_state: str) -> Tuple[str, str]:
    now = time.time()
    ttl = max(60, conf_int(conf, "SMART_CACHE_SECONDS", 900))
    cached = _HEALTH_CACHE.get(device)

    if is_hdd and power_state == "standby" and not conf_bool(conf, "READ_SMART_ON_STANDBY", "0"):
        if cached:
            return str(cached.get("state") or "unknown"), "SMART cache / veille"
        return "unknown", "SMART non lu en veille"

    if cached and (now - float(cached.get("time", 0))) < ttl:
        return str(cached.get("state") or "unknown"), str(cached.get("label") or "SMART cache")

    if not conf_bool(conf, "READ_SMART_ON_ACTIVE", "1"):
        return "unknown", "SMART désactivé"

    state, label = smart_health_raw(conf, device)
    _HEALTH_CACHE[device] = {"time": now, "state": state, "label": label}
    return state, label


def logical_name(index: int, device: str, mountpoint: str) -> str:
    if mountpoint:
        base = os.path.basename(mountpoint.rstrip("/"))
        if base:
            return base
    return os.path.basename(device) or f"disk{index}"


def partition_summary(part_names: List[str], mounts_by_name: Dict[str, List[Dict[str, str]]],
                      uuid_by_dev: Dict[str, str], label_by_dev: Dict[str, str],
                      conf: Optional[Dict[str, str]] = None,
                      allow_live_meta: bool = False) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for part in part_names:
        mounts = mounts_by_name.get(part, [])
        target = f"/dev/{part}"

        # Onglet Général anti-réveil : jamais de blkid ici.
        # Pour une partition démontée, on se limite à mountinfo, cache udev et cache JSON.
        meta: Dict[str, str] = cached_block_meta(conf, target, allow_live_read=allow_live_meta) if conf else {}
        fs_type = first_fstype(mounts) or meta.get("type", "")

        label = label_by_dev.get(part, "") or meta.get("label", "")
        uuid = uuid_by_dev.get(part, "") or meta.get("uuid", "")
        mount = first_mount(mounts)

        rows.append({
            "name": part,
            "path": target,
            "size": human_bytes(sysfs_block_size_bytes(part)),
            "fstype": fs_type,
            "blkid_fstype": fs_type,
            "label": label,
            "uuid": uuid,
            "mount": mount,
            "mounted": bool(mount),
            "type": "partition",
            "meta_cached": bool(meta.get("cached") or meta.get("cache_source")),
            "meta_source": meta.get("cache_source", ""),
        })
    return rows


def disk_role_and_sort(row: Dict[str, Any]) -> Tuple[str, Tuple[int, int, str]]:
    name = str(row.get("name") or "").lower()
    mount = str(row.get("mountpoint") or "")
    device = str(row.get("device") or "")

    if name == "cache" or mount == "/mnt/cache":
        return "Cache", (0, 0, device)

    match = re.fullmatch(r"disk(\d+)", name)
    if match:
        return "Array", (1, int(match.group(1)), device)

    base = os.path.basename(mount.rstrip("/")).lower() if mount else ""
    match = re.fullmatch(r"disk(\d+)", base)
    if match:
        return "Array", (1, int(match.group(1)), device)

    # Variante labo possible : /mnt/20, /mnt/30, /mnt/40.
    # On les garde avec les disques de données, entre cache et montages spéciaux.
    match = re.fullmatch(r"(\d+)", base)
    if match:
        return "Array", (1, int(match.group(1)), device)

    if row.get("is_system_disk"):
        return "Système / boot", (3, 0, device)

    if mount:
        return "Montages spéciaux", (2, 0, mount.lower() + device)

    return "Non monté", (4, 0, device)


def build_disk_row(conf: Dict[str, str], index: int, disk_name: str,
                   mountinfo: Dict[str, List[Dict[str, str]]],
                   btrfs_by_name: Dict[str, List[Dict[str, str]]],
                   uuid_by_dev: Dict[str, str], label_by_dev: Dict[str, str],
                   by_id_by_dev: Dict[str, str]) -> Dict[str, Any]:
    sys_block_path = str(conf.get("SYS_BLOCK_PATH") or "/sys/block")
    device = f"/dev/{disk_name}"
    part_names = list_partitions(sys_block_path, disk_name)
    names = [disk_name] + part_names
    mounts_by_name = disk_mounts_for_names(mountinfo, names, btrfs_by_name)
    mountpoint = best_mount_for_disk(disk_name, part_names, mounts_by_name)
    mounts_display = display_mounts_for_disk(disk_name, part_names, mounts_by_name, mountpoint)

    rota = read_text(os.path.join(sysfs_disk_base(sys_block_path, disk_name), "queue", "rotational"))
    is_hdd = rota == "1"

    power_state, power_label = read_power_state(conf, device, is_hdd)
    hdd_is_sleeping = is_hdd and power_state == "standby"

    usage_allowed = (
        bool(mountpoint)
        and conf_bool(conf, "READ_USAGE_ON_ACTIVE", "1")
        and not hdd_is_sleeping
    )
    usage = cached_disk_usage(conf, mountpoint, device, allow_live_read=usage_allowed) if mountpoint else None

    temp, temp_source = cached_temperature(conf, device, is_hdd, power_state)
    health_state, health_label = cached_smart_health(conf, device, is_hdd, power_state)

    temp_warn = conf_int(conf, "TEMP_WARN", 45)
    temp_crit = conf_int(conf, "TEMP_CRIT", 55)

    standby_label = str(conf.get("TEMP_STANDBY_LABEL") or "Zzz")

    if hdd_is_sleeping:
        temp_class = "standby"
        temp_label = standby_label
        temp_display = None
    elif isinstance(temp, int):
        temp_display = temp
        temp_label = f"{temp}°C"
        if temp >= temp_crit:
            temp_class = "crit"
        elif temp >= temp_warn:
            temp_class = "warn"
        else:
            temp_class = "ok"
    else:
        temp_display = None
        temp_label = "—"
        temp_class = "unknown"

    if health_state == "bad":
        status_class = "bad"
        status_label = "Alerte SMART"
    elif hdd_is_sleeping:
        status_class = "standby"
        status_label = "Veille"
    elif is_hdd and power_state == "active":
        status_class = "active"
        status_label = "Actif"
    elif not is_hdd:
        status_class = "ssd"
        status_label = "SSD/NVMe"
    else:
        status_class = "unknown"
        status_label = "Présent"

    vendor, model, serial = sysfs_model(sys_block_path, disk_name)
    if not serial:
        serial = by_id_by_dev.get(disk_name, "")

    model_label = " ".join(x.strip() for x in [vendor, model] if x and x.strip()).strip() or "—"
    media = "HDD" if is_hdd else "SSD/NVMe"
    system_disk = is_system_or_boot_disk(disk_name, part_names, mounts_by_name)

    logical_label = logical_name(index, device, mountpoint)

    # /disk/api doit rester froid : pas de blkid dans l'onglet Général, même
    # pour les partitions démontées. Le type vient de mountinfo, du cache udev
    # ou du cache JSON alimenté par Maintenance/actions explicites.
    parts = partition_summary(part_names, mounts_by_name, uuid_by_dev, label_by_dev, conf, allow_live_meta=False)
    disk_meta = cached_block_meta(conf, device, allow_live_read=False)

    fs_type = first_fstype(mounts_by_name.get(disk_name, [])) or first_fstype(sum((mounts_by_name.get(p, []) for p in part_names), []))
    if not fs_type:
        for part in parts:
            fs_type = str(part.get("fstype") or part.get("blkid_fstype") or "")
            if fs_type:
                break
    if not fs_type:
        fs_type = disk_meta.get("type", "")

    disk_label = label_by_dev.get(disk_name, "") or disk_meta.get("label", "")
    disk_uuid = uuid_by_dev.get(disk_name, "") or disk_meta.get("uuid", "")

    row = {
        # name reste le nom logique interne pour le tri (cache, disk1, etc.).
        # L'HTML affiche display_device pour éviter de répéter le point de montage.
        "name": logical_label,
        "logical_name": logical_label,
        "display_device": device,
        "device": device,
        "sleep_label": standby_label,
        "model": model_label,
        "serial": serial,
        "transport": sysfs_transport(disk_name),
        "media": media,
        "is_hdd": is_hdd,
        "size": human_bytes(sysfs_block_size_bytes(disk_name)),
        "mountpoint": mountpoint,
        "fstype": fs_type,
        "blkid_fstype": fs_type,
        "label": disk_label,
        "uuid": disk_uuid,
        "meta_cached": bool(disk_meta.get("cached") or disk_meta.get("cache_source")),
        "meta_source": disk_meta.get("cache_source", ""),
        "free": usage["free_h"] if usage else "—",
        "used": usage["used_h"] if usage else "—",
        "used_pct": usage["used_pct"] if usage else None,
        "usage_cached": bool(usage.get("cached")) if usage else False,
        "usage_sleeping": bool(usage.get("sleeping")) if usage else False,
        "power_state": power_state,
        "power_label": power_label,
        "temp": temp_display,
        "temp_label": temp_label,
        "temp_class": temp_class,
        "temp_source": temp_source,
        "status_class": status_class,
        "status_label": status_label,
        "health_state": health_state,
        "health_label": health_label,
        "is_system_disk": system_disk,
        "role": "",
        "sort_key": "",
        "parts": parts,
        "mounts_all": all_mounts_for_disk(disk_name, part_names, mounts_by_name),
        "mounts": mounts_display,
    }

    role, sort_tuple = disk_role_and_sort(row)
    row["role"] = role
    row["_sort_tuple"] = sort_tuple
    return row


def list_disk_names(conf: Dict[str, str]) -> List[str]:
    sys_block_path = str(conf.get("SYS_BLOCK_PATH") or "/sys/block")
    try:
        names = os.listdir(sys_block_path)
    except Exception:
        return []

    show_loop = conf_bool(conf, "SHOW_LOOP", "0")
    show_rom = conf_bool(conf, "SHOW_ROM", "0")

    out: List[str] = []
    for name in names:
        if name.startswith("loop") and not show_loop:
            continue
        if name.startswith("sr") and not show_rom:
            continue
        if name.startswith(("ram", "zram", "dm-")):
            continue
        if not SAFE_BLOCK_NAME_RE.match(name):
            continue
        if not os.path.exists(os.path.join("/sys/class/block", name, "dev")):
            continue
        out.append(name)

    def sort_key(name: str) -> Tuple[int, str]:
        if name.startswith("nvme"):
            return (0, name)
        if name.startswith("sd"):
            return (1, name)
        return (2, name)

    return sorted(out, key=sort_key)


def collect_disks() -> Dict[str, Any]:
    conf = get_config()
    mountinfo = read_mountinfo(conf)
    btrfs_by_name = btrfs_mounts_by_devname(mountinfo)
    uuid_by_dev = symlink_map_by_devname("/dev/disk/by-uuid")
    label_by_dev = symlink_map_by_devname("/dev/disk/by-label")
    by_id_by_dev = by_id_map_by_devname()

    disks: List[Dict[str, Any]] = []
    for name in list_disk_names(conf):
        row = build_disk_row(conf, len(disks) + 1, name, mountinfo, btrfs_by_name, uuid_by_dev, label_by_dev, by_id_by_dev)
        if not conf_bool(conf, "SHOW_UNMOUNTED", "1") and not row.get("mountpoint"):
            continue
        disks.append(row)

    disks = sorted(disks, key=lambda item: item.get("_sort_tuple", (99, 0, str(item.get("device", "")))))
    for row in disks:
        row.pop("_sort_tuple", None)

    total_bytes = 0
    mounted_count = 0
    active_count = 0
    standby_count = 0
    alert_count = 0

    for row in disks:
        if row.get("mountpoint"):
            mounted_count += 1
        if row.get("status_class") in {"active", "ssd"}:
            active_count += 1
        if row.get("status_class") == "standby":
            standby_count += 1
        if row.get("status_class") == "bad":
            alert_count += 1
        try:
            # On garde la capacité brute depuis sysfs : pas d'I/O disque.
            total_text = str(row.get("size") or "")
        except Exception:
            total_text = ""

    for name in list_disk_names(conf):
        try:
            total_bytes += int(sysfs_block_size_bytes(name))
        except Exception:
            pass

    mergerfs = build_mergerfs_summary(disks, mountinfo)

    return {
        "ok": True,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "refresh_seconds": conf_int(conf, "REFRESH_SECONDS", 10),
        "mode": "sysfs+mountinfo+hdparm",
        "disk_log_file": disk_log_file(conf),
        "summary": {
            "count": len(disks),
            "mounted": mounted_count,
            "active": active_count,
            "standby": standby_count,
            "alerts": alert_count,
            "total_size": human_bytes(total_bytes),
        },
        "disks": disks,
        "mergerfs": mergerfs,
    }



# ---------------------------------------------------------------------------
# Actions Maintenance host
# ---------------------------------------------------------------------------

