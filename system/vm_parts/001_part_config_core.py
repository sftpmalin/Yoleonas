#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# PART 001 - Imports, blueprint, configuration et helpers communs

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import tempfile
import hashlib
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Sequence, Tuple

from flask import Blueprint, Response, abort, jsonify, redirect, render_template, request, send_from_directory


vm_bp = Blueprint("vm_bp", __name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ==========================================================
# ðŸ“ CONF CENTRALISÃ‰E
# ==========================================================
# app.py pose NAS_CONF_DIR. Les modules le lisent sans importer app.py
# pour Ã©viter les imports circulaires pendant le chargement des blueprints.
_NAS_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_NAS_DEFAULT_CONF_DIR = os.path.abspath(os.path.join(_NAS_MODULE_DIR, "..", "conf"))
NAS_CONF_DIR = os.path.abspath(os.path.expanduser(os.path.expandvars(os.environ.get("NAS_CONF_DIR", _NAS_DEFAULT_CONF_DIR))))
NAS_ROOT_DIR = os.path.abspath(os.path.join(NAS_CONF_DIR, ".."))

def nas_conf_file(name: str) -> str:
    return os.path.join(NAS_CONF_DIR, name)

def nas_root_path(*parts: str) -> str:
    return os.path.join(NAS_ROOT_DIR, *parts)

CONFIG_CANDIDATES = [
    nas_conf_file("vm.conf"),
    os.path.join(BASE_DIR, "conf", "vm.conf"),
    os.path.join(os.path.dirname(BASE_DIR), "conf", "vm.conf"),
]

DEFAULT_CONFIG = {
    # Connexion libvirt locale standard Debian/Linux.
    # Si tu veux laisser virsh choisir son dÃ©faut, mets LIBVIRT_URI= dans vm.conf.
    "VIRSH_BIN": "virsh",
    "LIBVIRT_URI": "qemu:///system",

    # Commandes optionnelles pour enrichir le panneau.
    "QEMU_IMG_BIN": "qemu-img",
    "QEMU_SYSTEM_BIN": "qemu-system-x86_64",
    "VIRT_INSTALL_BIN": "virt-install",
    "SETFACL_BIN": "setfacl",
    "QEMU_USER": "",
    "VM_CREATE_AUTO_FIX_LIBVIRT_ACL": "1",
    "LSPCI_BIN": "lspci",

    # Export XML lisible. Mode first_disk_dir = Ã  cÃ´tÃ© du premier disque VM si possible,
    # sinon XML_EXPORT_DIR/NOM_VM/NOM_VM.xml.
    "XML_EXPORT_DIR": "/var/lib/libvirt/images",
    "XML_EXPORT_MODE": "first_disk_dir",
    "XML_BACKUP_DIR": "/var/backups/vm_xml_backups",

    # Lien externe facultatif vers virt-manager/noVNC.
    "VIRT_MANAGER_URL": "",

    # Console vidÃ©o noVNC locale via Flask + websockify.
    "SHOW_CONSOLE_BUTTON": "1",
    "CONSOLE_MODE": "websockify",
    "NOVNC_LOCAL_PLUGIN_DIR": "/usr/share/novnc",
    "CONSOLE_WS_TARGET_TEMPLATE": "ws://127.0.0.1:{wsport}/",
    "CONSOLE_WS_FALLBACKS": "1",
    "CONSOLE_LOG_FILE": "/var/log/vm_console_proxy.log",
    "NOVNC_URL_TEMPLATE": "{base}/vm/novnc/vnc.html?v={ts}&resize=scale&autoconnect=true&host={host:raw}&port={web_port}&path=vm/wsproxy/{wsport}/",
    "UNRAID_WEB_URL": "",
    "VNC_HOST": "",

    # Pont noVNC fiable. Exemple : VNC 5900 -> WebSocket 6080, VNC 5901 -> 6081.
    "WEBSOCKIFY_BIN": "websockify",
    "WEBSOCKIFY_BIND_HOST": "0.0.0.0",
    "WEBSOCKIFY_BASE_PORT": "6080",
    "WEBSOCKIFY_BROWSER_HOST": "",
    "WEBSOCKIFY_IDLE_TIMEOUT": "0",
    "WEBSOCKIFY_LOG_FILE": "/var/log/vm_websockify.log",

    # Interface.
    "REFRESH_SECONDS": "8",
    "ACTION_TIMEOUT": "90",
    "SHOW_XML_BUTTON": "1",
    "SHOW_EXPORT_XML_BUTTON": "1",
    "SHOW_HARDWARE_BUTTONS": "1",

    # Chemins Linux/libvirt standards.
    "HOST_ROOT": "/",
    "HOST_DEV_BY_ID": "/dev/disk/by-id",
    "STORAGE_DEFAULT_DIR": "/var/lib/libvirt/images",
    "ISO_DEFAULT_DIR": "/var/lib/libvirt/boot",
    "VM_LOG_FILE": "/var/log/vm.log",

    # Valeurs par dÃ©faut pour les crÃ©ations depuis l'interface.
    "VM_CREATE_DEFAULT_MEMORY_GB": "4",
    "VM_CREATE_DEFAULT_VCPUS": "2",
    "VM_CREATE_DEFAULT_DISK_GB": "64",
    "VM_CREATE_DEFAULT_DISK_FORMAT": "qcow2",
    "VM_CREATE_DEFAULT_DISK_BUS": "virtio",
    "VM_CREATE_DEFAULT_STORAGE_MODE": "pool",
    "VM_CREATE_DEFAULT_STORAGE_POOL": "default",
    "VM_CREATE_DEFAULT_NETWORK_KIND": "bridge",
    "VM_CREATE_DEFAULT_NETWORK_SOURCE": "br0",
    "VM_CREATE_DEFAULT_NETWORK_MODEL": "virtio",
    "VM_CREATE_DEFAULT_GRAPHICS": "vnc",
    "VM_CREATE_DEFAULT_VIDEO": "bochs",
    "VM_VIDEO3D_AUTO_REPAIR": "1",
    "VM_VIDEO3D_AUTO_INSTALL_QEMU_OPENGL": "1",
    "VM_VIDEO3D_QEMU_OPENGL_PACKAGES": "qemu-system-modules-opengl libvirglrenderer1",
    "VM_VIDEO3D_PREFER_GPU": "intel",
    "VM_CREATE_DEFAULT_FIRMWARE": "bios",
    "VM_CREATE_DEFAULT_OS_VARIANT": "generic",
    "VM_CREATE_DEFAULT_CPU": "host-passthrough",
    "VM_CREATE_ISO_STAGING": "auto",
    "VM_CREATE_ISO_STAGING_DIR": "/var/lib/libvirt/boot/yoleo",
    "VM_SERIAL_TTYD_BASE_PORT": "7820",
    "VM_SERIAL_TTYD_PORT_COUNT": "40",
    "POOL_AUTOSTART_DEFAULT": "1",
    "NETWORK_AUTOSTART_DEFAULT": "1",
    "ALLOW_LIVE_DEVICE_CHANGES": "0",
}


SAFE_NAME_RE = re.compile(r"^[^\x00/]+$")


def strip_quotes(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def vm_default_conf_text(conf: Optional[Dict[str, str]] = None) -> str:
    """Contenu de vm.conf crÃ©Ã© automatiquement si absent."""
    data = dict(DEFAULT_CONFIG)
    if conf:
        data.update(conf)

    sections = [
        ("Libvirt", ["VIRSH_BIN", "LIBVIRT_URI", "QEMU_IMG_BIN", "VIRT_INSTALL_BIN", "SETFACL_BIN", "QEMU_USER", "VM_CREATE_AUTO_FIX_LIBVIRT_ACL", "LSPCI_BIN"]),
        ("Export / sauvegarde XML", ["XML_EXPORT_DIR", "XML_EXPORT_MODE", "XML_BACKUP_DIR"]),
        ("Console noVNC locale", [
            "SHOW_CONSOLE_BUTTON", "CONSOLE_MODE", "NOVNC_LOCAL_PLUGIN_DIR",
            "CONSOLE_WS_TARGET_TEMPLATE", "CONSOLE_WS_FALLBACKS", "CONSOLE_LOG_FILE",
            "NOVNC_URL_TEMPLATE", "VIRT_MANAGER_URL", "UNRAID_WEB_URL", "VNC_HOST",
        ]),
        ("Websockify", [
            "WEBSOCKIFY_BIN", "WEBSOCKIFY_BIND_HOST", "WEBSOCKIFY_BASE_PORT",
            "WEBSOCKIFY_BROWSER_HOST", "WEBSOCKIFY_IDLE_TIMEOUT", "WEBSOCKIFY_LOG_FILE",
        ]),
        ("Interface", [
            "REFRESH_SECONDS", "ACTION_TIMEOUT", "SHOW_XML_BUTTON",
            "SHOW_EXPORT_XML_BUTTON", "SHOW_HARDWARE_BUTTONS",
        ]),
        ("Chemins Linux / libvirt", [
            "HOST_ROOT", "HOST_DEV_BY_ID", "STORAGE_DEFAULT_DIR", "ISO_DEFAULT_DIR", "VM_LOG_FILE",
        ]),
        ("CrÃ©ation depuis l'interface", [
            "VM_CREATE_DEFAULT_MEMORY_GB", "VM_CREATE_DEFAULT_VCPUS", "VM_CREATE_DEFAULT_DISK_GB",
            "VM_CREATE_DEFAULT_DISK_FORMAT", "VM_CREATE_DEFAULT_DISK_BUS",
            "VM_CREATE_DEFAULT_STORAGE_MODE", "VM_CREATE_DEFAULT_STORAGE_POOL",
            "VM_CREATE_DEFAULT_NETWORK_KIND", "VM_CREATE_DEFAULT_NETWORK_SOURCE", "VM_CREATE_DEFAULT_NETWORK_MODEL",
            "VM_CREATE_DEFAULT_GRAPHICS", "VM_CREATE_DEFAULT_VIDEO", "VM_CREATE_DEFAULT_FIRMWARE",
            "VM_CREATE_DEFAULT_OS_VARIANT", "VM_CREATE_DEFAULT_CPU",
            "VM_CREATE_ISO_STAGING", "VM_CREATE_ISO_STAGING_DIR",
            "POOL_AUTOSTART_DEFAULT", "NETWORK_AUTOSTART_DEFAULT", "ALLOW_LIVE_DEVICE_CHANGES",
        ]),
    ]

    lines = [
        "# Module VM - Flask System host / libvirt",
        "# Fichier crÃ©Ã© automatiquement par vm.py si absent.",
        "# Les chemins de logs utilisent /var/log, les sauvegardes XML /var/backups.",
        "",
    ]
    written: set[str] = set()
    for title, keys in sections:
        lines.append(f"# {title}")
        for key in keys:
            lines.append(f"{key}={data.get(key, '')}")
            written.add(key)
        lines.append("")

    extra_keys = [key for key in data.keys() if key not in written and not key.startswith("_")]
    if extra_keys:
        lines.append("# Autres paramÃ¨tres")
        for key in extra_keys:
            lines.append(f"{key}={data.get(key, '')}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def ensure_vm_config_file(path: str) -> bool:
    """CrÃ©e vm.conf avec les valeurs standards si le fichier manque."""
    if os.path.exists(path):
        return False
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(vm_default_conf_text())
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
        return True
    except OSError:
        # Si le service n'a pas le droit d'Ã©crire le conf, le module continue
        # avec DEFAULT_CONFIG au lieu de casser la page VM.
        return False


def read_config_file(path: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not os.path.exists(path):
        return data
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key:
                    data[key] = strip_quotes(value)
    except OSError:
        pass
    return data


def get_config_path() -> str:
    env_path = os.environ.get("VM_CONFIG_PATH", "").strip()
    if env_path:
        return env_path
    for candidate in CONFIG_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return CONFIG_CANDIDATES[0]


def get_config() -> Dict[str, str]:
    path = get_config_path()
    ensure_vm_config_file(path)
    conf = DEFAULT_CONFIG.copy()
    conf.update(read_config_file(path))
    conf = normalize_runtime_paths(conf)
    conf["_CONFIG_FILE"] = path
    return conf


def resolve_module_path(path: str, fallback: str = "") -> str:
    """RÃ©sout les chemins relatifs au dossier du module VM.

    Les chemins absolus Linux (/var/log, /var/backups, /var/lib/libvirt, etc.)
    restent absolus. Les chemins relatifs restent possibles pour un usage portable.
    """
    value = str(path or fallback or "").strip()
    if not value:
        return ""
    if os.path.isabs(value):
        return value
    return os.path.normpath(os.path.join(BASE_DIR, value))


def normalize_runtime_paths(conf: Dict[str, str]) -> Dict[str, str]:
    conf = dict(conf)
    for key, fallback in (
        ("VM_LOG_FILE", DEFAULT_CONFIG["VM_LOG_FILE"]),
        ("CONSOLE_LOG_FILE", DEFAULT_CONFIG["CONSOLE_LOG_FILE"]),
        ("WEBSOCKIFY_LOG_FILE", DEFAULT_CONFIG["WEBSOCKIFY_LOG_FILE"]),
        ("XML_BACKUP_DIR", DEFAULT_CONFIG["XML_BACKUP_DIR"]),
    ):
        conf[key] = resolve_module_path(conf.get(key, ""), fallback)
    return conf


def conf_bool(conf: Dict[str, str], key: str, default: str = "0") -> bool:
    return str(conf.get(key, default)).strip().lower() in {"1", "true", "yes", "on", "oui"}


def conf_int(conf: Dict[str, str], key: str, default: int) -> int:
    try:
        return int(str(conf.get(key, default)).strip())
    except Exception:
        return default


def clean_vm_name(name: str) -> str:
    name = (name or "").strip()
    if not name or not SAFE_NAME_RE.match(name):
        raise ValueError("Nom de VM invalide.")
    return name


def safe_filename(name: str) -> str:
    # Nom de fichier export uniquement. Le nom libvirt original reste utilisÃ© pour virsh.
    out = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip()).strip("._")
    return out or "vm"


def run_cmd(cmd: List[str], timeout: int = 30) -> Tuple[int, str]:
    try:
        # Force une sortie stable en anglais pour virsh/systemctl/qemu-img.
        # Sans Ã§a, sur un systÃ¨me en franÃ§ais, virsh peut rÃ©pondre "fermÃ© inconnu"
        # au lieu de "shut off", et l'UI croit que la VM n'est pas arrÃªtÃ©e.
        env = os.environ.copy()
        env.update({"LC_ALL": "C", "LANG": "C", "LANGUAGE": "C"})
        completed = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )
        return completed.returncode, completed.stdout or ""
    except subprocess.TimeoutExpired:
        return 124, f"Timeout aprÃ¨s {timeout}s : {' '.join(cmd)}"
    except FileNotFoundError:
        return 127, f"Commande introuvable : {cmd[0]}"
    except Exception as exc:
        return 1, f"Exception Python : {exc}"


def virsh_cmd(conf: Dict[str, str], *args: str) -> List[str]:
    cmd = [conf.get("VIRSH_BIN", "virsh").strip() or "virsh"]
    uri = conf.get("LIBVIRT_URI", "").strip()
    if uri:
        cmd.extend(["-c", uri])
    cmd.extend(args)
    return cmd


def virsh(conf: Dict[str, str], *args: str, timeout: Optional[int] = None) -> Tuple[int, str]:
    if timeout is None:
        try:
            timeout = int(conf.get("ACTION_TIMEOUT", "90"))
        except ValueError:
            timeout = 90
    return run_cmd(virsh_cmd(conf, *args), timeout=timeout)


def parse_key_values(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw in (text or "").splitlines():
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key:
            out[key] = value
    return out


def human_bytes(value: Optional[int]) -> str:
    if value is None or value < 0:
        return "â€”"
    units = ["o", "Ko", "Mo", "Go", "To"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "o":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{value} o"


def memory_to_bytes(text: Optional[str], unit: Optional[str]) -> Optional[int]:
    if not text:
        return None
    try:
        value = int(str(text).strip())
    except ValueError:
        return None
    unit = (unit or "KiB").strip().lower()
    factor = 1
    if unit in {"b", "bytes", "byte"}:
        factor = 1
    elif unit in {"kb", "k", "kib"}:
        factor = 1024
    elif unit in {"mb", "m", "mib"}:
        factor = 1024 ** 2
    elif unit in {"gb", "g", "gib"}:
        factor = 1024 ** 3
    return value * factor


def first_text(root: ET.Element, path: str, default: str = "") -> str:
    node = root.find(path)
    return (node.text or "").strip() if node is not None and node.text else default


def node_attr(node: Optional[ET.Element], name: str, default: str = "") -> str:
    if node is None:
        return default
    return (node.get(name) or default).strip()


def xml_to_string(root: ET.Element) -> str:
    try:
        ET.indent(root, space="  ")  # Python >= 3.9
    except Exception:
        pass
    return ET.tostring(root, encoding="unicode")


def pci_address_from_xml(addr: Optional[ET.Element]) -> str:
    if addr is None:
        return ""
    try:
        domain = int((addr.get("domain") or "0"), 16)
        bus = int((addr.get("bus") or "0"), 16)
        slot = int((addr.get("slot") or "0"), 16)
        function = int((addr.get("function") or "0"), 16)
        return f"{domain:04x}:{bus:02x}:{slot:02x}.{function:x}"
    except Exception:
        return ""


def lspci_label(conf: Dict[str, str], pci_addr: str) -> str:
    if not pci_addr:
        return ""
    lspci = conf.get("LSPCI_BIN", "lspci").strip() or "lspci"
    if shutil.which(lspci) is None and not os.path.isabs(lspci):
        return ""
    rc, out = run_cmd([lspci, "-nn", "-s", pci_addr], timeout=5)
    if rc != 0:
        return ""
    # Exemple : 01:00.0 VGA compatible controller [0300]: NVIDIA ...
    return (out or "").strip()


def disk_size_label(path: str) -> str:
    if not path or not os.path.exists(path):
        return ""
    try:
        return human_bytes(os.path.getsize(path))
    except OSError:
        return ""
