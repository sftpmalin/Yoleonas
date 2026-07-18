#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stacks.py - Gestionnaire Docker Compose LABO Debian.

But :
  Garder l'ancien esprit du script, mais supprimer uniquement la partie CREATE.

Ce script fait :
  - UPDATE / START direct depuis les YAML
  - LIST depuis stacks.conf
  - OLLAMA / REMOVE-OLLAMA
  - login registre si nécessaire selon mode.conf

Ce script ne fait PLUS :
  - pas de --create / --generate
  - pas de génération de docker-compose.yml
  - pas de /boot
  - pas de Compose Manager Unraid
  - pas de PROJECTS_DIR

Principe chemins :
  Le script est prévu dans <base>/scripts/stacks.py.
  Il remonte avec ../ pour retrouver <base>, puis utilise :
  YML_DIR  : <base>/yml
  CONF_DIR : <base>/conf, dont dockers.conf, stacks.conf, mode.conf, registre_login.conf

Format stacks.conf :
  STACK1=base
  YML1=registry.yml
  YML2=nginxproxymanager.yml

  STACK2=media
  YML1=emby.yml
  YML2=metube.yml
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


# ============================================================
# CHEMINS PRINCIPAUX
# ============================================================

# Le script est prévu pour être placé dans :
#   <dossier_labo>/scripts/stacks.py
# On remonte donc avec ../ pour retrouver le dossier LABO.
SCRIPT_DIR = Path(__file__).resolve().parent
LABO_DIR = Path(os.environ.get("LABO_DIR", str((SCRIPT_DIR / "..").resolve()))).resolve()

YML_DIR = Path(os.environ.get("YML_DIR", str(LABO_DIR / "yml"))).resolve()
CONF_DIR = Path(os.environ.get("CONF_DIR", str(LABO_DIR / "conf"))).resolve()

# Tout découle des deux chemins ci-dessus.
STACKS_CONF_FILE = Path(os.environ.get("STACKS_CONF_FILE", str(CONF_DIR / "stacks.conf")))
DOCKERS_CONF_FILE = Path(os.environ.get("DOCKERS_CONF_FILE", str(CONF_DIR / "dockers.conf")))
REGISTRY_LOGIN_FILE = Path(os.environ.get("REGISTRY_LOGIN_FILE", str(CONF_DIR / "registre_login.conf")))
MODE_FILE = Path(os.environ.get("MODE_FILE", str(CONF_DIR / "mode.conf")))


# ============================================================
# RÉSEAU OLLAMA
# ============================================================

DEFAULT_NETWORK_NAME = os.environ.get("STACKS_NETWORK_NAME", "ollama_lan")
DEFAULT_NETWORK_SUBNET = os.environ.get("STACKS_NETWORK_SUBNET", "172.20.0.0/16")
DEFAULT_NETWORK_GATEWAY = os.environ.get("STACKS_NETWORK_GATEWAY", "172.20.0.1")
DEFAULT_NETWORK_BRIDGE = os.environ.get("STACKS_NETWORK_BRIDGE", "ollama_lan")


# ============================================================
# OUTILS GÉNÉRAUX
# ============================================================

def q(cmd: Iterable[str]) -> str:
    return " ".join(shlex.quote(str(x)) for x in cmd)


def info(text: str = "") -> None:
    print(text, flush=True)


def is_valid_kv_line(line: str) -> bool:
    line = line.strip().rstrip("\r")
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
        key = key.strip()
        value = value.strip()

        if not key:
            continue

        try:
            parts = shlex.split(value, posix=True)
            value = parts[0] if parts else ""
        except ValueError:
            value = value.strip("\"'")

        data[key] = value

    return data


def resolve_labo_path(value: str | Path, *, default: str | Path = "") -> Path:
    """Résout les chemins CLI anciens : absolu si déjà absolu, sinon relatif à LABO_DIR."""
    raw = str(value or default or "").strip()
    if not raw:
        raw = str(default or ".")
    p = Path(os.path.expanduser(os.path.expandvars(raw)))
    if not p.is_absolute():
        p = LABO_DIR / p
    return p.resolve()


def resolve_conf_path(value: str | Path, conf_dir: Path, *, default: str | Path = "") -> Path:
    """Résout les chemins issus de dockers.conf depuis le dossier conf.

    C'est plus robuste que le cwd : ../yml dans ../conf/dockers.conf donne bien
    le dossier yml frère de conf, que le script soit lancé depuis scripts/ ou system/.
    """
    raw = str(value or default or "").strip()
    if not raw:
        raw = str(default or ".")
    p = Path(os.path.expanduser(os.path.expandvars(raw)))
    if not p.is_absolute():
        p = conf_dir / p
    return p.resolve()


def conf_bool(conf: dict[str, str], key: str, default: str = "0") -> bool:
    value = os.environ.get(key, conf.get(key, default))
    return str(value).strip().lower() in {"1", "true", "yes", "on", "oui", "y"}


def get_conf_value(conf: dict[str, str], key: str, default: str = "") -> str:
    return os.environ.get(key, conf.get(key, default)).strip()


def cli_has(argv: list[str], *names: str) -> bool:
    for item in argv:
        for name in names:
            if item == name or item.startswith(name + "="):
                return True
    return False


def get_password(conf: dict[str, str]) -> str:
    password = get_conf_value(conf, "REGISTRY_PASS", "")
    if password:
        return password

    password_file = get_conf_value(conf, "REGISTRY_PASS_FILE", "")
    if password_file:
        try:
            return Path(password_file).read_text(encoding="utf-8", errors="replace").strip()
        except FileNotFoundError:
            return ""

    return ""


@dataclass
class RunOptions:
    dry_run: bool = False
    strict_login: bool = False


@dataclass
class StackDefinition:
    name: str
    files: list[str]
    line: int


@dataclass
class StackRuntime:
    name: str
    files: list[Path]
    line: int


def run(cmd: list[str], *, cwd: Optional[Path] = None, input_text: Optional[str] = None, opts: RunOptions) -> int:
    if cwd:
        info(f"$ cd {cwd}")
    info(f"$ {q(cmd)}")

    if opts.dry_run:
        return 0

    if input_text is None:
        env = os.environ.copy()
        env.setdefault("COMPOSE_PROGRESS", "auto")
        completed = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
        )
        return completed.returncode

    completed = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.returncode


def run_check(cmd: list[str], *, cwd: Optional[Path] = None, input_text: Optional[str] = None, opts: RunOptions) -> None:
    rc = run(cmd, cwd=cwd, input_text=input_text, opts=opts)
    if rc != 0:
        raise SystemExit(rc)


def capture_ok(cmd: list[str]) -> bool:
    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
        ).returncode == 0
    except Exception:
        return False


# ============================================================
# MODE REGISTRE : même principe que docker.py
# ============================================================

IMAGE_LINE_RE = re.compile(r"^\s*image\s*:\s*(.+?)\s*$")


def normalize_mode(value: str) -> str:
    """Convention Yoleo : 0 = HTTP local/insecure, 1 = HTTPS."""
    value = (value or "").strip().lower()

    if value in {"0", "http", "local", "insecure", "disabled", "tls_disabled", "no_tls", "no", "false", "off"}:
        return "0"

    if value in {"1", "https", "remote", "secure", "tls", "enabled", "yes", "true", "on"}:
        return "1"

    return value or "0"


def mode_key_candidates(name: str) -> list[str]:
    clean = (name or "").strip()
    out: list[str] = []
    if clean:
        out.append(clean)
        if clean.endswith(".tar"):
            out.append(clean[:-4])
    return out


def mode_for_name(modes: dict[str, str], name: str) -> str:
    for key in mode_key_candidates(name):
        if key in modes:
            return normalize_mode(modes[key])

    for default_key in ("_default", "default", "DEFAULT", "*"):
        if default_key in modes:
            return normalize_mode(modes[default_key])

    # Même défaut que Flask : sans mode.conf, le registre local HTTP reste le cas NAS le plus courant.
    return "0"


def strip_image_value(raw_value: str) -> str:
    value = raw_value.strip()

    if "#" in value:
        value = value.split("#", 1)[0].strip()

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]

    return value.strip()


def normalize_registry_env_value(value: str) -> str:
    """Normalise REGISTRY_HOST/REGISTRY_URL pour Docker/Compose.

    Transforme http://host:port en host:port et corrige l'erreur fréquente
    192.168.1.140.7777 -> 192.168.1.140:7777.
    """
    value = strip_image_value(value or "").strip()
    if not value:
        return ""

    value = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", value)
    value = value.split("/", 1)[0].strip()

    match = re.fullmatch(r"((?:\d{1,3}\.){3}\d{1,3})\.(\d{2,5})", value)
    if match:
        ip, port = match.groups()
        try:
            ipaddress.ip_address(ip)
            port_i = int(port)
            if 1 <= port_i <= 65535:
                return f"{ip}:{port_i}"
        except Exception:
            pass

    return value


def image_name_from_ref(image: str) -> str:
    ref = strip_image_value(image)

    if not ref:
        return ""

    ref = ref.split("@", 1)[0]
    last = ref.rsplit("/", 1)[-1]

    if ":" in last:
        last = last.rsplit(":", 1)[0]

    return last.strip()


def images_from_compose_file(path: Path) -> list[str]:
    images: list[str] = []

    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = IMAGE_LINE_RE.match(raw)

            if not match:
                continue

            image = strip_image_value(match.group(1))

            if image:
                images.append(image)

    except FileNotFoundError:
        pass

    return images


def images_from_stacks(stacks: list[StackRuntime]) -> list[str]:
    images: list[str] = []
    seen: set[str] = set()

    for stack in stacks:
        for compose_file in stack.files:
            for image in images_from_compose_file(compose_file):
                if image not in seen:
                    seen.add(image)
                    images.append(image)

    return images


def registry_login_required_for_update(args: argparse.Namespace, stacks: list[StackRuntime]) -> bool:
    mode_file = effective_mode_file(args)
    modes = read_kv_file(mode_file)
    images = images_from_stacks(stacks)

    if not images:
        info("ℹ️ Login registre ignoré : aucune image détectée dans les YAML.")
        return False

    info(f"🧭 Mode registre : {mode_file} (0=HTTP local, 1=HTTPS)")
    if not modes:
        info("ℹ️ mode.conf absent/vide : défaut Yoleo = HTTP local/0.")

    https_images: list[str] = []
    local_images: list[str] = []

    for image in images:
        name = image_name_from_ref(image)
        mode = mode_for_name(modes, name)

        if mode == "0":
            local_images.append(f"{name} ({image})")
        else:
            https_images.append(f"{name} ({image})")

    if https_images:
        info("🔐 Login registre nécessaire : au moins une image est en mode HTTPS/1.")
        for item in https_images[:12]:
            info(f"   - {item}")
        if len(https_images) > 12:
            info(f"   - ... {len(https_images) - 12} autre(s)")
        return True

    info(f"ℹ️ Login registre ignoré : toutes les images détectées sont en mode HTTP/local dans {mode_file}")
    for item in local_images[:12]:
        info(f"   - {item}")
    if len(local_images) > 12:
        info(f"   - ... {len(local_images) - 12} autre(s)")

    return False




def effective_mode_file(args: argparse.Namespace) -> Path:
    """Retourne le mode.conf réel avec compatibilité anciens noms de fichiers."""
    path = Path(args.mode_file)
    if path.exists():
        return path

    for alt_name in ("mode.conf", "mod.conf", "mod.txt", "mode.txt"):
        alt = path.parent / alt_name
        if alt.exists():
            return alt

    return path


def registry_host_from_image(image: str) -> str:
    ref = strip_image_value(image).split("@", 1)[0]
    if "/" not in ref:
        return ""
    host = ref.split("/", 1)[0].strip()
    if "." in host or ":" in host or host == "localhost":
        return host
    return ""


def http_registries_from_modes(mode_file: Path, stacks: list[StackRuntime]) -> list[str]:
    modes = read_kv_file(mode_file)
    registries: list[str] = []

    for image in images_from_stacks(stacks):
        name = image_name_from_ref(image)
        if mode_for_name(modes, name) != "0":
            continue
        host = registry_host_from_image(image)
        if host and host not in registries:
            registries.append(host)

    return registries


def current_insecure_registries() -> list[str]:
    daemon_json = Path("/etc/docker/daemon.json")
    if not daemon_json.exists():
        return []
    try:
        data = json.loads(daemon_json.read_text(encoding="utf-8", errors="replace") or "{}")
    except Exception:
        return []
    values = data.get("insecure-registries", [])
    if not isinstance(values, list):
        return []
    return [str(v).strip() for v in values if str(v).strip()]


def ensure_insecure_registries(registries: list[str], opts: RunOptions) -> None:
    clean: list[str] = []
    for registry in registries:
        registry = normalize_registry_env_value(registry)
        if registry and registry not in clean:
            clean.append(registry)

    if not clean:
        return

    current = current_insecure_registries()
    missing = [registry for registry in clean if registry not in current]

    if not missing:
        info(f"✅ Registre(s) HTTP déjà autorisé(s) dans Docker : {', '.join(clean)}")
        return

    info(f"🧩 mode.conf demande HTTP/0 : ajout insecure-registries Docker : {', '.join(missing)}")
    if opts.dry_run:
        info("DRY-RUN: /etc/docker/daemon.json serait mis à jour puis Docker redémarré.")
        return

    daemon_dir = Path("/etc/docker")
    daemon_json = daemon_dir / "daemon.json"

    try:
        daemon_dir.mkdir(parents=True, exist_ok=True)
        data: dict = {}

        if daemon_json.exists():
            backup = daemon_json.with_name(f"daemon.json.bak.{time.strftime('%Y%m%d_%H%M%S')}")
            backup.write_text(daemon_json.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            info(f"💾 Sauvegarde Docker : {backup}")
            try:
                loaded = json.loads(daemon_json.read_text(encoding="utf-8", errors="replace") or "{}")
                if isinstance(loaded, dict):
                    data = loaded
            except Exception as exc:
                info(f"⚠️ daemon.json illisible, réécriture propre : {exc}")

        merged = list(current)
        for registry in missing:
            if registry not in merged:
                merged.append(registry)

        data["insecure-registries"] = merged

        tmp = daemon_json.with_name(f"daemon.json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, daemon_json)
        info(f"✅ /etc/docker/daemon.json mis à jour : {', '.join(merged)}")

        rc = run(["systemctl", "restart", "docker"], opts=opts)
        if rc != 0:
            raise SystemExit("❌ Redémarrage Docker impossible après ajout du registre HTTP.")
        info("✅ Docker redémarré avec le registre HTTP autorisé.")
    except Exception as exc:
        raise SystemExit(f"❌ Configuration insecure-registries impossible : {exc}") from exc


def repair_compose_env_registry(args: argparse.Namespace) -> list[str]:
    """Même garde-fou que Flask : normalise les valeurs REGISTRY* dans .env."""
    yml_dir = Path(args.yml_dir)
    candidates: list[Path] = []

    def add(path: Path) -> None:
        try:
            if not path.is_absolute():
                path = (LABO_DIR / path).resolve()
            else:
                path = path.resolve()
        except Exception:
            pass
        if path not in candidates:
            candidates.append(path)

    add(yml_dir / ".env")

    dockers_conf = read_kv_file(Path(args.dockers_conf))
    env_file = dockers_conf.get("ENV_FILE", "").strip()
    if env_file:
        add(resolve_conf_path(env_file, Path(args.dockers_conf).parent))

    registry_keys = {"REGISTRY", "REGISTRY_HOST", "REGISTRY_URL", "DOCKER_REGISTRY", "DOCKER_REGISTRY_HOST"}
    messages: list[str] = []
    key_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

    for env_path in candidates:
        if not env_path.exists() or not env_path.is_file():
            continue
        try:
            original = env_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            messages.append(f"⚠️ Lecture .env impossible : {env_path} ({exc})")
            continue

        changed = False
        new_lines: list[str] = []
        for raw_line in original.splitlines():
            match = key_re.match(raw_line.strip())
            if not match:
                new_lines.append(raw_line)
                continue
            key, raw_value = match.groups()
            if key.upper() not in registry_keys:
                new_lines.append(raw_line)
                continue

            clean_value = normalize_registry_env_value(raw_value)
            if clean_value and clean_value != strip_image_value(raw_value).strip():
                new_lines.append(f"{key}={clean_value}")
                messages.append(f"🛠️ .env corrigé : {env_path} : {key}={clean_value}")
                changed = True
            else:
                new_lines.append(raw_line)

        if changed:
            try:
                suffix = "\n" if original.endswith("\n") else ""
                env_path.write_text("\n".join(new_lines).rstrip("\n") + suffix, encoding="utf-8")
            except Exception as exc:
                messages.append(f"❌ Écriture .env impossible : {env_path} ({exc})")

    return messages


def warn_malformed_registry_images(stacks: list[StackRuntime]) -> list[str]:
    messages: list[str] = []
    seen: set[tuple[str, str]] = set()
    host_re = re.compile(r"^((?:\d{1,3}\.){3}\d{1,3})\.(\d{2,5})(?=/|$)")

    for stack in stacks:
        for compose_file in stack.files:
            for image in images_from_compose_file(compose_file):
                clean_image = strip_image_value(image)
                match = host_re.match(clean_image)
                if not match:
                    continue
                ip, port = match.groups()
                try:
                    ipaddress.ip_address(ip)
                    port_i = int(port)
                    if not (1 <= port_i <= 65535):
                        continue
                except Exception:
                    continue
                fixed = f"{ip}:{port_i}" + clean_image[match.end():]
                key = (str(compose_file), clean_image)
                if key in seen:
                    continue
                seen.add(key)
                messages.append(f"⚠️ Registre mal formé dans {compose_file.name} : {clean_image} -> {fixed}")

    return messages

# ============================================================
# LOGIN REGISTRE
# ============================================================

def login_registry(args: argparse.Namespace, opts: RunOptions) -> None:
    if args.no_login:
        return

    login_file = Path(args.login_file)
    conf = read_kv_file(login_file)

    raw_host = get_conf_value(conf, "REGISTRY_HOST", args.registry_host)
    host = normalize_registry_env_value(raw_host)
    user = get_conf_value(conf, "REGISTRY_USER", "")

    if raw_host and host != raw_host:
        info(f"🛠️ REGISTRY_HOST normalisé : {raw_host} -> {host}")
    password = get_password(conf)

    if not host:
        info("⚠️ Login registre ignoré : REGISTRY_HOST vide.")
        if opts.strict_login:
            raise SystemExit(1)
        return

    if not user:
        info(f"⚠️ Login registre ignoré : REGISTRY_USER absent dans {login_file}")
        if opts.strict_login:
            raise SystemExit(1)
        return

    if not password:
        info(f"⚠️ Login registre ignoré : REGISTRY_PASS ou REGISTRY_PASS_FILE absent dans {login_file}")
        if opts.strict_login:
            raise SystemExit(1)
        return

    retries = max(1, int(getattr(args, "login_retries", 1)))
    wait_s = max(0, int(getattr(args, "login_wait", 0)))

    info(f"🔐 Login registre : {host}")
    last_rc = 1

    for attempt in range(1, retries + 1):
        if retries > 1:
            info(f"➡️  Tentative login {attempt}/{retries}")

        last_rc = run(
            ["docker", "login", host, "-u", user, "--password-stdin"],
            input_text=password,
            opts=opts,
        )

        if last_rc == 0:
            info(f"✅ Login registre OK : {host}")
            return

        if attempt < retries and wait_s > 0 and not opts.dry_run:
            info(f"⏳ Registre pas encore prêt, nouvelle tentative dans {wait_s}s...")
            time.sleep(wait_s)

    info(f"❌ Login registre échoué : {host}")
    raise SystemExit(last_rc)


# ============================================================
# LECTURE stacks.conf + RÉSOLUTION YAML
# ============================================================

def split_csv(value: str) -> list[str]:
    return [x.strip().strip("\"'") for x in value.split(",") if x.strip()]


def clean_conf_value(value: str) -> str:
    value = value.strip().rstrip("\r")

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]

    return value.strip()


def read_stacks_conf(path: Path) -> list[StackDefinition]:
    if not path.exists():
        raise SystemExit(f"❌ Fichier stacks.conf introuvable : {path}")

    stacks: list[StackDefinition] = []
    current: Optional[StackDefinition] = None
    seen_names: set[str] = set()

    for lineno, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        line = raw.strip().rstrip("\r")

        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            raise SystemExit(f"❌ Ligne invalide dans {path}:{lineno} : {raw}")

        key, value = line.split("=", 1)
        key_upper = key.strip().upper()
        value = clean_conf_value(value)

        if key_upper.startswith("STACK"):
            if not value:
                raise SystemExit(f"❌ Nom de stack vide dans {path}:{lineno}")

            normalized = value.lower()

            if normalized in seen_names:
                raise SystemExit(f"❌ Stack en double dans {path}:{lineno} : {value}")

            seen_names.add(normalized)
            current = StackDefinition(name=value, files=[], line=lineno)
            stacks.append(current)
            continue

        if key_upper.startswith("YML") or key_upper.startswith("YAML"):
            if current is None:
                raise SystemExit(f"❌ YML déclaré avant STACK dans {path}:{lineno}")

            values = split_csv(value)

            if not values:
                raise SystemExit(f"❌ YML vide dans {path}:{lineno}")

            current.files.extend(values)
            continue

        info(f"⚠️ Ligne ignorée dans {path}:{lineno} : {key.strip()}")

    for stack in stacks:
        if not stack.files:
            raise SystemExit(f"❌ Stack sans YML dans {path}:{stack.line} : {stack.name}")

    if not stacks:
        raise SystemExit(f"❌ Aucune stack trouvée dans : {path}")

    return stacks


def compose_files_in_yml_dir(yml_dir: Path) -> list[Path]:
    files: list[Path] = []

    for pattern in ("*.yml", "*.yaml"):
        for path in yml_dir.glob(pattern):
            if path.is_file():
                files.append(path)

    return sorted(files, key=lambda p: p.name.lower())


def build_yml_lookup(yml_dir: Path) -> dict[str, Path]:
    lookup: dict[str, Path] = {}

    for path in compose_files_in_yml_dir(yml_dir):
        lookup.setdefault(path.name.lower(), path)
        lookup.setdefault(path.stem.lower(), path)

    return lookup


def candidate_yml_names(raw_name: str) -> list[str]:
    path = Path(raw_name)
    names: list[str] = []

    if path.suffix.lower() == ".xml":
        names.append(path.with_suffix(".yml").name)
        names.append(path.with_suffix(".yaml").name)
        names.append(path.stem)
    elif path.suffix:
        names.append(path.name)
    else:
        names.append(path.name)
        names.append(f"{path.name}.yml")
        names.append(f"{path.name}.yaml")

    return names


def resolve_yml_file(raw_name: str, *, yml_dir: Path, lookup: dict[str, Path]) -> Optional[Path]:
    raw_name = clean_conf_value(raw_name)
    raw_path = Path(raw_name)

    if raw_path.is_absolute() and raw_path.is_file():
        return raw_path

    exact_candidates: list[Path] = []

    for name in candidate_yml_names(raw_name):
        candidate = Path(name)
        exact_candidates.append(candidate if candidate.is_absolute() else yml_dir / candidate)

    for candidate in exact_candidates:
        if candidate.is_file():
            return candidate

    for name in candidate_yml_names(raw_name):
        found = lookup.get(Path(name).name.lower()) or lookup.get(Path(name).stem.lower())

        if found:
            return found

    return None


def runtime_stacks_from_conf(
    conf_file: Path,
    *,
    yml_dir: Path,
    stack_filter: Optional[list[str]] = None,
) -> list[StackRuntime]:
    definitions = read_stacks_conf(conf_file)

    if not yml_dir.is_dir():
        raise SystemExit(f"❌ Dossier YAML introuvable : {yml_dir}")

    wanted = {x.lower() for x in (stack_filter or [])}
    lookup = build_yml_lookup(yml_dir)
    result: list[StackRuntime] = []

    for stack in definitions:
        if wanted and stack.name.lower() not in wanted:
            continue

        resolved_files: list[Path] = []
        missing: list[str] = []

        for raw_name in stack.files:
            resolved = resolve_yml_file(raw_name, yml_dir=yml_dir, lookup=lookup)

            if resolved is None:
                missing.append(raw_name)
            else:
                resolved_files.append(resolved)

        if missing:
            info("")
            info(f"❌ Fichiers YAML introuvables pour la stack [{stack.name}] dans {conf_file}")
            for item in missing:
                info(f"   - {item}")
            info(f"📁 Dossier YAML : {yml_dir}")
            raise SystemExit(1)

        result.append(StackRuntime(name=stack.name, files=resolved_files, line=stack.line))

    if wanted and not result:
        raise SystemExit(f"❌ Aucune stack trouvée dans stacks.conf pour : {', '.join(stack_filter or [])}")

    return result


# ============================================================
# RÉSEAU OLLAMA OPTIONNEL
# ============================================================

def ensure_network(args: argparse.Namespace, opts: RunOptions) -> None:
    name = args.network_name

    if not opts.dry_run and capture_ok(["docker", "network", "inspect", name]):
        info(f"✅ Réseau Ollama [{name}] déjà présent")
        return

    info(f"🌐 Création du réseau Ollama [{name}]")

    cmd = [
        "docker", "network", "create",
        "-d", "bridge",
        "--subnet", args.network_subnet,
        "--gateway", args.network_gateway,
        "-o", f"com.docker.network.bridge.name={args.network_bridge}",
        name,
    ]

    run_check(cmd, opts=opts)
    info(f"✅ Réseau Ollama [{name}] OK")


def remove_network(args: argparse.Namespace, opts: RunOptions) -> None:
    name = args.network_name

    if not opts.dry_run and not capture_ok(["docker", "network", "inspect", name]):
        info(f"✅ Réseau Ollama [{name}] déjà absent")
        return

    info(f"🧹 Suppression du réseau Ollama [{name}]")
    info("⚠️ Si un container utilise encore ce réseau, Docker refusera.")

    run_check(["docker", "network", "rm", name], opts=opts)
    info(f"✅ Réseau Ollama [{name}] supprimé")


# ============================================================
# UPDATE / START DIRECT DEPUIS stacks.conf + YAML
# ============================================================

_COMPOSE_COMMAND_CACHE: Optional[list[str]] = None


def detect_compose_command() -> list[str]:
    """Utilise docker compose si disponible, sinon docker-compose."""
    global _COMPOSE_COMMAND_CACHE
    if _COMPOSE_COMMAND_CACHE is not None:
        return list(_COMPOSE_COMMAND_CACHE)

    for candidate in (["docker", "compose"], ["docker-compose"]):
        if capture_ok(candidate + ["version"]):
            _COMPOSE_COMMAND_CACHE = list(candidate)
            return list(candidate)

    _COMPOSE_COMMAND_CACHE = []
    return []


def compose_base_command(stack: StackRuntime) -> list[str]:
    cmd = detect_compose_command()
    if not cmd:
        return []

    cmd += ["-p", stack.name]

    for yml in stack.files:
        cmd += ["-f", str(yml)]

    return cmd


def compose_up_stack(stack: StackRuntime, args: argparse.Namespace, opts: RunOptions) -> bool:
    info("")
    info(f"📂 Stack détectée : [{stack.name}]")
    info(f"🧩 YAML : {', '.join(path.name for path in stack.files)}")

    base_cmd = compose_base_command(stack)
    if not base_cmd:
        info("❌ Docker Compose est introuvable sur cet hôte.")
        info("   Installe docker-compose-plugin ou docker-compose, puis relance l'action Compose.")
        return False

    if args.pull:
        rc = run(
            base_cmd + ["pull"],
            cwd=Path(args.yml_dir),
            opts=opts,
        )

        if rc != 0:
            if getattr(args, "strict_pull", False):
                info(f"  ❌ Pull échoué sur [{stack.name}]")
                return False
            info(f"  ⚠️ Pull échoué sur [{stack.name}] : images existantes conservées, démarrage tenté.")
        else:
            info(f"  ✅ Pull OK sur [{stack.name}]")

    up_cmd = base_cmd + ["up", "-d"]

    if args.remove_orphans:
        up_cmd.append("--remove-orphans")

    if args.no_recreate:
        up_cmd.append("--no-recreate")

    if args.force_recreate:
        up_cmd.append("--force-recreate")

    rc = run(up_cmd, cwd=Path(args.yml_dir), opts=opts)

    if rc == 0:
        info(f"  ✅ Stack [{stack.name}] OK")
        return True

    info(f"  ❌ Erreur sur [{stack.name}]")
    return False


def update_stacks(args: argparse.Namespace, opts: RunOptions) -> None:
    yml_dir = Path(args.yml_dir)
    stacks_conf = Path(args.stacks_conf)
    stack_filter = split_csv(args.stack) if args.stack else None

    stack_list = runtime_stacks_from_conf(
        stacks_conf,
        yml_dir=yml_dir,
        stack_filter=stack_filter,
    )

    info("")
    info("-------------------------------------------------------")
    info("🚀 Démarrage des Stacks Docker Compose LABO")
    info(f"📅 {time.strftime('%Y-%m-%d %H:%M:%S')}")
    info("-------------------------------------------------------")
    info(f"📁 YAML source : {yml_dir}")
    info(f"🧾 Conf stacks : {stacks_conf}")
    info(f"🔄 Pull        : {'oui' if args.pull else 'non'}")
    info(f"📦 Stacks      : {', '.join(s.name for s in stack_list)}")

    ok = 0
    ko = 0

    for stack in stack_list:
        if compose_up_stack(stack, args, opts):
            ok += 1
        else:
            ko += 1

    info("")
    info("-------------------------------------------------------")
    info(f"✅ Stacks OK      : {ok}")
    info(f"❌ Stacks erreur  : {ko}")
    info("-------------------------------------------------------")

    if ko:
        raise SystemExit(1)


def list_stacks(args: argparse.Namespace) -> None:
    yml_dir = Path(args.yml_dir)
    stacks_conf = Path(args.stacks_conf)
    stack_filter = split_csv(args.stack) if args.stack else None

    stack_list = runtime_stacks_from_conf(
        stacks_conf,
        yml_dir=yml_dir,
        stack_filter=stack_filter,
    )

    if not stack_list:
        info(f"❌ Aucune stack trouvée dans : {stacks_conf}")
        return

    info(f"Stacks détectées dans l'ordre de : {stacks_conf}")
    for stack in stack_list:
        info(f"✅ {stack.name:<24} {', '.join(str(p) for p in stack.files)}")


def bootstrap_first_stack_before_login(
    args: argparse.Namespace,
    opts: RunOptions,
    stack_list: list[StackRuntime],
) -> None:
    if not stack_list:
        return

    first_stack = stack_list[0]

    info("")
    info("🧩 Bootstrap registre : démarrage de la première stack avant docker login")
    info(f"📦 Première stack : [{first_stack.name}]")

    boot_args = argparse.Namespace(**vars(args))
    boot_args.pull = False

    if not compose_up_stack(first_stack, boot_args, opts):
        raise SystemExit(1)


# ============================================================
# CLI
# ============================================================

def apply_extra_args_to_namespace(args: argparse.Namespace, extra_args: str, argv: list[str]) -> None:
    raw = (extra_args or "").strip()
    if not raw:
        return
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()

    i = 0
    while i < len(tokens):
        token = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""

        if token == "--stack" and nxt and not cli_has(argv, "--stack"):
            args.stack = nxt
            i += 2
            continue
        if token.startswith("--stack=") and not cli_has(argv, "--stack"):
            args.stack = token.split("=", 1)[1]
        elif token == "--no-pull" and not cli_has(argv, "--pull", "--no-pull"):
            args.no_pull = True
            args.pull = False
        elif token == "--pull" and not cli_has(argv, "--pull", "--no-pull"):
            args.pull = True
            args.no_pull = False
        elif token == "--no-recreate" and not cli_has(argv, "--no-recreate", "--force-recreate"):
            args.no_recreate = True
            args.force_recreate = False
        elif token == "--force-recreate" and not cli_has(argv, "--no-recreate", "--force-recreate"):
            args.force_recreate = True
            args.no_recreate = False
        elif token == "--no-remove-orphans" and not cli_has(argv, "--no-remove-orphans"):
            args.no_remove_orphans = True
        elif token == "--strict-pull" and not cli_has(argv, "--strict-pull", "--pull-optional"):
            args.strict_pull = True
        elif token == "--pull-optional" and not cli_has(argv, "--strict-pull", "--pull-optional"):
            args.strict_pull = False
        elif token == "--no-login" and not cli_has(argv, "--no-login", "--login"):
            args.no_login = True
        elif token == "--login" and not cli_has(argv, "--no-login", "--login"):
            args.login = True
        elif token == "--strict-login" and not cli_has(argv, "--strict-login"):
            args.strict_login = True
        elif token == "--dry-run" and not cli_has(argv, "--dry-run"):
            args.dry_run = True
        elif token == "--registry-host" and nxt and not cli_has(argv, "--registry-host"):
            args.registry_host = nxt
            i += 2
            continue
        elif token.startswith("--registry-host=") and not cli_has(argv, "--registry-host"):
            args.registry_host = token.split("=", 1)[1]
        elif token == "--login-retries" and nxt and not cli_has(argv, "--login-retries"):
            try:
                args.login_retries = int(nxt)
            except ValueError:
                pass
            i += 2
            continue
        elif token.startswith("--login-retries=") and not cli_has(argv, "--login-retries"):
            try:
                args.login_retries = int(token.split("=", 1)[1])
            except ValueError:
                pass
        elif token == "--login-wait" and nxt and not cli_has(argv, "--login-wait"):
            try:
                args.login_wait = int(nxt)
            except ValueError:
                pass
            i += 2
            continue
        elif token.startswith("--login-wait=") and not cli_has(argv, "--login-wait"):
            try:
                args.login_wait = int(token.split("=", 1)[1])
            except ValueError:
                pass
        i += 1


def effective_dockers_conf_file(args: argparse.Namespace, argv: list[str]) -> Path:
    configured = Path(args.dockers_conf).expanduser()
    if configured.exists() or cli_has(argv, "--dockers-conf"):
        return configured.resolve() if configured.exists() else configured

    candidates = [
        configured,
        CONF_DIR / "dockers.conf",
        LABO_DIR.parent / "conf" / "dockers.conf",
        SCRIPT_DIR.parent.parent / "conf" / "dockers.conf",
        Path.cwd().parent / "conf" / "dockers.conf",
    ]

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            candidate = candidate.expanduser().resolve()
        except Exception:
            candidate = candidate.expanduser()
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.exists():
            return candidate

    return configured


def apply_dockers_conf_defaults(args: argparse.Namespace, argv: list[str]) -> None:
    conf_file = effective_dockers_conf_file(args, argv)
    args.dockers_conf = str(conf_file)
    conf = read_kv_file(conf_file)
    if not conf:
        return

    conf_dir = conf_file.parent

    if not cli_has(argv, "--yml-dir") and conf.get("YML_FOLDER"):
        args.yml_dir = str(resolve_conf_path(conf["YML_FOLDER"], conf_dir))

    if not cli_has(argv, "--stacks-conf") and (conf.get("SYSTEM_STACKS_CONF_FILE") or conf.get("STACKS_FILE")):
        args.stacks_conf = str(resolve_conf_path(conf.get("SYSTEM_STACKS_CONF_FILE") or conf.get("STACKS_FILE") or "", conf_dir))

    if not cli_has(argv, "--login-file") and conf.get("DOCKER_REGISTRY_LOGIN_FILE"):
        args.login_file = str(resolve_conf_path(conf["DOCKER_REGISTRY_LOGIN_FILE"], conf_dir))

    if not cli_has(argv, "--mode-file") and conf.get("DOCKER_MODE_FILE"):
        args.mode_file = str(resolve_conf_path(conf["DOCKER_MODE_FILE"], conf_dir))

    if not cli_has(argv, "--network-name") and conf.get("SYSTEM_NETWORK_NAME"):
        args.network_name = conf["SYSTEM_NETWORK_NAME"].strip()
    if not cli_has(argv, "--network-subnet") and conf.get("SYSTEM_NETWORK_SUBNET"):
        args.network_subnet = conf["SYSTEM_NETWORK_SUBNET"].strip()
    if not cli_has(argv, "--network-gateway") and conf.get("SYSTEM_NETWORK_GATEWAY"):
        args.network_gateway = conf["SYSTEM_NETWORK_GATEWAY"].strip()
    if not cli_has(argv, "--network-bridge") and conf.get("SYSTEM_NETWORK_BRIDGE"):
        args.network_bridge = conf["SYSTEM_NETWORK_BRIDGE"].strip()

    if not cli_has(argv, "--pull", "--no-pull") and conf_bool(conf, "SYSTEM_STACKS_UPDATE_NO_PULL", "0"):
        args.no_pull = True
        args.pull = False

    if not cli_has(argv, "--strict-pull", "--pull-optional") and conf_bool(conf, "SYSTEM_STACKS_STRICT_PULL", "0"):
        args.strict_pull = True

    if not cli_has(argv, "--no-recreate", "--force-recreate") and conf_bool(conf, "SYSTEM_UP_NO_RECREATE", "0"):
        args.no_recreate = True
        args.force_recreate = False

    if not cli_has(argv, "--no-remove-orphans") and not conf_bool(conf, "SYSTEM_UP_REMOVE_ORPHANS", "1"):
        args.no_remove_orphans = True

    apply_extra_args_to_namespace(args, conf.get("SYSTEM_STACKS_EXTRA_ARGS", ""), argv)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Gestionnaire Docker Compose LABO : update/start/list/ollama.",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    actions = parser.add_argument_group("Actions")
    actions.add_argument("--update", "--install", action="store_true", help="Pull les images puis docker compose up -d sur les stacks.")
    actions.add_argument("--start", action="store_true", help="docker compose up -d sur les stacks, sans pull.")
    actions.add_argument("--all", action="store_true", help="Compatibilité ancienne : équivalent de --update seulement.")
    actions.add_argument("--list", action="store_true", help="Liste les stacks depuis stacks.conf.")
    actions.add_argument("--ollama", "--olama", "--install-ollama", "--install-olama", "--network", dest="ollama", action="store_true", help="Crée seulement le réseau Ollama si absent.")
    actions.add_argument("--remove-ollama", "--remove-olama", dest="remove_ollama", action="store_true", help="Supprime le réseau Ollama.")

    paths = parser.add_argument_group("Chemins")
    paths.add_argument("--dockers-conf", default=str(DOCKERS_CONF_FILE), help=f"Fichier dockers.conf Flask/Yoleo. Défaut : {DOCKERS_CONF_FILE}")
    paths.add_argument("--yml-dir", default=str(YML_DIR), help=f"Dossier des YAML source. Défaut : {YML_DIR}")
    paths.add_argument("--login-file", default=str(REGISTRY_LOGIN_FILE), help=f"Fichier login registre. Défaut : {REGISTRY_LOGIN_FILE}")
    paths.add_argument("--stacks-conf", default=str(STACKS_CONF_FILE), help=f"Fichier d'ordre des stacks. Défaut : {STACKS_CONF_FILE}")
    paths.add_argument("--mode-file", default=str(MODE_FILE), help=f"Fichier mode HTTP/HTTPS par image. Défaut : {MODE_FILE}")

    update_opts = parser.add_argument_group("Options update/start")
    update_opts.add_argument("--stack", help="Limite à une ou plusieurs stacks, séparées par virgule. Ex : base,normal")
    update_opts.add_argument("--no-pull", action="store_true", help="Avec --update : ne fait pas docker compose pull.")
    update_opts.add_argument("--pull", action="store_true", help="Avec --start : force docker compose pull avant up.")
    update_opts.add_argument("--strict-pull", dest="strict_pull", action="store_true", default=False, help="Avec --update : échoue si docker compose pull échoue.")
    update_opts.add_argument("--pull-optional", dest="strict_pull", action="store_false", help="Avec --update : garde les images existantes si le pull échoue. Défaut.")
    update_opts.add_argument("--no-recreate", action="store_true", help="Ajoute docker compose up --no-recreate.")
    update_opts.add_argument("--force-recreate", action="store_true", help="Ajoute docker compose up --force-recreate.")
    update_opts.add_argument("--no-remove-orphans", action="store_true", help="Ne met pas --remove-orphans.")

    network_opts = parser.add_argument_group("Options réseau Ollama")
    network_opts.add_argument("--no-network", action="store_true", help="Compatibilité ancienne version : le réseau Ollama n’est plus créé automatiquement.")
    network_opts.add_argument("--network-name", default=DEFAULT_NETWORK_NAME, help=f"Nom réseau. Défaut : {DEFAULT_NETWORK_NAME}")
    network_opts.add_argument("--network-subnet", default=DEFAULT_NETWORK_SUBNET, help=f"Subnet. Défaut : {DEFAULT_NETWORK_SUBNET}")
    network_opts.add_argument("--network-gateway", default=DEFAULT_NETWORK_GATEWAY, help=f"Gateway. Défaut : {DEFAULT_NETWORK_GATEWAY}")
    network_opts.add_argument("--network-bridge", default=DEFAULT_NETWORK_BRIDGE, help=f"Bridge host. Défaut : {DEFAULT_NETWORK_BRIDGE}")

    login_opts = parser.add_argument_group("Options login")
    login_opts.add_argument("--no-login", action="store_true", help="Ne tente aucun docker login.")
    login_opts.add_argument("--login", action="store_true", help="Force le login registre même avec --start.")
    login_opts.add_argument("--registry-host", default="registry.sftpmalin.com", help="Hôte par défaut si REGISTRY_HOST absent.")
    login_opts.add_argument("--strict-login", action="store_true", help="Échoue si le fichier login ou les identifiants sont absents.")
    login_opts.add_argument("--login-retries", type=int, default=10, help="Nombre de tentatives docker login. Défaut : 10")
    login_opts.add_argument("--login-wait", type=int, default=3, help="Secondes entre deux tentatives docker login. Défaut : 3")

    misc = parser.add_argument_group("Divers")
    misc.add_argument("--dry-run", action="store_true", help="Affiche les commandes sans les exécuter.")

    args = parser.parse_args(argv)
    apply_dockers_conf_defaults(args, argv)

    if args.no_recreate and args.force_recreate:
        parser.error("--no-recreate et --force-recreate ne peuvent pas être utilisés ensemble.")

    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    opts = RunOptions(dry_run=args.dry_run, strict_login=args.strict_login)

    if args.all:
        args.update = True

    if not any([args.update, args.start, args.list, args.ollama, args.remove_ollama]):
        args.update = True

    if args.start:
        args.update = True
        args.pull = bool(args.pull)
    elif args.update:
        args.pull = not args.no_pull

    args.remove_orphans = not args.no_remove_orphans

    if args.list:
        list_stacks(args)
        return 0

    if args.remove_ollama:
        remove_network(args, opts)
        if not any([args.update, args.ollama]):
            return 0

    if args.ollama:
        ensure_network(args, opts)
        if not args.update:
            return 0

    if args.update:
        yml_dir = Path(args.yml_dir)
        stacks_conf = Path(args.stacks_conf)
        stack_filter = split_csv(args.stack) if args.stack else None

        stack_list = runtime_stacks_from_conf(
            stacks_conf,
            yml_dir=yml_dir,
            stack_filter=stack_filter,
        )

        for message in repair_compose_env_registry(args):
            info(message)
        for message in warn_malformed_registry_images(stack_list):
            info(message)

        mode_file = effective_mode_file(args)
        http_registries = http_registries_from_modes(mode_file, stack_list)
        if http_registries:
            ensure_insecure_registries(http_registries, opts)

        if args.pull:
            bootstrap_first_stack_before_login(args, opts, stack_list)

            if registry_login_required_for_update(args, stack_list):
                login_registry(args, opts)
            else:
                info("ℹ️ Docker login sauté : mode local/HTTP.")

        elif args.login:
            if registry_login_required_for_update(args, stack_list):
                login_registry(args, opts)
            else:
                info("ℹ️ Docker login sauté : mode local/HTTP.")

        else:
            info("ℹ️ Login registre ignoré : --start sans pull.")

        update_stacks(args, opts)

    info("")
    info("✅ Terminé.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\nInterrompu.")
        raise SystemExit(130)
