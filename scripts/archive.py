#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
archive.py - moteur CLI Archive piloté par ../conf/archive.conf

Principe :
  - Le Python reste un moteur neutre.
  - Les chemins source/destination sont uniquement dans archive.conf.
  - Si archive.py est dans .../system/archive.py ou .../scripts/archive.py, le conf par défaut est .../conf/archive.conf.
  - 1 bloc du conf = 1 profil autonome.
  - command_start est lancé avant la sauvegarde du bloc.
  - command_end est lancé après l'archive/la copie du bloc, même si la copie/archive échoue.
  - archive = 0 : copie miroir/non destructive via rsync.
  - archive = 1 : crée des archives tar.gz ou tar.7z dans la destination.

Commandes :
  python3 archive.py --all
  python3 archive.py mon_profil
  python3 archive.py --mon_profil
  python3 archive.py --list
  python3 archive.py --dry-run --all
  python3 archive.py --init-conf

Commandes intégrées utilisables dans archive.conf :
  command_start = @docker_stop_running
  command_end   = @docker_start_stopped

Ces deux commandes évitent de démarrer des containers qui étaient déjà arrêtés avant l'archive.
"""

from __future__ import annotations

import argparse
import builtins
import configparser
import fcntl
import os
import shlex
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


def print(*args, **kwargs):
    """Print flushé pour garder les logs propres dans cron/tmux/terminal."""
    kwargs.setdefault("flush", True)
    return builtins.print(*args, **kwargs)


# Chemins relatifs : si archive.py est dans .../system/archive.py ou .../scripts/archive.py,
# le fichier de configuration par défaut est .../conf/archive.conf.
# Donc le dossier complet peut être déplacé ou renommé sans casser le script.
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
CONF_DIR = Path(os.environ.get("CONF_DIR", str(BASE_DIR / "conf")))
ARCHIVE_CONF = Path(os.environ.get("ARCHIVE_CONF", str(CONF_DIR / "archive.conf")))
DEFAULT_LOG_DIR = Path(os.environ.get("ARCHIVE_LOG_DIR", "/var/log/archive"))
DEFAULT_LOCK_FILE = Path(os.environ.get("ARCHIVE_LOCK_FILE", "/tmp/archive.py.lock"))

DANGEROUS_EXACT_PATHS = {
    "/",
    "/mnt",
    "/mnt/user",
    "/mnt/user0",
    "/mnt/cache",
    "/home",
    "/root",
    "/boot",
}

DEFAULT_ARCHIVE_CONF = """# ============================================================
# archive.conf - Configuration commune Archive CLI/Flask
# ============================================================
# Fichier éditable par le module Flask Archive.
# Le même fichier est utilisable depuis le terminal et depuis Flask.
# Aucun profil n'est créé par défaut : ajoute tes profils depuis l'interface.
# ============================================================

[settings]
log_dir = /var/log/archive
lock_file = /tmp/archive.py.lock
"""


@dataclass(frozen=True)
class ArchiveAction:
    key: str
    title: str
    source: Path | None
    destination: Path | None
    command_start: str = ""
    command_end: str = ""
    aliases: tuple[str, ...] = ()
    enabled: bool = True
    include_in_all: bool = True
    archive: bool = False
    archive_format: str = "tar.7z"
    archive_children: bool = True
    archive_name: str = ""
    compression_level: str = "7"
    replace_existing: bool = True
    date_suffix: bool = False
    delete_extra: bool = False
    excludes: tuple[str, ...] = ()
    allow_dangerous_source: bool = False
    allow_destination_inside_source: bool = False
    docker_exclude: tuple[str, ...] = ()
    wait_start_docker_stopped: str = ""
    wait_end_docker_running: str = ""
    docker_wait_timeout: float = 0.0
    docker_wait_interval: float = 2.0

    @property
    def has_backup(self) -> bool:
        return self.source is not None and self.destination is not None

    @property
    def is_command_only(self) -> bool:
        return not self.has_backup and bool(self.command_start.strip() or self.command_end.strip())

    @property
    def mode_label(self) -> str:
        if not self.has_backup:
            return "commande"
        if self.archive:
            scope = "children" if self.archive_children else "source"
            return f"archive {self.archive_format} ({scope})"
        return "copie rsync"


@dataclass(frozen=True)
class ArchiveConfig:
    conf_file: Path
    actions: dict[str, ArchiveAction]
    aliases: dict[str, str]
    lock_file: Path
    log_dir: Path


@dataclass
class RuntimeContext:
    date_stamp: str
    stopped_by_backup: dict[str, list[str]] = field(default_factory=dict)
    running_before: dict[str, list[str]] = field(default_factory=dict)


class NonBlockingLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fp = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = self.path.open("w")
        try:
            fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"ERREUR : archive.py est déjà en cours d'exécution : {self.path}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fp:
            fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
            self.fp.close()


class Tee:
    """Duplique stdout/stderr vers un fichier log, sans dépendance externe."""

    def __init__(self, log_file: Path) -> None:
        self.log_file = log_file
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.fp = self.log_file.open("a", encoding="utf-8", errors="replace")
        self.stdout = sys.stdout
        self.stderr = sys.stderr

    def write(self, data: str) -> None:
        self.stdout.write(data)
        self.stdout.flush()
        self.fp.write(data)
        self.fp.flush()

    def flush(self) -> None:
        self.stdout.flush()
        self.fp.flush()

    def __enter__(self):
        sys.stdout = self
        sys.stderr = self
        print(f"\n--- LOG {now_text()} : {self.log_file} ---")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        print(f"--- FIN LOG {now_text()} ---\n")
        sys.stdout = self.stdout
        sys.stderr = self.stderr
        self.fp.close()


def now_stamp() -> str:
    return time.strftime("%Y-%m-%d_%H%M")


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def title(text: str) -> None:
    print("============================================================")
    print(text)
    print("============================================================")


def quote_cmd(cmd: Iterable[str]) -> str:
    return " ".join(shlex.quote(str(x)) for x in cmd)


def run(
    cmd: list[str],
    *,
    check: bool = False,
    input_text: str | None = None,
    quiet: bool = False,
) -> subprocess.CompletedProcess[str]:
    if quiet:
        return subprocess.run(
            cmd,
            input=input_text,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )
    print(f">>> {quote_cmd(cmd)}")
    return subprocess.run(cmd, input=input_text, text=True, encoding="utf-8", errors="replace", check=check)


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def find_7z() -> str | None:
    for name in ("7z", "7zz", "7za"):
        found = shutil.which(name)
        if found:
            return found
    return None


def split_csv(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.replace("\n", ",").split(",") if part.strip())


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    value = str(value).strip().lower()
    if value in {"1", "true", "yes", "y", "on", "oui"}:
        return True
    if value in {"0", "false", "no", "n", "off", "non"}:
        return False
    return default


def parse_float(value: str | None, default: float = 0.0) -> float:
    if value is None or str(value).strip() == "":
        return default
    try:
        return float(str(value).strip().replace(",", "."))
    except ValueError:
        return default


def normalize_token(value: str) -> str:
    value = value.strip().lower().replace("_", "-")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return value


def resolve_conf_path(text: str | None, *, default: Path | None = None) -> Path | None:
    if text is None or not str(text).strip():
        return default
    raw = os.path.expandvars(os.path.expanduser(str(text).strip()))
    path = Path(raw)
    if path.is_absolute():
        return path
    return (BASE_DIR / path).resolve()


def ensure_default_conf(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_ARCHIVE_CONF, encoding="utf-8")
    print(f"✅ Fichier conf créé : {path}")


def read_conf(path: Path) -> configparser.ConfigParser:
    ensure_default_conf(path)
    parser = configparser.ConfigParser(interpolation=None, allow_no_value=False)
    parser.optionxform = str.lower
    with path.open("r", encoding="utf-8", errors="replace") as fp:
        parser.read_file(fp)
    return parser


def first_non_empty(*values: str | None) -> str:
    for value in values:
        if value is not None and value.strip():
            return value.strip()
    return ""


def load_config(path: Path = ARCHIVE_CONF) -> ArchiveConfig:
    parser = read_conf(path)
    settings = parser["settings"] if parser.has_section("settings") else {}

    log_dir = resolve_conf_path(settings.get("log_dir", "logs/archive"), default=DEFAULT_LOG_DIR) or DEFAULT_LOG_DIR
    lock_file = resolve_conf_path(settings.get("lock_file", str(DEFAULT_LOCK_FILE)), default=DEFAULT_LOCK_FILE) or DEFAULT_LOCK_FILE

    actions: dict[str, ArchiveAction] = {}
    aliases: dict[str, str] = {}

    for section in parser.sections():
        if section == "settings" or section.startswith("group:"):
            continue

        sec = parser[section]
        key = section.strip()
        source_text = sec.get("source", "").strip()
        destination_text = sec.get("destination", "").strip()
        source = resolve_conf_path(source_text) if source_text else None
        destination = resolve_conf_path(destination_text) if destination_text else None

        if bool(source) != bool(destination):
            raise RuntimeError(f"Section incomplète [{section}] : source et destination doivent être remplis ensemble, ou vides ensemble.")

        command_start = first_non_empty(
            sec.get("command_start", ""),
            sec.get("before", ""),
            sec.get("command_before", ""),
            sec.get("stop", ""),
        )
        command_end = first_non_empty(
            sec.get("command_end", ""),
            sec.get("after", ""),
            sec.get("command_after", ""),
            sec.get("start", ""),
        )

        action = ArchiveAction(
            key=key,
            title=sec.get("title", key).strip() or key,
            source=source,
            destination=destination,
            command_start=command_start,
            command_end=command_end,
            aliases=split_csv(sec.get("aliases", "")),
            enabled=parse_bool(sec.get("enabled", "true"), default=True),
            include_in_all=parse_bool(sec.get("include_in_all", "true"), default=True),
            archive=parse_bool(sec.get("archive", sec.get("tar", "false")), default=False),
            archive_format=sec.get("archive_format", sec.get("format", "tar.7z")).strip().lower() or "tar.7z",
            archive_children=parse_bool(sec.get("archive_children", "true"), default=True),
            archive_name=sec.get("archive_name", "").strip(),
            compression_level=sec.get("compression_level", "7").strip() or "7",
            replace_existing=parse_bool(sec.get("replace_existing", "true"), default=True),
            date_suffix=parse_bool(sec.get("date_suffix", "false"), default=False),
            delete_extra=parse_bool(sec.get("delete_extra", "false"), default=False),
            excludes=split_csv(sec.get("excludes", "")),
            allow_dangerous_source=parse_bool(sec.get("allow_dangerous_source", "false"), default=False),
            allow_destination_inside_source=parse_bool(sec.get("allow_destination_inside_source", "false"), default=False),
            docker_exclude=split_csv(sec.get("docker_exclude", "")),
            wait_start_docker_stopped=sec.get("wait_start_docker_stopped", "").strip(),
            wait_end_docker_running=sec.get("wait_end_docker_running", "").strip(),
            docker_wait_timeout=parse_float(sec.get("docker_wait_timeout", "0"), default=0.0),
            docker_wait_interval=parse_float(sec.get("docker_wait_interval", "2"), default=2.0),
        )

        if action.archive_format not in {"tar.gz", "tgz", "gz", "tar.7z", "7z"}:
            raise RuntimeError(f"Format archive inconnu dans [{section}] : {action.archive_format}")

        actions[key] = action
        aliases[normalize_token(key)] = key
        for alias in action.aliases:
            aliases[normalize_token(alias)] = key

    return ArchiveConfig(conf_file=path, actions=actions, aliases=aliases, lock_file=lock_file, log_dir=log_dir)


def select_by_prefix(name: str, config: ArchiveConfig) -> list[str]:
    token = normalize_token(name)
    prefix = token.replace("-", "_") + "_"
    return [key for key in config.actions if key.startswith(prefix)]


def resolve_names(name: str, config: ArchiveConfig) -> list[str]:
    token = normalize_token(name)
    if token in config.aliases:
        return [config.aliases[token]]

    prefixed = select_by_prefix(name, config)
    if prefixed:
        return prefixed

    raise KeyError(f"Nom inconnu dans archive.conf : {name}")


def realpath_text(path: Path) -> str:
    return os.path.realpath(path)


def is_under_or_equal(path: Path, root: Path) -> bool:
    path_s = realpath_text(path)
    root_s = realpath_text(root)
    return path_s == root_s or path_s.startswith(root_s.rstrip("/") + "/")


def safe_path_checks(action: ArchiveAction) -> None:
    if action.source is None or action.destination is None:
        return

    src = action.source
    dst = action.destination
    src_real = realpath_text(src)
    dst_real = realpath_text(dst)

    if src_real == dst_real:
        raise RuntimeError(f"ERREUR [{action.key}] : source et destination identiques : {src}")

    if src_real in DANGEROUS_EXACT_PATHS and not action.allow_dangerous_source:
        raise RuntimeError(
            f"ERREUR [{action.key}] : source trop large/dangereuse refusée : {src}\n"
            f"Ajoute allow_dangerous_source = 1 seulement si c'est volontaire."
        )

    if dst_real in DANGEROUS_EXACT_PATHS:
        raise RuntimeError(f"ERREUR [{action.key}] : destination trop large/dangereuse refusée : {dst}")

    if is_under_or_equal(dst, src) and not action.allow_destination_inside_source:
        raise RuntimeError(
            f"ERREUR [{action.key}] : destination placée dans la source, risque de boucle archive :\n"
            f"  source      = {src}\n"
            f"  destination = {dst}"
        )


def path_has_payload(path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        next(path.iterdir())
        return True
    except StopIteration:
        return False


def should_exclude(path: Path, patterns: tuple[str, ...]) -> bool:
    if not patterns:
        return False
    name = path.name
    for pattern in patterns:
        if name == pattern or path.match(pattern):
            return True
    return False


def docker_names(*, all_containers: bool = False) -> list[str]:
    cmd = ["docker", "ps", "--format", "{{.Names}}"]
    if all_containers:
        cmd = ["docker", "ps", "-a", "--format", "{{.Names}}"]
    cp = run(cmd, quiet=True)
    if cp.returncode != 0:
        stderr = cp.stderr.strip() if cp.stderr else ""
        raise RuntimeError(f"Commande Docker impossible : {' '.join(cmd)} {stderr}")
    return [x.strip() for x in cp.stdout.splitlines() if x.strip()]


def filter_docker_exclude(names: Iterable[str], excludes: tuple[str, ...]) -> list[str]:
    excluded = {x.strip() for x in excludes if x.strip()}
    return [name for name in names if name not in excluded]


def docker_stop(names: list[str], *, dry_run: bool = False) -> bool:
    if not names:
        print("Aucun container à arrêter.")
        return True
    cmd = ["docker", "stop", *names]
    if dry_run:
        print("DRY-RUN : " + quote_cmd(cmd))
        return True
    return run(cmd).returncode == 0


def docker_start(names: list[str], *, dry_run: bool = False) -> bool:
    if not names:
        print("Aucun container à démarrer.")
        return True
    cmd = ["docker", "start", *names]
    if dry_run:
        print("DRY-RUN : " + quote_cmd(cmd))
        return True
    return run(cmd).returncode == 0


def docker_spec_names(spec: str, action: ArchiveAction, context: RuntimeContext) -> tuple[str, list[str]]:
    spec = (spec or "").strip()
    if not spec:
        return "none", []

    if spec in {"stopped_by_backup", "stopped_by_archive"}:
        return "names", context.stopped_by_backup.get(action.key, [])

    running = docker_names(all_containers=False)
    running = filter_docker_exclude(running, action.docker_exclude)

    if spec == "all":
        return "all", running

    names = list(split_csv(spec))
    return "names", names


def wait_docker_stopped(spec: str, action: ArchiveAction, context: RuntimeContext, *, dry_run: bool = False) -> bool:
    spec = (spec or "").strip()
    if not spec:
        return True
    if dry_run:
        print(f"DRY-RUN : attente containers arrêtés ignorée : {spec}")
        return True

    timeout = action.docker_wait_timeout
    interval = max(action.docker_wait_interval, 0.5)
    start = time.monotonic()

    while True:
        mode, names = docker_spec_names(spec, action, context)
        running = set(filter_docker_exclude(docker_names(all_containers=False), action.docker_exclude))

        if mode == "all":
            remaining = sorted(running)
        else:
            remaining = sorted(name for name in names if name in running)

        if not remaining:
            print(f"✅ Containers arrêtés : {spec}")
            return True

        print(f"⏳ Attente arrêt Docker ({spec}) : {', '.join(remaining)}")
        if timeout > 0 and (time.monotonic() - start) >= timeout:
            print(f"❌ Timeout attente Docker arrêtés : {', '.join(remaining)}")
            return False
        time.sleep(interval)


def wait_docker_running(spec: str, action: ArchiveAction, context: RuntimeContext, *, dry_run: bool = False) -> bool:
    spec = (spec or "").strip()
    if not spec:
        return True
    if dry_run:
        print(f"DRY-RUN : attente containers démarrés ignorée : {spec}")
        return True

    timeout = action.docker_wait_timeout
    interval = max(action.docker_wait_interval, 0.5)
    start = time.monotonic()

    while True:
        _, names = docker_spec_names(spec, action, context)
        running = set(docker_names(all_containers=False))
        missing = sorted(name for name in names if name not in running)

        if not missing:
            print(f"✅ Containers démarrés : {spec}")
            return True

        print(f"⏳ Attente démarrage Docker ({spec}) : {', '.join(missing)}")
        if timeout > 0 and (time.monotonic() - start) >= timeout:
            print(f"❌ Timeout attente Docker démarrés : {', '.join(missing)}")
            return False
        time.sleep(interval)


def shell_join(names: Iterable[str]) -> str:
    return " ".join(shlex.quote(name) for name in names)


def expand_command(command: str, action: ArchiveAction, context: RuntimeContext) -> str:
    running_before = context.running_before.get(action.key, [])
    stopped = context.stopped_by_backup.get(action.key, [])
    return (
        command.replace("{running_containers}", shell_join(running_before))
        .replace("{stopped_by_backup}", shell_join(stopped))
        .replace("{stopped_by_archive}", shell_join(stopped))
        .replace("{date}", shlex.quote(context.date_stamp))
    )


def run_builtin_command(command: str, action: ArchiveAction, context: RuntimeContext, *, label: str, dry_run: bool = False) -> bool:
    parts = shlex.split(command)
    if not parts:
        return True
    name = parts[0]

    if name == "@docker_stop_running":
        if dry_run:
            running = []
        else:
            running = docker_names(all_containers=False)
        running = filter_docker_exclude(running, action.docker_exclude)
        context.running_before[action.key] = list(running)
        context.stopped_by_backup[action.key] = list(running)
        print(f"Containers à arrêter : {', '.join(running) if running else 'aucun'}")
        if not docker_stop(running, dry_run=dry_run):
            return False
        return wait_docker_stopped("stopped_by_archive", action, context, dry_run=dry_run)

    if name == "@docker_start_stopped":
        names = context.stopped_by_backup.get(action.key, [])
        print(f"Containers à redémarrer : {', '.join(names) if names else 'aucun'}")
        if not docker_start(names, dry_run=dry_run):
            return False
        return wait_docker_running("stopped_by_archive", action, context, dry_run=dry_run)

    if name == "@sleep":
        seconds = float(parts[1]) if len(parts) > 1 else 1.0
        if dry_run:
            print(f"DRY-RUN : sleep {seconds}s ignoré")
            return True
        time.sleep(seconds)
        return True

    print(f"❌ Commande intégrée inconnue ({label}) : {name}")
    return False


def run_config_command(command: str, action: ArchiveAction, context: RuntimeContext, *, label: str, dry_run: bool = False) -> bool:
    command = command.strip()
    if not command:
        return True

    print("")
    print(f">>> {label}")
    print(command)

    if command.startswith("@"):
        return run_builtin_command(command, action, context, label=label, dry_run=dry_run)

    if action.key not in context.running_before:
        try:
            context.running_before[action.key] = docker_names(all_containers=False) if command_exists("docker") and not dry_run else []
        except Exception:
            context.running_before[action.key] = []

    expanded = expand_command(command, action, context)
    if dry_run:
        print("DRY-RUN : commande ignorée")
        print(expanded)
        return True

    rc = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", expanded], text=True).returncode
    if rc != 0:
        print(f"❌ Commande échouée ({rc}) : {label}")
        return False
    return True


def ensure_tools_for_action(action: ArchiveAction, *, dry_run: bool = False) -> None:
    if dry_run or not action.has_backup:
        return
    if action.archive:
        if not command_exists("tar"):
            raise RuntimeError("ERREUR : tar introuvable.")
        if action.archive_format in {"tar.7z", "7z"} and not find_7z():
            raise RuntimeError("ERREUR : 7z/7zz/7za introuvable. Installe p7zip / 7zip.")
    else:
        if not command_exists("rsync"):
            raise RuntimeError("ERREUR : rsync introuvable.")


def prepare_destination(destination: Path, *, dry_run: bool = False) -> None:
    if dry_run:
        print(f"DRY-RUN : création destination ignorée : {destination}")
        return
    destination.mkdir(parents=True, exist_ok=True)


def build_rsync_cmd(action: ArchiveAction, *, dry_run: bool = False) -> list[str]:
    assert action.source is not None
    assert action.destination is not None

    cmd = [
        "rsync",
        "-aAXHhv",
        "--numeric-ids",
        "--info=progress2",
        "--stats",
    ]
    if action.delete_extra:
        cmd.append("--delete")
    for pattern in action.excludes:
        cmd.extend(["--exclude", pattern])
    if dry_run:
        cmd.append("--dry-run")
    cmd.extend([str(action.source) + "/", str(action.destination) + "/"])
    return cmd


def copy_payload(action: ArchiveAction, *, dry_run: bool = False) -> bool:
    assert action.source is not None
    assert action.destination is not None

    print("")
    title(f"COPIE ARCHIVE : {action.title}")
    print(f"SOURCE      : {action.source}/")
    print(f"DESTINATION : {action.destination}/")
    print(f"DELETE_EXTRA: {int(action.delete_extra)}")

    prepare_destination(action.destination, dry_run=dry_run)
    cmd = build_rsync_cmd(action, dry_run=dry_run)
    rc = run(cmd).returncode
    if rc != 0:
        print(f"❌ Erreur rsync : {action.title}")
        return False
    print(f"✅ Copie terminée : {action.title}")
    return True


def safe_archive_basename(name: str) -> str:
    cleaned = name.strip().replace("/", "_").replace("\0", "")
    return cleaned or "archive"


def archive_output_path(action: ArchiveAction, item: Path, *, whole_source: bool, date_stamp: str) -> Path:
    assert action.destination is not None
    base = action.archive_name.strip() if whole_source and action.archive_name.strip() else item.name
    base = safe_archive_basename(base)
    if action.date_suffix:
        base = f"{base}_{date_stamp}"

    fmt = action.archive_format
    if fmt in {"tar.gz", "tgz", "gz"}:
        suffix = ".tar.gz"
    else:
        suffix = ".tar.7z"
    return action.destination / f"{base}{suffix}"


def archive_item_tar_gz(item: Path, outfile: Path, *, dry_run: bool = False) -> bool:
    parent = item.parent
    tmp = outfile.with_name(outfile.name + ".tmp")
    cmd = [
        "tar",
        "--xattrs",
        "--acls",
        "--numeric-owner",
        "-czf",
        str(tmp),
        "-C",
        str(parent),
        item.name,
    ]
    if dry_run:
        print("DRY-RUN : " + quote_cmd(cmd))
        print(f"DRY-RUN : mv {tmp} {outfile}")
        return True
    if tmp.exists():
        tmp.unlink()
    rc = run(cmd).returncode
    if rc != 0:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        return False
    tmp.replace(outfile)
    return True


def archive_item_tar_7z(item: Path, outfile: Path, *, level: str = "7", dry_run: bool = False) -> bool:
    seven = find_7z()
    if not seven:
        raise RuntimeError("ERREUR : 7z/7zz/7za introuvable.")

    parent = item.parent
    tmp = outfile.with_name(outfile.name + ".tmp")
    tar_cmd = ["tar", "--xattrs", "--acls", "--numeric-owner", "-C", str(parent), "-cf", "-", item.name]
    seven_cmd = [seven, "a", "-t7z", f"-mx={level}", f"-si{item.name}.tar", str(tmp)]

    if dry_run:
        print("DRY-RUN : " + quote_cmd(tar_cmd) + " | " + quote_cmd(seven_cmd))
        print(f"DRY-RUN : mv {tmp} {outfile}")
        return True

    if tmp.exists():
        tmp.unlink()

    print(f">>> {quote_cmd(tar_cmd)} | {quote_cmd(seven_cmd)}")
    tar_proc = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE)
    assert tar_proc.stdout is not None
    seven_proc = subprocess.run(seven_cmd, stdin=tar_proc.stdout, text=False)
    tar_proc.stdout.close()
    tar_rc = tar_proc.wait()

    if tar_rc != 0 or seven_proc.returncode != 0:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        print(f"❌ Erreur archive tar.7z : tar={tar_rc}, 7z={seven_proc.returncode}")
        return False

    tmp.replace(outfile)
    return True


def archive_one_item(action: ArchiveAction, item: Path, *, whole_source: bool, date_stamp: str, dry_run: bool = False) -> bool:
    assert action.destination is not None
    outfile = archive_output_path(action, item, whole_source=whole_source, date_stamp=date_stamp)

    if outfile.exists() and not action.replace_existing:
        print(f"⏭️  Archive déjà présente : {outfile}")
        return True

    print("")
    print(f"→ Archive : {item}")
    print(f"  Sortie  : {outfile}")
    prepare_destination(action.destination, dry_run=dry_run)

    if action.archive_format in {"tar.gz", "tgz", "gz"}:
        ok = archive_item_tar_gz(item, outfile, dry_run=dry_run)
    else:
        ok = archive_item_tar_7z(item, outfile, level=action.compression_level, dry_run=dry_run)

    if ok:
        print(f"✅ OK → {outfile}")
    else:
        print(f"❌ Échec archive → {outfile}")
    return ok


def archive_payload(action: ArchiveAction, context: RuntimeContext, *, dry_run: bool = False) -> bool:
    assert action.source is not None
    assert action.destination is not None

    print("")
    title(f"ARCHIVE : {action.title}")
    print(f"SOURCE      : {action.source}/")
    print(f"DESTINATION : {action.destination}/")
    print(f"FORMAT      : {action.archive_format}")
    print(f"CHILDREN    : {int(action.archive_children)}")

    prepare_destination(action.destination, dry_run=dry_run)

    if action.archive_children:
        items = sorted(
            [p for p in action.source.iterdir() if not should_exclude(p, action.excludes)],
            key=lambda p: p.name.lower(),
        )
        if not items:
            print(f"⏭️  Source vide : {action.source}")
            return True

        ok = True
        for item in items:
            if not archive_one_item(action, item, whole_source=False, date_stamp=context.date_stamp, dry_run=dry_run):
                ok = False
        return ok

    return archive_one_item(action, action.source, whole_source=True, date_stamp=context.date_stamp, dry_run=dry_run)


def archive_payload_is_needed(action: ArchiveAction, *, dry_run: bool = False) -> bool:
    """Vérifie la source avant command_start, pour ne pas stopper Docker si rien n'est à archiver."""
    if not action.has_backup:
        return True
    assert action.source is not None

    safe_path_checks(action)
    ensure_tools_for_action(action, dry_run=dry_run)

    if not action.source.exists():
        print(f"⏭️  Source absente : {action.title}")
        print(f"    {action.source}")
        return False

    if not action.source.is_dir():
        raise RuntimeError(f"ERREUR [{action.key}] : la source doit être un dossier : {action.source}")

    if not path_has_payload(action.source):
        print(f"⏭️  Source vide : {action.title}")
        print(f"    {action.source}")
        return False

    return True


def execute_archive_payload(action: ArchiveAction, context: RuntimeContext, *, dry_run: bool = False) -> bool:
    if not action.has_backup:
        return True
    assert action.source is not None
    assert action.destination is not None

    safe_path_checks(action)
    ensure_tools_for_action(action, dry_run=dry_run)

    if action.archive:
        return archive_payload(action, context, dry_run=dry_run)
    return copy_payload(action, dry_run=dry_run)


def execute_action(action: ArchiveAction, context: RuntimeContext, *, dry_run: bool = False, explicit: bool = False) -> bool:
    if not action.enabled:
        print(f"⏭️  Désactivé : {action.title}")
        return True

    if action.is_command_only and not explicit and not action.include_in_all:
        return True

    print("")
    title(f"BLOC : {action.key} - {action.title}")
    print(f"MODE : {action.mode_label}")

    if action.has_backup and not archive_payload_is_needed(action, dry_run=dry_run):
        return True

    start_ok = False
    archive_ok = True
    end_ok = True

    if action.command_start.strip():
        start_ok = run_config_command(action.command_start, action, context, label=f"commande début {action.key}", dry_run=dry_run)
        if start_ok and not wait_docker_stopped(action.wait_start_docker_stopped, action, context, dry_run=dry_run):
            start_ok = False
    else:
        start_ok = True

    if not start_ok:
        print(f"❌ Début échoué : l'archive ne démarre pas pour {action.key}")
        if action.command_end.strip():
            run_config_command(action.command_end, action, context, label=f"commande fin sécurité {action.key}", dry_run=dry_run)
        return False

    try:
        if action.has_backup:
            archive_ok = execute_archive_payload(action, context, dry_run=dry_run)
        elif action.is_command_only:
            print("Bloc commande seule : aucune source/destination.")
        else:
            print(f"⏭️  Bloc sans action : {action.title}")
    finally:
        if action.command_end.strip():
            end_ok = run_config_command(action.command_end, action, context, label=f"commande fin {action.key}", dry_run=dry_run)
            if end_ok and not wait_docker_running(action.wait_end_docker_running, action, context, dry_run=dry_run):
                end_ok = False

    return bool(archive_ok and end_ok)


def list_actions(config: ArchiveConfig) -> int:
    print("Configuration :")
    print(f"  conf : {config.conf_file}")
    print(f"  logs : {config.log_dir}")
    print(f"  lock : {config.lock_file}")
    print("")
    print("Commandes :")
    print("  --all                 Tous les blocs enabled + include_in_all")
    print("  --NOM                 Lance un bloc par son nom/alias")
    print("  --PREFIXE             Lance tous les blocs PREFIXE_*. Exemple : --mon_profil")
    print("  NOM                   Pareil, sans les tirets")
    print("  --dry-run             Teste sans copier, archiver ni exécuter les commandes")
    print("  --init-conf           Crée archive.conf s'il est absent")
    print("")
    print("Blocs :")
    for key, action in config.actions.items():
        if not action.enabled:
            status = "désactivé"
        elif not action.include_in_all:
            status = "hors --all"
        else:
            status = "actif"
        cmd_start = "oui" if action.command_start.strip() else "non"
        cmd_end = "oui" if action.command_end.strip() else "non"
        aliases = f" | aliases: {', '.join(action.aliases)}" if action.aliases else ""
        print(f"  --{key:<22} {action.title} ({status}) | {action.mode_label} | start: {cmd_start} | end: {cmd_end}{aliases}")
        if action.has_backup:
            print(f"      {action.source}  ->  {action.destination}")
        elif action.is_command_only:
            print("      commande seule")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Archive CLI piloté par archive.conf.",
        formatter_class=argparse.RawTextHelpFormatter,
        add_help=True,
    )
    parser.add_argument("--all", action="store_true", help="Lance tous les blocs include_in_all.")
    parser.add_argument("--list", action="store_true", help="Liste les blocs du conf.")
    parser.add_argument("--dry-run", action="store_true", help="Teste sans copier/archiver et sans exécuter les commandes.")
    parser.add_argument("--conf", default=str(ARCHIVE_CONF), help=f"Fichier conf à utiliser. Défaut : {ARCHIVE_CONF}")
    parser.add_argument("--init-conf", action="store_true", help="Crée archive.conf s'il est absent, puis quitte.")
    parser.add_argument("--no-log", action="store_true", help="Affiche seulement dans le terminal, sans fichier log.")
    parser.add_argument("names", nargs="*", help="Blocs ou préfixes. Ex : archive.py mon_profil")

    args, unknown = parser.parse_known_args(argv)

    # Permet --appdata, --boot, etc. sans les coder dans argparse.
    dynamic_names: list[str] = []
    for item in unknown:
        if item.startswith("--") and len(item) > 2:
            dynamic_names.append(item[2:])
        else:
            dynamic_names.append(item)
    args.names.extend(dynamic_names)
    return args


def build_selection(args: argparse.Namespace, config: ArchiveConfig) -> tuple[list[ArchiveAction], set[str]]:
    selected_keys: list[str] = []
    explicit_keys: set[str] = set()

    if args.all:
        for key, action in config.actions.items():
            if action.enabled and action.include_in_all:
                selected_keys.append(key)

    for name in args.names:
        keys = resolve_names(name, config)
        for key in keys:
            selected_keys.append(key)
            explicit_keys.add(key)

    result: list[ArchiveAction] = []
    seen: set[str] = set()
    for key in selected_keys:
        if key not in seen:
            result.append(config.actions[key])
            seen.add(key)

    return result, explicit_keys


def run_main(args: argparse.Namespace, config: ArchiveConfig) -> int:
    if args.list:
        return list_actions(config)

    actions, explicit_keys = build_selection(args, config)
    if not actions:
        print("❌ Action manquante.")
        print("Exemples :")
        print("  python3 archive.py --all")
        print("  python3 archive.py --mon_profil")
        print("  python3 archive.py mon_profil")
        print("  python3 archive.py --list")
        print("  python3 archive.py --dry-run --all")
        return 1

    ok = True
    context = RuntimeContext(date_stamp=now_stamp())

    with NonBlockingLock(config.lock_file):
        title("ARCHIVE - moteur CLI piloté par archive.conf")
        print(f"Date : {now_text()}")
        print(f"CONF : {config.conf_file}")
        print(f"BASE : {BASE_DIR}")
        if args.dry_run:
            print("MODE : DRY-RUN")

        for action in actions:
            explicit = action.key in explicit_keys
            if not execute_action(action, context, dry_run=args.dry_run, explicit=explicit):
                ok = False

    if not args.dry_run:
        try:
            os.sync()
        except Exception:
            pass

    print("")
    if ok:
        print("✅ Archive terminée.")
        return 0
    print("❌ Archive terminée avec erreur.")
    return 1


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    conf_file = Path(args.conf)

    if args.init_conf:
        ensure_default_conf(conf_file)
        return 0

    config = load_config(conf_file)

    if args.no_log or args.list:
        return run_main(args, config)

    log_name = f"archive_{now_stamp()}.log"
    log_file = config.log_dir / log_name
    with Tee(log_file):
        return run_main(args, config)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\nInterrompu.")
        raise SystemExit(130)
    except (RuntimeError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"❌ {exc}")
        raise SystemExit(1)
