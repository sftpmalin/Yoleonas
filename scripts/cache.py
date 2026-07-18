#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cache.py - Mover cache Unraid piloté par ../conf/cache.conf

Principe :
  - Le Python reste un moteur neutre.
  - Chaque bloc du cache.conf est autonome.
  - Pas de [group:...] séparé, pas de logique Docker codée en dur.
  - command_start est lancé avant le déplacement du bloc.
  - command_end est lancé après le déplacement du bloc.
  - Si command_start / command_end est vide, le moteur ne fait rien.

Exemples :
  python3 cache.py --all
  python3 cache.py media
  python3 cache.py --media
  python3 cache.py --immich        # lance tous les blocs immich_* par préfixe
  python3 cache.py --nextcloud     # lance tous les blocs nextcloud_* par préfixe
  python3 cache.py --list
  python3 cache.py --init-conf
"""

from __future__ import annotations

import argparse
import builtins
import configparser
import fcntl
import glob
import os
import re
import shlex
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path


def print(*args, **kwargs):
    """Print flushé pour garder les logs propres dans cron/tmux/terminal."""
    kwargs.setdefault("flush", True)
    return builtins.print(*args, **kwargs)


# Chemins relatifs : si cache.py est dans .../scripts/cache.py,
# le fichier de configuration par défaut est .../conf/cache.conf.
# Donc le dossier complet peut être déplacé ou renommé sans casser le script.
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
CONF_DIR = Path(os.environ.get("CONF_DIR", str(BASE_DIR / "conf")))
CACHE_CONF = Path(os.environ.get("CACHE_CONF", str(CONF_DIR / "cache.conf")))
LOCK_FILE = Path(os.environ.get("CACHE_LOCK_FILE", "/tmp/cache.py.lock"))

# Sécurité interne volontairement non affichée dans cache.conf.
ALLOWED_MOVE_ROOTS = ("/",)
ALLOWED_SOURCE_ROOTS = tuple(Path(root) for root in ALLOWED_MOVE_ROOTS)
ALLOWED_DESTINATION_ROOTS = ALLOWED_MOVE_ROOTS


DEFAULT_CACHE_CONF = """# ============================================================
# cache.conf - Configuration complète du mover cache.py
# ============================================================
# Principe simple : 1 bloc = 1 action autonome.
#
# Champs :
#   title         = nom affiché
#   command_start = commande libre avant le déplacement, vide si rien
#   source        = dossier source à vider/déplacer
#   destination   = dossier destination
#   command_end   = commande libre après le déplacement, vide si rien
#   aliases       = autres noms acceptés en ligne de commande
#
# Pas de [group:...] séparé.
# Pas de group = immich.
#
# Astuce :
#   python3 cache.py --immich    lance tous les blocs qui commencent par immich_
#   python3 cache.py --nextcloud lance tous les blocs qui commencent par nextcloud_
# ============================================================

[a_voir]
title = Yoan / a voir
command_start =
source = /mnt/cache/Yoan/a voir
destination = /mnt/user0/Yoan/a voir
command_end =
aliases = a-voir, avoir

[archive]
title = Yoan / Archive
command_start =
source = /mnt/cache/Yoan/Archive
destination = /mnt/user0/Yoan/Archive
command_end =
aliases =

[dockers]
title = dockers
command_start =
source = /mnt/cache/dockers
destination = /mnt/user0/dockers
command_end =
aliases = docker

[isos_jeux]
title = ISOs_Jeux
command_start =
source = /mnt/cache/ISOs_Jeux
destination = /mnt/user0/ISOs_Jeux
command_end =
aliases = isos, iso, isos-jeux, jeux

[media]
title = Media
command_start =
source = /mnt/cache/Media
destination = /mnt/user0/Media
command_end =
aliases = média

[mobile]
title = Yoan / Mobile
command_start =
source = /mnt/cache/Yoan/Mobile
destination = /mnt/user0/Yoan/Mobile
command_end =
aliases =

[soft]
title = Yoan / Soft
command_start =
source = /mnt/cache/Yoan/Soft
destination = /mnt/user0/Yoan/Soft
command_end =
aliases =

[youtube]
title = Yoan / Youtube
command_start =
source = /mnt/cache/Yoan/Youtube
destination = /mnt/user0/Yoan/Youtube
command_end =
aliases = yt

# ============================================================
# IMMICH
# ============================================================

[immich_backups]
title = Immich backups
command_start = docker stop Immich Immich_postgres Immich_redis
source = /mnt/cache/Photos/backups
destination = /mnt/user0/Photos/backups
command_end = docker start Immich_redis Immich_postgres Immich
aliases = photos-backups, backups

[immich_library]
title = Immich library
command_start = docker stop Immich Immich_postgres Immich_redis
source = /mnt/cache/Photos/library
destination = /mnt/user0/Photos/library
command_end = docker start Immich_redis Immich_postgres Immich
aliases = photos-library, library

# ============================================================
# NEXTCLOUD
# ============================================================

[nextcloud_antony]
title = Nextcloud / antony
command_start = docker stop Nextcloud
source = /mnt/cache/Nextcloud/antony
destination = /mnt/user0/Nextcloud/antony
command_end = docker start Nextcloud
aliases = nextcloud-antony, nc-antony, antony

[nextcloud_enzo]
title = Nextcloud / enzo
command_start = docker stop Nextcloud
source = /mnt/cache/Nextcloud/enzo
destination = /mnt/user0/Nextcloud/enzo
command_end = docker start Nextcloud
aliases = nextcloud-enzo, nc-enzo, enzo

[nextcloud_lea]
title = Nextcloud / lea
command_start = docker stop Nextcloud
source = /mnt/cache/Nextcloud/lea
destination = /mnt/user0/Nextcloud/lea
command_end = docker start Nextcloud
aliases = nextcloud-lea, nc-lea, lea

[nextcloud_leonie]
title = Nextcloud / leonie
command_start = docker stop Nextcloud
source = /mnt/cache/Nextcloud/leonie
destination = /mnt/user0/Nextcloud/leonie
command_end = docker start Nextcloud
aliases = nextcloud-leonie, nc-leonie, leonie

[nextcloud_root]
title = Nextcloud / root
command_start = docker stop Nextcloud
source = /mnt/cache/Nextcloud/root
destination = /mnt/user0/Nextcloud/root
command_end = docker start Nextcloud
aliases = nextcloud-root, nc-root, root

[nextcloud_yoan]
title = Nextcloud / yoan/files/Backup
command_start = docker stop Nextcloud
source = /mnt/cache/Nextcloud/yoan/files/Backup
destination = /mnt/user0/Nextcloud/yoan/files/Backup
command_end = docker start Nextcloud
aliases = nextcloud-yoan, nc-yoan, yoan

# ============================================================
# EXEMPLE DE COMMANDE SYSTÈME SANS DÉPLACEMENT
# ============================================================
# [registry_reload]
# title = Registry - reload TAR
# command_start = python3 /mnt/user/dockers/scripts/docker.py --load
# source =
# destination =
# command_end =
# include_in_all = false
"""


@dataclass(frozen=True)
class CacheAction:
    key: str
    title: str
    source: Path | None
    destination: Path | None
    command_start: str = ""
    command_end: str = ""
    aliases: tuple[str, ...] = ()
    enabled: bool = True
    include_in_all: bool = True

    @property
    def has_move(self) -> bool:
        return self.source is not None and self.destination is not None

    @property
    def is_command_only(self) -> bool:
        return not self.has_move and bool(self.command_start.strip() or self.command_end.strip())


@dataclass(frozen=True)
class CacheConfig:
    conf_file: Path
    actions: dict[str, CacheAction]
    aliases: dict[str, str]
    lock_file: Path


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
            raise RuntimeError(f"ERREUR : cache.py est déjà en cours d'exécution : {self.path}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fp:
            fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
            self.fp.close()


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
    return subprocess.run(cmd, input=input_text, text=True, encoding="utf-8", errors="replace", check=check)


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


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


def normalize_token(value: str) -> str:
    value = value.strip().lower().replace("_", "-")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return value


def ensure_default_conf(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CACHE_CONF, encoding="utf-8")
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


def load_config(path: Path = CACHE_CONF) -> CacheConfig:
    parser = read_conf(path)
    actions: dict[str, CacheAction] = {}
    aliases: dict[str, str] = {}

    for section in parser.sections():
        # Ancienne syntaxe volontairement ignorée si elle reste dans un vieux conf.
        if section.startswith("group:") or section == "settings":
            continue

        sec = parser[section]
        key = section.strip()
        source_text = sec.get("source", "").strip()
        destination_text = sec.get("destination", "").strip()
        source = Path(source_text) if source_text else None
        destination = Path(destination_text) if destination_text else None

        if bool(source) != bool(destination):
            raise RuntimeError(f"Section incomplète [{section}] : source et destination doivent être remplis ensemble, ou vides ensemble.")

        # Compatibilité avec plusieurs noms possibles, mais le conf propre utilise command_start/command_end.
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

        action = CacheAction(
            key=key,
            title=sec.get("title", key).strip() or key,
            source=source,
            destination=destination,
            command_start=command_start,
            command_end=command_end,
            aliases=split_csv(sec.get("aliases", "")),
            enabled=parse_bool(sec.get("enabled", "true"), default=True),
            include_in_all=parse_bool(sec.get("include_in_all", "true"), default=True),
        )
        actions[key] = action
        aliases[normalize_token(key)] = key
        for alias in action.aliases:
            aliases[normalize_token(alias)] = key

    return CacheConfig(conf_file=path, actions=actions, aliases=aliases, lock_file=LOCK_FILE)


def select_by_prefix(name: str, config: CacheConfig) -> list[str]:
    token = normalize_token(name)
    prefix = token.replace("-", "_") + "_"
    return [key for key in config.actions if key.startswith(prefix)]


def resolve_names(name: str, config: CacheConfig) -> list[str]:
    token = normalize_token(name)
    if token in config.aliases:
        return [config.aliases[token]]

    prefixed = select_by_prefix(name, config)
    if prefixed:
        return prefixed

    raise KeyError(f"Nom inconnu dans cache.conf : {name}")


def realpath_clean(path: Path | str) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    real = os.path.normpath(os.path.realpath(raw))
    return real.rstrip("/") or "/"


def path_matches_move_root(path: Path, root: Path | str) -> bool:
    path_s = realpath_clean(path)
    root_s = realpath_clean(root)
    if root_s == "/":
        return path_s.startswith("/")
    if root_s == "/mnt/disk":
        return bool(re.fullmatch(r"/mnt/disk(?:\d+)?(?:/.*)?", path_s))
    return path_s == root_s or path_s.startswith(root_s + "/")


def is_dangerous_move_endpoint(path: Path) -> bool:
    path_s = realpath_clean(path)
    return path_s in {"", "/", "/mnt"}


def is_under_or_equal(path: Path, root: Path) -> bool:
    return path_matches_move_root(path, root)


def path_matches_destination_root(path: Path, root: str) -> bool:
    return path_matches_move_root(path, root)


def fstab_unescape(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        try:
            return chr(int(match.group(1), 8))
        except Exception:
            return match.group(0)
    return re.sub(r"\\([0-7]{3})", repl, str(value or ""))


def clean_mergerfs_branch(value: str) -> str:
    branch = fstab_unescape(value).strip()
    if "=" in branch:
        candidate, _mode = branch.rsplit("=", 1)
        if candidate.startswith("/"):
            branch = candidate
    return branch


def expand_mergerfs_branches(source: str) -> list[str]:
    branches: list[str] = []
    for raw_branch in fstab_unescape(source).split(":"):
        branch = clean_mergerfs_branch(raw_branch)
        if not branch or not branch.startswith("/"):
            continue
        expanded = glob.glob(branch) if any(ch in branch for ch in "*?[") else []
        values = expanded or [branch]
        for item in values:
            clean = realpath_clean(item)
            if clean and clean not in branches:
                branches.append(clean)
    return branches


def mergerfs_source_spec(source: str, options: str = "") -> str:
    clean_source = fstab_unescape(source).strip()
    if clean_source and clean_source != "mergerfs":
        return clean_source
    for item in fstab_unescape(options).split(","):
        item = item.strip()
        if item.startswith("branches="):
            return item.split("=", 1)[1].strip()
    return clean_source


def add_mergerfs_view(views: list[dict[str, object]], seen: set, target: str, source: str, origin: str) -> None:
    clean_target = realpath_clean(fstab_unescape(target))
    branches = expand_mergerfs_branches(source)
    if not clean_target or not branches:
        return
    key = (clean_target, tuple(branches))
    if key in seen:
        return
    seen.add(key)
    views.append({"target": clean_target, "branches": branches, "origin": origin})


def mergerfs_views_from_fstab() -> list[dict[str, object]]:
    path = Path(os.environ.get("CACHE_FSTAB_FILE", "/etc/fstab"))
    views: list[dict[str, object]] = []
    seen: set = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return views
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 3:
            continue
        source, target, fstype = parts[:3]
        options = parts[3] if len(parts) > 3 else ""
        blob = " ".join([source, target, fstype, options]).lower()
        if "mergerfs" not in blob:
            continue
        add_mergerfs_view(views, seen, target, mergerfs_source_spec(source, options), "fstab")
    return views


def mergerfs_views_from_mountinfo() -> list[dict[str, object]]:
    path = Path(os.environ.get("CACHE_MOUNTINFO_FILE", "/proc/self/mountinfo"))
    views: list[dict[str, object]] = []
    seen: set = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return views
    for raw in lines:
        parts = raw.strip().split()
        if len(parts) < 10 or "-" not in parts:
            continue
        try:
            sep = parts.index("-")
            target = fstab_unescape(parts[4])
            fstype = parts[sep + 1] if len(parts) > sep + 1 else ""
            source = fstab_unescape(parts[sep + 2]) if len(parts) > sep + 2 else ""
            options = fstab_unescape(parts[sep + 3]) if len(parts) > sep + 3 else ""
        except Exception:
            continue
        source_spec = mergerfs_source_spec(source, options)
        if "mergerfs" not in fstype.lower() or ":" not in source_spec:
            continue
        add_mergerfs_view(views, seen, target, source_spec, "mountinfo")
    return views


def collect_mergerfs_views() -> list[dict[str, object]]:
    views: list[dict[str, object]] = []
    seen: set = set()
    for row in mergerfs_views_from_fstab() + mergerfs_views_from_mountinfo():
        target = str(row.get("target") or "")
        branches = [str(item) for item in (row.get("branches") or []) if str(item or "")]
        key = (target, tuple(branches))
        if not target or not branches or key in seen:
            continue
        seen.add(key)
        views.append({"target": target, "branches": branches, "origin": row.get("origin") or ""})
    return views


def relative_under(path: str, root: str) -> str | None:
    path_s = realpath_clean(path)
    root_s = realpath_clean(root)
    if not path_s or not root_s:
        return None
    if path_s == root_s:
        return ""
    prefix = root_s + "/"
    if path_s.startswith(prefix):
        return path_s[len(prefix):]
    return None


def join_under(root: str, rel: str) -> str:
    if not rel:
        return realpath_clean(root)
    return realpath_clean(os.path.join(root, rel))


def paths_overlap(left: str, right: str) -> bool:
    left_s = realpath_clean(left)
    right_s = realpath_clean(right)
    if not left_s or not right_s:
        return False
    return left_s == right_s or left_s.startswith(right_s + "/") or right_s.startswith(left_s + "/")


def physical_candidates(path: Path, views: list[dict[str, object]]) -> list[str]:
    clean_path = realpath_clean(path)
    candidates: list[str] = []

    def add(value: str) -> None:
        clean = realpath_clean(value)
        if clean and clean not in candidates:
            candidates.append(clean)

    add(clean_path)
    for view in views:
        rel = relative_under(clean_path, str(view.get("target") or ""))
        if rel is None:
            continue
        for branch in view.get("branches") or []:
            add(join_under(str(branch), rel))
    return candidates


def find_incompatible_overlap(source: Path, destination: Path) -> tuple[str, str] | None:
    views = collect_mergerfs_views()
    source_candidates = physical_candidates(source, views)
    destination_candidates = physical_candidates(destination, views)
    for src in source_candidates:
        for dst in destination_candidates:
            if paths_overlap(src, dst):
                return src, dst
    return None


def check_move_coherence(source: Path, destination: Path) -> None:
    source_s = realpath_clean(source)
    destination_s = realpath_clean(destination)
    if not source_s.startswith("/") or not destination_s.startswith("/"):
        raise RuntimeError("ERREUR : source incompatible avec cible : les chemins doivent etre absolus.")
    if is_dangerous_move_endpoint(source) or is_dangerous_move_endpoint(destination):
        raise RuntimeError("ERREUR : source incompatible avec cible : chemin trop large.")
    conflict = find_incompatible_overlap(source, destination)
    if conflict:
        left, right = conflict
        raise RuntimeError(
            "ERREUR : source incompatible avec cible : les deux chemins se voient ou se chevauchent "
            f"({left} <-> {right})."
        )


def safe_path_checks(action: CacheAction) -> None:
    if action.source is None or action.destination is None:
        return

    source = action.source
    destination = action.destination

    if not any(is_under_or_equal(source, root) for root in ALLOWED_SOURCE_ROOTS):
        roots = ", ".join(str(p) for p in ALLOWED_SOURCE_ROOTS)
        raise RuntimeError(f"ERREUR : source refusée hors racines autorisées ({roots}) : {source}")

    for root in ALLOWED_SOURCE_ROOTS:
        if os.path.realpath(source) == os.path.realpath(root):
            raise RuntimeError(f"ERREUR : source trop dangereuse, racine directe refusée : {source}")

    if not any(path_matches_destination_root(destination, root) for root in ALLOWED_DESTINATION_ROOTS):
        roots = ", ".join(ALLOWED_DESTINATION_ROOTS)
        raise RuntimeError(f"ERREUR : destination refusée hors racines autorisées ({roots}) : {destination}")

    dangerous = {"/", "/mnt"}
    if os.path.realpath(destination) in dangerous:
        raise RuntimeError(f"ERREUR : destination trop dangereuse : {destination}")

    src_real = os.path.realpath(source)
    dst_real = os.path.realpath(destination)
    if src_real == dst_real:
        raise RuntimeError("ERREUR : source et destination identiques.")

    check_move_coherence(source, destination)


def has_payload(path: Path) -> bool:
    if not path.is_dir():
        return False
    cp = run(
        ["find", str(path), "(", "-type", "f", "-o", "-type", "l", ")", "-print", "-quit"],
        quiet=True,
    )
    return bool(cp.stdout.strip())


def cleanup_empty_dirs(source: Path, dry_run: bool = False) -> None:
    if dry_run:
        print(f"DRY-RUN : nettoyage dossiers vides ignoré : {source}")
        return
    if not source.exists():
        return

    run(["find", str(source), "-mindepth", "1", "-type", "d", "-empty", "-delete"], quiet=True)
    try:
        source.rmdir()
        print(f"🧹 Dossier source vide supprimé : {source}")
    except OSError:
        pass


def prepare_destination(source: Path, destination: Path, dry_run: bool = False) -> None:
    if dry_run:
        print(f"DRY-RUN : création destination ignorée : {destination}")
        return

    destination.mkdir(parents=True, exist_ok=True)
    run(["chmod", f"--reference={source}", str(destination)], quiet=True)
    run(["chown", f"--reference={source}", str(destination)], quiet=True)


def run_shell_command(command: str, *, label: str, dry_run: bool = False) -> bool:
    command = command.strip()
    if not command:
        return True

    print("")
    print(f">>> {label}")
    print(command)
    if dry_run:
        print("DRY-RUN : commande ignorée")
        return True

    rc = subprocess.run(["bash", "-lc", command], text=True).returncode
    if rc != 0:
        print(f"❌ Commande échouée ({rc}) : {label}")
        return False
    return True


def move_payload(action: CacheAction, *, dry_run: bool = False) -> bool:
    assert action.source is not None
    assert action.destination is not None

    print("")
    print("============================================================")
    print(f"Déplacement : {action.title}")
    print(f"SOURCE      : {action.source}/")
    print(f"DESTINATION : {action.destination}/")
    print("============================================================")

    prepare_destination(action.source, action.destination, dry_run=dry_run)

    if not dry_run:
        run(["sync"])

    rsync_cmd = [
        "rsync",
        "-aAXHhv",
        "--info=progress2",
        "--stats",
        "--remove-source-files",
    ]
    if dry_run:
        rsync_cmd.append("--dry-run")
    rsync_cmd.extend([str(action.source) + "/", str(action.destination) + "/"])

    print(">>> " + " ".join(shlex.quote(x) for x in rsync_cmd))
    rc = run(rsync_cmd).returncode
    if rc != 0:
        print(f"❌ Erreur rsync : {action.title}")
        return False

    cleanup_empty_dirs(action.source, dry_run=dry_run)
    print(f"✅ Terminé : {action.title}")
    return True


def execute_action(action: CacheAction, *, dry_run: bool = False, explicit: bool = False) -> bool:
    if not action.enabled:
        print(f"⏭️  Désactivé : {action.title}")
        return True

    if action.has_move:
        safe_path_checks(action)
        assert action.source is not None

        if not action.source.exists():
            print(f"⏭️  Source absente : {action.title}")
            print(f"    {action.source}")
            return True

        if not has_payload(action.source):
            print(f"⏭️  Aucun fichier à déplacer : {action.title}")
            print(f"    Nettoyage éventuel des dossiers vides : {action.source}")
            cleanup_empty_dirs(action.source, dry_run=dry_run)
            return True

        ok = True
        if not run_shell_command(action.command_start, label=f"commande début {action.key}", dry_run=dry_run):
            return False
        try:
            ok = move_payload(action, dry_run=dry_run)
        finally:
            if not run_shell_command(action.command_end, label=f"commande fin {action.key}", dry_run=dry_run):
                ok = False
        return ok

    # Bloc sans source/destination : uniquement des commandes libres.
    if action.is_command_only:
        if not explicit and not action.include_in_all:
            return True
        ok = run_shell_command(action.command_start, label=f"commande début {action.key}", dry_run=dry_run)
        if not run_shell_command(action.command_end, label=f"commande fin {action.key}", dry_run=dry_run):
            ok = False
        return ok

    print(f"⏭️  Bloc sans action : {action.title}")
    return True


def list_actions(config: CacheConfig) -> int:
    print("Configuration :")
    print(f"  conf : {config.conf_file}")
    print(f"  lock : {config.lock_file}")
    print("")
    print("Commandes :")
    print("  --all                 Tous les blocs enabled + include_in_all")
    print("  --NOM                 Lance un bloc par son nom/alias")
    print("  --PREFIXE             Lance tous les blocs PREFIXE_*. Exemple : --nextcloud")
    print("  NOM                   Pareil, sans les tirets")
    print("  --dry-run             Teste sans déplacer ni exécuter les commandes")
    print("  --init-conf           Crée cache.conf s'il est absent")
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
        print(f"  --{key:<22} {action.title} ({status}) | start: {cmd_start} | end: {cmd_end}{aliases}")
        if action.has_move:
            print(f"      {action.source}  ->  {action.destination}")
        elif action.is_command_only:
            print("      commande seule")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mover cache Unraid piloté par cache.conf.",
        formatter_class=argparse.RawTextHelpFormatter,
        add_help=True,
    )
    parser.add_argument("--all", action="store_true", help="Déplace tous les blocs include_in_all.")
    parser.add_argument("--list", action="store_true", help="Liste les blocs du conf.")
    parser.add_argument("--dry-run", action="store_true", help="Teste sans déplacer et sans exécuter les commandes.")
    parser.add_argument("--conf", default=str(CACHE_CONF), help=f"Fichier conf à utiliser. Défaut : {CACHE_CONF}")
    parser.add_argument("--init-conf", action="store_true", help="Crée cache.conf s'il est absent, puis quitte.")
    parser.add_argument("names", nargs="*", help="Blocs ou préfixes. Ex : cache.py media nextcloud")

    args, unknown = parser.parse_known_args(argv)

    # Permet --media, --nextcloud, --immich, etc. sans les coder dans argparse.
    dynamic_names: list[str] = []
    for item in unknown:
        if item.startswith("--") and len(item) > 2:
            dynamic_names.append(item[2:])
        else:
            dynamic_names.append(item)
    args.names.extend(dynamic_names)
    return args


def build_selection(args: argparse.Namespace, config: CacheConfig) -> tuple[list[CacheAction], set[str]]:
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

    # Déduplication en gardant l'ordre du conf / de la sélection.
    result: list[CacheAction] = []
    seen: set[str] = set()
    for key in selected_keys:
        if key not in seen:
            result.append(config.actions[key])
            seen.add(key)

    return result, explicit_keys


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    conf_file = Path(args.conf)

    if args.init_conf:
        ensure_default_conf(conf_file)
        return 0

    config = load_config(conf_file)

    if args.list:
        return list_actions(config)

    if not command_exists("rsync"):
        print("ERREUR : rsync introuvable.")
        return 1

    actions, explicit_keys = build_selection(args, config)

    if not actions:
        print("❌ Action manquante.")
        print("Exemples :")
        print("  python3 cache.py --all")
        print("  python3 cache.py --media")
        print("  python3 cache.py media")
        print("  python3 cache.py --immich")
        print("  python3 cache.py --nextcloud")
        print("  python3 cache.py --list")
        return 1

    ok = True
    with NonBlockingLock(config.lock_file):
        print("============================================================")
        print("CACHE MOVER - moteur neutre piloté par cache.conf")
        print(f"CONF : {config.conf_file}")
        if args.dry_run:
            print("MODE : DRY-RUN")
        print("============================================================")

        for action in actions:
            explicit = action.key in explicit_keys
            if not execute_action(action, dry_run=args.dry_run, explicit=explicit):
                ok = False

    print("")
    if ok:
        print("✅ Cache mover terminé.")
        return 0
    print("❌ Cache mover terminé avec erreur.")
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\nInterrompu.")
        raise SystemExit(130)
    except (RuntimeError, KeyError) as exc:
        print(f"❌ {exc}")
        raise SystemExit(1)
