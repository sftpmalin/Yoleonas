#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Module SYSTEM - moniteur hôte + gestionnaire de services systemd.

Fusion de monitor.py et systemctl.py :
  - /system : page unique avec onglets
  - /system/api/sys_stats : métriques hôte
  - /system/api/services : services systemd

Le fichier de configuration associé est system.conf.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict

import configparser
import psutil
import platform
import time
import os
import subprocess
import re
import shutil
import shlex
import signal
import uuid
try:
    import docker
except Exception:
    docker = None
import urllib.request
from urllib.parse import unquote
import json
import glob
try:
    import paramiko
except Exception:
    paramiko = None
from flask import Blueprint, render_template, jsonify, request, flash, redirect, url_for

system_bp = Blueprint('system_bp', __name__)
bp = system_bp
blueprint = system_bp

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
    "terminal=terminal",
]


def ensure_app_conf_backup_module(path: str = "") -> list[str]:
    """Complète ../conf/app.conf sans écraser l'existant.

    app.py crée le fichier au tout premier démarrage, mais system.py est aussi
    importé en bootstrap. Cette sécurité garantit que si app.conf est supprimé
    ou ancien, les modules essentiels restent redéclarés sous la forme
    nom=route avant le chargement dynamique.
    """
    target = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path or nas_conf_file("app.conf")).strip())))
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
        if module_name in loaded:
            continue
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(declaration)
        added.append(module_name)

    if added:
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines).rstrip() + "\n")
    return added


try:
    ensure_app_conf_backup_module(nas_conf_file("app.conf"))
except Exception:
    pass


# --- CHARGEMENT DE LA CONFIGURATION (system.conf) ---
CONF = {
    # NVIDIA reste volontairement en SSH : la carte est dans une VM.
    "SSH_GPU_HOST": "",
    "SSH_GPU_PORT": "22",
    "SSH_GPU_USER": "",
    "SSH_GPU_KEY_PATH": "",
    "REMOTE_NVIDIA_SMI": "/usr/bin/nvidia-smi",
    "SSH_GPU_CONNECT_TIMEOUT": "5",
    "SSH_GPU_COMMAND_TIMEOUT": "8",
    "SSH_GPU_CACHE_SECONDS": "2",

    # Accès matériel/réseau direct : plus de Home Assistant pour lire le réseau.
    "INTEL_GPU_CACHE_SECONDS": "1",
    "NET_INTERFACES": "",  # vide = auto via route par défaut, ex: br0
    "NET_EXCLUDE_PREFIXES": "lo,veth,docker,virbr,br-,vnet,tun,tap",
    "PUBLIC_IP_CACHE_SECONDS": "300",

    # Gestionnaire de services systemd.
    "SYSTEMCTL_REFRESH_SECONDS": "5",
    "SYSTEMCTL_MAX_OUTPUT_CHARS": "5000",
    "PROCESS_REFRESH_SECONDS": "3",
    "SYSTEM_LOG_LINES": "180",
    "SYSTEM_LOG_DEFAULT_UNIT": "",

    # mDNS intégré au module Système.
    # Le vrai fichier de noms reste séparé : ../conf/mdns.conf
    # Format du vrai fichier de noms : IP=nom.local
    "EXEC_MODE": "local-python",
    "TITLE": "mDNS local",
    "SUBTITLE": "Gestion des noms .local sans terminal",
    "SERVICE_SCRIPT": "",
    "SERVICE_CONF": nas_conf_file("mdns.conf"),
    "SERVICE_LOG": "/var/log/mdns/mdns.log",
    "SERVICE_RUN_DIR": "/var/run/mdns",
    "RUNTIME_HOSTS": "/var/run/mdns/mdns.hosts",
    "PID_FILE": "/var/run/mdns/mdns-publish.pids",
    "MDNS_LITE_PID": "/var/run/mdns/mdns-lite.pid",
    "AVAHI_HOSTS": "/etc/avahi/hosts",
    "AVAHI_DAEMON_CONF": "/etc/avahi/avahi-daemon.conf",
    "MDNS_INTERFACE": "auto",
    "MDNS_USE_IPV6": "no",
    "SYSTEMD_SERVICE": "mdns-labo.service",
    "PING_BIN": "ping",
    "SS_BIN": "ss",
    "PGREP_BIN": "pgrep",
    "PKILL_BIN": "pkill",
    "PS_BIN": "ps",
    "AVAHI_DAEMON_BIN": "avahi-daemon",
    "AVAHI_PUBLISH_BIN": "avahi-publish-address",
    "ACTION_TIMEOUT": "90",
    "LOG_TAIL_LINES": "180",
    "DEFAULT_IP": "192.168.1.126",

    # Gestionnaire de fichiers conf intégré.
    # Format conservé de l'ancien settings_ini.conf : DIR1, DIR2, DIR3... / FILE1, FILE2...
    # Les chemins relatifs sont résolus depuis le dossier de system.conf.
    "DIR1": NAS_CONF_DIR,
    "DIR2": "",
    "FILE1": "",
    # Compatibilité : on accepte aussi une liste séparée par virgules si besoin.
    "CONF_MANAGER_WATCH_DIRS": "",
    "CONF_MANAGER_INDIVIDUAL_FILES": "",
    "CONF_MANAGER_ALLOWED_EXTENSIONS": ".ini,.conf,.cfg,.txt",

    # Dépannage : dernier dossier conf choisi pour lister / rétablir.
    "path_rescue": "",
}

def _unique_existing_order(paths):
    seen = set()
    out = []
    for path in paths:
        if not path:
            continue
        abs_path = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path))))
        if abs_path in seen:
            continue
        seen.add(abs_path)
        out.append(abs_path)
    return out

def _project_root_candidates():
    """Racines possibles du projet Flask.

    Cas normal : /dockers/system/system.py avec conf officielle dans
    /dockers/conf/system.conf. Le parent de /system doit passer AVANT /system,
    sinon Flask peut charger/créer /dockers/system/conf/system.conf et créer
    /dockers/system/tabs.
    """
    roots = []

    def add(path):
        if not path:
            return
        path = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path))))
        if path not in roots:
            roots.append(path)

    for env_name in ("FLASK_SYSTEM_ROOT", "SYSTEM_ROOT", "PROJECT_ROOT"):
        add(os.environ.get(env_name, "").strip())

    for base in (os.path.dirname(os.path.abspath(__file__)), os.getcwd()):
        base = os.path.abspath(base)
        parent = os.path.dirname(base)
        grandparent = os.path.dirname(parent)
        if os.path.basename(base).lower() in {"system", "modules", "app"}:
            add(parent)
            add(base)
        else:
            add(base)
            add(parent)
        add(grandparent)

    return _unique_existing_order(roots)


def _is_bad_system_subconf(path):
    """Détecte /.../system/conf/system.conf si /.../conf/system.conf existe."""
    try:
        path = os.path.abspath(path)
        conf_dir = os.path.dirname(path)
        system_dir = os.path.dirname(conf_dir)
        project_dir = os.path.dirname(system_dir)
        if os.path.basename(system_dir).lower() != "system":
            return False
        official = os.path.join(project_dir, "conf", "system.conf")
        return os.path.exists(official) and os.path.abspath(official) != path
    except Exception:
        return False


def _build_config_candidates():
    """
    Ne dépend pas du dossier de lancement de gunicorn/systemd.

    Priorité corrigée :
      /dockers/conf/system.conf AVANT /dockers/system/conf/system.conf
    quand le module tourne depuis /dockers/system.
    """
    candidates = []

    env_conf = os.environ.get("SYSTEM_CONF", "").strip()
    if env_conf:
        candidates.append(env_conf)

    roots = _project_root_candidates()

    # Source officielle : dossier conf central.
    candidates.append(nas_conf_file("system.conf"))
    for root in roots:
        candidates.append(os.path.join(root, "conf", "system.conf"))

    # Fallbacks ensuite seulement.
    for root in roots:
        candidates.append(os.path.join(root, "system.conf"))

    candidates.extend([
        nas_conf_file("system.conf"),
        nas_conf_file("system.conf"),
        nas_conf_file("system.conf"),
        "system.conf",
    ])

    # On garde l'ordre mais on évite le piège /system/conf si le vrai fichier existe.
    ordered = _unique_existing_order(candidates)
    filtered = [p for p in ordered if not _is_bad_system_subconf(p)]
    return filtered or ordered



# ---------------------------------------------------------------------------
# Génération automatique des fichiers de configuration System.
# Objectif : si system.conf ou mdns.conf sont supprimés, un redémarrage Flask
# recrée des fichiers propres au lieu de laisser l'UI afficher une erreur rouge.
# ---------------------------------------------------------------------------
SYSTEM_DEFAULT_CONF_TEXT = '# ============================================================\n# Module Flask System\n# Fusion de monitor.conf + réglages du gestionnaire systemd.\n# Fichier prévu pour : system.py / system.html\n# Version v4 : LAN intégré, onglets nettoyés, conf visible dans l’onglet Système\n# ============================================================\n\n# NVIDIA distante : carte graphique dans la VM, donc SSH conservé.\nSSH_GPU_HOST=\nSSH_GPU_PORT=22\nSSH_GPU_USER=\nSSH_GPU_KEY_PATH=\nREMOTE_NVIDIA_SMI=/usr/bin/nvidia-smi\nSSH_GPU_CONNECT_TIMEOUT=5\nSSH_GPU_COMMAND_TIMEOUT=8\nSSH_GPU_CACHE_SECONDS=2\n\n# Intel iGPU local/direct.\nINTEL_GPU_CACHE_SECONDS=1\n\n# Réseau local/direct.\n# Vide = auto : utilise l\'interface de la route par défaut, souvent br0.\n# Tu peux forcer : NET_INTERFACES=br0 ou NET_INTERFACES=eth0,br0\nNET_INTERFACES=\nNET_EXCLUDE_PREFIXES=lo,veth,docker,virbr,br-,vnet,tun,tap\nPUBLIC_IP_CACHE_SECONDS=300\n\n# Gestionnaire de services systemd.\nSYSTEMCTL_REFRESH_SECONDS=5\nSYSTEMCTL_MAX_OUTPUT_CHARS=5000\n\n# Processus / logs du module Système.\nPROCESS_REFRESH_SECONDS=3\nSYSTEM_LOG_LINES=180\n# Optionnel : unité affichée par défaut dans l\'onglet Logs quand on choisit "Service systemd".\n# Exemple : SYSTEM_LOG_DEFAULT_UNIT=systeme.service\nSYSTEM_LOG_DEFAULT_UNIT=\n\n# ============================================================\n# Réseau / LAN hôte intégré au module Système\n# Ancien lan.conf fusionné ici.\n#\n# Principe :\n#   - modifier / ajouter / supprimer dans l\'UI = modifie seulement le plan JSON\n#   - le réseau Linux réel change uniquement au bouton APPLIQUER\n# ============================================================\n\nLAN_MODULE_TITLE=Réseau Linux\n\nLAN_PLAN_FILE=../conf/lan_plans.json\nLAN_BACKUP_DIR=../conf/lan_backups\nLAN_LOG_FILE=/var/log/lan/lan.log\n\n# Clés partagées avec le module système.\nIP_BIN=ip\nSYSTEMCTL_BIN=systemctl\nRESOLV_CONF=/etc/resolv.conf\n\nLAN_DHCLIENT_BIN=dhclient\n\n# Valeurs proposées automatiquement dans l\'UI.\nLAN_DEFAULT_BRIDGE_NAME=br0\nLAN_DEFAULT_IPV4_MODE=copy\nLAN_DEFAULT_PERSIST_BACKEND=interfaces\n\n# Runtime : exécute vraiment ip link / ip addr / ip route au bouton Appliquer.\nLAN_ALLOW_RUNTIME_APPLY=1\n\n# Persistant : par sécurité, l\'écriture /etc/network/interfaces.d est désactivée.\n# Mets 1 seulement quand tu veux autoriser l\'écriture du fichier persistant.\nLAN_ALLOW_PERSISTENT_WRITE=0\nLAN_INTERFACES_OUTPUT_FILE=/etc/network/interfaces.d/zz-flask-lan.conf\nLAN_NETWORKD_OUTPUT_DIR=/etc/systemd/network\n\n# Actions directes sur la table d\'interfaces.\nLAN_ALLOW_INTERFACE_UPDOWN=1\nLAN_ALLOW_VIRTUAL_DELETE=1\n\n# Rollback automatique : si tu appliques un bridge et que l\'accès saute,\n# le module arme un script de retour arrière après ce délai.\n# Si tout fonctionne, clique sur "Annuler rollback" dans l\'interface.\nLAN_ROLLBACK_SECONDS=90\n\nLAN_COMMAND_TIMEOUT=25\nLAN_APPLY_TIMEOUT=90\n\n# Optionnel : cible ping testée après application. Laisse vide pour désactiver.\n# Exemple : LAN_PING_TARGET=192.168.1.254\nLAN_PING_TARGET=\n\nLAN_SHOW_LOOPBACK=0\nLAN_SHOW_DOCKER_INTERFACES=1\n\n# Noms autorisés pour interfaces et bridges.\nLAN_SAFE_NAME_RE=^[A-Za-z0-9_.:-]+$\n\n# ============================================================\n# mDNS intégré au module Système\n# Ancien mdns_ui.conf fusionné ici : tu peux supprimer mdns_ui.conf.\n# Le vrai fichier de noms reste séparé : ../conf/mdns.conf\n# Format du vrai fichier de noms : IP=nom.local\n# ============================================================\n# ============================================================\n# Module Flask mDNS\n#\n# Ce fichier configure seulement l\'interface Flask.\n# Le vrai fichier de noms reste séparé : ../conf/mdns.conf\n# Format du vrai fichier de noms : IP=nom.local\n# ============================================================\n\n# Flask exécute directement la logique mDNS en Python.\nEXEC_MODE=local-python\n\nTITLE=mDNS local\nSUBTITLE=Gestion des noms .local\n\n# Chemins hôte du module mDNS.\nSERVICE_CONF=../conf/mdns.conf\nSERVICE_LOG=/var/log/mdns/mdns.log\nSERVICE_RUN_DIR=/var/run/mdns\nRUNTIME_HOSTS=/var/run/mdns/mdns.hosts\nPID_FILE=/var/run/mdns/mdns-publish.pids\nMDNS_LITE_PID=/var/run/mdns/mdns-lite.pid\n\n# Fichier système Avahi réel.\nAVAHI_HOSTS=/etc/avahi/hosts\n\n# Service systemd optionnel, seulement affiché dans le diagnostic.\nSYSTEMD_SERVICE=mdns-labo.service\n\nPING_BIN=ping\nSS_BIN=ss\nPGREP_BIN=pgrep\nPKILL_BIN=pkill\nPS_BIN=ps\n# IP_BIN=ip   # déjà défini plus haut pour LAN/Système\n# SYSTEMCTL_BIN=systemctl   # déjà défini plus haut pour LAN/Système\nSERVICE_BIN=service\nAVAHI_DAEMON_BIN=avahi-daemon\nAVAHI_PUBLISH_BIN=avahi-publish-address\n\nACTION_TIMEOUT=90\nLOG_TAIL_LINES=180\nDEFAULT_IP=192.168.1.xxx\n\n[WATCH_DIRS]\nDIR1=../conf\n[INDIVIDUAL_FILES]\nFILE1=\n\n# ============================================================\n# Personnalisation générale de l\'interface\n# Le menu détaillé est stocké dans path_menu_root.\n# ============================================================\n# BEGIN_SYSTEM_PERSONALIZATION_MENU\ntitre_tab=Yoleo NAS OS\ntitre_logo=Yoleo NAS OS\nnav_icons=/static/logo.png\npath_menu_root=../conf/menu\npath_rescue=\n# END_SYSTEM_PERSONALIZATION_MENU\n\n\n# Menu latéral : la source détaillée est dans path_menu_root.\n# Le dossier ../conf/menu est généré automatiquement si absent.\n'

MDNS_DEFAULT_CONF_TEXT = '# mDNS local names\n# Format normal: IP=name.local\n# Exemple: 192.168.1.2=system.local\n'


def _ensure_text_file_if_missing(path: str, content: str, mode: int = 0o644) -> bool:
    """Crée un fichier texte s'il est absent, sans jamais écraser l'existant."""
    path = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path or "").strip())))
    if not path:
        return False
    if os.path.exists(path):
        return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content.rstrip() + "\n")
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return True


def ensure_system_conf_file(path: str = "") -> bool:
    """Crée system.conf avec le modèle officiel du module Système si absent."""
    return _ensure_text_file_if_missing(path or nas_conf_file("system.conf"), SYSTEM_DEFAULT_CONF_TEXT)


def ensure_mdns_conf_file(path: str = "") -> bool:
    """Crée mdns.conf vide avec son en-tête propre si absent."""
    return _ensure_text_file_if_missing(path or nas_conf_file("mdns.conf"), MDNS_DEFAULT_CONF_TEXT)


# Création au chargement du module : le premier affichage de /system ou /system/mdns
# ne doit plus tomber sur "conf introuvable" si les fichiers viennent d'être supprimés.
try:
    ensure_system_conf_file(nas_conf_file("system.conf"))
    ensure_mdns_conf_file(nas_conf_file("mdns.conf"))
except Exception as exc:
    print(f"⚠️ Impossible de créer les confs System par défaut : {exc}")


def _clean_conf_value(key, value, conf_dir):
    value = str(value).strip().strip('"').strip("'")
    value = os.path.expanduser(os.path.expandvars(value))

    # Les clés *_PATH peuvent être relatives au dossier du system.conf chargé.
    # Exemple normal : /dockers/conf/system.conf + ../key/yoan -> /dockers/key/yoan.
    # Correction V11 : si un vieux system.conf est chargé depuis /dockers/system/conf,
    # ../key/yoan pointerait vers /dockers/system/key/yoan. On teste donc aussi
    # les parents du dossier de conf et on prend le premier chemin qui existe.
    if key.endswith("_PATH") and value and not os.path.isabs(value) and conf_dir:
        bases = []
        cur = os.path.abspath(conf_dir)
        for _ in range(4):
            bases.append(cur)
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent

        try:
            bases.append(os.getcwd())
        except Exception:
            pass

        try:
            here = os.path.dirname(os.path.abspath(__file__))
            cur = here
            for _ in range(4):
                bases.append(cur)
                parent = os.path.dirname(cur)
                if parent == cur:
                    break
                cur = parent
        except Exception:
            pass

        candidates = _unique_existing_order(os.path.join(base, value) for base in bases)
        if key == "SSH_GPU_KEY_PATH":
            CONF["_SSH_GPU_KEY_PATH_CANDIDATES"] = " | ".join(candidates[:10])

        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate

        if candidates:
            value = candidates[0]

    return value

loaded_config = None
loaded_config_dir = None

for config_path in _build_config_candidates():
    try:
        if not os.path.exists(config_path):
            continue

        conf_dir = os.path.dirname(os.path.abspath(config_path))
        with open(config_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue

                key, value = line.split("=", 1)
                key = key.strip()
                CONF[key] = _clean_conf_value(key, value, conf_dir)

        loaded_config = os.path.abspath(config_path)
        loaded_config_dir = conf_dir
        print(f"✅ system.conf chargé depuis : {loaded_config}")
        print(f"🔑 SSH_GPU_KEY_PATH utilisé : {CONF.get('SSH_GPU_KEY_PATH')}")
        break
    except Exception as e:
        print(f"❌ Erreur lecture {config_path} : {e}")

if loaded_config is None:
    print("⚠️ Aucun system.conf trouvé, utilisation des valeurs par défaut.")
    print(f"🔎 Candidats testés : {', '.join(_build_config_candidates())}")


# -----------------------------------------------------
# Nettoyage runtime des vieux chemins de logs.
# Important : si un ancien system.conf contient encore un chemin de log dans le dossier application,
# on refuse de recréer des logs dans le dossier de l'application.
# -----------------------------------------------------
