#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ============================================================
# merge.py - Gestion simple des pools mergerfs / fstab
# ============================================================
#
# Version "Unraid-like" :
#   - /mnt/user  = cache + disques
#   - /mnt/user0 = disques seulement
#   - écriture cache d'abord grâce à category.create=ff
#   - ne plus utiliser category.create=mfs, car mfs choisit le disque
#     avec le plus d'espace libre et peut contourner le cache.
#
# LEXIQUE DES COMMANDES
#
# Afficher les montages mergerfs actifs et les lignes fstab :
#   python3 merge.py
#   python3 merge.py -liste
#   python3 merge.py -list
#   python3 merge.py -l
#
# Monter un pool mergerfs à chaud, sans modifier /etc/fstab :
#   sudo python3 merge.py -mount /mnt/cache /mnt/disk1 /mnt/disk2 /mnt/disk3 /mnt/user
#   sudo python3 merge.py -mount /mnt/disk1 /mnt/disk2 /mnt/disk3 /mnt/user0
#
# Installer un pool mergerfs dans /etc/fstab pour montage automatique :
#   sudo python3 merge.py -install /mnt/cache /mnt/disk1 /mnt/disk2 /mnt/disk3 /mnt/user
#   sudo python3 merge.py -install /mnt/disk1 /mnt/disk2 /mnt/disk3 /mnt/user0
#
# Installer automatiquement la paire Unraid-like :
#   sudo python3 merge.py -install-unraid /mnt/cache /mnt/disk1 /mnt/disk2 /mnt/disk3
#
# Ce qui ajoute :
#   /mnt/cache:/mnt/disk1:/mnt/disk2:/mnt/disk3  /mnt/user   fuse.mergerfs ...
#   /mnt/disk1:/mnt/disk2:/mnt/disk3             /mnt/user0  fuse.mergerfs ...
#
# Monter automatiquement la paire Unraid-like à chaud :
#   sudo python3 merge.py -mount-unraid /mnt/cache /mnt/disk1 /mnt/disk2 /mnt/disk3
#
# Installer tous les pools préparés dans ../conf/marge.conf :
#   sudo python3 merge.py -conf
#
# Supprimer le montage automatique depuis /etc/fstab :
#   sudo python3 merge.py -remove /mnt/user
#   sudo python3 merge.py -remove /mnt/user0
#   sudo python3 merge.py -remove-unraid
#
# Démonter un pool mergerfs à chaud :
#   sudo python3 merge.py -umount /mnt/user
#   sudo python3 merge.py -umount /mnt/user0
#   sudo python3 merge.py -umount-unraid
#
# Règle : dans -mount et -install, le dernier chemin est la destination.
#
# OPTIONS MERGERFS UTILISÉES
#
# Montage à chaud :
#   defaults,use_ino,cache.files=partial,category.create=ff,minfreespace=50G,moveonenospc=true
#
# Montage automatique /etc/fstab :
#   defaults,use_ino,cache.files=partial,category.create=ff,minfreespace=50G,moveonenospc=true,nofail,x-systemd.device-timeout=5s
#
# Le script ajoute aussi automatiquement :
#   x-systemd.requires-mounts-for=/mnt/...
#
# SÉCURITÉ
#
# - Ne formate rien.
# - Ne fait aucun apt install.
# - Sauvegarde /etc/fstab avant modification.
# - Refuse les sources qui ne semblent pas être sur un vrai montage.
# - Sources autorisées : /mnt/... ou vrais montages OMV /srv/dev-disk-by-uuid-...
# - Destination autorisée seulement sous /mnt/.
#
# ============================================================

from __future__ import annotations

import os
import sys
import shutil
import subprocess
import shlex
from datetime import datetime
from typing import Iterable, List, Sequence, Tuple


FSTAB = "/etc/fstab"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONF = os.path.abspath(os.path.join(SCRIPT_DIR, "../conf/marge.conf"))

USER_MOUNT = "/mnt/user"
USER0_MOUNT = "/mnt/user0"

SPECIAL_DESTINATIONS = {USER_MOUNT, USER0_MOUNT}


def is_special_destination(path: str) -> bool:
    try:
        return os.path.abspath(str(path or "").strip()) in SPECIAL_DESTINATIONS
    except Exception:
        return False


# Logique cache-first :
#   ff = first found / première branche disponible dans l'ordre donné
#   donc avec /mnt/cache en premier, les nouveaux fichiers vont d'abord au cache.
#   mfs est volontairement abandonné ici.
MERGERFS_MIN_FREE = os.environ.get("MERGE_MINFREE", "50G").strip() or "50G"

HOT_OPTIONS = [
    "defaults",
    "use_ino",
    "cache.files=partial",
    "category.create=ff",
    f"minfreespace={MERGERFS_MIN_FREE}",
    "moveonenospc=true",
]

FSTAB_BASE_OPTIONS = HOT_OPTIONS + [
    "nofail",
    "x-systemd.device-timeout=5s",
]


# ------------------------------------------------------------
# OUTILS GÉNÉRAUX
# ------------------------------------------------------------
def run(cmd: Sequence[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def need_root() -> None:
    if os.geteuid() != 0:
        print("ERREUR : lance le script en root.")
        print("Exemple : sudo python3 merge.py -install-unraid /mnt/cache /mnt/disk1 /mnt/disk2 /mnt/disk3")
        sys.exit(1)


def need_cmd(name: str) -> None:
    if shutil.which(name) is None:
        print(f"ERREUR : commande manquante : {name}")
        print("Je n'installe rien automatiquement.")
        sys.exit(1)


def backup_fstab() -> str:
    if not os.path.exists(FSTAB):
        print("ERREUR : /etc/fstab introuvable.")
        sys.exit(1)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{FSTAB}.backup_merge_{stamp}"
    shutil.copy2(FSTAB, backup)
    return backup


def normalize_path(path: str) -> str:
    path = str(path or "").strip()

    if not path.startswith("/"):
        print(f"ERREUR : chemin non absolu : {path}")
        print("Exemple correct : /mnt/disk1")
        sys.exit(1)

    path = os.path.abspath(path)

    if any(c.isspace() for c in path):
        print(f"ERREUR : chemin avec espace refusé : {path}")
        print("Garde des chemins simples pour /etc/fstab.")
        sys.exit(1)

    return path


def validate_destination(dest: str) -> str:
    dest = normalize_path(dest)

    forbidden = {"/", "/mnt", "/boot", "/boot/efi", "/home", "/var", "/usr", "/etc"}

    if dest in forbidden:
        print(f"ERREUR : destination interdite : {dest}")
        print("Choisis un dossier dédié, exemple : /mnt/user ou /mnt/user0")
        sys.exit(1)

    if not dest.startswith("/mnt/"):
        print(f"ERREUR : destination refusée : {dest}")
        print("Par sécurité, ce script accepte seulement les destinations dans /mnt/")
        sys.exit(1)

    # Important Debian pure :
    # /mnt/user et /mnt/user0 n'existent pas forcément encore.
    # Ce sont des DESTINATIONS mergerfs, donc le script doit les créer tout seul.
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as exc:
        print(f"ERREUR : impossible de créer la destination : {dest}")
        print(f"Détail : {exc}")
        sys.exit(1)

    return dest


def find_mount_target(path: str) -> str | None:
    r = run(["findmnt", "-rn", "--target", path, "-o", "TARGET"], check=False)

    if r.returncode != 0:
        return None

    lines = [x.strip() for x in r.stdout.splitlines() if x.strip()]
    if not lines:
        return None

    return lines[-1]


def is_exact_mountpoint(path: str) -> bool:
    r = run(["findmnt", "-rn", "--mountpoint", path, "-o", "TARGET"], check=False)
    return r.returncode == 0 and bool(r.stdout.strip())


def validate_sources(sources: Sequence[str]) -> List[str]:
    clean_sources: List[str] = []

    if len(sources) < 1:
        print("ERREUR : il faut au moins une source.")
        sys.exit(1)

    for src in sources:
        src = normalize_path(src)

        allowed_source = (
            src.startswith("/mnt/")
            or src.startswith("/srv/dev-disk-by-uuid-")
            or src.startswith("/srv/dev-disk-by-label-")
        )

        if not allowed_source:
            print(f"ERREUR : source refusée : {src}")
            print("Par sécurité, ce script accepte seulement :")
            print("  - les vrais montages simples /mnt/...")
            print("  - les vrais montages OMV /srv/dev-disk-by-uuid-...")
            print("  - les vrais montages OMV /srv/dev-disk-by-label-...")
            sys.exit(1)

        if not os.path.isdir(src):
            print(f"ERREUR : source introuvable ou pas un dossier : {src}")
            sys.exit(1)

        mount_target = find_mount_target(src)

        if mount_target in (None, "/"):
            print()
            print("ERREUR SÉCURITÉ")
            print("================")
            print(f"Source refusée : {src}")
            print("Raison : cette source ne semble pas être sur un vrai disque monté.")
            print("Elle semble dépendre seulement de /.")
            print()
            print("Vérifie avec :")
            print(f"  findmnt --target {src}")
            print()
            sys.exit(1)

        clean_sources.append(src)

    if len(set(clean_sources)) != len(clean_sources):
        print("ERREUR : source en double dans la commande.")
        sys.exit(1)

    return clean_sources


def parse_pool_args(args: Sequence[str]) -> Tuple[List[str], str]:
    if len(args) < 3:
        print("ERREUR : il faut au minimum 2 sources et 1 destination.")
        print("Exemple : sudo python3 merge.py -install /mnt/disk1 /mnt/disk2 /mnt/user0")
        sys.exit(1)

    sources = validate_sources(args[:-1])
    dest = validate_destination(args[-1])

    if dest in sources:
        print("ERREUR : la destination ne peut pas être aussi une source.")
        sys.exit(1)

    return sources, dest


def parse_unraid_args(args: Sequence[str]) -> Tuple[str, List[str]]:
    args = list(args)

    # Tolérance :
    # Si l'utilisateur met par erreur /mnt/user ou /mnt/user0 à la fin de
    # -install-unraid, on ne le prend pas comme source.
    # Le mode -install-unraid crée déjà automatiquement /mnt/user et /mnt/user0.
    if args and is_special_destination(args[-1]):
        print(f"INFO : destination finale ignorée en mode -install-unraid : {args[-1]}")
        print("INFO : -install-unraid crée automatiquement /mnt/user et /mnt/user0.")
        args = args[:-1]

    if len(args) < 2:
        print("ERREUR : il faut le cache puis au moins un disque.")
        print("Exemple : sudo python3 merge.py -install-unraid /mnt/cache /mnt/disk1 /mnt/disk2 /mnt/disk3")
        sys.exit(1)

    cache = validate_sources([args[0]])[0]
    data_disks = validate_sources(args[1:])

    if cache in data_disks:
        print("ERREUR : le cache ne peut pas être aussi dans la liste des disques.")
        sys.exit(1)

    return cache, data_disks


def format_sources(sources: Sequence[str]) -> str:
    return ":".join(sources)


def hot_options_string() -> str:
    return ",".join(HOT_OPTIONS)


def fstab_options_string(sources: Sequence[str]) -> str:
    options = list(FSTAB_BASE_OPTIONS)

    for src in sources:
        options.append(f"x-systemd.requires-mounts-for={src}")

    return ",".join(options)


# ------------------------------------------------------------
# LECTURE / ÉCRITURE FSTAB
# ------------------------------------------------------------
def read_fstab_lines() -> List[str]:
    if not os.path.exists(FSTAB):
        print("ERREUR : /etc/fstab introuvable.")
        sys.exit(1)

    with open(FSTAB, "r", encoding="utf-8") as f:
        return f.readlines()


def is_fstab_mergerfs_line(line: str) -> bool:
    clean = line.strip()

    if not clean or clean.startswith("#"):
        return False

    parts = clean.split()

    if len(parts) < 3:
        return False

    return "mergerfs" in parts[2].lower() or "mergerfs" in parts[0].lower()


def fstab_has_destination(dest: str) -> bool:
    for line in read_fstab_lines():
        if not is_fstab_mergerfs_line(line):
            continue

        parts = line.split()

        if len(parts) >= 2 and parts[1] == dest:
            return True

    return False


def make_fstab_line(sources: Sequence[str], dest: str) -> str:
    source_line = format_sources(sources)
    options = fstab_options_string(sources)
    return f"{source_line}  {dest}  fuse.mergerfs  {options}  0  0\n"


def append_fstab_pools(pools: Sequence[Tuple[str, Sequence[str], str]]) -> str:
    """
    pools = [(label, sources, dest), ...]
    Vérifie tout avant d'écrire, puis fait une seule sauvegarde fstab.
    """
    for _label, _sources, dest in pools:
        if fstab_has_destination(dest):
            print(f"ERREUR : une ligne mergerfs existe déjà dans /etc/fstab pour {dest}")
            print("Supprime-la d'abord avec :")
            print(f"  sudo python3 merge.py -remove {dest}")
            sys.exit(1)

    backup = backup_fstab()

    with open(FSTAB, "a", encoding="utf-8") as f:
        f.write("\n# Ajouté par merge.py\n")
        for label, sources, dest in pools:
            f.write(f"# {label}\n")
            f.write(make_fstab_line(sources, dest))

    subprocess.run(["systemctl", "daemon-reload"], check=False)
    return backup


def remove_fstab_destinations(destinations: Iterable[str]) -> Tuple[str | None, List[str]]:
    dest_set = {validate_destination(d) for d in destinations}
    lines = read_fstab_lines()
    remove_indexes: set[int] = set()
    found: List[str] = []

    for i, line in enumerate(lines):
        if not is_fstab_mergerfs_line(line):
            continue

        parts = line.split()

        if len(parts) >= 2 and parts[1] in dest_set:
            found.append(parts[1])
            remove_indexes.add(i)

            # Supprime les commentaires générés juste au-dessus.
            j = i - 1
            while j >= 0 and lines[j].strip().startswith("#"):
                if "Ajouté par merge.py" in lines[j] or "merge.py" in lines[j] or lines[j].strip() in {"# /mnt/user cache + disques", "# /mnt/user0 disques sans cache"}:
                    remove_indexes.add(j)
                    j -= 1
                    continue
                # Si commentaire label juste au-dessus, on l'enlève aussi.
                if lines[j].strip().startswith("# /mnt/"):
                    remove_indexes.add(j)
                    j -= 1
                    continue
                break

    if not remove_indexes:
        return None, []

    backup = backup_fstab()

    with open(FSTAB, "w", encoding="utf-8") as f:
        for i, line in enumerate(lines):
            if i not in remove_indexes:
                f.write(line)

    subprocess.run(["systemctl", "daemon-reload"], check=False)
    return backup, sorted(set(found))


# ------------------------------------------------------------
# ACTIONS GENERIQUES
# ------------------------------------------------------------
def ensure_mount_destination_ready(dest: str) -> None:
    if is_exact_mountpoint(dest):
        print(f"ERREUR : destination déjà montée : {dest}")
        print("Démonte d'abord avec :")
        print(f"  sudo python3 merge.py -umount {dest}")
        sys.exit(1)

    try:
        if os.path.isdir(dest) and os.listdir(dest):
            print(f"ERREUR : destination non vide : {dest}")
            print("Par sécurité, je refuse de monter mergerfs sur un dossier déjà rempli.")
            sys.exit(1)
    except PermissionError:
        print(f"ERREUR : impossible de lire la destination : {dest}")
        sys.exit(1)


def mount_sources_to_dest(sources: Sequence[str], dest: str, label: str = "pool") -> None:
    ensure_mount_destination_ready(dest)

    source_line = format_sources(sources)
    options = hot_options_string()

    print()
    print(f"MONTAGE MERGERFS À CHAUD : {label}")
    print("=" * 44)
    print(f"Sources     : {source_line}")
    print(f"Destination : {dest}")
    print(f"Options     : {options}")
    print()

    r = subprocess.run(["mergerfs", "-o", options, source_line, dest], text=True)

    if r.returncode != 0:
        print("ERREUR : montage mergerfs échoué.")
        sys.exit(r.returncode)

    print("OK : pool mergerfs monté à chaud.")
    print(f"Teste avec : ls -la {dest}")
    print()


def mount_pool(args: Sequence[str]) -> None:
    need_root()
    need_cmd("mergerfs")
    need_cmd("findmnt")

    sources, dest = parse_pool_args(args)
    mount_sources_to_dest(sources, dest, dest)


def install_pool(args: Sequence[str]) -> None:
    need_root()
    need_cmd("mergerfs")
    need_cmd("findmnt")

    sources, dest = parse_pool_args(args)

    backup = append_fstab_pools([(dest, sources, dest)])

    print()
    print("OK : pool mergerfs ajouté dans /etc/fstab")
    print(f"Sauvegarde créée : {backup}")
    print(f"Sources     : {format_sources(sources)}")
    print(f"Destination : {dest}")
    print(f"Options     : {fstab_options_string(sources)}")
    print()
    print("Le pool sera monté automatiquement au prochain démarrage.")
    print("Pour le monter tout de suite :")
    print(f"  sudo python3 merge.py -mount {' '.join(sources)} {dest}")
    print()


def remove_pool(args: Sequence[str]) -> None:
    need_root()

    if len(args) != 1:
        print("ERREUR : -remove attend seulement la destination.")
        print("Exemple : sudo python3 merge.py -remove /mnt/user")
        sys.exit(1)

    dest = validate_destination(args[0])
    backup, found = remove_fstab_destinations([dest])

    if not found:
        print(f"Aucune ligne mergerfs trouvée dans /etc/fstab pour : {dest}")
        sys.exit(0)

    print()
    print("OK : pool mergerfs supprimé de /etc/fstab")
    print(f"Sauvegarde créée : {backup}")
    print(f"Destination : {dest}")
    print()
    print("Note : si le pool est déjà monté maintenant, il reste monté à chaud.")
    print("Pour le démonter :")
    print(f"  sudo python3 merge.py -umount {dest}")
    print()


def umount_pool(args: Sequence[str]) -> None:
    need_root()

    if len(args) != 1:
        print("ERREUR : -umount attend seulement la destination.")
        print("Exemple : sudo python3 merge.py -umount /mnt/user")
        sys.exit(1)

    dest = validate_destination(args[0])

    if not is_exact_mountpoint(dest):
        print(f"INFO : {dest} n'est pas un point de montage actif.")
        return

    print()
    print("DÉMONTAGE MERGERFS")
    print("==================")
    print(f"Destination : {dest}")
    print()

    r = subprocess.run(["umount", dest], text=True)

    if r.returncode != 0:
        print("ERREUR : démontage impossible.")
        print("Si le dossier est occupé, quitte les terminaux qui sont dedans.")
        print("Exemple : cd /")
        sys.exit(r.returncode)

    print("OK : pool démonté à chaud.")
    print()


# ------------------------------------------------------------
# ACTIONS UNRAID-LIKE
# ------------------------------------------------------------
def unraid_pools_from_args(args: Sequence[str]) -> Tuple[str, List[str], List[Tuple[str, List[str], str]]]:
    cache, data_disks = parse_unraid_args(args)

    user_sources = [cache] + data_disks
    user0_sources = list(data_disks)

    pools = [
        ("/mnt/user cache + disques", user_sources, validate_destination(USER_MOUNT)),
        ("/mnt/user0 disques sans cache", user0_sources, validate_destination(USER0_MOUNT)),
    ]

    return cache, data_disks, pools


def install_unraid(args: Sequence[str]) -> None:
    need_root()
    need_cmd("mergerfs")
    need_cmd("findmnt")

    cache, data_disks, pools = unraid_pools_from_args(args)
    backup = append_fstab_pools(pools)

    print()
    print("OK : logique Unraid-like ajoutée dans /etc/fstab")
    print("================================================")
    print(f"Sauvegarde créée : {backup}")
    print()
    print(f"Cache       : {cache}")
    print(f"Disques     : {' '.join(data_disks)}")
    print()
    print("/mnt/user  = cache + disques")
    print(f"  Sources  : {format_sources([cache] + data_disks)}")
    print()
    print("/mnt/user0 = disques seulement")
    print(f"  Sources  : {format_sources(data_disks)}")
    print()
    print(f"Options    : {fstab_options_string([cache] + data_disks)}")
    print()
    print("Pour monter tout de suite :")
    print(f"  sudo python3 merge.py -mount-unraid {cache} {' '.join(data_disks)}")
    print()


def mount_unraid(args: Sequence[str]) -> None:
    need_root()
    need_cmd("mergerfs")
    need_cmd("findmnt")

    _cache, _data_disks, pools = unraid_pools_from_args(args)

    print()
    print("MONTAGE UNRAID-LIKE")
    print("===================")
    print("Ordre important :")
    print("  1) /mnt/user0 = disques sans cache")
    print("  2) /mnt/user  = cache + disques")
    print()

    # Monte user0 d'abord, puis user.
    for label, sources, dest in [pools[1], pools[0]]:
        mount_sources_to_dest(sources, dest, label)


def remove_unraid(args: Sequence[str]) -> None:
    need_root()

    if args:
        print("ERREUR : -remove-unraid ne prend aucun argument.")
        print("Il supprime seulement les lignes /mnt/user et /mnt/user0 dans /etc/fstab.")
        sys.exit(1)

    backup, found = remove_fstab_destinations([USER_MOUNT, USER0_MOUNT])

    if not found:
        print("Aucune ligne mergerfs trouvée dans /etc/fstab pour /mnt/user ou /mnt/user0.")
        return

    print()
    print("OK : lignes Unraid-like supprimées de /etc/fstab")
    print(f"Sauvegarde créée : {backup}")
    print(f"Destinations supprimées : {', '.join(found)}")
    print()
    print("Note : si les pools sont déjà montés maintenant, ils restent montés à chaud.")
    print("Pour les démonter :")
    print("  sudo python3 merge.py -umount-unraid")
    print()


def umount_unraid(args: Sequence[str]) -> None:
    need_root()

    if args:
        print("ERREUR : -umount-unraid ne prend aucun argument.")
        sys.exit(1)

    # Démonte /mnt/user avant /mnt/user0.
    for dest in [USER_MOUNT, USER0_MOUNT]:
        umount_pool([dest])


# ------------------------------------------------------------
# CONF
# ------------------------------------------------------------
def strip_python_command(tokens: List[str]) -> List[str]:
    """
    Accepte volontairement plusieurs formats dans marge.conf :

      -install /srv/a /srv/b /mnt/user
      merge.py -install /srv/a /srv/b /mnt/user
      python3 merge.py -install /srv/a /srv/b /mnt/user
      sudo python3 merge.py -install /srv/a /srv/b /mnt/user

      -install-unraid /mnt/cache /mnt/disk1 /mnt/disk2 /mnt/disk3
      sudo python3 merge.py -install-unraid /mnt/cache /mnt/disk1 /mnt/disk2
    """
    if not tokens:
        return tokens

    if tokens[0] == "sudo":
        tokens = tokens[1:]

    if tokens and os.path.basename(tokens[0]) in ("python", "python3"):
        tokens = tokens[1:]

    if tokens and os.path.basename(tokens[0]) == "merge.py":
        tokens = tokens[1:]

    return tokens


def read_conf_lines(conf_path: str) -> List[Tuple[int, str, List[str], str]]:
    if not os.path.exists(conf_path):
        print(f"ERREUR : fichier conf introuvable : {conf_path}")
        print()
        print("Crée par exemple :")
        print("  mkdir -p ../conf")
        print("  nano ../conf/marge.conf")
        print()
        print("Exemple Unraid-like :")
        print("  -install-unraid /mnt/cache /mnt/disk1 /mnt/disk2 /mnt/disk3")
        sys.exit(1)

    commands: List[Tuple[int, str, List[str], str]] = []

    with open(conf_path, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()

            if not line or line.startswith("#"):
                continue

            try:
                tokens = shlex.split(line, comments=True, posix=True)
            except ValueError as e:
                print(f"ERREUR : syntaxe invalide dans {conf_path}:{lineno}")
                print(f"Ligne : {raw.rstrip()}")
                print(f"Détail : {e}")
                sys.exit(1)

            tokens = strip_python_command(tokens)

            if not tokens:
                continue

            action = tokens[0].lower()

            # Si la ligne commence directement par des chemins, on considère que c'est un -install.
            if action.startswith("/"):
                action = "-install"
                args = tokens
            else:
                args = tokens[1:]

            if action in ("-install", "install"):
                commands.append((lineno, "-install", args, raw.rstrip()))
                continue

            if action in ("-install-unraid", "install-unraid", "-unraid", "unraid"):
                # Deux formats acceptés dans marge.conf :
                #
                # 1) Format automatique recommandé :
                #    -install-unraid /mnt/cache /mnt/disk1 /mnt/disk2
                #    -> crée /mnt/user et /mnt/user0
                #
                # 2) Format explicite/toléré :
                #    -install-unraid /mnt/cache /mnt/disk1 /mnt/disk2 /mnt/user
                #    -install-unraid /mnt/disk1 /mnt/disk2 /mnt/user0
                #    -> traité comme deux -install classiques
                #
                # Ça évite l'ancien bug "destination déclarée plusieurs fois"
                # et ça évite aussi de prendre /mnt/user comme une source.
                if args and is_special_destination(args[-1]):
                    commands.append((lineno, "-install", args, raw.rstrip()))
                else:
                    commands.append((lineno, "-install-unraid", args, raw.rstrip()))
                continue

            print(f"ERREUR : action refusée dans {conf_path}:{lineno} : {tokens[0]}")
            print("Pour -conf, seules ces actions sont acceptées :")
            print("  -install /mnt/disk1 /mnt/disk2 /mnt/user0")
            print("  -install-unraid /mnt/cache /mnt/disk1 /mnt/disk2 /mnt/disk3")
            sys.exit(1)

    return commands


def declared_destinations_for_command(action: str, args: Sequence[str]) -> List[str]:
    if action == "-install":
        if len(args) < 3:
            return []
        return [normalize_path(args[-1])]
    if action == "-install-unraid":
        return [USER_MOUNT, USER0_MOUNT]
    return []


def validate_conf_no_duplicate_destinations(commands: Sequence[Tuple[int, str, List[str], str]]) -> None:
    """
    Sécurité simple : on refuse un marge.conf qui déclare deux fois la même destination.
    """
    seen: dict[str, int] = {}

    for lineno, action, args, _raw_line in commands:
        for dest in declared_destinations_for_command(action, args):
            if dest in seen:
                print("ERREUR : destination mergerfs déclarée plusieurs fois dans le conf.")
                print(f"Destination : {dest}")
                print(f"Première ligne : {seen[dest]}")
                print(f"Deuxième ligne : {lineno}")
                print()
                print("Corrige ../conf/marge.conf : une seule ligne active par destination.")
                sys.exit(1)

            seen[dest] = lineno


def install_from_conf(args: Sequence[str]) -> None:
    need_root()
    need_cmd("mergerfs")
    need_cmd("findmnt")

    conf_path = DEFAULT_CONF

    if len(args) > 1:
        print("ERREUR : -conf accepte au maximum un chemin de fichier conf.")
        print("Exemple : sudo python3 merge.py -conf")
        print("Exemple : sudo python3 merge.py -conf ../conf/marge.conf")
        sys.exit(1)

    if len(args) == 1:
        conf_path = os.path.abspath(args[0])

    commands = read_conf_lines(conf_path)
    validate_conf_no_duplicate_destinations(commands)

    print()
    print("INSTALLATION MERGERFS DEPUIS CONF")
    print("=================================")
    print(f"Conf : {conf_path}")
    print()

    if not commands:
        print("Aucune ligne active dans le fichier conf.")
        print()
        return

    pools_to_append: List[Tuple[str, List[str], str]] = []

    for lineno, action, cmd_args, raw_line in commands:
        print()
        print(f"[Ligne {lineno}] {raw_line}")
        print("-" * 80)

        if action == "-install":
            sources, dest = parse_pool_args(cmd_args)
            pools_to_append.append((dest, sources, dest))
            print(f"Préparé : {format_sources(sources)} -> {dest}")

        elif action == "-install-unraid":
            _cache, _data_disks, pools = unraid_pools_from_args(cmd_args)
            for label, sources, dest in pools:
                pools_to_append.append((label, sources, dest))
            print("Préparé : paire Unraid-like /mnt/user + /mnt/user0")

    if not pools_to_append:
        print("Aucun pool à installer.")
        return

    backup = append_fstab_pools(pools_to_append)

    print()
    print("OK : toutes les lignes actives du fichier conf ont été ajoutées dans /etc/fstab.")
    print(f"Sauvegarde créée : {backup}")
    print()
    for label, sources, dest in pools_to_append:
        print(f"- {label}")
        print(f"  Sources     : {format_sources(sources)}")
        print(f"  Destination : {dest}")
    print()


# ------------------------------------------------------------
# AFFICHAGE
# ------------------------------------------------------------
def show_active_mounts() -> None:
    print()
    print("MONTAGES MERGERFS ACTIFS")
    print("========================")
    print()

    r = run(["findmnt", "-rn", "-t", "fuse.mergerfs", "-o", "SOURCE,TARGET,OPTIONS"], check=False)
    output = r.stdout.strip()

    if not output:
        r = run(["mount"], check=False)
        lines = [line for line in r.stdout.splitlines() if "mergerfs" in line.lower()]
        output = "\n".join(lines)

    if not output:
        print("Aucun montage mergerfs actif.")
    else:
        print(output)

    print()


def show_fstab_pools() -> None:
    print("POOLS MERGERFS DANS /etc/fstab")
    print("==============================")
    print()

    found = False

    for line in read_fstab_lines():
        if not is_fstab_mergerfs_line(line):
            continue

        parts = line.split()

        if len(parts) < 4:
            continue

        found = True
        print(f"Sources     : {parts[0]}")
        print(f"Destination : {parts[1]}")
        print(f"Type        : {parts[2]}")
        print(f"Options     : {parts[3]}")
        print()

    if not found:
        print("Aucune ligne mergerfs dans /etc/fstab.")
        print()


def show_list() -> None:
    need_cmd("findmnt")
    show_active_mounts()
    show_fstab_pools()

    print("LOGIQUE UNRAID-LIKE")
    print("===================")
    print("  /mnt/user  = cache + disques")
    print("  /mnt/user0 = disques seulement")
    print("  policy     = category.create=ff, donc ordre des branches respecté")
    print("  cache      = première branche, exemple /mnt/cache:/mnt/disk1:/mnt/disk2")
    print()

    print("EXEMPLES")
    print("========")
    print("  sudo python3 merge.py -install-unraid /mnt/cache /mnt/disk1 /mnt/disk2 /mnt/disk3")
    print("  sudo python3 merge.py -mount-unraid /mnt/cache /mnt/disk1 /mnt/disk2 /mnt/disk3")
    print("  sudo python3 merge.py -remove-unraid")
    print("  sudo python3 merge.py -umount-unraid")
    print()
    print("  sudo python3 merge.py -install /mnt/disk1 /mnt/disk2 /mnt/user0")
    print("  sudo python3 merge.py -remove /mnt/user")
    print("  sudo python3 merge.py -conf")
    print()


def usage() -> None:
    print()
    print("Usage :")
    print("  python3 merge.py -liste")
    print()
    print("Mode Unraid-like recommandé :")
    print("  sudo python3 merge.py -install-unraid /mnt/cache /mnt/disk1 /mnt/disk2 /mnt/disk3")
    print("  sudo python3 merge.py -mount-unraid /mnt/cache /mnt/disk1 /mnt/disk2 /mnt/disk3")
    print("  sudo python3 merge.py -remove-unraid")
    print("  sudo python3 merge.py -umount-unraid")
    print()
    print("Mode manuel :")
    print("  sudo python3 merge.py -mount /mnt/cache /mnt/disk1 /mnt/disk2 /mnt/disk3 /mnt/user")
    print("  sudo python3 merge.py -install /mnt/cache /mnt/disk1 /mnt/disk2 /mnt/disk3 /mnt/user")
    print("  sudo python3 merge.py -remove /mnt/user")
    print("  sudo python3 merge.py -umount /mnt/user")
    print()
    print("Règle : dans -mount et -install, le dernier chemin est la destination.")
    print("Option par défaut importante : category.create=ff, pas mfs.")
    print()


def main() -> None:
    if len(sys.argv) == 1:
        show_list()
        sys.exit(0)

    action = sys.argv[1].lower()

    if action in ["-h", "--help", "help"]:
        usage()
        sys.exit(0)

    if action in ["-liste", "-list", "--list", "list", "-l"]:
        show_list()
        sys.exit(0)

    if action == "-mount":
        mount_pool(sys.argv[2:])
        sys.exit(0)

    if action == "-install":
        install_pool(sys.argv[2:])
        sys.exit(0)

    if action in ["-conf", "--conf", "conf"]:
        install_from_conf(sys.argv[2:])
        sys.exit(0)

    if action in ["-remove", "-rm", "-delete", "-del"]:
        remove_pool(sys.argv[2:])
        sys.exit(0)

    if action in ["-umount", "-unmount", "-u"]:
        umount_pool(sys.argv[2:])
        sys.exit(0)

    if action in ["-install-unraid", "install-unraid", "-unraid", "unraid"]:
        install_unraid(sys.argv[2:])
        sys.exit(0)

    if action in ["-mount-unraid", "mount-unraid"]:
        mount_unraid(sys.argv[2:])
        sys.exit(0)

    if action in ["-remove-unraid", "remove-unraid", "-rm-unraid"]:
        remove_unraid(sys.argv[2:])
        sys.exit(0)

    if action in ["-umount-unraid", "umount-unraid", "-unmount-unraid"]:
        umount_unraid(sys.argv[2:])
        sys.exit(0)

    print(f"ERREUR : action inconnue : {action}")
    usage()
    sys.exit(1)


if __name__ == "__main__":
    main()
