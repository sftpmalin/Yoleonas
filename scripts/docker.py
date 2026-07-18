#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
docker.py - Gestionnaire unique Docker BUILD -> TAR OCI et TAR OCI -> registre.

Objectif : remplacer les anciens scripts :
  - Build - TAR.sh
  - Build - TAR Select.sh
  - TAR - Registry.sh
  - TAR - Registry Select.sh
  - .save_one_tar.sh

Chemins par défaut alignés sur le module Build Flask :
  CONFIG    = ../conf/builds.conf
  BASE_DIR  = ../docker_buils
  TAR_DIR   = ../tar
  CONF_DIR  = ../conf
  LOG_DIR   = /var/log/builds

Mini-bases Build utilisées par Flask :
  registre.conf, platforms.conf, mode.conf, registre_login.conf

Exemples :
  python3 docker.py --save
  python3 docker.py --save --select meteo
  python3 docker.py --select --save
  python3 docker.py --load
  python3 docker.py --select --load
  python3 docker.py --select meteo --load
  python3 docker.py --load --registry registry.sftpmalin.com
  python3 docker.py --load --registry sftpmalin/dockerup
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


# ============================================================
# Configuration alignée avec le module Build Flask.
#
# Le script est prévu dans :
#   BASE_ROOT/scripts/docker.py
#
# Il lit en priorité ../conf/builds.conf, puis les mêmes mini-bases que Flask :
#   registre.conf, platforms.conf, mode.conf, registre_login.conf
#
# Les chemins relatifs du builds.conf sont résolus depuis le dossier conf,
# comme dans builds_parts/001_config_options.py côté Flask.
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_ROOT = SCRIPT_DIR.parent
NAS_CONF_DIR = Path(os.environ.get("NAS_CONF_DIR", str(BASE_ROOT / "conf"))).expanduser().resolve()
BUILD_CONFIG_FILE = Path(os.environ.get("BUILDS_CONFIG_PATH", str(NAS_CONF_DIR / "builds.conf"))).expanduser()


def _strip_quotes(value: str) -> str:
    value = str(value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _read_simple_conf(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return data
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            data[key] = _strip_quotes(value)
    return data


def _resolve_conf_path(value: str, base_dir: Path = NAS_CONF_DIR) -> str:
    raw = _strip_quotes(value).strip()
    if not raw:
        return ""
    raw = os.path.expanduser(os.path.expandvars(raw))
    if os.path.isabs(raw):
        return os.path.abspath(raw)
    return os.path.abspath(str(base_dir / raw))


def _conf_bool(data: dict[str, str], key: str, default: str = "0") -> bool:
    return str(data.get(key, default)).strip().lower() in {"1", "true", "yes", "on"}


BUILD_CONF: dict[str, str] = {
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
}
BUILD_CONF.update(_read_simple_conf(BUILD_CONFIG_FILE))

# Compat anciennes clés éventuelles, mais on bascule sur la nouvelle base .conf.
for old_key, new_key in (
    ("BUILDS_DIR", "HOST_BUILDS_DIR"),
    ("TAR_DIR", "HOST_TAR_DIR"),
    ("REGISTRY_FILE", "HOST_REGISTRY_FILE"),
    ("MODE_FILE", "HOST_MODE_FILE"),
    ("PLATFORMS_FILE", "HOST_PLATFORMS_FILE"),
):
    if old_key in BUILD_CONF and new_key not in _read_simple_conf(BUILD_CONFIG_FILE):
        BUILD_CONF[new_key] = BUILD_CONF[old_key]

_PATH_KEYS = {
    "HOST_BUILDS_DIR", "HOST_TAR_DIR", "HOST_CONF_DIR", "HOST_LOG_DIR",
    "HOST_REGISTRY_FILE", "HOST_MODE_FILE", "HOST_PLATFORMS_FILE",
    "HOST_REGISTRY_LOGIN_FILE", "HOST_REGISTRY_CONFIG_FILE",
    "DOCKER_BUILDS_DIR", "DOCKER_TAR_DIR", "DOCKER_CONF_DIR", "DOCKER_LOG_DIR",
    "DOCKER_REGISTRY_FILE", "DOCKER_MODE_FILE", "DOCKER_PLATFORMS_FILE",
    "DOCKER_REGISTRY_LOGIN_FILE", "DOCKER_REGISTRY_CONFIG_FILE",
    "REGCTL", "STATE_DIR", "DOCKER_CLI_CONFIG_DIR",
}
for _key in list(BUILD_CONF):
    if _key in _PATH_KEYS and BUILD_CONF.get(_key):
        BUILD_CONF[_key] = _resolve_conf_path(BUILD_CONF[_key])

if _conf_bool(BUILD_CONF, "UNIFIED_PATHS", "1"):
    for _name in ("BUILDS_DIR", "TAR_DIR", "CONF_DIR", "LOG_DIR", "REGISTRY_FILE", "MODE_FILE", "PLATFORMS_FILE", "REGISTRY_LOGIN_FILE", "REGISTRY_CONFIG_FILE"):
        BUILD_CONF[f"DOCKER_{_name}"] = BUILD_CONF.get(f"HOST_{_name}", BUILD_CONF.get(f"DOCKER_{_name}", ""))

BASE_DIR = Path(os.environ.get("BASE_DIR", BUILD_CONF.get("DOCKER_BUILDS_DIR") or str(BASE_ROOT / "docker_buils")))
TAR_DIR = Path(os.environ.get("TAR_DIR", BUILD_CONF.get("DOCKER_TAR_DIR") or str(BASE_ROOT / "tar")))
CONF_DIR = Path(os.environ.get("CONF_DIR", BUILD_CONF.get("DOCKER_CONF_DIR") or str(NAS_CONF_DIR)))
LOG_DIR = Path(os.environ.get("LOG_DIR", BUILD_CONF.get("DOCKER_LOG_DIR") or "/var/log/builds"))
BIN_DIR = Path(os.environ.get("BIN_DIR", str(BASE_ROOT / "bin")))

STATE_DIR = Path(os.environ.get("STATE_DIR", BUILD_CONF.get("STATE_DIR") or str(CONF_DIR / ".save_state")))
PLATFORMS_FILE = Path(os.environ.get("PLATFORMS_FILE", BUILD_CONF.get("DOCKER_PLATFORMS_FILE") or str(CONF_DIR / "platforms.conf")))
REGISTRY_FILE = Path(os.environ.get("REGISTRY_FILE", BUILD_CONF.get("DOCKER_REGISTRY_FILE") or str(CONF_DIR / "registre.conf")))
# mode.conf sépare la cible registre du mode de transport.
# Format : nom=0 pour HTTP local, nom=1 pour HTTPS normal.
MODE_FILE = Path(os.environ.get("MODE_FILE", BUILD_CONF.get("DOCKER_MODE_FILE") or str(CONF_DIR / "mode.conf")))
REGISTRY_LOGIN_FILE = Path(os.environ.get("REGISTRY_LOGIN_FILE", BUILD_CONF.get("DOCKER_REGISTRY_LOGIN_FILE") or str(CONF_DIR / "registre_login.conf")))
REGCTL = Path(os.environ.get("REGCTL", BUILD_CONF.get("REGCTL") or str(BIN_DIR / "regctl")))

DOCKER_BIN = os.environ.get("DOCKER_BIN", BUILD_CONF.get("DOCKER_BIN", "docker")).strip() or "docker"
BUILDX_BACKEND = os.environ.get("BUILDX_BACKEND", BUILD_CONF.get("BUILDX_BACKEND", "auto")).strip().lower()
DOCKER_CLI_FALLBACK = os.environ.get("DOCKER_CLI_FALLBACK", BUILD_CONF.get("DOCKER_CLI_FALLBACK", "0"))
DOCKER_CLI_IMAGE = os.environ.get("DOCKER_CLI_IMAGE", BUILD_CONF.get("DOCKER_CLI_IMAGE", "docker:27-cli")).strip() or "docker:27-cli"
DOCKER_CLI_CONFIG_DIR = Path(os.environ.get("DOCKER_CLI_CONFIG_DIR", BUILD_CONF.get("DOCKER_CLI_CONFIG_DIR") or str(CONF_DIR / ".docker_cli")))
DOCKER_CLI_MOUNTS = os.environ.get("DOCKER_CLI_MOUNTS", BUILD_CONF.get("DOCKER_CLI_MOUNTS", ""))
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", BUILD_CONF.get("DOCKER_SOCK", "/var/run/docker.sock")).strip() or "/var/run/docker.sock"

DEFAULT_PLATFORMS = os.environ.get("DEFAULT_PLATFORMS", BUILD_CONF.get("DEFAULT_PLATFORMS", "linux/amd64"))
REGISTRY_PREFIX = os.environ.get("REGISTRY_PREFIX", BUILD_CONF.get("REGISTRY_PREFIX", ""))
BUILDER_NAME = os.environ.get("BUILDER_NAME", BUILD_CONF.get("BUILDER_NAME", "mon_builder"))
LOCK_FILE = Path(os.environ.get("LOCK_FILE", BUILD_CONF.get("LOCK_FILE", "/tmp/flask_builds_python.lock")))
DEFAULT_TAG = os.environ.get("DEFAULT_TAG", "latest")
DEFAULT_KEEP_BUILDX_BUILDER = _conf_bool(BUILD_CONF, "KEEP_BUILDX_BUILDER", "0") or _conf_bool(BUILD_CONF, "KEEP_BUILDER", "0")
DEFAULT_CLEAN_BUILDX_IMAGE = _conf_bool(BUILD_CONF, "CLEAN_BUILDX_IMAGE", "1")
_BUILDX_BACKEND_SELECTED = ""


# ============================================================
# Petits outils texte / fichiers
# ============================================================


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ensure_dirs() -> None:
    TAR_DIR.mkdir(parents=True, exist_ok=True)
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def trim(value: str) -> str:
    return value.strip()


def strip_tar_suffix(name: str) -> str:
    return name[:-4] if name.endswith(".tar") else name


def is_valid_kv_line(line: str) -> bool:
    line = trim(line).rstrip("\r")
    return bool(line) and not line.startswith("#") and "=" in line


def read_kv_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.rstrip("\r")
        if not is_valid_kv_line(line):
            continue
        key, value = line.split("=", 1)
        key = trim(key)
        value = trim(value)
        if not key:
            continue
        # Support simple des guillemets Bash : KEY="value" ou KEY='value'
        try:
            parts = shlex.split(value, posix=True)
            value = parts[0] if parts else ""
        except ValueError:
            value = value.strip("\"'")
        data[key] = value
    return data


VALID_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def is_valid_name(name: Optional[str]) -> bool:
    return bool(name and VALID_NAME_RE.fullmatch(str(name)))


def normalize_item_name(name: Optional[str]) -> str:
    return strip_tar_suffix(str(name or "").strip())


def normalize_named_map(data: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in data.items():
        if key == "_default":
            out[key] = value
            continue
        clean = normalize_item_name(key)
        if clean and is_valid_name(clean):
            out[clean] = value
    return out


def read_kv_rows(path: Path) -> list[tuple[str, str]]:
    """Lit un fichier key=value en conservant l'ordre des lignes."""
    rows: list[tuple[str, str]] = []
    if not path.exists():
        return rows
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.rstrip("\r").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        try:
            parts = shlex.split(value, posix=True)
            value = parts[0] if parts else ""
        except ValueError:
            value = value.strip("\"'")
        rows.append((key, value))
    return rows


def effective_mode_file() -> Path:
    """Retourne mode.conf, avec compatibilité mod.conf/mod.txt/mode.txt en secours."""
    if MODE_FILE.exists():
        return MODE_FILE
    for alt_name in ("mod.conf", "mod.txt", "mode.txt"):
        alt = MODE_FILE.parent / alt_name
        if alt.exists():
            return alt
    return MODE_FILE


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read_text_or_empty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        return ""


def terminal_cols(default: int = 120) -> int:
    try:
        cols = shutil.get_terminal_size((default, 24)).columns
        return max(cols, 80)
    except Exception:
        return default


# ============================================================
# Logs + exécution commandes
# ============================================================


class TeeLogger:
    def __init__(self, log_file: Path, append: bool = False) -> None:
        self.log_file = log_file
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        self.fp = self.log_file.open(mode, encoding="utf-8", errors="replace")

    def write(self, text: str = "") -> None:
        print(text, flush=True)
        self.fp.write(text + "\n")
        self.fp.flush()

    def raw(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()
        self.fp.write(text)
        self.fp.flush()

    def close(self) -> None:
        self.fp.close()

    def __enter__(self) -> "TeeLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def run_stream(cmd: list[str], log: TeeLogger, *, env: Optional[dict[str, str]] = None) -> int:
    log.write(">>> " + " ".join(shlex.quote(x) for x in cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            log.raw(line)
    except KeyboardInterrupt:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=10)
        raise
    return proc.wait()


def run_capture(cmd: list[str], *, input_text: Optional[str] = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class NonBlockingLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fp = None

    def __enter__(self) -> "NonBlockingLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = self.path.open("w")
        try:
            fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"Un autre build/save est déjà en cours : {self.path}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fp:
            fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
            self.fp.close()


# ============================================================
# Plateformes : platforms.conf compatible avec le module Build Flask
# ============================================================


def normalize_platforms(value: str) -> str:
    v = trim(str(value or ""))
    low = v.lower().replace(" ", "")
    if low in {"1", "amd64", "linux/amd64"}:
        return "linux/amd64"
    if low in {"2", "multi", "multiarch", "linux/amd64,linux/arm64"}:
        return "linux/amd64,linux/arm64"
    if low in {"arm64", "linux/arm64"}:
        return "linux/arm64"
    return v or "linux/amd64"


def platforms_to_arches(platforms: str) -> set[str]:
    normalized = normalize_platforms(platforms).lower().replace(" ", "")
    archs: set[str] = set()
    if "linux/amd64" in normalized:
        archs.add("amd64")
    if "linux/arm64" in normalized:
        archs.add("arm64")
    return archs


def get_platforms_for(name: str, explicit_platforms: Optional[str] = None) -> str:
    if explicit_platforms:
        return normalize_platforms(explicit_platforms)

    env_platforms = os.environ.get("PLATFORMS")
    if env_platforms:
        return normalize_platforms(env_platforms)

    data = normalize_named_map(read_kv_file(PLATFORMS_FILE))
    clean_name = normalize_item_name(name)
    if clean_name in data and data[clean_name]:
        return normalize_platforms(data[clean_name])
    if "_default" in data and data["_default"]:
        return normalize_platforms(data["_default"])
    return normalize_platforms(DEFAULT_PLATFORMS)


# ============================================================
# Registre : registre.conf + override --registry
# ============================================================


@dataclass
class RegistryEntry:
    name: str
    target: str


def build_target_from_prefix(prefix: str, name: str, tag: str = DEFAULT_TAG) -> str:
    prefix = _strip_quotes(prefix).strip().rstrip("/")
    prefix = re.sub(r"^https?://", "", prefix)
    name = normalize_item_name(name)
    if not prefix:
        return name + ":" + tag
    return f"{prefix}/{name}:{tag}"


def registry_host_from_target(target: str) -> str:
    t = target.strip()
    t = re.sub(r"^https?://", "", t)
    first = t.split("/", 1)[0]

    # Docker Hub : sftpmalin/dockerup/name:latest n'a pas de domaine explicite.
    if "." not in first and ":" not in first and first != "localhost":
        return "registry-1.docker.io"
    return first


def normalize_registry_mode(value: str) -> str:
    """Retourne 'http' ou 'https' depuis mode.conf.

    Convention demandée :
      0 = HTTP local / TLS désactivé côté regctl
      1 = HTTPS normal
    On accepte aussi quelques mots lisibles pour rester pratique.
    """
    low = trim(str(value or "")).lower()
    if low in {"0", "http", "local", "insecure", "disabled", "tls_disabled", "no_tls"}:
        return "http"
    if low in {"1", "https", "secure", "tls", "enabled"}:
        return "https"
    # Défaut volontaire : HTTPS, pour ne pas rendre un registre distant insecure par erreur.
    return "https"


def get_registry_mode_for(name: str) -> str:
    data = normalize_named_map(read_kv_file(effective_mode_file()))
    clean_name = normalize_item_name(name)
    raw = data.get(clean_name, data.get("_default", "0"))
    return normalize_registry_mode(raw)


def regctl_host_args_for_entry(entry: RegistryEntry) -> list[str]:
    """Ajoute la configuration regctl uniquement pour les entrées en HTTP.

    Exemple généré :
      --host reg=192.168.1.2:5000,tls=disabled
    """
    mode = get_registry_mode_for(entry.name)
    if mode != "http":
        return []
    host = registry_host_from_target(entry.target)
    return ["--host", f"reg={host},tls=disabled"]


def load_registry_entries(registry_override: Optional[str] = None) -> list[RegistryEntry]:
    entries: list[RegistryEntry] = []

    if REGISTRY_FILE.exists():
        seen: set[str] = set()
        for key, target in read_kv_rows(REGISTRY_FILE):
            if key == "_default":
                continue
            name = normalize_item_name(key)
            if not is_valid_name(name) or name in seen:
                continue
            seen.add(name)
            if registry_override:
                target = build_target_from_prefix(registry_override, name)
            if target:
                entries.append(RegistryEntry(name=name, target=target))
        return entries

    # Si registre.conf est absent mais que tu donnes --registry, on peut pousser tous les TAR trouvés.
    if registry_override:
        for tar_path in sorted(TAR_DIR.glob("*.tar")):
            name = strip_tar_suffix(tar_path.name)
            entries.append(RegistryEntry(name=name, target=build_target_from_prefix(registry_override, name)))
        return entries

    raise FileNotFoundError(
        f"Fichier registre introuvable : {REGISTRY_FILE}\n"
        "Crée-le avec le format : meteo=registry.sftpmalin.com/meteo:latest\n"
        "Ou utilise --registry registry.sftpmalin.com pour construire les cibles automatiquement."
    )


def find_registry_entry(name: str, registry_override: Optional[str] = None) -> RegistryEntry:
    wanted = normalize_item_name(name)
    entries = load_registry_entries(registry_override)
    for entry in entries:
        if entry.name == wanted:
            return entry

    if registry_override:
        # Mode pratique : si le TAR existe et que --registry est fourni, on accepte le nom même absent du registre.conf.
        tar_path = TAR_DIR / f"{wanted}.tar"
        if tar_path.exists():
            return RegistryEntry(name=wanted, target=build_target_from_prefix(registry_override, wanted))

    raise KeyError(f"Nom inconnu dans le registre : {wanted}")


def regctl_command() -> str:
    configured = str(REGCTL).strip() or "regctl"
    path = Path(configured)
    if path.is_absolute():
        return configured
    found = shutil.which(configured)
    return found or configured


def ensure_registry_login(target: str, log: TeeLogger, dry_run: bool = False) -> bool:
    if dry_run:
        return True

    login_data = read_kv_file(REGISTRY_LOGIN_FILE)
    default_host = registry_host_from_target(target)

    host = login_data.get("REGISTRY_HOST", default_host)
    user = login_data.get("REGISTRY_USER", "")
    password = login_data.get("REGISTRY_PASS", "") or login_data.get("REGISTRY_PASSWORD", "")
    password_file = login_data.get("REGISTRY_PASS_FILE", "")

    if not host:
        log.write("⚠️ Hôte registre introuvable. Login automatique ignoré.")
        return True

    if not user:
        log.write(f"⚠️ Login registre non configuré : {REGISTRY_LOGIN_FILE}")
        log.write("⚠️ L'import continue, mais il échouera si le registre demande une authentification.")
        return True

    if not password and password_file:
        try:
            password = Path(password_file).read_text(encoding="utf-8", errors="replace").strip()
        except FileNotFoundError:
            password = ""

    if not password:
        log.write(f"⚠️ Mot de passe registre non configuré : {REGISTRY_LOGIN_FILE}")
        log.write("⚠️ Renseigne REGISTRY_PASS ou REGISTRY_PASS_FILE.")
        return True

    regctl = regctl_command()
    if Path(regctl).is_absolute() and not Path(regctl).exists():
        log.write(f"❌ regctl introuvable : {regctl}")
        return False

    log.write(f">>> Login registre : {host}")
    cp = run_capture([regctl, "registry", "login", host, "-u", user, "--pass-stdin"], input_text=password)
    if cp.returncode == 0:
        log.write(f"✅ Login registre OK : {host}")
        return True

    if cp.stdout:
        log.write(cp.stdout.rstrip())
    if cp.stderr:
        log.write(cp.stderr.rstrip())
    log.write(f"❌ Login registre échoué : {host}")
    return False


# ============================================================
# Hash contexte + validation TAR OCI
# ============================================================


EXCLUDED_NAMES = {".DS_Store"}
EXCLUDED_SUFFIXES = {".pyc", ".tmp", ".log"}
EXCLUDED_DIRS = {".git", "__pycache__"}


def iter_context_files(context_dir: Path) -> Iterable[Path]:
    for root, dirs, files in os.walk(context_dir):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        root_path = Path(root)
        for filename in files:
            if filename in EXCLUDED_NAMES:
                continue
            if any(filename.endswith(suffix) for suffix in EXCLUDED_SUFFIXES):
                continue
            yield root_path / filename


def context_hash(context_dir: Path) -> str:
    """Hash compatible dans l'esprit avec le Bash : hash des hash de fichiers triés."""
    rows: list[tuple[str, str]] = []
    for path in iter_context_files(context_dir):
        rel = "./" + path.relative_to(context_dir).as_posix()
        h = hashlib.sha256()
        with path.open("rb") as fp:
            for chunk in iter(lambda: fp.read(1024 * 1024), b""):
                h.update(chunk)
        rows.append((rel, h.hexdigest()))

    rows.sort(key=lambda item: item[0])
    combined = "".join(f"{digest}  {rel}\n" for rel, digest in rows)
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def saved_sha_hash(sha_file: Path) -> str:
    text = read_text_or_empty(sha_file)
    return text.split()[0] if text.split() else ""


def tar_sha_ok(tar_file: Path, sha_file: Path) -> bool:
    if not tar_file.exists() or not sha_file.exists():
        return False
    current = file_sha256(tar_file)
    saved = saved_sha_hash(sha_file)
    return bool(current and saved and current == saved)


def write_sha_file(tar_file: Path, sha_file: Path) -> None:
    digest = file_sha256(tar_file)
    write_text(sha_file, f"{digest}  {tar_file}\n")


def tar_is_oci(tar_file: Path) -> bool:
    try:
        with tarfile.open(tar_file, "r") as tf:
            return any(m.name == "index.json" for m in tf.getmembers())
    except Exception:
        return False


def tar_architectures(tar_file: Path) -> set[str]:
    archs: set[str] = set()
    try:
        with tarfile.open(tar_file, "r") as tf:
            member = tf.getmember("index.json")
            fp = tf.extractfile(member)
            if fp is None:
                return archs
            data = json.loads(fp.read().decode("utf-8", errors="replace"))
    except Exception:
        return archs

    def walk(obj) -> None:
        if isinstance(obj, dict):
            platform = obj.get("platform")
            if isinstance(platform, dict) and isinstance(platform.get("architecture"), str):
                archs.add(platform["architecture"])
            if isinstance(obj.get("architecture"), str):
                archs.add(obj["architecture"])
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(data)
    return archs


def tar_matches_platforms(tar_file: Path, platforms: str) -> bool:
    """Le TAR doit correspondre exactement aux plateformes demandées par platforms.conf.

    Même correction que côté Flask : un TAR amd64+arm64 n'est plus considéré
    comme à jour si la base demande seulement amd64.
    """
    if not tar_is_oci(tar_file):
        return False
    desired = platforms_to_arches(platforms)
    if not desired:
        return False
    actual = tar_architectures(tar_file)
    return actual == desired


def human_size(path: Path) -> str:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return "?"
    units = ["B", "K", "M", "G", "T"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)}{unit}"
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}B"


# ============================================================
# Buildx / SAVE
# ============================================================


@dataclass
class SaveOptions:
    force: bool = False
    check_updates: bool = False
    no_pull: bool = False
    no_cache: bool = False
    skip_binfmt: bool = False
    keep_builder: bool = False
    platforms: Optional[str] = None


_builder_created = False
_builder_used = False


def bool_text(value: str, default: str = "0") -> bool:
    raw = str(value if value is not None else default).strip().lower()
    if raw == "":
        raw = default
    return raw in {"1", "true", "yes", "on"}


def command_ok(cmd: list[str]) -> bool:
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def docker_cmd(args: list[str]) -> list[str]:
    return [DOCKER_BIN, *list(args)]


def local_buildx_cmd(args: list[str]) -> list[str]:
    return docker_cmd(["buildx", *list(args)])


def docker_cli_config_dir() -> Path:
    DOCKER_CLI_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return DOCKER_CLI_CONFIG_DIR


def docker_cli_mounts() -> list[str]:
    mounts: list[str] = []

    def add_mount(value: str) -> None:
        value = str(value or "").strip()
        if value and value not in mounts:
            mounts.append(value)

    for part in str(DOCKER_CLI_MOUNTS or "").split(","):
        add_mount(part)

    for path in (BASE_DIR, TAR_DIR, CONF_DIR, LOG_DIR):
        clean = str(path).strip().rstrip("/")
        if not clean:
            continue
        if clean == "/data" or clean.startswith("/data/"):
            add_mount("/data:/data")
        else:
            add_mount(f"{clean}:{clean}")
    return mounts


def container_buildx_cmd(args: list[str]) -> list[str]:
    cmd = [
        DOCKER_BIN, "run", "--rm", "-i",
        "-v", f"{DOCKER_SOCK}:{DOCKER_SOCK}",
        "-e", f"DOCKER_HOST=unix://{DOCKER_SOCK}",
        "-v", f"{docker_cli_config_dir()}:/root/.docker",
    ]
    for mount in docker_cli_mounts():
        cmd.extend(["-v", mount])
    cmd.extend([DOCKER_CLI_IMAGE, "docker", "buildx", *list(args)])
    return cmd


def selected_buildx_cmd(args: list[str]) -> list[str]:
    if _BUILDX_BACKEND_SELECTED == "container":
        return container_buildx_cmd(args)
    return local_buildx_cmd(args)


def select_buildx_backend(log: TeeLogger) -> bool:
    global _BUILDX_BACKEND_SELECTED
    if _BUILDX_BACKEND_SELECTED in {"local", "container"}:
        return True

    requested = BUILDX_BACKEND.strip().lower() or "auto"
    fallback_enabled = bool_text(DOCKER_CLI_FALLBACK, "0")

    if requested in {"local", "native"}:
        _BUILDX_BACKEND_SELECTED = "local"
        log.write(">>> Backend buildx imposé : docker local")
        return True

    if requested in {"container", "fallback", "docker-cli", "docker_cli"}:
        if not fallback_enabled:
            log.write("❌ Backend buildx container demandé, mais DOCKER_CLI_FALLBACK=0.")
            return False
        _BUILDX_BACKEND_SELECTED = "container"
        log.write(f">>> Backend buildx imposé : {DOCKER_CLI_IMAGE}")
        log.write(f">>> État client docker/buildx : {docker_cli_config_dir()}")
        return True

    cp = run_capture(local_buildx_cmd(["version"]))
    if cp.returncode == 0:
        _BUILDX_BACKEND_SELECTED = "local"
        first_line = (cp.stdout or "").strip().splitlines()[0] if (cp.stdout or "").strip() else "OK"
        log.write(f">>> buildx local disponible : {first_line}")
        return True

    if not fallback_enabled:
        log.write("❌ docker buildx est absent et le fallback Docker CLI est désactivé.")
        diag = ((cp.stdout or "") + (cp.stderr or "")).strip()
        if diag:
            log.write("--- Diagnostic buildx local ---")
            log.write(diag)
        return False

    _BUILDX_BACKEND_SELECTED = "container"
    log.write("⚠️  docker buildx absent : bascule automatique sur un client Docker officiel.")
    log.write(f">>> Image client Docker : {DOCKER_CLI_IMAGE}")
    log.write(f">>> État client docker/buildx : {docker_cli_config_dir()}")
    log.write(">>> Montage chemins : " + ", ".join(docker_cli_mounts()))
    return True


def prepare_buildx(log: TeeLogger) -> None:
    global _builder_created, _builder_used
    _builder_used = True

    if not select_buildx_backend(log):
        raise RuntimeError("Impossible de préparer docker buildx")

    log.write(f">>> Vérification builder buildx : {BUILDER_NAME}")
    cp = run_capture(selected_buildx_cmd(["inspect", BUILDER_NAME]))
    if cp.returncode == 0:
        log.write(f">>> Utilisation builder buildx existant : {BUILDER_NAME}")
        if not DEFAULT_KEEP_BUILDX_BUILDER:
            log.write(">>> Nettoyage prévu en fin de build, sauf --keep-builder / KEEP_BUILDX_BUILDER=1")
        status = run_stream(selected_buildx_cmd(["use", BUILDER_NAME]), log)
        if status != 0:
            raise RuntimeError(f"Échec sélection buildx builder : {BUILDER_NAME}")
    else:
        log.write(f">>> Builder buildx absent/non prêt : création de {BUILDER_NAME}")
        status = run_stream(selected_buildx_cmd(["create", "--name", BUILDER_NAME, "--driver-opt", "network=host", "--use"]), log)
        if status != 0:
            diag = ((cp.stdout or "") + (cp.stderr or "")).strip()
            if diag:
                log.write("--- Diagnostic inspect buildx ---")
                log.write(diag)
            raise RuntimeError(f"Échec création buildx builder : {BUILDER_NAME}")
        _builder_created = True

    status = run_stream(selected_buildx_cmd(["inspect", "--bootstrap"]), log)
    if status != 0:
        raise RuntimeError("Échec bootstrap buildx")


def docker_names_by_prefix(prefix: str) -> list[str]:
    cp = run_capture(docker_cmd([
        "ps", "-a",
        "--filter", f"name={prefix}",
        "--format", "{{.Names}}",
    ]))
    if cp.returncode != 0:
        return []
    return [line.strip() for line in cp.stdout.splitlines() if line.strip()]


def docker_images_for_containers(names: list[str]) -> set[str]:
    images: set[str] = set()
    for name in names:
        cp = run_capture(docker_cmd(["inspect", "-f", "{{.Config.Image}}", name]))
        if cp.returncode == 0:
            image = cp.stdout.strip()
            if image:
                images.add(image)
    return images


def docker_volumes_by_prefix(prefix: str) -> list[str]:
    cp = run_capture(docker_cmd([
        "volume", "ls",
        "--filter", f"name={prefix}",
        "--format", "{{.Name}}",
    ]))
    if cp.returncode != 0:
        return []
    return [line.strip() for line in cp.stdout.splitlines() if line.strip()]


def cleanup_builder(keep_builder: bool, log: Optional[TeeLogger] = None) -> None:
    """Nettoie le builder buildx docker-container et ses restes."""
    global _builder_created, _builder_used, _BUILDX_BACKEND_SELECTED

    if not _builder_used and not _builder_created:
        return

    if keep_builder:
        if log:
            log.write(f">>> Builder conservé : {BUILDER_NAME} (--keep-builder / KEEP_BUILDX_BUILDER=1)")
        _builder_created = False
        _builder_used = False
        return

    container_prefix = f"buildx_buildkit_{BUILDER_NAME}"
    containers = docker_names_by_prefix(container_prefix)
    images = docker_images_for_containers(containers)

    if log:
        log.write(f">>> Nettoyage builder buildx : {BUILDER_NAME}")

    subprocess.run(selected_buildx_cmd(["rm", "-f", BUILDER_NAME]), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    containers = sorted(set(containers) | set(docker_names_by_prefix(container_prefix)))
    if containers:
        if log:
            log.write(">>> Suppression conteneur(s) buildkit : " + ", ".join(containers))
        subprocess.run(docker_cmd(["rm", "-f", *containers]), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    volumes = docker_volumes_by_prefix(container_prefix)
    if volumes:
        if log:
            log.write(">>> Suppression volume(s) buildkit : " + ", ".join(volumes))
        subprocess.run(docker_cmd(["volume", "rm", "-f", *volumes]), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if DEFAULT_CLEAN_BUILDX_IMAGE:
        if not images:
            images.add("moby/buildkit:buildx-stable-1")
        for image in sorted(images):
            if log:
                log.write(f">>> Suppression image buildkit si inutilisée : {image}")
            subprocess.run(docker_cmd(["rmi", image]), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    _builder_created = False
    _builder_used = False

def need_arm64(platforms: str) -> bool:
    return "linux/arm64" in platforms


def find_dockerfile(context_dir: Path) -> Optional[Path]:
    for name in ("Dockerfile", "dockerfile"):
        p = context_dir / name
        if p.exists():
            return p
    return None


def save_one(name: str, options: SaveOptions) -> bool:
    name = normalize_item_name(name)
    if not is_valid_name(name):
        print(f"❌ Nom Docker invalide : {name}")
        return False
    context_dir = BASE_DIR / name
    platforms = get_platforms_for(name, options.platforms)

    tar_file = TAR_DIR / f"{name}.tar"
    tmp_file = TAR_DIR / f"{name}.tar.tmp"
    sha_file = TAR_DIR / f"{name}.tar.sha256"
    log_file = LOG_DIR / f"save_{name}.log"
    image = f"localbackup/{name}:backup"

    state_prefix = STATE_DIR / name
    state_context = Path(str(state_prefix) + ".context.sha256")
    state_platforms = Path(str(state_prefix) + ".platforms")
    state_tar_hash = Path(str(state_prefix) + ".tar.sha256")

    with TeeLogger(log_file) as log:
        log.write("=== SAVE ONE DOCKER IMAGE OCI / BUILDX ===")
        log.write(f"Date          : {now_text()}")
        log.write(f"NAME          : {name}")
        log.write(f"CONTEXT       : {context_dir}")
        log.write(f"PLATFORMS     : {platforms}")
        log.write(f"TAR_FILE      : {tar_file}")
        log.write(f"CONF_DIR      : {CONF_DIR}")
        log.write(f"LOG_DIR       : {LOG_DIR}")
        log.write(f"STATE_DIR     : {STATE_DIR}")
        log.write(f"BUILDER       : {BUILDER_NAME}")
        log.write(f"NO_CACHE      : {int(options.no_cache)}")
        log.write(f"FORCE         : {int(options.force)}")
        log.write(f"CHECK_UPDATES : {int(options.check_updates)}")
        log.write(f"NO_PULL       : {int(options.no_pull)}")
        log.write(f"SKIP_BINFMT   : {int(options.skip_binfmt)}")
        log.write(f"KEEP_BUILDER  : {int(options.keep_builder)}")
        log.write("")

        if not context_dir.is_dir():
            log.write(f"❌ Dossier Docker introuvable : {context_dir}")
            return False

        dockerfile = find_dockerfile(context_dir)
        if dockerfile is None:
            log.write(f"❌ Aucun Dockerfile trouvé dans : {context_dir}")
            return False

        current_context_hash = context_hash(context_dir)
        old_context_hash = read_text_or_empty(state_context)
        old_platforms = read_text_or_empty(state_platforms)
        old_tar_hash = read_text_or_empty(state_tar_hash) or saved_sha_hash(sha_file)

        log.write(f"Contexte actuel : {current_context_hash}")
        log.write(f"Ancien contexte : {old_context_hash or 'aucun'}")
        log.write(f"Anciennes plateformes : {old_platforms or 'aucune'}")

        # Skip direct : contexte inchangé + TAR OK + plateformes identiques.
        if (
            not options.force
            and not options.check_updates
            and not options.no_cache
            and tar_sha_ok(tar_file, sha_file)
            and current_context_hash == old_context_hash
            and platforms == old_platforms
        ):
            log.write("⏭️  SKIP DIRECT : contexte inchangé + TAR déjà bon. Rien à refaire.")
            log.write(f"TAR : {tar_file}")
            return True

        # Adoption d'un TAR existant si état absent.
        if (
            not options.force
            and not options.check_updates
            and not options.no_cache
            and not old_context_hash
            and not old_platforms
            and tar_sha_ok(tar_file, sha_file)
            and tar_matches_platforms(tar_file, platforms)
        ):
            current_tar_hash = file_sha256(tar_file)
            write_text(state_context, current_context_hash + "\n")
            write_text(state_platforms, platforms + "\n")
            write_text(state_tar_hash, current_tar_hash + "\n")
            log.write("⏭️  ADOPT + SKIP : TAR OCI existant validé, état créé, rien à refaire.")
            log.write(f"TAR : {tar_file}")
            return True

        if need_arm64(platforms) and not options.skip_binfmt:
            log.write(">>> Installation binfmt pour ARM64/multi-arch")
            status = run_stream(docker_cmd(["run", "--privileged", "--rm", "tonistiigi/binfmt", "--install", "all"]), log)
            if status != 0:
                log.write("❌ Échec tonistiigi/binfmt")
                return False

        try:
            prepare_buildx(log)
            log.write("")
            log.write(f">>> BUILDX OCI TAR : {name}")
            try:
                tmp_file.unlink()
            except FileNotFoundError:
                pass

            cmd = selected_buildx_cmd([
                "build",
                "--platform", platforms,
                "-t", image,
                "--network=host",
                "-f", str(dockerfile),
                "--output", f"type=oci,dest={tmp_file}",
            ])
            if not options.no_pull:
                cmd.append("--pull")
            if options.no_cache:
                cmd.append("--no-cache")
            cmd.append(str(context_dir))

            start = time.time()
            status = run_stream(cmd, log)
            duration = int(time.time() - start)

            if status != 0:
                log.write(f"❌ BUILDX ÉCHEC : {name}")
                try:
                    tmp_file.unlink()
                except FileNotFoundError:
                    pass
                return False

            if not tmp_file.exists():
                log.write(f"❌ TAR temporaire introuvable après build : {tmp_file}")
                return False

            new_tar_hash = file_sha256(tmp_file)
            log.write(f"✅ BUILDX OK : {name} ({platforms}, {human_size(tmp_file)}, {duration}s)")

            # Résultat identique : on garde l'ancien TAR.
            if (
                not options.force
                and tar_file.exists()
                and tar_sha_ok(tar_file, sha_file)
                and old_tar_hash
                and new_tar_hash == old_tar_hash
            ):
                tmp_file.unlink(missing_ok=True)
                write_text(state_context, current_context_hash + "\n")
                write_text(state_platforms, platforms + "\n")
                write_text(state_tar_hash, new_tar_hash + "\n")
                log.write("⏭️  SKIP TAR : résultat identique, ancien TAR gardé.")
                log.write(f"TAR : {tar_file}")
                return True

            tmp_file.replace(tar_file)
            write_sha_file(tar_file, sha_file)
            write_text(state_context, current_context_hash + "\n")
            write_text(state_platforms, platforms + "\n")
            write_text(state_tar_hash, new_tar_hash + "\n")

            log.write(f"✅ TAR OK : {tar_file} ({human_size(tar_file)})")
            log.write(f"✅ SHA OK : {sha_file}")
            log.write(f"✅ ÉTAT OK : {state_prefix}.*")
            os.sync()
            return True
        finally:
            cleanup_builder(options.keep_builder, log)


def list_projects() -> list[str]:
    if not BASE_DIR.is_dir():
        raise FileNotFoundError(f"Dossier build introuvable : {BASE_DIR}")
    return sorted((p.name for p in BASE_DIR.iterdir() if p.is_dir() and is_valid_name(p.name)), key=str.lower)


def save_all(options: SaveOptions) -> int:
    ensure_dirs()
    projects = list_projects()
    log_file = LOG_DIR / "builds_python.log"

    with NonBlockingLock(LOCK_FILE), TeeLogger(log_file) as log:
        total = len(projects)
        done = 0
        missing_dockerfile = 0
        failed = 0

        log.write("=== SAVE ALL DOCKER BUILDS -> TAR OCI ===")
        log.write(f"Date           : {now_text()}")
        log.write(f"BASE_DIR       : {BASE_DIR}")
        log.write(f"TAR_DIR        : {TAR_DIR}")
        log.write(f"CONF_DIR       : {CONF_DIR}")
        log.write(f"LOG_DIR        : {LOG_DIR}")
        log.write(f"PLATFORMS_FILE : {PLATFORMS_FILE}")
        log.write(f"DEFAULT        : {DEFAULT_PLATFORMS}")
        log.write(f"TOTAL          : {total}")
        log.write("")

        if not projects:
            log.write(f"❌ Aucun dossier trouvé dans : {BASE_DIR}")
            return 1

        if not PLATFORMS_FILE.exists():
            log.write(f"⚠️ Fichier plateformes absent : {PLATFORMS_FILE}")
            log.write(f"⚠️ Utilisation du défaut : {DEFAULT_PLATFORMS}")

        for idx, name in enumerate(projects, start=1):
            context = BASE_DIR / name
            platforms = get_platforms_for(name, options.platforms)
            log.write("")
            log.write("============================================================")
            log.write(f"[{idx}/{total}] NOM        : {name}")
            log.write(f"[{idx}/{total}] CONTEXTE   : {context}")
            log.write(f"[{idx}/{total}] PLATFORMS  : {platforms}")
            log.write("============================================================")

            if find_dockerfile(context) is None:
                log.write(f"⚠️ Aucun Dockerfile trouvé, ignoré : {context}")
                missing_dockerfile += 1
                continue

            start = time.time()
            ok = save_one(name, options)
            duration = int(time.time() - start)
            if ok:
                log.write(f"✅ OK/SKIP : {name} ({duration}s)")
                done += 1
            else:
                log.write(f"❌ ÉCHEC : {name} ({duration}s)")
                failed += 1

        os.sync()
        log.write("")
        log.write("==================== RÉSUMÉ SAVE ====================")
        log.write(f"Total dossiers       : {total}")
        log.write(f"OK ou skip           : {done}")
        log.write(f"Sans Dockerfile      : {missing_dockerfile}")
        log.write(f"Échecs               : {failed}")
        log.write(f"Log                  : {log_file}")
        log.write("=====================================================")

        if failed == 0:
            log.write("✅ SAVE terminé.")
            return 0
        log.write(f"⚠️ SAVE terminé avec erreurs. Regarde le log : {log_file}")
        return 1


def save_selected(name: str, options: SaveOptions) -> int:
    ensure_dirs()
    with NonBlockingLock(LOCK_FILE):
        return 0 if save_one(name, options) else 1


# ============================================================
# LOAD : import TAR OCI direct vers registre
# ============================================================


@dataclass
class LoadOptions:
    dry_run: bool = False
    registry_override: Optional[str] = None


def ensure_regctl(log: TeeLogger) -> bool:
    regctl = regctl_command()
    path = Path(regctl)
    if path.is_absolute() and not path.exists():
        log.write(f"❌ regctl introuvable : {regctl}")
        log.write("Télécharge-le avec :")
        log.write("mkdir -p ../bin && curl -L https://github.com/regclient/regclient/releases/latest/download/regctl-linux-amd64 -o ../bin/regctl && chmod 755 ../bin/regctl")
        return False
    try:
        if path.is_absolute() and path.exists():
            path.chmod(path.stat().st_mode | 0o111)
    except Exception:
        pass
    cp = run_capture([regctl, "version"])
    if cp.returncode != 0:
        log.write(f"❌ regctl existe mais ne s'exécute pas : {regctl}")
        if cp.stdout:
            log.write(cp.stdout.rstrip())
        if cp.stderr:
            log.write(cp.stderr.rstrip())
        return False
    return True


def registry_state_paths(name: str) -> tuple[Path, Path]:
    clean = normalize_item_name(name)
    return (
        STATE_DIR / f"{clean}.registry.tar.sha256",
        STATE_DIR / f"{clean}.registry.target",
    )


def mark_registry_import_state(name: str, target: str, tar_path: Path) -> None:
    saved = saved_sha_hash(Path(str(tar_path) + ".sha256"))
    if not saved:
        return
    hash_path, target_path = registry_state_paths(name)
    write_text(hash_path, saved + "\n")
    write_text(target_path, target + "\n")


def import_one(entry: RegistryEntry, log: TeeLogger, options: LoadOptions, count: int = 1, total: int = 1) -> bool:
    name = normalize_item_name(entry.name)
    tar_path = TAR_DIR / f"{name}.tar"
    sha_file = TAR_DIR / f"{name}.tar.sha256"

    log.write("")
    log.write("============================================================")
    log.write(f"[{count}/{total}] NOM    : {name}")
    log.write(f"[{count}/{total}] TAR    : {tar_path.name}")
    mode = get_registry_mode_for(name)
    host_args = regctl_host_args_for_entry(entry)

    log.write(f"[{count}/{total}] IMAGE  : {entry.target}")
    log.write(f"[{count}/{total}] MODE   : {'HTTP local' if mode == 'http' else 'HTTPS'}")
    if host_args:
        log.write(f"[{count}/{total}] REGCTL : {' '.join(host_args)}")
    log.write("============================================================")

    if not tar_path.exists():
        log.write(f"❌ MANQUANT : {tar_path}")
        return False

    log.write(f"Taille : {human_size(tar_path)}")

    if sha_file.exists():
        log.write(">>> Vérification SHA256...")
        if not tar_sha_ok(tar_path, sha_file):
            log.write(f"❌ SHA256 ÉCHEC : {sha_file}")
            return False
        log.write(f"✅ SHA256 OK : {sha_file}")

    if options.dry_run:
        log.write("DRY-RUN : import ignoré")
        return True

    log.write(">>> Import direct vers le registre...")
    start = time.time()
    cmd = [regctl_command(), *host_args, "image", "import", entry.target, str(tar_path)]
    status = run_stream(cmd, log)
    duration = int(time.time() - start)

    if status == 0:
        mark_registry_import_state(name, entry.target, tar_path)
        log.write(f"✅ OK : {entry.target} ({duration}s)")
        log.write("✅ ÉTAT REGISTRE OK : import mémorisé localement.")
        return True

    log.write(f"❌ IMPORT ÉCHEC : {entry.target}")
    return False


def load_all(options: LoadOptions) -> int:
    ensure_dirs()
    log_file = LOG_DIR / "registry_python.log"
    with TeeLogger(log_file) as log:
        try:
            entries = load_registry_entries(options.registry_override)
        except Exception as exc:
            log.write(f"❌ {exc}")
            return 1

        total = len(entries)
        done = 0
        missing_or_failed = 0

        log.write("=== LOAD IMPORT TAR -> REGISTRY ===")
        log.write(f"Date          : {now_text()}")
        log.write(f"REGCTL        : {REGCTL}")
        log.write(f"TAR_DIR       : {TAR_DIR}")
        log.write(f"REGISTRY_FILE : {REGISTRY_FILE}")
        log.write(f"MODE_FILE     : {MODE_FILE}")
        log.write(f"CONF_DIR      : {CONF_DIR}")
        log.write(f"LOG_DIR       : {LOG_DIR}")
        log.write(f"LOG_FILE      : {log_file}")
        log.write(f"TOTAL         : {total}")
        if options.registry_override:
            log.write(f"REGISTRY OVERRIDE : {options.registry_override}")
        if options.dry_run:
            log.write("MODE          : DRY-RUN")
        log.write("")

        if total == 0:
            log.write("❌ Aucune entrée à importer.")
            return 1

        if not ensure_regctl(log):
            return 1

        login_entry = next((entry for entry in entries if get_registry_mode_for(entry.name) != "http"), None)
        if login_entry is None:
            log.write(">>> Login registre ignoré : toutes les entrées sont en mode HTTP/local.")
        elif not ensure_registry_login(login_entry.target, log, dry_run=options.dry_run):
            return 1

        for idx, entry in enumerate(entries, start=1):
            ok = import_one(entry, log, options, idx, total)
            if ok:
                done += 1
            else:
                missing_or_failed += 1

        os.sync()
        log.write("")
        log.write("==================== RÉSUMÉ ====================")
        log.write(f"Total       : {total}")
        log.write(f"Importés   : {done}")
        log.write(f"Échecs/manquants : {missing_or_failed}")
        log.write(f"Log        : {log_file}")
        log.write("================================================")

        if missing_or_failed == 0:
            log.write("✅ LOAD terminé avec succès.")
            return 0
        log.write(f"⚠️ LOAD terminé avec erreurs. Regarde le log : {log_file}")
        return 1


def load_selected(name: str, options: LoadOptions) -> int:
    ensure_dirs()
    log_file = LOG_DIR / "registry_python.log"
    with TeeLogger(log_file) as log:
        try:
            entry = find_registry_entry(name, options.registry_override)
        except Exception as exc:
            log.write(f"❌ {exc}")
            return 1

        log.write("=== LOADT IMPORT TAR -> REGISTRY ===")
        log.write(f"Date          : {now_text()}")
        log.write(f"Nom           : {entry.name}")
        log.write(f"TAR           : {TAR_DIR / (entry.name + '.tar')}")
        log.write(f"REGISTRY_FILE : {REGISTRY_FILE}")
        log.write(f"MODE_FILE     : {MODE_FILE}")
        log.write(f"CONF_DIR      : {CONF_DIR}")
        log.write(f"LOG_DIR       : {LOG_DIR}")
        log.write(f"LOG_FILE      : {log_file}")
        log.write(f"TARGET        : {entry.target}")
        log.write(f"DRY_RUN       : {int(options.dry_run)}")
        if options.registry_override:
            log.write(f"REGISTRY OVERRIDE : {options.registry_override}")
        log.write("")

        if not ensure_regctl(log):
            return 1
        if get_registry_mode_for(entry.name) == "http":
            log.write(">>> Login registre ignoré : entrée en mode HTTP/local.")
        elif not ensure_registry_login(entry.target, log, dry_run=options.dry_run):
            return 1
        ok = import_one(entry, log, options, 1, 1)
        if ok:
            os.sync()
            return 0
        return 1


# ============================================================
# Menus interactifs
# ============================================================


def print_two_columns(items: list[str]) -> None:
    total = len(items)
    rows = (total + 1) // 2
    width = max((terminal_cols() - 4) // 2, 38)
    for i in range(rows):
        left = f"{i + 1:2d}) {items[i]}"
        print(left[:width].ljust(width), end="")
        right_index = i + rows
        if right_index < total:
            right = f"{right_index + 1:2d}) {items[right_index]}"
            print("  " + right)
        else:
            print()


def choose_from_list(title: str, subtitle: str, items: list[str]) -> Optional[str]:
    while True:
        os.system("clear 2>/dev/null || true")
        print("============================================================")
        print(f" {title}")
        print(f" {subtitle}")
        print("============================================================")
        print("0) Retour")
        print()
        print_two_columns(items)
        print()
        choice = input("Votre choix : ").strip()
        if choice == "0":
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(items):
            return items[int(choice) - 1]
        print("Choix invalide.")
        time.sleep(1)


def choose_save_project() -> Optional[str]:
    projects = list_projects()
    if not projects:
        print(f"❌ Aucun dossier trouvé dans : {BASE_DIR}")
        return None
    return choose_from_list("Choisis le Docker à builder en TAR", f"Dossier : {BASE_DIR}", projects)


def tar_status_for_name(name: str) -> str:
    tar_file = TAR_DIR / f"{strip_tar_suffix(name)}.tar"
    if tar_file.exists():
        return f"OK {human_size(tar_file)}"
    return "MANQUANT"


def print_registry_entries_readable(entries: list[RegistryEntry]) -> None:
    """Affiche les entrées registre en mode lisible, une entrée par bloc.

    Important : on évite volontairement les deux colonnes ici, car la cible
    registre peut être longue et rendait le menu --select --load illisible.
    """
    if not entries:
        print("Aucune entrée registre.")
        return

    for idx, entry in enumerate(entries, start=1):
        status = tar_status_for_name(entry.name)
        print(f"{idx:2d}) {entry.name}")
        print(f"    TAR    : {status}")
        print(f"    IMAGE  : {entry.target}")
        print()


def choose_load_entry(options: LoadOptions) -> Optional[str]:
    entries = load_registry_entries(options.registry_override)

    while True:
        os.system("clear 2>/dev/null || true")
        print("============================================================")
        print(" Choisis le TAR à envoyer vers le registre")
        print(f" Registre : {REGISTRY_FILE}")
        print(f" TAR dir  : {TAR_DIR}")
        print("============================================================")
        print("0) Retour")
        print()
        print_registry_entries_readable(entries)

        choice = input("Votre choix : ").strip()
        if choice == "0":
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(entries):
            return entries[int(choice) - 1].name

        print("Choix invalide.")
        time.sleep(1)


def main_menu(args) -> int:
    while True:
        os.system("clear 2>/dev/null || true")
        print("============================================================")
        print(" Docker manager Python")
        print("============================================================")
        print("1) SAVE  - Build dossier Docker vers TAR")
        print("2) LOAD  - TAR vers registre")
        print("3) LIST  - Dossiers build")
        print("4) LIST  - Entrées registre / TAR")
        print("0) Quitter")
        print()
        choice = input("Votre choix : ").strip()

        if choice == "0":
            print("Annulé.")
            return 0
        if choice == "1":
            name = choose_save_project()
            if name:
                return save_selected(name, make_save_options(args))
        elif choice == "2":
            options = make_load_options(args)
            name = choose_load_entry(options)
            if name:
                return load_selected(name, options)
        elif choice == "3":
            for p in list_projects():
                print(p)
            input("\nEntrée pour continuer...")
        elif choice == "4":
            options = make_load_options(args)
            for e in load_registry_entries(options.registry_override):
                print(f"{e.name:<30} {tar_status_for_name(e.name):<12} {e.target}")
            input("\nEntrée pour continuer...")
        else:
            print("Choix invalide.")
            time.sleep(1)


# ============================================================
# CLI
# ============================================================


def make_save_options(args) -> SaveOptions:
    return SaveOptions(
        force=args.force,
        check_updates=args.check_updates or args.force,
        no_pull=args.no_pull,
        no_cache=args.no_cache,
        skip_binfmt=args.skip_binfmt,
        keep_builder=args.keep_builder or DEFAULT_KEEP_BUILDX_BUILDER,
        platforms=args.platforms,
    )


def make_load_options(args) -> LoadOptions:
    return LoadOptions(
        dry_run=args.dry_run,
        registry_override=args.registry,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Gestionnaire Docker unique : build -> TAR OCI et TAR OCI -> registre.",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    action = parser.add_mutually_exclusive_group()
    action.add_argument("--save", action="store_true", help="Build un ou tous les dossiers Docker en TAR OCI.")
    action.add_argument("--load", action="store_true", help="Importe un ou tous les TAR OCI vers le registre.")

    parser.add_argument(
        "--select",
        nargs="?",
        const="__MENU__",
        metavar="NOM",
        help=(
            "Sélection interactive ou nom précis.\n"
            "  --select --save        : liste les dossiers à builder\n"
            "  --select --load        : liste les TAR/entrées registre\n"
            "  --select meteo --save  : build seulement meteo\n"
            "  --select meteo --load  : push seulement meteo"
        ),
    )
    parser.add_argument("name", nargs="?", help="Nom optionnel du Docker/TAR, alternative à --select NOM.")

    parser.add_argument("--registry", help="Préfixe registre à utiliser au lieu de registre.conf. Ex: registry.sftpmalin.com ou sftpmalin/dockerup")
    parser.add_argument("--dry-run", action="store_true", help="Mode test pour LOAD : vérifie sans importer.")

    parser.add_argument("--force", action="store_true", help="SAVE : rebuild + remplace le TAR même si tout semble identique.")
    parser.add_argument("--check-updates", action="store_true", help="SAVE : build avec --pull pour vérifier si les FROM ont changé.")
    parser.add_argument("--no-pull", action="store_true", help="SAVE : ne fait pas --pull pendant le build.")
    parser.add_argument("--no-cache", action="store_true", help="SAVE : build sans cache.")
    parser.add_argument("--skip-binfmt", action="store_true", help="SAVE : ne relance pas tonistiigi/binfmt.")
    parser.add_argument("--keep-builder", action="store_true", help="SAVE : ne supprime pas le builder buildx créé par ce script.")
    parser.add_argument("--platforms", help="SAVE : override ponctuel. Ex: linux/amd64,linux/arm64")

    parser.add_argument("--list", choices=["builds", "tars", "registry", "all"], help="Affiche une liste sans action.")

    return parser.parse_args(argv)


def resolve_selected_name(args) -> Optional[str]:
    if args.select and args.select != "__MENU__":
        return strip_tar_suffix(args.select)
    if args.name:
        return strip_tar_suffix(args.name)
    return None


def handle_list(kind: str, args) -> int:
    ensure_dirs()
    if kind in {"builds", "all"}:
        print("=== DOSSIERS BUILD ===")
        for name in list_projects():
            dockerfile = "OK" if find_dockerfile(BASE_DIR / name) else "SANS Dockerfile"
            print(f"{name:<32} {dockerfile}")
        print()
    if kind in {"tars", "all"}:
        print("=== TAR ===")
        for tar_path in sorted(TAR_DIR.glob("*.tar")):
            print(f"{tar_path.name:<40} {human_size(tar_path)}")
        print()
    if kind in {"registry", "all"}:
        print("=== REGISTRE ===")
        try:
            entries = load_registry_entries(args.registry)
        except Exception as exc:
            print(f"❌ {exc}")
            return 1
        print_registry_entries_readable(entries)
    return 0


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    ensure_dirs()

    if args.list:
        return handle_list(args.list, args)

    # --select seul : menu principal.
    if args.select == "__MENU__" and not args.save and not args.load:
        return main_menu(args)

    selected_name = resolve_selected_name(args)

    if args.save:
        options = make_save_options(args)
        if args.select == "__MENU__" and selected_name is None:
            name = choose_save_project()
            if not name:
                print("Annulé.")
                return 0
            return save_selected(name, options)
        if selected_name:
            return save_selected(selected_name, options)
        return save_all(options)

    if args.load:
        options = make_load_options(args)
        if args.select == "__MENU__" and selected_name is None:
            name = choose_load_entry(options)
            if not name:
                print("Annulé.")
                return 0
            return load_selected(name, options)
        if selected_name:
            return load_selected(selected_name, options)
        return load_all(options)

    print("❌ Action manquante.")
    print("Utilise --save, --load, --select, ou --help.")
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\nInterrompu.")
        raise SystemExit(130)
    except RuntimeError as exc:
        print(f"❌ {exc}")
        raise SystemExit(1)
