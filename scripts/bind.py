#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
bind.py - Gestion simple des bind mounts Linux via un fichier conf.

Logique :
  - OMV garde ses vrais montages dans /srv/dev-disk-by-uuid-...
  - Toi tu déclares des chemins propres dans /mnt/...
  - Le script écrit/supprime uniquement son propre bloc dans /etc/fstab

Chemin conf par défaut :
  ../conf/bind.conf

Exemples :
  python bind.py -list
  python bind.py -install
  python bind.py -remove
  python bind.py -remove cache
  python bind.py -mount cache
  python bind.py -umount cache
"""

import argparse
import configparser
import datetime as dt
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONF = (SCRIPT_DIR / "../conf/bind.conf").resolve()
FSTAB = Path("/etc/fstab")

BEGIN_MARKER = "# >>> bind.py managed"
END_MARKER = "# <<< bind.py managed"


@dataclass
class BindEntry:
    name: str
    source: str
    target: str
    options: str


def die(message: str, code: int = 1) -> None:
    print(f"ERREUR: {message}", file=sys.stderr)
    sys.exit(code)


def run(cmd, check=True, capture=False):
    if capture:
        return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)
    return subprocess.run(cmd, text=True, check=check)


def require_root() -> None:
    if os.geteuid() != 0:
        die("lance ce script en root.")


def fstab_escape(path: str) -> str:
    return path.replace("\\", "\\\\").replace(" ", "\\040")


def load_config(conf_path: Path):
    if not conf_path.exists():
        die(f"fichier conf introuvable: {conf_path}")

    cp = configparser.ConfigParser(
        interpolation=None,
        inline_comment_prefixes=("#", ";"),
        strict=False,
    )
    cp.optionxform = str
    cp.read(conf_path, encoding="utf-8")

    if "binds" not in cp:
        die("section [binds] introuvable dans le fichier conf")

    default_options = cp.get("global", "default_options", fallback="bind,nofail")
    add_requires = cp.getboolean("global", "add_requires_mounts_for", fallback=True)
    source_must_exist = cp.getboolean("global", "source_must_exist", fallback=True)
    source_must_be_mountpoint = cp.getboolean("global", "source_must_be_mountpoint", fallback=True)
    create_targets = cp.getboolean("global", "create_targets", fallback=True)
    allow_non_empty_target = cp.getboolean("global", "allow_non_empty_target", fallback=False)
    auto_mount_after_install = cp.getboolean("global", "auto_mount_after_install", fallback=True)

    entries = []

    for name, raw in cp["binds"].items():
        name = name.strip()
        raw = raw.strip()

        if not raw or raw.lower() in ("0", "no", "false", "disabled", "off"):
            continue

        # Format principal :
        # cache = /srv/dev-disk-by-uuid-XXXX -> /mnt/cache
        if "->" not in raw:
            die(f"{name}: format invalide. Utilise: source -> cible")

        source, target = [p.strip() for p in raw.split("->", 1)]

        if not source or not target:
            die(f"{name}: source ou cible vide")

        options = default_options

        if add_requires:
            req = f"x-systemd.requires-mounts-for={source}"
            opts = [x.strip() for x in options.split(",") if x.strip()]
            if req not in opts:
                opts.append(req)
            options = ",".join(opts)

        entries.append(BindEntry(name=name, source=source, target=target, options=options))

    settings = {
        "source_must_exist": source_must_exist,
        "source_must_be_mountpoint": source_must_be_mountpoint,
        "create_targets": create_targets,
        "allow_non_empty_target": allow_non_empty_target,
        "auto_mount_after_install": auto_mount_after_install,
    }

    return entries, settings


def is_path_inside(child: str, parent: str) -> bool:
    try:
        child_p = Path(child).resolve()
        parent_p = Path(parent).resolve()
        child_p.relative_to(parent_p)
        return child_p != parent_p
    except Exception:
        return False


def is_mounted(path: str) -> bool:
    return subprocess.run(
        ["findmnt", "--target", path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def mount_source(path: str) -> str:
    result = subprocess.run(
        ["findmnt", "--noheadings", "--output", "SOURCE", "--target", path],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def validate_entries(entries, settings):
    seen_targets = set()

    for entry in entries:
        source = Path(entry.source)
        target = Path(entry.target)

        if not entry.source.startswith("/"):
            die(f"{entry.name}: source non absolue: {entry.source}")

        if not entry.target.startswith("/"):
            die(f"{entry.name}: cible non absolue: {entry.target}")

        if entry.source == entry.target:
            die(f"{entry.name}: source et cible identiques")

        if is_path_inside(entry.target, entry.source):
            die(f"{entry.name}: cible dans la source, risque de boucle: {entry.target}")

        if entry.target in seen_targets:
            die(f"{entry.name}: cible déclarée plusieurs fois: {entry.target}")

        seen_targets.add(entry.target)

        if settings["source_must_exist"] and not source.exists():
            die(f"{entry.name}: source introuvable: {entry.source}")

        if settings["source_must_be_mountpoint"] and not os.path.ismount(entry.source):
            die(
                f"{entry.name}: la source existe mais n'est pas un point de montage: {entry.source}\n"
                "Si c'est volontaire, mets source_must_be_mountpoint = no dans [global]."
            )

        if target.exists() and target.is_dir() and not os.path.ismount(entry.target):
            try:
                has_content = any(target.iterdir())
            except PermissionError:
                has_content = True

            if has_content and not settings["allow_non_empty_target"]:
                die(
                    f"{entry.name}: la cible existe et n'est pas vide: {entry.target}\n"
                    "Par sécurité, vide-la ou mets allow_non_empty_target = yes dans [global]."
                )


def make_fstab_block(entries):
    lines = []
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines.append(BEGIN_MARKER)
    lines.append(f"# Generated by bind.py on {now}")
    lines.append("# Do not edit this block manually. Edit ../conf/bind.conf instead.")

    for entry in entries:
        line = (
            f"{fstab_escape(entry.source)} "
            f"{fstab_escape(entry.target)} "
            f"none "
            f"{entry.options} "
            f"0 0"
        )
        lines.append(f"# name={entry.name}")
        lines.append(line)

    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def read_fstab() -> str:
    if not FSTAB.exists():
        die("/etc/fstab introuvable")
    return FSTAB.read_text(encoding="utf-8", errors="replace")


def backup_fstab():
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = FSTAB.with_name(f"fstab.backup-bind-{stamp}")
    shutil.copy2(FSTAB, backup)
    print(f"Backup fstab: {backup}")


def replace_managed_block(text: str, new_block: str) -> str:
    begin = text.find(BEGIN_MARKER)
    end = text.find(END_MARKER)

    if begin == -1 and end == -1:
        text = text.rstrip() + "\n\n" + new_block
        return text

    if begin == -1 or end == -1 or end < begin:
        die("bloc bind.py cassé dans /etc/fstab. Corrige manuellement avant de continuer.")

    end += len(END_MARKER)
    while end < len(text) and text[end] in "\r\n":
        end += 1

    return text[:begin].rstrip() + "\n\n" + new_block + "\n" + text[end:].lstrip()


def remove_managed_block(text: str) -> str:
    begin = text.find(BEGIN_MARKER)
    end = text.find(END_MARKER)

    if begin == -1 and end == -1:
        return text

    if begin == -1 or end == -1 or end < begin:
        die("bloc bind.py cassé dans /etc/fstab. Corrige manuellement avant de continuer.")

    end += len(END_MARKER)
    while end < len(text) and text[end] in "\r\n":
        end += 1

    return text[:begin].rstrip() + "\n\n" + text[end:].lstrip()


def write_fstab(new_text: str):
    FSTAB.write_text(new_text, encoding="utf-8")


def list_entries(entries):
    print(f"Conf: {DEFAULT_CONF}")
    print()
    if not entries:
        print("Aucun bind déclaré.")
        return

    print(f"{'NOM':<16} {'ETAT':<10} {'SOURCE':<45} -> CIBLE")
    print("-" * 110)

    for entry in entries:
        state = "mounted" if is_mounted(entry.target) else "absent"
        src = mount_source(entry.target) if state == "mounted" else ""
        suffix = f" ({src})" if src else ""
        print(f"{entry.name:<16} {state:<10} {entry.source:<45} -> {entry.target}{suffix}")


def ensure_targets(entries):
    for entry in entries:
        target = Path(entry.target)
        if not target.exists():
            print(f"Création cible: {entry.target}")
            target.mkdir(parents=True, exist_ok=True)


def install(entries, settings):
    require_root()
    validate_entries(entries, settings)

    if settings["create_targets"]:
        ensure_targets(entries)

    backup_fstab()
    old = read_fstab()
    new = replace_managed_block(old, make_fstab_block(entries))
    write_fstab(new)

    print("Bloc bind.py installé dans /etc/fstab.")
    run(["systemctl", "daemon-reload"], check=False)

    if settings["auto_mount_after_install"]:
        print("Montage des binds...")
        run(["mount", "-a"], check=True)

    list_entries(entries)


def remove_all(entries):
    require_root()

    # On démonte d'abord les cibles connues, en ordre inverse.
    for entry in reversed(entries):
        if is_mounted(entry.target):
            print(f"Umount: {entry.target}")
            run(["umount", entry.target], check=False)

    backup_fstab()
    old = read_fstab()
    new = remove_managed_block(old)
    write_fstab(new)
    run(["systemctl", "daemon-reload"], check=False)
    print("Bloc bind.py supprimé de /etc/fstab.")


def find_entry(entries, name: str) -> BindEntry:
    for entry in entries:
        if entry.name == name or entry.target == name:
            return entry
    die(f"entrée introuvable: {name}")


def mount_one(entry):
    require_root()
    Path(entry.target).mkdir(parents=True, exist_ok=True)

    if is_mounted(entry.target):
        print(f"Déjà monté: {entry.target}")
        return

    print(f"Mount bind: {entry.source} -> {entry.target}")
    run(["mount", "--bind", entry.source, entry.target], check=True)
    list_entries([entry])


def umount_one(entry):
    require_root()

    if not is_mounted(entry.target):
        print(f"Pas monté: {entry.target}")
        return

    print(f"Umount: {entry.target}")
    run(["umount", entry.target], check=True)


def remove_one(entries, name: str, settings):
    require_root()
    entry = find_entry(entries, name)

    if is_mounted(entry.target):
        print(f"Umount: {entry.target}")
        run(["umount", entry.target], check=False)

    remaining = [e for e in entries if e.name != entry.name]

    backup_fstab()
    old = read_fstab()
    if remaining:
        new = replace_managed_block(old, make_fstab_block(remaining))
    else:
        new = remove_managed_block(old)
    write_fstab(new)
    run(["systemctl", "daemon-reload"], check=False)

    print(f"Entrée supprimée: {entry.name}")


def main():
    parser = argparse.ArgumentParser(description="Gestion des bind mounts via ../conf/bind.conf")
    parser.add_argument("-conf", "--conf", default=str(DEFAULT_CONF), help="Chemin du fichier conf")
    parser.add_argument("-list", action="store_true", help="Lister les binds déclarés")
    parser.add_argument("-install", action="store_true", help="Installer/régénérer le bloc fstab et monter")
    parser.add_argument("-remove", nargs="?", const="__ALL__", help="Supprimer tous les binds ou une entrée par nom")
    parser.add_argument("-mount", nargs="?", const="__ALL__", help="Monter tous les binds ou une entrée")
    parser.add_argument("-umount", nargs="?", const="__ALL__", help="Démonter tous les binds ou une entrée")

    args = parser.parse_args()

    conf_path = Path(args.conf).resolve()
    entries, settings = load_config(conf_path)

    if args.list:
        list_entries(entries)
        return

    if args.install:
        install(entries, settings)
        return

    if args.remove:
        if args.remove == "__ALL__":
            remove_all(entries)
        else:
            remove_one(entries, args.remove, settings)
        return

    if args.mount:
        require_root()
        if args.mount == "__ALL__":
            for entry in entries:
                mount_one(entry)
        else:
            mount_one(find_entry(entries, args.mount))
        return

    if args.umount:
        require_root()
        if args.umount == "__ALL__":
            for entry in reversed(entries):
                umount_one(entry)
        else:
            umount_one(find_entry(entries, args.umount))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
