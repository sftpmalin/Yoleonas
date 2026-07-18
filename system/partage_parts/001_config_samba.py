#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
partage.py - Module Flask host pour Samba + NFS.

But :
  - Piloter les vrais services Linux depuis Flask.
  - Lire partage.conf pour connaître les vrais chemins à piloter.
  - Éditer confortablement le fichier source Samba indiqué par SAMBA_CONF.
  - Éditer confortablement la configuration NFS portable indiquée par NFS_SERVER_CONF.
  - Générer le fichier Linux indiqué par NFS_EXPORTS_FILE au reload exportfs.
  - Appliquer/recharger smbd, nmbd, wsdd/wsdd2, nfs-server/nfs-kernel-server.

Installation Flask :
  from partage import partage_bp
  app.register_blueprint(partage_bp)

CLI utile pour le service systemd d'application Samba :
  python3 partage.py --samba-apply --conf /chemin/samba.conf
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import hashlib
import io
import os
import pwd
import grp
import re
import shlex
import shutil
import subprocess
import threading
import sys
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from flask import (
    Blueprint,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    stream_with_context,
    url_for,
)

partage_bp = Blueprint("partage_bp", __name__)

VERSION = "2026-06-09-samba-auto-linux-users-rights-matrix-v2"
MANAGED_MARKER = "# Managed by partage.py"
SAMBA_APPLY_SERVICE = "samba-host-apply.service"
LEGACY_WSDD_SERVICE = "samba-wsdd-host.service"
DISTRO_WSDD_SERVICES = ("wsdd2.service", "wsdd.service")
SAMBA_SERVICES = ("smbd.service", "nmbd.service")
NFS_SERVICES = ("nfs-server.service", "nfs-kernel-server.service")
WSDD2_OVERRIDE = Path("/etc/systemd/system/wsdd2.service.d/10-yoleo-samba.conf")
WSDD_REFRESH_SERVICE = "samba-wsdd-refresh.service"
WSDD_REFRESH_TIMER = "samba-wsdd-refresh.timer"
STATE_DIR = Path("/var/lib/samba-host")
USER_HASH_FILE = STATE_DIR / "users.sha256"
SHARE_RIGHTS_STATE_FILE = STATE_DIR / "share_rights.sha256"

BASE_DIR = Path(__file__).resolve().parent

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


# ---------------------------------------------------------------------------
# Configuration du module Partage
# ---------------------------------------------------------------------------
# Le module lit d'abord un fichier partage.conf. Les chemins relatifs dans ce
# fichier sont résolus par rapport au dossier de partage.py.
# Exemple si partage.py est dans /dockers/system :
#   SAMBA_CONF=../conf/samba.conf  =>  /dockers/conf/samba.conf
# Tu peux forcer le chemin du partage.conf avec PARTAGE_CONFIG=/chemin/partage.conf.

DEFAULT_PARTAGE_CONFIG = {
    # Valeurs standards créées automatiquement si partage.conf manque.
    # Les champs techniques restent dans le conf, mais ne sont plus exposés dans l'UI Réglages.
    "SAMBA_CONF": "../conf/samba.conf",
    "NFS_SERVER_CONF": "../conf/nfs_server.conf",
    "NFS_EXPORTS_FILE": "/etc/exports.d/nfs.exports",
    "NFS_DEFAULT_CLIENT": "192.168.1.0/24",
    "NFS_DEFAULT_OPTIONS": "sync,no_subtree_check,no_root_squash",
    "LOG_LINES": "300",
    "BROWSE_START": "/",
    # Dossiers où ranger les sauvegardes automatiques au lieu de polluer ../conf.
    "SAV_SAMBA_BACKUP": "../backups",
    "SAV_NFS_BACKUP": "../backups",
    "SAV_PARTAGE_BACKUP": "../backups",
    # Commande(s) lancée(s) après Enregistrer partage.conf.
    # Chemin relatif = même dossier que app.py / partage.py.
    "restart_scripts": "system.sh -restart",
}

HIDDEN_PARTAGE_SETTING_KEYS = {
    "SAMBA_CONF",
    "NFS_SERVER_CONF",
    "NFS_EXPORTS_FILE",
    "NFS_DEFAULT_CLIENT",
    "NFS_DEFAULT_OPTIONS",
    "LOG_LINES",
    "BROWSE_START",
    "SAV_SAMBA_BACKUP",
    "SAV_NFS_BACKUP",
    "SAV_PARTAGE_BACKUP",
    "restart_scripts",
    "RESTART_SCRIPTS",
}


def strip_conf_quotes(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def partage_default_conf_text() -> str:
    """Contenu de partage.conf créé automatiquement si absent."""
    return """# partage.conf - configuration du module Flask partage.py
# Les chemins relatifs sont résolus depuis le dossier où se trouve partage.py.
# Exemple : si partage.py est dans /dockers/system,
# SAMBA_CONF=../conf/samba.conf pointe vers /dockers/conf/samba.conf.

# Fichier source Samba édité par l'interface.
SAMBA_CONF=../conf/samba.conf

# Fichier NFS serveur portable édité par l'interface.
NFS_SERVER_CONF=../conf/nfs_server.conf

# Fichier Linux généré au reload exportfs.
NFS_EXPORTS_FILE=/etc/exports.d/nfs.exports

# Valeurs par défaut pour les nouveaux exports NFS.
NFS_DEFAULT_CLIENT=192.168.1.0/24
NFS_DEFAULT_OPTIONS=sync,no_subtree_check,no_root_squash

# Nombre de lignes par défaut dans l'onglet logs.
LOG_LINES=300

# Dossier de départ du bouton Parcourir.
BROWSE_START=/

# Dossiers des sauvegardes automatiques.
# Objectif : ne pas polluer le dossier conf avec les .bak.
SAV_SAMBA_BACKUP=../backups
SAV_NFS_BACKUP=../backups
SAV_PARTAGE_BACKUP=../backups

# Commande lancée automatiquement après Enregistrer partage.conf.
# Chemin relatif = même dossier que app.py / partage.py / system.sh.
restart_scripts=system.sh -restart
"""


def ensure_partage_config_file(path: Path) -> bool:
    """Crée partage.conf avec les valeurs par défaut si le fichier manque."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(partage_default_conf_text(), encoding="utf-8")
    try:
        path.chmod(0o644)
    except OSError:
        pass
    return True


def ensure_partage_config_default_keys(path: Path, existing_keys: set[str]) -> bool:
    """Ajoute automatiquement les clés par défaut manquantes dans partage.conf.

    L'UI n'expose plus ces champs techniques : le module doit donc garder
    partage.conf complet tout seul, même après migration depuis un ancien fichier.
    """
    missing = [key for key in DEFAULT_PARTAGE_CONFIG.keys() if key not in existing_keys]
    if not missing or not path.exists():
        return False
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n# Valeurs par défaut ajoutées automatiquement par partage.py.\n")
            for key in missing:
                fh.write(f"{key}={DEFAULT_PARTAGE_CONFIG[key]}\n")
        return True
    except OSError:
        return False

def resolve_module_path(value: str | Path, *, default: str | Path | None = None) -> Path:
    raw = strip_conf_quotes(str(value or default or "")).strip()
    if not raw:
        raw = str(default or "")
    raw = os.path.expandvars(os.path.expanduser(raw))
    path = Path(raw)
    if path.is_absolute():
        return path.resolve()
    return (BASE_DIR / path).resolve()


def partage_config_candidates() -> list[Path]:
    env_path = os.environ.get("PARTAGE_CONFIG", "").strip()
    candidates: list[Path] = []
    if env_path:
        candidates.append(resolve_module_path(env_path))
    candidates.extend([
        Path(nas_conf_file("partage.conf")),
        BASE_DIR.parent / "conf" / "partage.conf",
        BASE_DIR / "conf" / "partage.conf",
        Path.cwd().parent / "conf" / "partage.conf",
        Path.cwd() / "conf" / "partage.conf",
        BASE_DIR / "partage.conf",
    ])
    out: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            out.append(resolved)
    return out


def get_partage_config_path() -> Path:
    for candidate in partage_config_candidates():
        if candidate.exists():
            return candidate
    # Par défaut portable : module dans /dockers/system => /dockers/conf/partage.conf.
    return Path(nas_conf_file("partage.conf")).resolve()


def read_partage_config() -> dict[str, str]:
    conf = DEFAULT_PARTAGE_CONFIG.copy()
    path = get_partage_config_path()
    ensure_partage_config_file(path)
    existing_keys: set[str] = set()
    if path.exists():
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key:
                existing_keys.add(key)
                conf[key] = strip_conf_quotes(value)
        ensure_partage_config_default_keys(path, existing_keys)

    # Compat anciennes variables d'environnement, au cas où.
    if os.environ.get("PARTAGE_SAMBA_CONF"):
        conf["SAMBA_CONF"] = os.environ["PARTAGE_SAMBA_CONF"]
    if os.environ.get("PARTAGE_NFS_EXPORTS_FILE"):
        conf["NFS_EXPORTS_FILE"] = os.environ["PARTAGE_NFS_EXPORTS_FILE"]
    if os.environ.get("PARTAGE_NFS_SERVER_CONF"):
        conf["NFS_SERVER_CONF"] = os.environ["PARTAGE_NFS_SERVER_CONF"]

    # Alias acceptés : l'interface écrit restart_scripts en minuscules,
    # mais si un ancien fichier contient RESTART_SCRIPTS on le reprend.
    if not conf.get("restart_scripts") and conf.get("RESTART_SCRIPTS"):
        conf["restart_scripts"] = conf["RESTART_SCRIPTS"]
    return conf


def partage_setting(key: str, default: str = "") -> str:
    return read_partage_config().get(key, default)


def configured_path(key: str, default: str) -> Path:
    return resolve_module_path(partage_setting(key, default), default=default)


def nfs_default_client() -> str:
    return partage_setting("NFS_DEFAULT_CLIENT", "192.168.1.0/24") or "192.168.1.0/24"


def nfs_default_options() -> str:
    return partage_setting("NFS_DEFAULT_OPTIONS", "sync,no_subtree_check,no_root_squash") or "sync,no_subtree_check,no_root_squash"


PARTAGE_CONF = read_partage_config()
PARTAGE_CONF_PATH = get_partage_config_path()
DEFAULT_CLIENT = PARTAGE_CONF.get("NFS_DEFAULT_CLIENT", "192.168.1.0/24")
DEFAULT_BASE_OPTIONS = PARTAGE_CONF.get("NFS_DEFAULT_OPTIONS", "sync,no_subtree_check,no_root_squash")
DEFAULT_NFS_SERVER_CONF = configured_path("NFS_SERVER_CONF", "../conf/nfs_server.conf")
DEFAULT_EXPORTS_FILE = configured_path("NFS_EXPORTS_FILE", "/etc/exports.d/nfs.exports")

FORBIDDEN_SHARE_PATHS = {
    "/", "/bin", "/boot", "/boot/efi", "/dev", "/etc", "/lib", "/lib64",
    "/proc", "/run", "/sbin", "/sys", "/tmp", "/usr", "/var", "/home", "/mnt",
}
AUTO_FSID_PATHS = {
    "/mnt/user": "100",
    "/mnt/user0": "101",
    "/mnt/cache": "102",
}


def nfs_auto_fsid_for_path(path: str) -> str:
    """Retourne un fsid stable pour les exports NFS qui en ont besoin.

    Les chemins de type /mnt/user/... viennent souvent d'un pool mergerfs/FUSE.
    Avec nfs-kernel-server, exportfs exige alors un fsid explicite, sinon
    l'erreur typique est : "requires fsid= for NFS export".
    """
    norm = os.path.normpath(path or "")
    direct = AUTO_FSID_PATHS.get(norm)
    if direct:
        return direct

    for base, base_fsid in AUTO_FSID_PATHS.items():
        base_norm = os.path.normpath(base)
        if norm.startswith(base_norm + os.sep):
            # Identifiant stable par chemin, lisible et peu susceptible de collision.
            seed = f"{base_fsid}:{norm}".encode("utf-8", errors="ignore")
            return str(1000 + (int(hashlib.sha1(seed).hexdigest()[:8], 16) % 900000))
    return ""


def nfs_options_with_auto_fsid(path: str, options: str | None) -> str:
    parts = normalize_nfs_options(options).split(",") if options else []
    parts = [x for x in parts if x]
    if not any(x.startswith("fsid=") or x == "fsid" for x in parts):
        fsid = nfs_auto_fsid_for_path(path)
        if fsid:
            parts.append(f"fsid={fsid}")
    return ",".join(parts)

USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SHARE_NAME_RE = re.compile(r"^[^/\[\]\x00-\x1f]{1,80}$")
WSDD_ARG_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,63}$")
WSDD_WINDOWS_NAME_RE = re.compile(r"[^A-Za-z0-9-]+")
SAMBA_HUMAN_UID_MIN = 1000
SAMBA_HUMAN_UID_MAX = 59999
SAMBA_EXCLUDED_LINUX_USERS = {"nobody", "wsdd", "wsdd2"}


def windows_discovery_name(value: str | None, default: str = "Samba", *, max_length: int = 63, upper: bool = False) -> str:
    """Return a DNS-ish name that Windows accepts in WSD/LLMNR replies."""
    raw = str(value or "").strip() or default
    cleaned = WSDD_WINDOWS_NAME_RE.sub("-", raw)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    if not cleaned:
        cleaned = default
    if not re.match(r"^[A-Za-z0-9]", cleaned):
        cleaned = f"Samba-{cleaned}"
    cleaned = cleaned[:max_length].rstrip("-") or default
    return cleaned.upper() if upper else cleaned


@dataclass
class SambaUser:
    name: str
    password: str
    uid: int
    gid: int
    shell: str = "/usr/sbin/nologin"
    home: str = "/nonexistent"


@dataclass
class SambaShare:
    name: str
    path: Path
    share_type: str = "normal"
    guest_ok: str = "no"
    read_only: str = "no"
    browsable: str = "yes"
    writable: str = "yes"
    recycle_bin: str = "no"
    owner: str = ""
    access_mode: str = "private"
    read_users: list[str] = field(default_factory=list)
    write_users: list[str] = field(default_factory=list)


@dataclass
class SambaConfig:
    conf_path: Path
    workgroup: str = "WORKGROUP"
    server_string: str = "Host Samba"
    netbios_name: str = "Samba"
    interface: str = "br0"
    smb_conf: Path = Path("/etc/samba/smb.conf")
    log_file: str = "/var/log/samba/log.%m"
    max_log_size: str = "50"
    min_protocol: str = "SMB2"
    enable_wsdd: bool = True
    wsdd_name: str = "Samba"
    create_missing_dirs: bool = True
    users: list[SambaUser] = field(default_factory=list)
    shares: list[SambaShare] = field(default_factory=list)


@dataclass(frozen=True)
class NfsEntry:
    path: str
    client: str = DEFAULT_CLIENT
    access: str = "rw"
    # Options avancées sans rw/ro.
    # None = génération automatique depuis NFS_DEFAULT_OPTIONS + fsid auto si besoin.
    # ""   = aucune option avancée volontairement.
    advanced_options: str | None = None

    @property
    def options_extra(self) -> str:
        base_options = nfs_default_options() if self.advanced_options is None else self.advanced_options
        return nfs_options_with_auto_fsid(self.path, base_options)

    @property
    def options(self) -> str:
        opts = [self.access]
        extra = self.options_extra
        if extra:
            opts.extend(x for x in extra.split(",") if x)
        return ",".join(opts)

    def to_line(self) -> str:
        return f"{escape_exports_path(self.path)} {self.client}({self.options})"


def bool_value(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "yes", "y", "true", "on", "oui"}


def yesno(value: bool) -> str:
    return "yes" if value else "no"


def command_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def run_capture(cmd: list[str], *, input_text: str | None = None, check: bool = False, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=check,
        env=env,
    )


def shell_join(cmd: Iterable[str]) -> str:
    import shlex
    return " ".join(shlex.quote(str(x)) for x in cmd)

def detect_samba_interface(default: str = "") -> str:
    """Détecte l'interface LAN à utiliser pour wsdd/wsdd2.

    L'interface ne doit plus être choisie dans l'UI : une mauvaise valeur
    comme br0 sur une machine qui sort réellement par enp1s0 suffit à casser
    la découverte Windows. On privilégie donc l'interface de la route par
    défaut, puis un fallback sur une interface UP non virtuelle.
    """
    ignored_prefixes = (
        "lo", "docker", "br-", "veth", "virbr", "vmnet", "vboxnet",
        "tun", "tap", "wg", "tailscale", "zt", "cni", "flannel", "kube",
    )

    def clean(name: str) -> str:
        return (name or "").strip().split("@", 1)[0]

    def usable(name: str) -> bool:
        name = clean(name)
        return bool(name) and not any(name.startswith(prefix) for prefix in ignored_prefixes)

    # 1) Interface réellement utilisée par la route par défaut.
    if command_exists("ip"):
        res = run_capture(["ip", "-o", "route", "show", "default"])
        for line in res.stdout.splitlines():
            match = re.search(r"\bdev\s+(\S+)", line)
            if match:
                iface = clean(match.group(1))
                if iface:
                    return iface

        # 2) Fallback : première interface UP non virtuelle.
        res = run_capture(["ip", "-o", "link", "show", "up"])
        for line in res.stdout.splitlines():
            match = re.match(r"\d+:\s+([^:]+):", line)
            if match:
                iface = clean(match.group(1))
                if usable(iface):
                    return iface

    fallback = clean(default)
    if fallback and fallback.lower() not in {"auto", "detect", "detected"}:
        return fallback
    return ""


def samba_autodetected_interface(previous: str = "") -> str:
    return detect_samba_interface(previous or "br0") or (previous or "br0")


def samba_bound_interfaces(cfg: SambaConfig) -> str:
    """Limite Samba à l'interface LAN principale sans toucher aux interfaces Docker."""
    iface = (cfg.interface or "").strip().split("@", 1)[0]
    if iface:
        return f"lo {iface}"
    return "lo"


def read_stored_samba_interface(conf_path: Path | None = None) -> str:
    """Lit l'interface réellement stockée dans samba.conf, sans auto-détection.

    load_samba_config() retourne volontairement l'interface auto-détectée.
    Pour le garde-fou de démarrage, on a besoin de comparer la valeur écrite
    dans le fichier avec l'interface réseau actuelle de la machine.
    """
    path = (conf_path or resolve_samba_conf_path()).resolve()
    if not path.exists():
        return ""

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read(path, encoding="utf-8")
    except Exception:
        return ""
    if "global" not in parser:
        return ""
    return str(parser["global"].get("interface", "")).strip()


def _append_samba_boot_log(text: str) -> None:
    """Écrit un petit log persistant pour le garde-fou interface Samba."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with (STATE_DIR / "interface-autofix.log").open("a", encoding="utf-8") as handle:
            handle.write(text.rstrip() + "\n")
    except Exception:
        pass


def _samba_interface_lock_path() -> Path:
    # Verrou très simple pour éviter que deux workers Flask/Gunicorn relancent
    # Samba/wsdd en même temps. /run est préféré, avec fallback /var/lib.
    run_dir = Path("/run/yoleo")
    if run_dir.exists() or os.access("/run", os.W_OK):
        return run_dir / "samba-interface-autofix.lock"
    return STATE_DIR / "interface-autofix.lock"


def _acquire_samba_interface_lock() -> int | None:
    lock_path = _samba_interface_lock_path()
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if lock_path.exists():
            # Verrou mort après crash : on le libère après 10 minutes.
            try:
                if time.time() - lock_path.stat().st_mtime > 600:
                    lock_path.unlink(missing_ok=True)
            except OSError:
                pass
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, f"pid={os.getpid()}\nstarted={now_stamp()}\n".encode("utf-8", errors="ignore"))
        return fd
    except FileExistsError:
        return None
    except Exception as exc:
        _append_samba_boot_log(f"[{now_stamp()}] LOCK impossible : {exc}")
        return None


def _release_samba_interface_lock(fd: int | None) -> None:
    if fd is None:
        return
    lock_path = _samba_interface_lock_path()
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def samba_interface_needs_autofix(cfg: SambaConfig, stored_interface: str, detected_interface: str) -> tuple[bool, list[str]]:
    """Décide si le démarrage Flask doit réappliquer Samba/wsdd.

    On ne redémarre pas pour rien : si l'interface stockée correspond déjà à
    l'interface détectée ET si l'override wsdd2 existe avec la bonne commande,
    le garde-fou ne fait rien.
    """
    reasons: list[str] = []
    if detected_interface and stored_interface != detected_interface:
        reasons.append(f"interface changée : {stored_interface or '(vide)'} -> {detected_interface}")

    if cfg.enable_wsdd and service_state("wsdd2.service")["exists"]:
        expected = render_wsdd2_override(cfg)
        current = WSDD2_OVERRIDE.read_text(encoding="utf-8", errors="ignore") if WSDD2_OVERRIDE.exists() else ""
        if current != expected:
            reasons.append("override wsdd2 absent ou différent")

    return bool(reasons), reasons


def samba_interface_startup_guard_stream(reason: str = "flask-startup") -> Iterator[str]:
    """Réenregistre Samba/WSDD au démarrage de l'interface Flask.

    Windows peut perdre l'annonce WSD après un redémarrage du serveur alors
    même que smbd et wsdd2 sont actifs. Le bouton ``OK`` de l'interface règle
    ce cas parce qu'il réapplique la configuration puis redémarre les services.
    Le démarrage de Yoleo reproduit donc volontairement cette action complète,
    y compris quand l'interface réseau et l'override wsdd2 semblent inchangés.
    """
    yield f"[{now_stamp()}] Réenregistrement Samba/WSDD ({reason})\n"

    root_error = require_root_text()
    if root_error:
        yield "Skip : " + root_error + "\n"
        return

    conf_path = resolve_samba_conf_path()
    if not conf_path.exists():
        yield f"Skip : samba.conf absent ({conf_path}).\n"
        return

    stored_interface = read_stored_samba_interface(conf_path)
    detected_interface = samba_autodetected_interface(stored_interface or "br0")
    if not detected_interface:
        yield "Skip : aucune interface LAN détectée.\n"
        return

    cfg = load_samba_config(conf_path)
    cfg.interface = detected_interface

    needs_fix, reasons = samba_interface_needs_autofix(cfg, stored_interface, detected_interface)
    if needs_fix:
        yield "Action : réparation requise : " + "; ".join(reasons) + "\n"
    else:
        yield f"Action : réapplication au démarrage (interface {detected_interface}).\n"
    yield from apply_and_restart_samba_stream(cfg, update_passwords=False)


_samba_interface_startup_guard_started = False


def start_samba_interface_startup_guard_once(delay_seconds: float = 3.0) -> None:
    """Lance un réenregistrement Samba/WSDD une seule fois par lancement Flask."""
    global _samba_interface_startup_guard_started
    if _samba_interface_startup_guard_started:
        return
    _samba_interface_startup_guard_started = True

    if __name__ == "__main__" or os.environ.get("YOLEO_SKIP_SAMBA_INTERFACE_GUARD") == "1":
        return

    def worker() -> None:
        if delay_seconds > 0:
            time.sleep(delay_seconds)

        fd = _acquire_samba_interface_lock()
        if fd is None:
            _append_samba_boot_log(f"[{now_stamp()}] Skip : garde-fou déjà en cours dans un autre worker.")
            return

        try:
            lines: list[str] = []
            for chunk in samba_interface_startup_guard_stream():
                text = str(chunk or "")
                lines.append(text.rstrip("\n"))
            _append_samba_boot_log("\n".join(line for line in lines if line))
        except Exception as exc:
            _append_samba_boot_log(f"[{now_stamp()}] ERREUR garde-fou Samba interface : {exc}")
        finally:
            _release_samba_interface_lock(fd)

    threading.Thread(target=worker, name="yoleo-samba-interface-guard", daemon=True).start()


def restart_scripts_value(settings: dict[str, str] | None = None) -> str:
    """Commande de redémarrage post-sauvegarde du partage.conf.

    Le réglage demandé est volontairement en minuscules dans partage.conf :
      restart_scripts=system.sh -restart

    Les chemins relatifs sont résolus depuis BASE_DIR, donc depuis le dossier
    où vivent app.py, partage.py et system.sh.
    """
    data = settings if isinstance(settings, dict) else read_partage_config()
    value = data.get("restart_scripts") or data.get("RESTART_SCRIPTS") or ""
    return strip_conf_quotes(str(value or "")).strip()


def split_restart_scripts(value: str) -> list[str]:
    """Retourne une liste de commandes simples.

    Pour rester confortable dans un fichier KEY=VALUE, on accepte :
      restart_scripts=system.sh -restart
      restart_scripts=cmd1 arg; cmd2 arg
    """
    commands: list[str] = []
    for raw in re.split(r"[;\n\r]+", value or ""):
        line = raw.strip()
        if line:
            commands.append(line)
    return commands


def command_parts_from_setting(command: str) -> list[str]:
    import shlex
    parts = shlex.split(command)
    if not parts:
        return []

    exe = parts[0]
    exe_path = Path(exe)
    if not exe_path.is_absolute() and ("/" in exe or exe.endswith(".sh") or exe == "system.sh"):
        exe_path = (BASE_DIR / exe_path).resolve()

    if str(exe_path).endswith(".sh"):
        return ["/bin/bash", str(exe_path), *parts[1:]]
    return [str(exe_path), *parts[1:]]


def schedule_restart_scripts(settings: dict[str, str] | None = None) -> tuple[bool, str]:
    """Lance restart_scripts en différé pour ne pas tuer la réponse HTTP.

    Si system.sh redémarre flask-system.service immédiatement, le navigateur
    peut ne jamais recevoir le JSON de retour. On détache donc un petit shell
    avec sleep 1, puis on lance la commande depuis BASE_DIR.
    """
    import shlex

    value = restart_scripts_value(settings)
    if not value:
        return False, "restart_scripts vide : aucun script de redémarrage lancé."

    commands = split_restart_scripts(value)
    if not commands:
        return False, "restart_scripts vide : aucun script de redémarrage lancé."

    settings_for_log = settings if isinstance(settings, dict) else read_partage_config()
    log_dir = resolve_module_path(settings_for_log.get("SAV_PARTAGE_BACKUP", "../backups"))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        log_dir = Path("/tmp")
    log_path = log_dir / f"restart_scripts.{now_stamp()}.log"

    launched: list[str] = []
    errors: list[str] = []
    for command in commands:
        try:
            parts = command_parts_from_setting(command)
            if not parts:
                continue
            shell_cmd = (
                "sleep 1; "
                f"cd {shlex.quote(str(BASE_DIR))} && "
                f"{shlex.join(parts)} >> {shlex.quote(str(log_path))} 2>&1"
            )
            subprocess.Popen(
                ["/bin/sh", "-c", shell_cmd],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            launched.append(command)
        except Exception as exc:
            errors.append(f"{command} : {exc}")

    if errors and not launched:
        return False, "Erreur restart_scripts :\n" + "\n".join(errors)
    msg = "restart_scripts planifié : " + ", ".join(launched)
    msg += f"\nLog restart : {log_path}"
    if errors:
        msg += "\nErreurs :\n" + "\n".join(errors)
    return bool(launched), msg


def require_root_text() -> str | None:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        return "ERREUR : cette action doit être lancée en root. Lance ton Flask host avec les droits nécessaires ou via le service système prévu."
    return None


def service_state(service: str) -> dict:
    if not command_exists("systemctl"):
        return {"name": service, "exists": False, "active": "systemctl absent", "enabled": "systemctl absent"}
    exists = run_capture(["systemctl", "list-unit-files", service, "--no-legend"]).stdout.strip()
    active = run_capture(["systemctl", "is-active", service]).stdout.strip() or "unknown"
    enabled = run_capture(["systemctl", "is-enabled", service]).stdout.strip() or "unknown"
    return {
        "name": service,
        "exists": bool(exists),
        "active": active,
        "enabled": enabled,
        "ok": active == "active",
    }


def systemctl_cmd(args: list[str]) -> tuple[int, str]:
    if not command_exists("systemctl"):
        return 0, "systemctl introuvable, action ignorée : " + " ".join(args)
    res = run_capture(["systemctl", *args])
    return res.returncode, res.stdout


def detect_wsdd_service() -> str | None:
    for service in DISTRO_WSDD_SERVICES:
        if service_state(service)["exists"]:
            return service
    return None


def resolve_samba_conf_path(raw: str | None = None) -> Path:
    """Chemin du samba.conf source.

    La source normale est partage.conf :
      SAMBA_CONF=../conf/samba.conf

    Les chemins relatifs sont résolus par rapport au dossier de partage.py.
    On garde aussi PARTAGE_SAMBA_CONF comme override de secours.
    """
    if raw:
        return resolve_module_path(raw)
    return configured_path("SAMBA_CONF", nas_conf_file("samba.conf"))


def get_nfs_exports_file() -> Path:
    return configured_path("NFS_EXPORTS_FILE", "/etc/exports.d/nfs.exports")


def get_nfs_server_conf_file() -> Path:
    return configured_path("NFS_SERVER_CONF", "../conf/nfs_server.conf")


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    except Exception as exc:
        return f"ERREUR lecture {path} : {exc}"


def backup_dir_for(path: Path) -> Path:
    """Dossier de sauvegarde configurable depuis partage.conf.

    Avant, les .bak étaient créés à côté des fichiers modifiés, donc ../conf
    finissait vite pollué. Maintenant, les sauvegardes partent dans les dossiers
    SAV_*_BACKUP du fichier partage.conf.
    """
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path

    conf = read_partage_config()
    nfs_path = get_nfs_exports_file()
    nfs_conf_path = get_nfs_server_conf_file()
    partage_path = get_partage_config_path()

    try:
        if resolved == partage_path.resolve():
            return resolve_module_path(conf.get("SAV_PARTAGE_BACKUP", "../backups"))
    except Exception:
        pass

    try:
        if resolved == nfs_conf_path.resolve() or resolved == nfs_path.resolve() or "nfs" in resolved.name.lower() or "exports" in resolved.name.lower():
            return resolve_module_path(conf.get("SAV_NFS_BACKUP", "../backups"))
    except Exception:
        pass

    return resolve_module_path(conf.get("SAV_SAMBA_BACKUP", "../backups"))


def backup_file(path: Path, reason: str = "bak") -> Path | None:
    if not path.exists():
        return None
    backup_dir = backup_dir_for(path)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{path.name}.{reason}.{now_stamp()}"
    shutil.copy2(path, backup)
    return backup


def write_if_changed(path: Path, content: str, *, mode: int = 0o644, backup_unmanaged_marker: str | None = None) -> tuple[bool, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        current = path.read_text(encoding="utf-8", errors="ignore")
        if current == content:
            return False, f"Inchangé : {path}"
        if backup_unmanaged_marker and backup_unmanaged_marker not in current:
            backup = backup_file(path, "unmanaged")
            msg = f"Backup fichier non géré : {backup}\n"
        else:
            backup = backup_file(path, "bak")
            msg = f"Backup : {backup}\n"
    else:
        msg = f"Création : {path}\n"

    path.write_text(content, encoding="utf-8")
    try:
        path.chmod(mode)
    except OSError:
        pass
    return True, msg + f"Écrit : {path}"


def normalize_samba_share_type(value: str) -> str:
    value = (value or "normal").strip().lower()
    if value in {"root", "admin", "777", "force_root", "force-root"}:
        return "root"
    return "normal"


def normalize_yes_no(value: str, default: str = "no") -> str:
    if str(value).strip().lower() in {"1", "yes", "true", "on", "oui", "y"}:
        return "yes"
    if str(value).strip().lower() in {"0", "no", "false", "off", "non", "n"}:
        return "no"
    return default


def normalize_share_owner(value: str | None) -> str:
    value = (value or "").strip()
    return value if USER_RE.match(value) else ""


SAMBA_ACCESS_MODES = {"public", "secure", "private"}


def normalize_samba_access_mode(value: str | None, default: str = "private") -> str:
    raw = str(value or "").strip().lower()
    if not raw and default == "":
        return ""
    aliases = {
        "public": "public",
        "publique": "public",
        "securise": "secure",
        "securisee": "secure",
        "secure": "secure",
        "secured": "secure",
        "private": "private",
        "prive": "private",
        "privee": "private",
    }
    return aliases.get(raw, default if default in SAMBA_ACCESS_MODES else "private")


def normalize_samba_user_list(value) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = re.split(r"[\s,;]+", str(value or ""))
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        name = str(item or "").strip()
        if not name or not USER_RE.match(name) or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def samba_user_list_conf(users: list[str]) -> str:
    return ",".join(normalize_samba_user_list(users))


def infer_samba_access_mode(data) -> str:
    explicit = normalize_samba_access_mode(data.get("access_mode", ""), "")
    if explicit:
        return explicit
    guest_ok = normalize_yes_no(data.get("guest_ok", "no"), "no")
    read_only = normalize_yes_no(data.get("read_only", "no"), "no")
    writable = normalize_yes_no(data.get("writable", "yes"), "yes")
    if guest_ok == "yes":
        return "public" if read_only == "yes" or writable == "no" else "secure"
    return "private"


def legacy_samba_access_users(data, mode: str) -> tuple[list[str], list[str]]:
    read_users = normalize_samba_user_list(data.get("read_users", ""))
    write_users = normalize_samba_user_list(data.get("write_users", ""))
    if read_users or write_users:
        return read_users, write_users

    owner = normalize_share_owner(data.get("owner", data.get("user", "")))
    if not owner:
        return [], []
    read_only = normalize_yes_no(data.get("read_only", "no"), "no")
    writable = normalize_yes_no(data.get("writable", "yes"), "yes")
    if writable == "yes" and not (read_only == "yes" and mode == "public"):
        return [], [owner]
    return [owner], []


def samba_share_allows_root(share: SambaShare) -> bool:
    return (
        normalize_samba_access_mode(share.access_mode, "private") == "private"
        and bool(normalize_samba_user_list(share.write_users))
    )


def linux_user_is_normal_for_samba(pw: pwd.struct_passwd) -> bool:
    """Utilisateur humain NAS utilisable automatiquement par Samba.

    Samba ne doit pas recréer une deuxième gestion des comptes : on reprend les
    comptes Linux normaux déjà gérés dans le module Utilisateurs. Root et les
    comptes système restent exclus de la sélection automatique.
    """
    if pw.pw_name == "root" or pw.pw_name in SAMBA_EXCLUDED_LINUX_USERS:
        return False
    return SAMBA_HUMAN_UID_MIN <= int(pw.pw_uid) <= SAMBA_HUMAN_UID_MAX


def samba_user_from_passwd(pw: pwd.struct_passwd) -> SambaUser:
    return SambaUser(
        name=pw.pw_name,
        password="",
        uid=pw.pw_uid,
        gid=pw.pw_gid,
        shell=pw.pw_shell or "/usr/sbin/nologin",
        home=pw.pw_dir or "/nonexistent",
    )


def auto_samba_users_from_linux(cfg: SambaConfig | None = None) -> list[SambaUser]:
    """Liste Samba = vrais utilisateurs Linux normaux, plus propriétaires déjà utilisés.

    Les mots de passe ne sont pas lus ici : Linux ne permet pas de récupérer le
    mot de passe en clair. Ils sont synchronisés au moment où le module
    Utilisateurs crée/change le mot de passe via smbpasswd.
    """
    wanted = set()
    if cfg is not None:
        for share in cfg.shares:
            if share.owner:
                wanted.add(share.owner)
            wanted.update(share.read_users or [])
            wanted.update(share.write_users or [])

    users: dict[str, SambaUser] = {}
    for pw in pwd.getpwall():
        if linux_user_is_normal_for_samba(pw) or pw.pw_name in wanted:
            users[pw.pw_name] = samba_user_from_passwd(pw)

    return sorted(users.values(), key=lambda u: (u.uid, u.name.lower()))


def sync_samba_users_from_linux(cfg: SambaConfig) -> SambaConfig:
    cfg.users = auto_samba_users_from_linux(cfg)
    user_names = [user.name for user in cfg.users]
    for share in cfg.shares:
        share.access_mode = normalize_samba_access_mode(share.access_mode, "private")
        share.read_users = normalize_samba_user_list(share.read_users)
        share.write_users = normalize_samba_user_list(share.write_users)
        if share.access_mode == "public":
            share.read_users = []
            share.write_users = []
            share.recycle_bin = "no"
        elif share.access_mode == "secure":
            covered = set(share.read_users) | set(share.write_users)
            for name in user_names:
                if name not in covered:
                    share.read_users.append(name)
                    covered.add(name)
        if share.share_type == "root" and not samba_share_allows_root(share):
            share.share_type = "normal"
    return cfg


def load_samba_config(conf_path: Path | None = None) -> SambaConfig:
    conf_path = (conf_path or resolve_samba_conf_path()).resolve()
    if not conf_path.exists():
        cfg = SambaConfig(conf_path=conf_path)
        cfg.interface = samba_autodetected_interface(cfg.interface)
        return sync_samba_users_from_linux(cfg)

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(conf_path, encoding="utf-8")

    g = parser["global"] if "global" in parser else {}
    cfg = SambaConfig(
        conf_path=conf_path,
        workgroup=str(g.get("workgroup", "WORKGROUP")),
        server_string=str(g.get("server_string", "Host Samba")),
        netbios_name=windows_discovery_name(str(g.get("netbios_name", "Samba"))),
        # Interface auto-détectée : l'ancien champ reste dans samba.conf,
        # mais il ne pilote plus l'UI ni wsdd si la machine a changé d'interface.
        interface=samba_autodetected_interface(str(g.get("interface", "br0"))),
        smb_conf=Path(str(g.get("smb_conf", "/etc/samba/smb.conf"))),
        log_file=str(g.get("log_file", "/var/log/samba/log.%m")),
        max_log_size=str(g.get("max_log_size", "50")),
        min_protocol=str(g.get("min_protocol", "SMB2")),
        enable_wsdd=bool_value(g.get("enable_wsdd"), True),
        wsdd_name=windows_discovery_name(str(g.get("wsdd_name", g.get("netbios_name", "Samba")))),
        create_missing_dirs=bool_value(g.get("create_missing_dirs"), True),
    )

    for section in parser.sections():
        if section.startswith("user:"):
            name = section.split(":", 1)[1].strip()
            data = parser[section]
            try:
                user = SambaUser(
                    name=name,
                    password=data.get("password", ""),
                    uid=int(data.get("uid", "0")),
                    gid=int(data.get("gid", "0")),
                    shell=data.get("shell", "/usr/sbin/nologin"),
                    home=data.get("home", "/nonexistent"),
                )
            except ValueError:
                continue
            cfg.users.append(user)
        elif section.startswith("share:"):
            name = section.split(":", 1)[1].strip()
            data = parser[section]
            if not name or not data.get("path"):
                continue
            access_mode = infer_samba_access_mode(data)
            read_users, write_users = legacy_samba_access_users(data, access_mode)
            cfg.shares.append(SambaShare(
                name=name,
                path=Path(data.get("path", "")),
                share_type=normalize_samba_share_type(data.get("type", "normal")),
                guest_ok=normalize_yes_no(data.get("guest_ok", "no"), "no"),
                read_only=normalize_yes_no(data.get("read_only", "no"), "no"),
                browsable=normalize_yes_no(data.get("browsable", "yes"), "yes"),
                writable=normalize_yes_no(data.get("writable", "yes"), "yes"),
                recycle_bin=normalize_yes_no(data.get("recycle_bin", data.get("recycle", "no")), "no"),
                owner=normalize_share_owner(data.get("owner", data.get("user", ""))),
                access_mode=access_mode,
                read_users=read_users,
                write_users=write_users,
            ))
    return sync_samba_users_from_linux(cfg)


def validate_samba_config(cfg: SambaConfig) -> list[str]:
    errors: list[str] = []
    for label, value in (
        ("interface WSDD", cfg.interface),
        ("nom WSDD", cfg.wsdd_name),
        ("nom NetBIOS", cfg.netbios_name),
        ("workgroup", cfg.workgroup),
    ):
        if value and not WSDD_ARG_RE.match(value):
            errors.append(f"{label} invalide : {value}")
    for user in cfg.users:
        if not USER_RE.match(user.name):
            errors.append(f"Utilisateur invalide : {user.name}")
        if user.uid <= 0 or user.gid <= 0:
            errors.append(f"UID/GID invalide pour {user.name}")
        try:
            pw = pwd.getpwnam(user.name)
            if pw.pw_uid != user.uid or pw.pw_gid != user.gid:
                errors.append(f"Utilisateur {user.name} existe, mais UID/GID ne correspondent pas à /etc/passwd ({pw.pw_uid}:{pw.pw_gid})")
        except KeyError:
            errors.append(f"Utilisateur Linux inexistant : {user.name}. Crée-le d'abord dans le module Utilisateurs Linux.")
    for share in cfg.shares:
        share_label = share.name or "ce partage"
        path_text = str(share.path or "").strip()
        if not share.name:
            errors.append("Nom de partage obligatoire.")
        elif not SHARE_NAME_RE.match(share.name):
            errors.append(f"Nom de partage invalide : {share.name}")
        if not path_text or path_text == ".":
            errors.append(f"Dossier à partager obligatoire pour {share_label}.")
        elif not path_text.startswith("/"):
            errors.append(f"Chemin non absolu pour {share_label} : {share.path}")
        for username in [*(share.read_users or []), *(share.write_users or [])]:
            try:
                pwd.getpwnam(username)
            except KeyError:
                errors.append(f"Utilisateur Samba inexistant pour {share_label} : {username}")
    return errors


def samba_config_from_payload(payload: dict) -> SambaConfig:
    conf_path = resolve_samba_conf_path(payload.get("conf_path") or None)
    global_cfg = payload.get("global") or {}
    cfg = SambaConfig(
        conf_path=conf_path,
        workgroup=(global_cfg.get("workgroup") or "WORKGROUP").strip(),
        server_string=(global_cfg.get("server_string") or "Host Samba").strip(),
        netbios_name=windows_discovery_name(global_cfg.get("netbios_name") or "Samba"),
        # L'interface n'est plus un réglage utilisateur : on l'auto-détecte
        # à chaque enregistrement/application pour éviter les valeurs cassantes.
        interface=samba_autodetected_interface(str(global_cfg.get("interface") or "br0")),
        smb_conf=Path((global_cfg.get("smb_conf") or "/etc/samba/smb.conf").strip()),
        log_file=(global_cfg.get("log_file") or "/var/log/samba/log.%m").strip(),
        max_log_size=(global_cfg.get("max_log_size") or "50").strip(),
        min_protocol=(global_cfg.get("min_protocol") or "SMB2").strip(),
        enable_wsdd=bool_value(global_cfg.get("enable_wsdd"), True),
        wsdd_name=windows_discovery_name(global_cfg.get("wsdd_name") or global_cfg.get("netbios_name") or "Samba"),
        create_missing_dirs=bool_value(global_cfg.get("create_missing_dirs"), True),
    )

    for raw in payload.get("users") or []:
        name = (raw.get("name") or "").strip()
        if not name:
            continue
        try:
            cfg.users.append(SambaUser(
                name=name,
                password=(raw.get("password") or "").strip(),
                uid=int(raw.get("uid") or 0),
                gid=int(raw.get("gid") or 0),
                shell=(raw.get("shell") or "/usr/sbin/nologin").strip(),
                home=(raw.get("home") or "/nonexistent").strip(),
            ))
        except ValueError:
            cfg.users.append(SambaUser(name=name, password="", uid=0, gid=0))

    for raw in payload.get("shares") or []:
        name = (raw.get("name") or "").strip()
        path = (raw.get("path") or "").strip()
        if not name and not path:
            continue
        access_mode = normalize_samba_access_mode(raw.get("access_mode", raw.get("mode", "private")))
        read_users = normalize_samba_user_list(raw.get("read_users", ""))
        write_users = normalize_samba_user_list(raw.get("write_users", ""))
        if access_mode == "secure":
            covered_users = set(read_users) | set(write_users)
            for user in cfg.users:
                if user.name not in covered_users:
                    read_users.append(user.name)
                    covered_users.add(user.name)
        legacy_owner = normalize_share_owner(raw.get("owner", ""))
        owner = legacy_owner or (write_users[0] if write_users else (read_users[0] if read_users else ""))
        guest_ok = "yes" if access_mode == "public" else "no"
        read_only = "yes"
        writable = "no"
        if access_mode in {"secure", "private"} and write_users:
            writable = "yes"
        recycle_bin = normalize_yes_no(raw.get("recycle_bin", "no"), "no")
        if access_mode == "public":
            recycle_bin = "no"
        share_type = normalize_samba_share_type(raw.get("type", "normal"))
        if access_mode != "private" or not write_users:
            share_type = "normal"
        cfg.shares.append(SambaShare(
            name=name,
            path=Path(path),
            share_type=share_type,
            guest_ok=guest_ok,
            read_only=read_only,
            browsable=normalize_yes_no(raw.get("browsable", "yes"), "yes"),
            writable=writable,
            recycle_bin=recycle_bin,
            owner=owner,
            access_mode=access_mode,
            read_users=read_users,
            write_users=write_users,
        ))
    return sync_samba_users_from_linux(cfg)


def render_samba_source_conf(cfg: SambaConfig) -> str:
    lines: list[str] = [
        "# samba.conf - configuration host pour partage.py",
        "# Ce fichier est le fichier source lisible depuis l'interface Flask.",
        "# Il sert à générer /etc/samba/smb.conf puis à piloter smbd/nmbd/wsdd.",
        "",
        "[global]",
        f"workgroup = {cfg.workgroup}",
        f"server_string = {cfg.server_string}",
        f"netbios_name = {cfg.netbios_name}",
        f"interface = {cfg.interface}",
        f"smb_conf = {cfg.smb_conf}",
        f"log_file = {cfg.log_file}",
        f"max_log_size = {cfg.max_log_size}",
        f"min_protocol = {cfg.min_protocol}",
        f"enable_wsdd = {yesno(cfg.enable_wsdd)}",
        f"wsdd_name = {cfg.wsdd_name}",
        f"create_missing_dirs = {yesno(cfg.create_missing_dirs)}",
        "",
    ]
    for user in cfg.users:
        lines.extend([
            f"[user:{user.name}]",
            f"uid = {user.uid}",
            f"gid = {user.gid}",
            f"shell = {user.shell}",
            f"home = {user.home}",
            "",
        ])
    for share in cfg.shares:
        lines.extend([
            f"[share:{share.name}]",
            f"path = {share.path}",
            f"type = {share.share_type}",
            f"guest_ok = {share.guest_ok}",
            f"read_only = {share.read_only}",
            f"browsable = {share.browsable}",
            f"writable = {share.writable}",
            f"recycle_bin = {share.recycle_bin}",
            f"owner = {share.owner}",
            f"access_mode = {share.access_mode}",
            f"read_users = {samba_user_list_conf(share.read_users)}",
            f"write_users = {samba_user_list_conf(share.write_users)}",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def samba_owner_group_token(owner: str) -> str:
    """Retourne le groupe Unix primaire du propriétaire au format Samba @groupe."""
    owner = (owner or "").strip()
    if not owner:
        return ""
    try:
        pw = pwd.getpwnam(owner)
        group_name = grp.getgrgid(pw.pw_gid).gr_name
    except Exception:
        return ""
    group_name = (group_name or "").strip()
    if not group_name:
        return ""
    return f"@{group_name}"


def samba_share_user_scope(share: SambaShare, cfg: SambaConfig) -> str:
    """Liste des comptes autorisés côté Samba pour un partage.

    Cas NAS volontaire : read_only=yes + writable=yes sans invité signifie
    "propriétaire en écriture, groupe Linux du propriétaire en lecture".
    On autorise donc le propriétaire et son groupe Unix primaire côté Samba,
    puis on met seulement le propriétaire dans write list. Les droits Linux
    récursifs 750/640 font ensuite respecter la même règle sur le disque.
    """
    fallback_valid_users = " ".join(user.name for user in cfg.users).strip()
    if share.guest_ok == "yes":
        return ""
    if share.owner and share.read_only == "yes" and share.writable == "yes":
        group_token = samba_owner_group_token(share.owner)
        return " ".join(x for x in (share.owner, group_token) if x).strip() or share.owner
    if share.owner:
        return share.owner
    return fallback_valid_users


def samba_share_smb_flags(share: SambaShare) -> dict[str, str]:
    """Traduit les colonnes UI en directives Samba non contradictoires."""
    # Invité écriture : public complet.
    if share.guest_ok == "yes" and share.read_only != "yes" and share.writable == "yes":
        return {"read_only": "no", "writable": "yes"}

    # Propriétaire écriture + autres comptes authentifiés en lecture.
    if share.guest_ok != "yes" and share.owner and share.read_only == "yes" and share.writable == "yes":
        return {"read_only": "yes", "writable": "no", "write_list": share.owner}

    # Dès qu'une des deux colonnes demande lecture seule, on force Samba en
    # lecture seule pour éviter le couple brut read only=yes + writable=yes.
    if share.read_only == "yes" or share.writable == "no":
        return {"read_only": "yes", "writable": "no"}

    return {"read_only": "no", "writable": "yes"}


def samba_share_smb_flags(share: SambaShare, cfg: SambaConfig | None = None) -> dict[str, str]:
    """Traduit Public/Securise/Prive en directives Samba uniquement."""
    mode = normalize_samba_access_mode(share.access_mode, "private")
    cfg_users = normalize_samba_user_list([user.name for user in (cfg.users if cfg else [])])
    read_users = normalize_samba_user_list(share.read_users)
    write_users = normalize_samba_user_list(share.write_users)
    write_set = set(write_users)
    read_only_users = [name for name in read_users if name not in write_set]
    listed_users = read_only_users + [name for name in write_users if name not in set(read_only_users)]

    if mode == "public":
        return {
            "guest_ok": "yes",
            "read_only": "yes",
            "writable": "no",
            "valid_users": "",
            "write_list": "",
        }

    if mode == "secure":
        valid_users = cfg_users or listed_users
        return {
            "guest_ok": "no",
            "read_only": "yes",
            "writable": "no",
            "valid_users": " ".join(valid_users),
            "write_list": " ".join(write_users),
        }

    valid_users = listed_users or ["__yoleo_no_access__"]
    return {
        "guest_ok": "no",
        "read_only": "yes",
        "writable": "no",
        "valid_users": " ".join(valid_users),
        "write_list": " ".join(write_users),
    }


def render_smb_conf(cfg: SambaConfig) -> str:
    lines = [
        MANAGED_MARKER,
        f"# Source: {cfg.conf_path}",
        "# Généré depuis le module Flask Partage.",
        "",
        "[global]",
        f"    workgroup = {cfg.workgroup}",
        f"    server string = {cfg.server_string}",
        f"    netbios name = {cfg.netbios_name}",
        f"    interfaces = {samba_bound_interfaces(cfg)}",
        "    bind interfaces only = yes",
        "    security = user",
        "    map to guest = Bad User",
        "    guest account = nobody",
        f"    log file = {cfg.log_file}",
        f"    max log size = {cfg.max_log_size}",
        f"    min protocol = {cfg.min_protocol}",
        "    change notify = yes",
        "    kernel change notify = yes",
        "    notify:allow_extended_notifications = yes",
        "    oplocks = no",
        "    level2 oplocks = no",
        "    strict sync = yes",
        "    sync always = yes",
        "    load printers = no",
        "    printing = bsd",
        "    printcap name = /dev/null",
        "    disable spoolss = yes",
        "",
    ]

    for share in cfg.shares:
        smb_flags = samba_share_smb_flags(share, cfg)
        valid_users = smb_flags.get("valid_users", "")
        lines.extend([
            f"[{share.name}]",
            f"    path = {share.path}",
            f"    guest ok = {smb_flags.get('guest_ok', share.guest_ok)}",
            f"    read only = {smb_flags.get('read_only', 'yes')}",
            f"    browsable = {share.browsable}",
            f"    writable = {smb_flags.get('writable', 'no')}",
        ])
        if valid_users:
            lines.append(f"    valid users = {valid_users}")
        if smb_flags.get("write_list"):
            lines.append(f"    write list = {smb_flags['write_list']}")
        vfs_objects = []
        if share.share_type != "root":
            vfs_objects.extend(["fruit", "streams_xattr"])
        if share.recycle_bin == "yes":
            vfs_objects.append("recycle")

        if share.share_type == "root":
            lines.extend([
                "    force user = root",
                "    force group = root",
                "    create mask = 0777",
                "    directory mask = 0777",
            ])
        else:
            lines.append("    inherit permissions = yes")

        if vfs_objects:
            lines.append(f"    vfs objects = {' '.join(vfs_objects)}")

        if share.recycle_bin == "yes":
            lines.extend([
                "    recycle:repository = .Recycle.Bin/%U",
                "    recycle:keeptree = yes",
                "    recycle:versions = yes",
                "    recycle:touch = yes",
                "    recycle:directory_mode = 0777",
                "    recycle:subdir_mode = 0777",
                "    recycle:exclude = *.tmp *.temp *.o *.obj ~$*",
                "    recycle:exclude_dir = /tmp /temp /cache",
            ])

        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_apply_service(cfg: SambaConfig) -> str:
    python_bin = sys.executable or "/usr/bin/python3"
    script = Path(__file__).resolve()
    return textwrap.dedent(f"""\
    [Unit]
    Description=Applique la configuration Samba host générée par partage.py
    After=local-fs.target network-pre.target
    Before=smbd.service nmbd.service

    [Service]
    Type=oneshot
    ExecStart={python_bin} {script} --samba-apply --conf {cfg.conf_path}
    RemainAfterExit=yes

    [Install]
    WantedBy=multi-user.target
    """)


def render_wsdd2_override(cfg: SambaConfig) -> str:
    module_dir = Path(__file__).resolve().parent
    wsdd_script = module_dir / "partage_parts" / "yoleo_wsdd.py"
    if not wsdd_script.exists():
        wsdd_script = module_dir / "yoleo_wsdd.py"
    cmd = [sys.executable or "/usr/bin/python3", str(wsdd_script)]
    wsdd_host = windows_discovery_name(cfg.wsdd_name or cfg.netbios_name or "Samba")
    wsdd_netbios = windows_discovery_name(cfg.netbios_name or wsdd_host, max_length=15, upper=True)
    wsdd_workgroup = windows_discovery_name(cfg.workgroup or "WORKGROUP", max_length=15, upper=True)
    if cfg.interface:
        cmd.extend(["--interface", cfg.interface])
    if wsdd_host:
        cmd.extend(["--host", wsdd_host])
    if wsdd_netbios:
        cmd.extend(["--netbios", wsdd_netbios])
    if wsdd_workgroup:
        cmd.extend(["--workgroup", wsdd_workgroup])

    return textwrap.dedent(f"""\
    # Managed by partage.py
    # Use Yoleo WSD compatibility mode: UDP discovery on 3702, metadata on TCP 5357.
    [Unit]
    After=network-online.target smbd.service nmbd.service
    Wants=network-online.target smbd.service nmbd.service

    [Service]
    Restart=always
    RestartSec=5
    ExecStart=
    ExecStart={shell_join(cmd)}
    """)


def render_wsdd_refresh_service() -> str:
    return textwrap.dedent("""\
    # Managed by partage.py
    [Unit]
    Description=Rafraichit la decouverte Windows Samba sans couper WSDD
    After=network-online.target smbd.service nmbd.service wsdd2.service wsdd.service
    Wants=network-online.target

    [Service]
    Type=oneshot
    ExecStart=/bin/sh -c '/bin/systemctl reload-or-restart wsdd2.service >/dev/null 2>&1 || /bin/systemctl reload-or-restart wsdd.service >/dev/null 2>&1 || true'
    """)


def render_wsdd_refresh_timer() -> str:
    return textwrap.dedent("""\
    # Managed by partage.py
    [Unit]
    Description=Rafraichit periodiquement la decouverte Windows Samba

    [Timer]
    OnBootSec=3min
    OnUnitActiveSec=10min
    AccuracySec=1min
    Persistent=true
    Unit=samba-wsdd-refresh.service

    [Install]
    WantedBy=timers.target
    """)


def configure_wsdd_refresh_units(enable: bool = False) -> tuple[bool, list[str]]:
    """Supprime l'ancien timer périodique WSDD.

    Codex avait ajouté ``samba-wsdd-refresh.timer`` avec un redémarrage toutes
    les 10 minutes. Sur un NAS qui sert des vidéos ou des copies longues, une
    boucle systemd qui touche aux services de découverte Windows est une mauvaise
    idée : Samba doit rester stable après l'application manuelle des réglages.

    Le paramètre ``enable`` est conservé pour compatibilité d'appel, mais il est
    volontairement ignoré : cette unité ne doit plus être créée ni activée.
    """
    service_path = Path("/etc/systemd/system") / WSDD_REFRESH_SERVICE
    timer_path = Path("/etc/systemd/system") / WSDD_REFRESH_TIMER
    messages: list[str] = []
    changed = False

    if command_exists("systemctl"):
        for unit in (WSDD_REFRESH_TIMER, WSDD_REFRESH_SERVICE):
            systemctl_cmd(["stop", unit])
            systemctl_cmd(["disable", unit])

    for path in (timer_path, service_path):
        if path.exists():
            backup = backup_file(path, "removed")
            path.unlink(missing_ok=True)
            changed = True
            messages.append(f"Unite WSDD refresh supprimee : {path} (backup {backup})")

    return changed, messages


def configure_wsdd_service(cfg: SambaConfig) -> tuple[bool, list[str]]:
    messages: list[str] = []
    changed = False

    if not service_state("wsdd2.service")["exists"]:
        if WSDD2_OVERRIDE.exists():
            backup = backup_file(WSDD2_OVERRIDE, "removed")
            WSDD2_OVERRIDE.unlink(missing_ok=True)
            changed = True
            messages.append(f"Override wsdd2 retiré : {WSDD2_OVERRIDE} (backup {backup})")
        refresh_changed, refresh_messages = configure_wsdd_refresh_units(False)
        changed = changed or refresh_changed
        messages.extend(refresh_messages)
        return changed, messages

    if not cfg.enable_wsdd:
        if WSDD2_OVERRIDE.exists():
            backup = backup_file(WSDD2_OVERRIDE, "disabled")
            WSDD2_OVERRIDE.unlink(missing_ok=True)
            changed = True
            messages.append(f"Override wsdd2 retiré : {WSDD2_OVERRIDE} (backup {backup})")
        refresh_changed, refresh_messages = configure_wsdd_refresh_units(False)
        changed = changed or refresh_changed
        messages.extend(refresh_messages)
        return changed, messages

    did_change, msg = write_if_changed(WSDD2_OVERRIDE, render_wsdd2_override(cfg), mode=0o644)
    changed = changed or did_change
    messages.append(msg)

    # Pas de refresh périodique : WSDD est lancé/stabilisé avec Samba, puis on
    # ne le redémarre plus en boucle pendant les lectures/copies SMB.
    refresh_changed, refresh_messages = configure_wsdd_refresh_units(False)
    changed = changed or refresh_changed
    messages.extend(refresh_messages)
    return changed, messages


def existing_group_by_gid(gid: int) -> str | None:
    res = run_capture(["getent", "group", str(gid)])
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip().split(":", 1)[0]
    return None


def user_exists(name: str) -> bool:
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def list_linux_users_for_ui() -> list[dict]:
    """Liste les vrais utilisateurs Linux depuis /etc/passwd.

    Le module Samba ne doit pas inventer un utilisateur Linux :
    on sélectionne ici un compte qui existe déjà. Pour éviter une liste
    illisible, on garde les comptes humains classiques UID >= 1000, root,
    et les comptes avec home dans /home. Un user déjà dans samba.conf reste
    affichable même s'il ne passe pas ce filtre, via enrich_samba_users_for_ui().
    """
    users: list[dict] = []
    seen: set[str] = set()
    for pw in pwd.getpwall():
        if pw.pw_name in seen:
            continue
        shell = pw.pw_shell or ""
        home = pw.pw_dir or ""
        keep = (
            pw.pw_name == "root"
            or linux_user_is_normal_for_samba(pw)
            or home.startswith("/home/")
        )
        if not keep:
            continue
        seen.add(pw.pw_name)
        users.append({
            "name": pw.pw_name,
            "uid": pw.pw_uid,
            "gid": pw.pw_gid,
            "shell": shell,
            "home": home,
            "display": f"{pw.pw_name}  UID:{pw.pw_uid} GID:{pw.pw_gid}  {home}",
        })
    users.sort(key=lambda x: (0 if x["name"] == "root" else 1, str(x["name"]).lower()))
    return users


def enrich_samba_users_for_ui(cfg: SambaConfig) -> list[dict]:
    """Liste UI = users Linux + users présents dans samba.conf si besoin."""
    users = list_linux_users_for_ui()
    by_name = {u["name"]: u for u in users}
    for user in cfg.users:
        if user.name not in by_name:
            try:
                pw = pwd.getpwnam(user.name)
                by_name[user.name] = {
                    "name": pw.pw_name,
                    "uid": pw.pw_uid,
                    "gid": pw.pw_gid,
                    "shell": pw.pw_shell or "",
                    "home": pw.pw_dir or "",
                    "display": f"{pw.pw_name}  UID:{pw.pw_uid} GID:{pw.pw_gid}  {pw.pw_dir}",
                }
            except KeyError:
                by_name[user.name] = {
                    "name": user.name,
                    "uid": user.uid,
                    "gid": user.gid,
                    "shell": user.shell,
                    "home": user.home,
                    "display": f"{user.name}  ⚠ inexistant dans /etc/passwd",
                    "missing": True,
                }
    return sorted(by_name.values(), key=lambda x: (bool(x.get("missing")), 0 if x["name"] == "root" else 1, str(x["name"]).lower()))


def list_samba_owner_users_for_ui() -> list[dict]:
    """Utilisateurs proposés comme propriétaires de partages.

    Affichage volontairement simple : on ne montre que le nom, pas UID/GID,
    parce que l'UID/GID se gère dans le module Utilisateurs.
    """
    users: list[dict] = []
    for pw in pwd.getpwall():
        if not linux_user_is_normal_for_samba(pw):
            continue
        users.append({
            "name": pw.pw_name,
            "uid": pw.pw_uid,
            "gid": pw.pw_gid,
            "shell": pw.pw_shell or "",
            "home": pw.pw_dir or "",
            "display": pw.pw_name,
        })
    users.sort(key=lambda x: (int(x["uid"]), str(x["name"]).lower()))
    return users


def browse_start_path() -> Path:
    return configured_path("BROWSE_START", "/")


def browse_directories(raw_path: str | None = None) -> dict:
    raw = (raw_path or "").strip()
    path = Path(raw).expanduser() if raw else browse_start_path()
    if not path.is_absolute():
        path = resolve_module_path(str(path))
    try:
        path = path.resolve()
    except Exception:
        path = browse_start_path().resolve()

    if not path.exists():
        # Si l'utilisateur tape /mnt/user/Med, on remonte au parent existant.
        current = path
        while not current.exists() and current != current.parent:
            current = current.parent
        path = current
    if not path.is_dir():
        path = path.parent

    dirs: list[dict] = []
    try:
        for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.is_dir():
                dirs.append({
                    "name": child.name,
                    "path": str(child),
                    "hidden": child.name.startswith("."),
                })
    except PermissionError as exc:
        return {"ok": False, "message": f"Permission refusée : {path} : {exc}", "path": str(path), "dirs": []}
    except Exception as exc:
        return {"ok": False, "message": f"Erreur lecture : {path} : {exc}", "path": str(path), "dirs": []}

    parent = str(path.parent) if path != path.parent else str(path)
    return {"ok": True, "path": str(path), "parent": parent, "dirs": dirs}


def install_samba_packages_stream() -> Iterator[str]:
    base_commands = ["smbd", "nmbd", "smbpasswd", "testparm"]
    packages = []
    if not all(command_exists(cmd) for cmd in base_commands):
        packages.extend(["samba", "samba-vfs-modules", "acl", "attr"])
    if not (command_exists("wsdd2") or command_exists("wsdd")):
        packages.append("wsdd2")

    if not packages:
        yield "Base Samba/wsdd déjà présente.\n"
        return
    if not command_exists("apt-get"):
        yield "ERREUR : apt-get introuvable. Installe manuellement : " + " ".join(packages) + "\n"
        return

    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    for cmd in (["apt-get", "update"], ["apt-get", "install", "-y", *packages]):
        yield "$ " + shell_join(cmd) + "\n"
        res = run_capture(cmd, env=env)
        yield res.stdout
        if res.returncode != 0 and "wsdd2" in packages:
            # Fallback wsdd si wsdd2 n'existe pas dans la distro.
            yield "wsdd2 indisponible, tentative fallback wsdd.\n"
            res2 = run_capture(["apt-get", "install", "-y", "wsdd"], env=env)
            yield res2.stdout
        elif res.returncode != 0:
            yield f"ERREUR : commande échouée avec code {res.returncode}\n"
            return


PROTECTED_PERMISSION_PATHS = {
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64",
    "/mnt", "/opt", "/proc", "/root", "/run", "/sbin", "/srv", "/sys",
    "/tmp", "/usr", "/var",
}


def samba_share_mode_pair(share: SambaShare) -> tuple[str, str, str]:
    """Retourne (mode_dossiers, mode_fichiers, explication).

    Matrice NAS retenue :
      - guest=yes + writable=yes + read_only=no : public écriture => 777/666
      - guest=yes + read_only=yes, même si writable=yes : public lecture seule
        => 755/644
      - guest=no + read_only=yes + writable=yes : propriétaire écrit, groupe
        Linux du propriétaire lit => 750/640
      - guest=no + writable=no ou read_only=yes seul : privé lecture seule
        propriétaire => 500/400
      - guest=no + read_only=no + writable=yes : privé écriture propriétaire
        => 700/600
    """
    if share.share_type == "root":
        return "0777", "0666", "type root / accès large"
    if share.guest_ok == "yes":
        if share.read_only == "yes" or share.writable == "no":
            return "0755", "0644", "invité lecture seule"
        return "0777", "0666", "invité écriture"
    if share.owner:
        if share.read_only == "yes" and share.writable == "yes":
            return "0750", "0640", f"{share.owner} écrit, groupe Linux de {share.owner} en lecture seule"
        if share.read_only == "yes" or share.writable == "no":
            return "0500", "0400", f"privé lecture seule pour {share.owner}"
        return "0700", "0600", f"privé écriture pour {share.owner}"
    return "", "", "aucun propriétaire défini"


def safe_permission_target(path: Path) -> tuple[bool, str]:
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path
    raw = str(resolved)
    if raw in PROTECTED_PERMISSION_PATHS:
        return False, f"chemin protégé : {raw}"
    if not resolved.is_absolute():
        return False, f"chemin non absolu : {resolved}"
    return True, raw


def samba_share_rights_key(share: SambaShare) -> str:
    return str(share.name or share.path).strip()


def samba_share_rights_signature(share: SambaShare, dir_mode: str, file_mode: str) -> str:
    raw = "\n".join([
        str(share.name or ""),
        str(share.path or ""),
        str(share.owner or ""),
        str(share.share_type or ""),
        str(share.guest_ok or ""),
        str(share.read_only or ""),
        str(share.writable or ""),
        str(dir_mode or ""),
        str(file_mode or ""),
    ])
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


def load_samba_share_rights_state() -> dict[str, str]:
    state: dict[str, str] = {}
    try:
        for raw_line in SHARE_RIGHTS_STATE_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "\t" not in raw_line:
                continue
            key, value = raw_line.split("\t", 1)
            key = key.strip()
            value = value.strip()
            if key and value:
                state[key] = value
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return state


def save_samba_share_rights_state(state: dict[str, str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}\t{value}" for key, value in sorted((state or {}).items()) if key and value]
    SHARE_RIGHTS_STATE_FILE.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    try:
        SHARE_RIGHTS_STATE_FILE.chmod(0o600)
    except OSError:
        pass


def seed_samba_share_rights_state_if_missing(cfg: SambaConfig) -> None:
    if SHARE_RIGHTS_STATE_FILE.exists():
        return
    state: dict[str, str] = {}
    for share in cfg.shares:
        dir_mode, file_mode, _label = samba_share_mode_pair(share)
        key = samba_share_rights_key(share)
        if key and dir_mode and file_mode:
            state[key] = samba_share_rights_signature(share, dir_mode, file_mode)
    if state:
        save_samba_share_rights_state(state)


def apply_samba_share_rights_stream(cfg: SambaConfig) -> Iterator[str]:
    """Ne modifie plus les droits Linux depuis le tableau Samba."""
    yield "Droits Linux non modifies : le tableau Samba pilote uniquement smb.conf.\n"

def ensure_share_dirs_stream(cfg: SambaConfig) -> Iterator[str]:
    if not cfg.create_missing_dirs:
        yield "Création automatique des dossiers désactivée.\n"
        return
    for share in cfg.shares:
        if share.path.exists():
            yield f"OK dossier : {share.path}\n"
        else:
            try:
                share.path.mkdir(parents=True, exist_ok=True)
                yield f"Création dossier : {share.path}\n"
            except Exception as exc:
                yield f"ERREUR création dossier {share.path} : {exc}\n"


def ensure_users_stream(cfg: SambaConfig, *, force_password_update: bool = False) -> Iterator[str]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    new_hash = hashlib.sha256()
    for user in sorted(cfg.users, key=lambda u: u.name):
        new_hash.update(f"{user.name}:{user.uid}:{user.gid}:{user.shell}:{user.home}\n".encode("utf-8"))
    new_hash_text = new_hash.hexdigest()
    old_hash = USER_HASH_FILE.read_text(encoding="utf-8").strip() if USER_HASH_FILE.exists() else ""
    password_update_needed = force_password_update or new_hash_text != old_hash

    for user in cfg.users:
        try:
            pw = pwd.getpwnam(user.name)
        except KeyError:
            yield f"ERREUR : utilisateur Linux inexistant : {user.name}. Crée-le d'abord dans le module Utilisateurs Linux.\n"
            continue

        yield f"Utilisateur Linux existant : {user.name} (UID:{pw.pw_uid} GID:{pw.pw_gid})\n"
        if pw.pw_uid != user.uid:
            yield f"ERREUR : UID différent du conf pour {user.name} ({pw.pw_uid} != {user.uid}). Recharge la liste ou corrige samba.conf.\n"
            continue
        if pw.pw_gid != user.gid:
            yield f"ERREUR : GID différent du conf pour {user.name} ({pw.pw_gid} != {user.gid}). Recharge la liste ou corrige samba.conf.\n"
            continue

        if user.password and password_update_needed:
            if not command_exists("smbpasswd"):
                yield "ERREUR : smbpasswd introuvable.\n"
                continue
            yield f"Mot de passe Samba mis à jour : {user.name}\n"
            yield run_capture(["smbpasswd", "-a", "-s", user.name], input_text=f"{user.password}\n{user.password}\n").stdout
            yield run_capture(["smbpasswd", "-e", user.name]).stdout
        else:
            yield f"Compte Samba repris depuis Linux : {user.name} (mot de passe géré dans Utilisateurs).\n"

    USER_HASH_FILE.write_text(new_hash_text + "\n", encoding="utf-8")
    USER_HASH_FILE.chmod(0o600)

def write_samba_apply_files_stream(cfg: SambaConfig) -> Iterator[str]:
    changed, msg = write_if_changed(cfg.smb_conf, render_smb_conf(cfg), mode=0o644, backup_unmanaged_marker=MANAGED_MARKER)
    yield msg + "\n"
    if command_exists("testparm"):
        res = run_capture(["testparm", "-s", str(cfg.smb_conf)])
        yield "$ testparm -s " + str(cfg.smb_conf) + "\n" + res.stdout

    service_path = Path("/etc/systemd/system") / SAMBA_APPLY_SERVICE
    changed_service, msg_service = write_if_changed(service_path, render_apply_service(cfg), mode=0o644)
    yield msg_service + "\n"

    legacy = Path("/etc/systemd/system") / LEGACY_WSDD_SERVICE
    if legacy.exists():
        yield "Suppression ancien service WSDD custom.\n"
        run_capture(["systemctl", "disable", "--now", LEGACY_WSDD_SERVICE])
        backup_file(legacy, "legacy")
        legacy.unlink(missing_ok=True)

    changed_wsdd, wsdd_messages = configure_wsdd_service(cfg)
    for message in wsdd_messages:
        yield message + "\n"

    if changed or changed_service or changed_wsdd:
        rc, out = systemctl_cmd(["daemon-reload"])
        yield out


def start_samba_services_stream(cfg: SambaConfig, *, restart: bool = True) -> Iterator[str]:
    yield "--- Démarrage / redémarrage services Samba ---\n"
    changed_wsdd, wsdd_messages = configure_wsdd_service(cfg)
    for message in wsdd_messages:
        yield message + "\n"

    for args in (["daemon-reload"], ["enable", SAMBA_APPLY_SERVICE]):
        rc, out = systemctl_cmd(args)
        yield "$ systemctl " + " ".join(args) + "\n" + out

    verb = "restart" if restart else "start"
    for svc in ("nmbd.service", "smbd.service"):
        for args in (["enable", svc], [verb, svc]):
            rc, out = systemctl_cmd(args)
            yield "$ systemctl " + " ".join(args) + "\n" + out

    if cfg.enable_wsdd:
        svc = detect_wsdd_service()
        if svc:
            # wsdd2.service est PartOf/BindsTo=smbd.service sur Debian :
            # restart smbd le relance déjà. On le démarre seulement ici pour
            # éviter un second arrêt/démarrage et garder l'annonce réseau stable.
            for args in (["enable", svc], ["start", svc]):
                rc, out = systemctl_cmd(args)
                yield "$ systemctl " + " ".join(args) + "\n" + out
            # Ancien timer samba-wsdd-refresh volontairement supprimé : pas
            # de redémarrage périodique pendant les flux SMB.
        else:
            yield "AVERTISSEMENT : aucun service wsdd2/wsdd disponible à démarrer.\n"
    else:
        rc, out = systemctl_cmd(["disable", "--now", WSDD_REFRESH_TIMER])
        yield out
        for svc in DISTRO_WSDD_SERVICES:
            if service_state(svc)["exists"]:
                rc, out = systemctl_cmd(["disable", "--now", svc])
                yield out


def stop_samba_services_stream() -> Iterator[str]:
    for svc in (WSDD_REFRESH_TIMER, WSDD_REFRESH_SERVICE, LEGACY_WSDD_SERVICE, *DISTRO_WSDD_SERVICES, *SAMBA_SERVICES):
        rc, out = systemctl_cmd(["stop", svc])
        yield "$ systemctl stop " + svc + "\n" + out


def disable_samba_services_stream() -> Iterator[str]:
    for svc in (SAMBA_APPLY_SERVICE, WSDD_REFRESH_TIMER, WSDD_REFRESH_SERVICE, LEGACY_WSDD_SERVICE, *SAMBA_SERVICES, *DISTRO_WSDD_SERVICES):
        rc, out = systemctl_cmd(["disable", svc])
        yield "$ systemctl disable " + svc + "\n" + out


def apply_samba_stream(cfg: SambaConfig, *, update_passwords: bool = False) -> Iterator[str]:
    root_error = require_root_text()
    if root_error:
        yield root_error + "\n"
        return
    errors = validate_samba_config(cfg)
    if errors:
        yield "ERREUR configuration Samba :\n" + "\n".join(" - " + e for e in errors) + "\n"
        return
    try:
        cfg.conf_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.conf_path.write_text(render_samba_source_conf(cfg), encoding="utf-8")
        cfg.conf_path.chmod(0o600)
        yield f"Source Samba écrit : {cfg.conf_path}\n"
    except Exception as exc:
        yield f"ERREUR écriture source Samba : {exc}\n"
        return
    yield from ensure_share_dirs_stream(cfg)
    yield from ensure_users_stream(cfg, force_password_update=update_passwords)
    yield from apply_samba_share_rights_stream(cfg)
    yield from write_samba_apply_files_stream(cfg)
    yield "OK : configuration Samba appliquée.\n"


def apply_and_restart_samba_stream(cfg: SambaConfig, *, update_passwords: bool = False) -> Iterator[str]:
    """Action utilisée par l'interface : appliquer puis relancer les services humains.

    apply_samba_stream() reste volontairement pur, car il est aussi appelé par
    le service systemd samba-host-apply.service au boot. Depuis l'UI, par
    contre, le bouton Appliquer doit aussi redémarrer smbd/nmbd et surtout
    wsdd/wsdd2, sinon Samba marche par IP mais Windows ne redécouvre pas le
    serveur dans l'explorateur réseau.
    """
    chunks: list[str] = []
    for chunk in apply_samba_stream(cfg, update_passwords=update_passwords):
        chunks.append(chunk)
        yield chunk
    if any("ERREUR" in chunk for chunk in chunks):
        yield "Redémarrage services annulé à cause de l'erreur précédente.\n"
        return
    yield "--- Rechargement des services Samba / découverte Windows ---\n"
    yield from start_samba_services_stream(cfg, restart=True)


def install_samba_stream(cfg: SambaConfig) -> Iterator[str]:
    root_error = require_root_text()
    if root_error:
        yield root_error + "\n"
        return
    yield from install_samba_packages_stream()
    yield from apply_samba_stream(cfg, update_passwords=True)
    yield from start_samba_services_stream(cfg, restart=True)
    yield "OK : Samba host installé et démarré.\n"


def remove_samba_stream(cfg: SambaConfig) -> Iterator[str]:
    root_error = require_root_text()
    if root_error:
        yield root_error + "\n"
        return
    yield from stop_samba_services_stream()
    yield from disable_samba_services_stream()
    for service_name in (LEGACY_WSDD_SERVICE, SAMBA_APPLY_SERVICE, WSDD_REFRESH_SERVICE, WSDD_REFRESH_TIMER):
        service_path = Path("/etc/systemd/system") / service_name
        if service_path.exists():
            backup = backup_file(service_path, "removed")
            service_path.unlink(missing_ok=True)
            yield f"Supprimé : {service_path} (backup {backup})\n"
    systemctl_cmd(["daemon-reload"])
    if cfg.smb_conf.exists():
        current = cfg.smb_conf.read_text(encoding="utf-8", errors="ignore")
        if MANAGED_MARKER in current:
            backup = backup_file(cfg.smb_conf, "removed")
            cfg.smb_conf.unlink(missing_ok=True)
            yield f"Supprimé : {cfg.smb_conf} (backup {backup})\n"
        else:
            yield f"Je ne supprime pas {cfg.smb_conf} : fichier non marqué partage.py.\n"
    yield "OK : Samba host retiré. Les paquets, users Linux et données ne sont pas supprimés.\n"
