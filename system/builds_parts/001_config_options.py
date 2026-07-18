import argparse
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - ce module tourne sur Linux/Unraid.
    fcntl = None

try:
    import requests
except ImportError:  # pragma: no cover - le reste du module Build peut tourner sans requests.
    requests = None

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    stream_with_context,
    url_for,
)

builds_bp = Blueprint("builds_bp", __name__)

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


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_CANDIDATES = [
    nas_conf_file("builds.conf"),
]

DEFAULT_CONFIG = {
    # Le module est autonome : il n'appelle plus les scripts shell du dossier scripts.
    # Il lance directement docker buildx et regctl depuis Python.
    "EXEC_MODE": "local-python",
    "UNIFIED_PATHS": "1",

    # Chemins moteurs Docker LABO.
    "HOST_BUILDS_DIR": nas_root_path("docker_buils"),
    "HOST_TAR_DIR": nas_root_path("tar"),
    "HOST_CONF_DIR": NAS_CONF_DIR,
    "HOST_LOG_DIR": "/var/log/builds",
    "HOST_REGISTRY_FILE": nas_conf_file("registre.conf"),
    "HOST_MODE_FILE": nas_conf_file("mode.conf"),
    "HOST_PLATFORMS_FILE": nas_conf_file("platforms.conf"),
    "HOST_REGISTRY_LOGIN_FILE": nas_conf_file("registre_login.conf"),
    "HOST_REGISTRY_CONFIG_FILE": nas_conf_file("builds.conf"),
    "BUILD_CACHE_FILE": nas_conf_file("build.jdom"),

    # Chemins vus depuis le Docker Flask. Avec /dockers:/dockers, ce sont les mêmes.
    "DOCKER_BUILDS_DIR": nas_root_path("docker_buils"),
    "DOCKER_TAR_DIR": nas_root_path("tar"),
    "DOCKER_CONF_DIR": NAS_CONF_DIR,
    "DOCKER_LOG_DIR": "/var/log/builds",
    "DOCKER_REGISTRY_FILE": nas_conf_file("registre.conf"),
    "DOCKER_MODE_FILE": nas_conf_file("mode.conf"),
    "DOCKER_PLATFORMS_FILE": nas_conf_file("platforms.conf"),
    "DOCKER_REGISTRY_LOGIN_FILE": nas_conf_file("registre_login.conf"),
    "DOCKER_REGISTRY_CONFIG_FILE": nas_conf_file("builds.conf"),

    # Onglet Registre intégré au module Build.
    # Ces anciennes valeurs de registry.conf sont maintenant lues directement ici.
    "REGISTRY_URL": "",
    "REGISTRY_USER": "domo",
    "REGISTRY_PASSWORD": "dome",
    "YML_DIR": nas_root_path("yml"),
    "REGISTRY_REQUEST_TIMEOUT": "10",

    # Outils.
    "REGCTL": nas_root_path("bin", "regctl"),
    "DOCKER_BIN": "docker",
    "FROM_CHECK_TIMEOUT": "30",
    "BUILDER_NAME": "mon_builder",
    # Par défaut le builder buildx est temporaire : on nettoie buildx_buildkit_<nom>, volumes et image BuildKit.
    "KEEP_BUILDX_BUILDER": "0",
    "CLEAN_BUILDX_IMAGE": "1",
    "STATE_DIR": nas_conf_file(".save_state"),
    "LOCK_FILE": "/tmp/flask_builds_python.lock",

    # Buildx : en Docker, le binaire docker monté peut exister sans plugin buildx.
    # En auto, on utilise buildx local si disponible, sinon un client Docker officiel
    # lancé en container, avec /dockers monté et un état buildx persistant.
    "BUILDX_BACKEND": "auto",
    "DOCKER_CLI_FALLBACK": "0",
    "DOCKER_CLI_IMAGE": "docker:27-cli",
    "DOCKER_CLI_CONFIG_DIR": nas_conf_file(".docker_cli"),
    "DOCKER_CLI_MOUNTS": "",
    "DOCKER_SOCK": "/var/run/docker.sock",
    # Valeurs par défaut.
    "REGISTRY_PREFIX": "registry.sftpmalin.com",
    "DEFAULT_PLATFORMS": "linux/amd64",
    "SHOW_DEFAULT_PLATFORMS_IN_TABLE": "0",

}

VALID_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SKIP_CONTEXT_DIRS = {".git", "__pycache__"}
SKIP_CONTEXT_EXT = {".pyc", ".tmp", ".log"}
SKIP_CONTEXT_NAMES = {".DS_Store"}


def get_config_path() -> str:
    env_path = os.environ.get("BUILDS_CONFIG_PATH", "").strip()
    if env_path:
        return env_path
    for candidate in DEFAULT_CONFIG_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return DEFAULT_CONFIG_CANDIDATES[0]


CONFIG_FILE = get_config_path()


def strip_quotes(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def normalize_registry_prefix(value: str) -> str:
    """Retourne un préfixe image Docker propre depuis une URL/host de registre.

    registry.conf peut contenir REGISTRY_URL=http://192.168.1.164:7777.
    Pour une image Docker, il faut 192.168.1.164:7777/name:latest, sans http://.
    """
    value = strip_quotes(value).strip().rstrip("/")
    if not value:
        return ""
    value = value.removeprefix("http://").removeprefix("https://")
    return value.split("/", 1)[0].strip().rstrip("/")


def registry_prefix_from_conf_file(path: str) -> str:
    data = read_config_file(path) if path else {}
    for key in ("REGISTRY_URL", "REGISTRY_HOST", "REGISTRY_PREFIX", "REGISTRY"):
        value = normalize_registry_prefix(data.get(key, ""))
        if value:
            return value
    return ""


def conf_bool(conf: Dict[str, str], key: str, default: str = "0") -> bool:
    return str(conf.get(key, default)).strip().lower() in {"1", "true", "yes", "on"}


def read_config_file(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key:
                out[key] = strip_quotes(value)
    return out



BUILD_CONFIG_PATH_KEYS = {
    "HOST_BUILDS_DIR", "HOST_TAR_DIR", "HOST_CONF_DIR", "HOST_LOG_DIR",
    "HOST_REGISTRY_FILE", "HOST_MODE_FILE", "HOST_PLATFORMS_FILE",
    "HOST_REGISTRY_LOGIN_FILE", "HOST_REGISTRY_CONFIG_FILE", "BUILD_CACHE_FILE",
    "DOCKER_BUILDS_DIR", "DOCKER_TAR_DIR", "DOCKER_CONF_DIR", "DOCKER_LOG_DIR",
    "DOCKER_REGISTRY_FILE", "DOCKER_MODE_FILE", "DOCKER_PLATFORMS_FILE",
    "DOCKER_REGISTRY_LOGIN_FILE", "DOCKER_REGISTRY_CONFIG_FILE",
    "REGCTL", "STATE_DIR", "DOCKER_CLI_CONFIG_DIR",
    "SYSTEM_LOG_FILE",
    "YML_DIR",
    "REGISTRY_HOST_CONF_FILE", "REGISTRY_HOST_YAML_FILE", "REGISTRY_HOST_LOG_DIR",
    "REGISTRY_HOST_LOG_FILE", "REGISTRY_HOST_MNT_READY_DIR", "REGISTRY_HOST_MNT_ROOT",
}

BUILD_CONFIG_CSV_PATH_KEYS = {
    "DOCKER_CLI_MOUNTS",
}


def build_conf_resolve_path(value: str, base_dir: Optional[str] = None) -> str:
    """Résout les chemins relatifs de builds.conf depuis le dossier conf officiel.

    Exemple :
      NAS_CONF_DIR=/dockers/conf
      HOST_TAR_DIR=../tar  -> /dockers/tar
    """
    raw = strip_quotes(str(value or "")).strip()
    if not raw:
        return ""
    raw = os.path.expanduser(os.path.expandvars(raw))
    if os.path.isabs(raw):
        return os.path.abspath(raw)
    root = base_dir or NAS_CONF_DIR
    return os.path.abspath(os.path.join(root, raw))


def build_conf_resolve_csv_paths(value: str, base_dir: Optional[str] = None) -> str:
    parts = []
    for raw in str(value or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        parts.append(build_conf_resolve_path(raw, base_dir))
    return ",".join(parts)


def builds_default_conf_text(conf: Optional[Dict[str, str]] = None) -> str:
    """Génère un builds.conf propre et minimal.

    Règle actuelle :
      - Build garde seulement ce qui concerne Build/TAR, Registre intégré et Registry host.
      - Les anciens blocs Compose/Stacks ne sont plus générés ici : c'est le module Docker qui porte YML_FOLDER/stacks.
      - Les logs vont dans des chemins Linux standards (/var/log/...).
      - Les fichiers de configuration restent relatifs à ../conf.
    """
    values = {
        "EXEC_MODE": "local-python",
        "UNIFIED_PATHS": "1",

        "HOST_BUILDS_DIR": "../docker_buils",
        "HOST_TAR_DIR": "../tar",
        "HOST_CONF_DIR": "../conf",
        "HOST_LOG_DIR": "/var/log/builds",
        "HOST_REGISTRY_FILE": "../conf/registre.conf",
        "HOST_MODE_FILE": "../conf/mode.conf",
        "HOST_PLATFORMS_FILE": "../conf/platforms.conf",
        "HOST_REGISTRY_LOGIN_FILE": "../conf/registre_login.conf",
        "HOST_REGISTRY_CONFIG_FILE": "../conf/builds.conf",
        "BUILD_CACHE_FILE": "../conf/build.jdom",

        "DOCKER_BUILDS_DIR": "../docker_buils",
        "DOCKER_TAR_DIR": "../tar",
        "DOCKER_CONF_DIR": "../conf",
        "DOCKER_LOG_DIR": "/var/log/builds",
        "DOCKER_REGISTRY_FILE": "../conf/registre.conf",
        "DOCKER_MODE_FILE": "../conf/mode.conf",
        "DOCKER_PLATFORMS_FILE": "../conf/platforms.conf",
        "DOCKER_REGISTRY_LOGIN_FILE": "../conf/registre_login.conf",
        "DOCKER_REGISTRY_CONFIG_FILE": "../conf/builds.conf",

        "REGCTL": "../bin/regctl",
        "DOCKER_BIN": "docker",
        "BUILDER_NAME": "mon_builder",
        "KEEP_BUILDX_BUILDER": "0",
        "CLEAN_BUILDX_IMAGE": "1",
        "STATE_DIR": "../conf/.save_state",
        "LOCK_FILE": "/tmp/flask_builds_python.lock",
        "BUILDX_BACKEND": "auto",
        "DOCKER_CLI_FALLBACK": "0",
        "DOCKER_CLI_IMAGE": "docker:27-cli",
        "DOCKER_CLI_CONFIG_DIR": "../conf/.docker_cli",
        "DOCKER_CLI_MOUNTS": "",
        "DOCKER_SOCK": "/var/run/docker.sock",

        "DEFAULT_PLATFORMS": "linux/amd64",
        "REGISTRY_PREFIX": "",
        "SHOW_DEFAULT_PLATFORMS_IN_TABLE": "0",

        "REGISTRY_URL": "http://192.168.1.xxx:xxxx",
        "REGISTRY_USER": "domo",
        "REGISTRY_PASSWORD": "dome",
        "YML_DIR": "../yml",
        "REGISTRY_REQUEST_TIMEOUT": "10",

        "SYSTEM_LOG_FILE": "/var/log/builds/system_python.log",

        "REGISTRY_HOST_CONF_FILE": "../conf/registry.conf",
        "REGISTRY_HOST_YAML_FILE": "../conf/registry.yml",
        "REGISTRY_HOST_LOG_DIR": "/var/log/registry",
        "REGISTRY_HOST_LOG_FILE": "/var/log/registry/registry.log",
        "REGISTRY_HOST_RUNTIME_BIN": "/tmp/registry-host-labo",
        "REGISTRY_HOST_PID_FILE": "/run/registry_labo_host.pid",
        "REGISTRY_HOST_SERVICE_NAME": "registry-labo-host.service",
        "REGISTRY_HOST_SERVICE_FILE": "/etc/systemd/system/registry-labo-host.service",
        "REGISTRY_HOST_MNT_READY_DIR": "",
        "REGISTRY_HOST_MNT_ROOT": "",
    }
    if conf:
        for key, value in conf.items():
            if key in values and value is not None:
                values[key] = str(value).strip()

    def kv(key: str) -> str:
        return f"{key}={values.get(key, '')}"

    lines = [
        "# ============================================================",
        "# builds.conf - Module Build / Registry",
        "#",
        "# Généré automatiquement par builds.py si absent ou après validation Options.",
        "# Les chemins de conf restent relatifs à ../conf.",
        "# Les chemins de logs sont en standard Linux sous /var/log.",
        "# Les anciens réglages Compose/Stacks ne sont plus générés ici.",
        "# ============================================================",
        "",
        "# Moteur Build",
        kv("EXEC_MODE"),
        kv("UNIFIED_PATHS"),
        "",
        "# Dossiers Build / TAR / logs",
        kv("HOST_BUILDS_DIR"),
        kv("HOST_TAR_DIR"),
        kv("HOST_CONF_DIR"),
        kv("HOST_LOG_DIR"),
        kv("DOCKER_BUILDS_DIR"),
        kv("DOCKER_TAR_DIR"),
        kv("DOCKER_CONF_DIR"),
        kv("DOCKER_LOG_DIR"),
        "",
        "# Fichiers internes Build",
        kv("HOST_REGISTRY_FILE"),
        kv("HOST_MODE_FILE"),
        kv("HOST_PLATFORMS_FILE"),
        kv("HOST_REGISTRY_LOGIN_FILE"),
        kv("HOST_REGISTRY_CONFIG_FILE"),
        kv("BUILD_CACHE_FILE"),
        kv("DOCKER_REGISTRY_FILE"),
        kv("DOCKER_MODE_FILE"),
        kv("DOCKER_PLATFORMS_FILE"),
        kv("DOCKER_REGISTRY_LOGIN_FILE"),
        kv("DOCKER_REGISTRY_CONFIG_FILE"),
        "",
        "# Outils et état",
        kv("REGCTL"),
        kv("DOCKER_BIN"),
        kv("BUILDER_NAME"),
        kv("KEEP_BUILDX_BUILDER"),
        kv("CLEAN_BUILDX_IMAGE"),
        kv("STATE_DIR"),
        kv("LOCK_FILE"),
        kv("BUILDX_BACKEND"),
        kv("DOCKER_CLI_FALLBACK"),
        kv("DOCKER_CLI_IMAGE"),
        kv("DOCKER_CLI_CONFIG_DIR"),
        kv("DOCKER_CLI_MOUNTS"),
        kv("DOCKER_SOCK"),
        "",
        "# Valeurs par défaut Build",
        kv("DEFAULT_PLATFORMS"),
        kv("REGISTRY_PREFIX"),
        kv("SHOW_DEFAULT_PLATFORMS_IN_TABLE"),
        "",
        "# Registre intégré au module Build",
        kv("REGISTRY_URL"),
        kv("REGISTRY_USER"),
        kv("REGISTRY_PASSWORD"),
        kv("YML_DIR"),
        kv("REGISTRY_REQUEST_TIMEOUT"),
        kv("SYSTEM_LOG_FILE"),
        "",
        "# Registry host",
        kv("REGISTRY_HOST_CONF_FILE"),
        kv("REGISTRY_HOST_YAML_FILE"),
        kv("REGISTRY_HOST_LOG_DIR"),
        kv("REGISTRY_HOST_LOG_FILE"),
        kv("REGISTRY_HOST_RUNTIME_BIN"),
        kv("REGISTRY_HOST_PID_FILE"),
        kv("REGISTRY_HOST_SERVICE_NAME"),
        kv("REGISTRY_HOST_SERVICE_FILE"),
        kv("REGISTRY_HOST_MNT_READY_DIR"),
        "# REGISTRY_HOST_MNT_ROOT vide = détection automatique du point de montage contenant le dossier système.",
        kv("REGISTRY_HOST_MNT_ROOT"),
        "",
    ]
    return "\n".join(lines)


def ensure_builds_conf_file(path: str) -> bool:
    """Crée un builds.conf minimal si absent.

    Important : on crée le fichier de conf, mais on ne crée pas encore les dossiers
    build/tar ici. Les dossiers seront créés quand l'utilisateur valide Options.
    """
    if os.path.exists(path):
        return False
    parent = os.path.dirname(path.rstrip("/")) or "."
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(builds_default_conf_text())
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass
    return True


DEFAULT_REGISTRY_LOGIN_CONF_TEXT = 'REGISTRY_HOST="demo"\nREGISTRY_USER="domo"\nREGISTRY_PASS="dome"\n'


def ensure_text_file_if_missing(path: str, content: str = "") -> bool:
    """Crée un fichier texte manquant sans écraser l'existant."""
    path = (path or "").strip()
    if not path or os.path.exists(path):
        return False
    parent = os.path.dirname(path.rstrip("/")) or "."
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass
    return True


def ensure_build_support_conf_files(conf: Dict[str, str]) -> List[str]:
    """Crée les petits fichiers conf du module Build quand ils manquent.

    Ils sont volontairement en .conf maintenant : registre.conf, platforms.conf,
    mode.conf et registre_login.conf. Les fichiers existants ne sont jamais
    écrasés, pour ne pas détruire une base déjà remplie.
    """
    created: List[str] = []
    targets = [
        (conf.get("DOCKER_REGISTRY_FILE") or conf.get("HOST_REGISTRY_FILE") or nas_conf_file("registre.conf"), ""),
        (conf.get("DOCKER_PLATFORMS_FILE") or conf.get("HOST_PLATFORMS_FILE") or nas_conf_file("platforms.conf"), ""),
        (conf.get("DOCKER_MODE_FILE") or conf.get("HOST_MODE_FILE") or nas_conf_file("mode.conf"), "_default=0\n"),
        (conf.get("DOCKER_REGISTRY_LOGIN_FILE") or conf.get("HOST_REGISTRY_LOGIN_FILE") or nas_conf_file("registre_login.conf"), DEFAULT_REGISTRY_LOGIN_CONF_TEXT),
    ]
    for path, content in targets:
        if ensure_text_file_if_missing(path, content):
            created.append(path)
    return created


def is_placeholder_value(value: str) -> bool:
    value = str(value or "").strip().lower()
    return not value or "xxx" in value or "xxxx" in value or "change-me" in value


def build_setup_status(conf: Dict[str, str]) -> Dict[str, object]:
    reasons: List[str] = []
    builds_dir = conf.get("DOCKER_BUILDS_DIR") or conf.get("HOST_BUILDS_DIR") or ""
    tar_dir = conf.get("DOCKER_TAR_DIR") or conf.get("HOST_TAR_DIR") or ""
    registry_url = strip_quotes(conf.get("REGISTRY_URL", "")).strip()

    if is_placeholder_value(registry_url):
        reasons.append("REGISTRY_URL n'est pas encore configuré.")
    if not builds_dir:
        reasons.append("Dossier builds vide.")
    elif not os.path.isdir(builds_dir):
        reasons.append(f"Dossier builds à créer : {builds_dir}")
    if not tar_dir:
        reasons.append("Dossier TAR vide.")
    elif not os.path.isdir(tar_dir):
        reasons.append(f"Dossier TAR à créer : {tar_dir}")

    return {
        "required": bool(reasons),
        "reasons": reasons,
        "builds_dir": builds_dir,
        "tar_dir": tar_dir,
        "registry_url": registry_url,
    }


def build_setup_redirect_if_needed(conf: Dict[str, str], current_tab: str):
    setup = build_setup_status(conf)
    if setup.get("required") and current_tab not in {"options", "info"}:
        flash("Configuration Build initiale requise. Choisis les dossiers builds/TAR et le registre.", "error")
        return redirect(url_for("builds_bp.builds_options_route"))
    return None




# ---------------------------------------------------------------------------
# Navigateur de dossiers pour Options Build
# ---------------------------------------------------------------------------
