#!/usr/bin/env python3

# ============================================================
# format.py - Outil LABO pour partitions / tables / formatage
# ============================================================
#
# LEXIQUE DES COMMANDES
#
# Afficher les disques / partitions / protections :
#   python3 format.py
#   python3 format.py -list
#   python3 format.py -liste
#   python3 format.py -l
#
# Créer une table de partition vide :
#   sudo python3 format.py -table /sda=gpt
#   sudo python3 format.py -table /sda=msdos
#   sudo python3 format.py -table /sda gpt
#   sudo python3 format.py -table /sda msdos
#
# Créer une table + partition swap seule :
#   sudo python3 format.py -table /sda=gpt swap=1G
#   sudo python3 format.py -table /sda swap=1G
#
# Créer une table + une partition principale formatée :
#   sudo python3 format.py -create /sda=xfs
#   sudo python3 format.py -create /sda=ext4
#   sudo python3 format.py -create /sda=btrfs
#   sudo python3 format.py -create /sda=ntfs
#   sudo python3 format.py -create /sda=fat32
#   sudo python3 format.py -create /sda=exfat
#
# Créer une table + partition principale + swap à la fin :
#   sudo python3 format.py -create /sda=xfs swap=1G
#   sudo python3 format.py -create /sda=ext4 swap=2G
#
# Formater une partition existante :
#   sudo python3 format.py -format /sda /sda1=xfs
#   sudo python3 format.py -format /sda /sda1=ext4
#   sudo python3 format.py -format /sda /sda1=btrfs
#   sudo python3 format.py -format /sda /sda1=ntfs
#   sudo python3 format.py -format /sda /sda1=fat32
#   sudo python3 format.py -format /sda /sda1=exfat
#   sudo python3 format.py -format /sda /sda1=swap
#
# Forme acceptée aussi :
#   sudo python3 format.py -format /sda /sda1 xfs
#
# Détruire une partition :
#   sudo python3 format.py -delete /sda /sda1
#
# Effacer les signatures d'un disque entier :
#   sudo python3 format.py -wipe /sda
#
# RÈGLES DE SÉCURITÉ
#
# - Le disque qui contient / est toujours bloqué.
# - Les disques contenant /boot ou /boot/efi sont aussi bloqués.
# - Le script refuse de toucher à une partition montée.
# - Le script refuse de refaire une table si une partition du disque est montée.
# - Le script ne modifie pas /etc/fstab.
# - Le script n'installe aucun paquet automatiquement.
# - Les formatages sont rapides par défaut : ce script ne fait pas
#   d'effacement complet du disque. Pour une revente/destruction,
#   utiliser une commande dédiée d'effacement sécurisé, séparée.
#
# NOTES
#
# - Ce script est prévu pour un LABO / VM / faux disques.
# - Sur un vrai NAS, ne l'utilise pas sans vérifier 3 fois le disque ciblé.
# - Les noms /sda, /sdb peuvent changer au reboot : vérifie avec -list.
#
# ============================================================

import json
import os
import re
import shutil
import subprocess
import sys
import time


SUPPORTED_FS = {"xfs", "ext4", "btrfs", "ntfs", "fat32", "exfat", "swap"}
SUPPORTED_TABLES = {"gpt", "msdos"}
PROTECTED_MOUNTS = {"/", "/boot", "/boot/efi"}


def run(cmd, input_text=None, check=True):
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
    )


def need_root():
    if os.geteuid() != 0:
        print("ERREUR : lance le script en root.")
        print("Exemple : sudo python3 format.py -format /sda /sda1=xfs")
        sys.exit(1)


def need_cmd(name, hint=None):
    if shutil.which(name) is None:
        print(f"ERREUR : commande manquante : {name}")
        if hint:
            print(hint)
        print("Je n'installe rien automatiquement.")
        sys.exit(1)


def dev_path(value):
    value = value.strip()
    if value.startswith("/dev/"):
        return value
    if value.startswith("/"):
        value = value[1:]
    return "/dev/" + value


def device_exists(dev):
    return os.path.exists(dev)


def read_lsblk():
    r = run([
        "lsblk", "-J", "-o",
        "NAME,PATH,PKNAME,TYPE,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,MODEL"
    ])
    return json.loads(r.stdout)


def find_node_by_path(path):
    data = read_lsblk()

    def walk(node):
        if node.get("path") == path:
            return node
        for child in node.get("children") or []:
            found = walk(child)
            if found:
                return found
        return None

    for dev in data.get("blockdevices", []):
        found = walk(dev)
        if found:
            return found
    return None


def node_type(dev):
    try:
        r = run(["lsblk", "-dn", "-o", "TYPE", dev])
        return r.stdout.strip()
    except subprocess.CalledProcessError:
        return ""


def is_disk(dev):
    return node_type(dev) == "disk"


def is_partition(dev):
    return node_type(dev) == "part"


def mounted_points(dev):
    node = find_node_by_path(dev)
    if not node:
        return []
    return [m for m in (node.get("mountpoints") or []) if m]


def part_is_mounted(part):
    return bool(mounted_points(part))


def disk_has_mounted_partitions(disk):
    node = find_node_by_path(disk)
    if not node:
        return True

    def walk(n):
        if any(m for m in (n.get("mountpoints") or []) if m):
            return True
        return any(walk(c) for c in (n.get("children") or []))

    for child in node.get("children") or []:
        if walk(child):
            return True
    return False


def detect_system_disks():
    data = read_lsblk()
    protected = set()

    def walk(node, current_disk=None):
        if node.get("type") == "disk":
            current_disk = node.get("path")

        for mount in node.get("mountpoints") or []:
            if mount in PROTECTED_MOUNTS and current_disk:
                protected.add(current_disk)

        for child in node.get("children") or []:
            walk(child, current_disk)

    for dev in data.get("blockdevices", []):
        walk(dev)

    return protected


def protect_disk_or_exit(disk):
    disk = dev_path(disk)

    if not device_exists(disk):
        print(f"ERREUR : disque introuvable : {disk}")
        sys.exit(1)

    if not is_disk(disk):
        print(f"ERREUR : ce n'est pas un disque brut : {disk}")
        sys.exit(1)

    protected = detect_system_disks()
    if disk in protected:
        print()
        print("BLOCAGE SÉCURITÉ")
        print("================")
        print(f"Disque protégé : {disk}")
        print("Raison : ce disque contient /, /boot ou /boot/efi.")
        print("Je refuse de toucher au disque système.")
        print()
        sys.exit(1)

    return disk


def ensure_part_belongs_to_disk(disk, part):
    disk = dev_path(disk)
    part = dev_path(part)
    node = find_node_by_path(part)

    if not node:
        print(f"ERREUR : partition introuvable : {part}")
        sys.exit(1)

    if node.get("type") != "part":
        print(f"ERREUR : ce n'est pas une partition : {part}")
        sys.exit(1)

    pkname = node.get("pkname")
    parent = f"/dev/{pkname}" if pkname else None

    if parent != disk:
        print(f"ERREUR : {part} n'appartient pas à {disk}")
        if parent:
            print(f"Parent détecté : {parent}")
        sys.exit(1)

    return disk, part


def partition_path(disk, num):
    base = disk
    # /dev/nvme0n1 -> /dev/nvme0n1p1 ; /dev/sda -> /dev/sda1
    if re.search(r"\d$", base):
        return f"{base}p{num}"
    return f"{base}{num}"


def partition_number(disk, part):
    disk = dev_path(disk)
    part = dev_path(part)
    base = re.escape(disk)
    m = re.match(rf"^{base}p?(\d+)$", part)
    if not m:
        print(f"ERREUR : impossible de trouver le numéro de partition de {part}")
        sys.exit(1)
    return m.group(1)


def normalize_size(size):
    value = size.strip().upper()
    m = re.match(r"^(\d+)([KMGTP])I?B?$", value)
    if not m:
        print(f"ERREUR : taille invalide : {size}")
        print("Exemples acceptés : 512M, 1G, 2G, 10G")
        sys.exit(1)
    number, unit = m.groups()
    return f"{number}{unit}iB"


def parse_swap_arg(args):
    for arg in args:
        if arg.lower().startswith("swap="):
            return normalize_size(arg.split("=", 1)[1])
    return None


def parse_table_args(args):
    if not args:
        usage()
        sys.exit(1)

    table_type = "gpt"
    swap_size = parse_swap_arg(args[1:])

    first = args[0]
    if "=" in first:
        disk, value = first.split("=", 1)
        value = value.lower().strip()
        if value in SUPPORTED_TABLES:
            table_type = value
        else:
            print(f"ERREUR : table invalide : {value}")
            print("Tables supportées : gpt, msdos")
            sys.exit(1)
    else:
        disk = first
        for arg in args[1:]:
            value = arg.lower().strip()
            if value in SUPPORTED_TABLES:
                table_type = value

    if table_type not in SUPPORTED_TABLES:
        print(f"ERREUR : table invalide : {table_type}")
        sys.exit(1)

    return disk, table_type, swap_size


def parse_create_args(args):
    if not args:
        usage()
        sys.exit(1)

    table_type = "gpt"
    fs_type = None
    swap_size = parse_swap_arg(args[1:])

    first = args[0]
    if "=" in first:
        disk, value = first.split("=", 1)
        value = value.lower().strip()
        if value in SUPPORTED_FS:
            fs_type = value
        elif value in SUPPORTED_TABLES:
            table_type = value
        else:
            print(f"ERREUR : valeur inconnue : {value}")
            sys.exit(1)
    else:
        disk = first

    for arg in args[1:]:
        value = arg.lower().strip()
        if value.startswith("swap="):
            continue
        if value in SUPPORTED_TABLES:
            table_type = value
        elif value in SUPPORTED_FS:
            fs_type = value
        elif "=" in value:
            k, v = value.split("=", 1)
            if k in {"fs", "format"} and v in SUPPORTED_FS:
                fs_type = v
            elif k in {"table", "label"} and v in SUPPORTED_TABLES:
                table_type = v
            else:
                print(f"ERREUR : option inconnue : {arg}")
                sys.exit(1)

    if not fs_type:
        # Compatibilité avec l'exemple : format.py -create /sda gpt
        # Dans ce cas on crée seulement la table vide.
        return disk, table_type, None, swap_size

    return disk, table_type, fs_type, swap_size


def parse_format_args(args):
    if len(args) < 2:
        usage()
        sys.exit(1)

    disk = args[0]
    part_arg = args[1]
    fs_type = None

    if "=" in part_arg:
        part, fs_type = part_arg.split("=", 1)
    else:
        part = part_arg
        if len(args) >= 3:
            fs_type = args[2]

    if not fs_type:
        print("ERREUR : format manquant.")
        print("Exemple : sudo python3 format.py -format /sda /sda1=xfs")
        sys.exit(1)

    fs_type = fs_type.lower().strip()
    if fs_type not in SUPPORTED_FS:
        print(f"ERREUR : format non supporté : {fs_type}")
        print("Formats supportés : xfs, ext4, btrfs, ntfs, fat32, exfat, swap")
        sys.exit(1)

    return disk, part, fs_type


def fs_command(fs_type, part):
    if fs_type == "xfs":
        need_cmd("mkfs.xfs", "Paquet habituel : xfsprogs")
        # Formatage rapide : -f force le formatage, -K évite le discard/TRIM complet.
        return ["mkfs.xfs", "-f", "-K", part]

    if fs_type == "ext4":
        need_cmd("mkfs.ext4", "Paquet habituel : e2fsprogs")
        # Formatage rapide : lazy init + pas de discard/TRIM complet.
        return [
            "mkfs.ext4",
            "-F",
            "-E",
            "lazy_itable_init=1,lazy_journal_init=1,nodiscard",
            part,
        ]

    if fs_type == "btrfs":
        need_cmd("mkfs.btrfs", "Paquet habituel : btrfs-progs")
        # Formatage rapide : -f force, -K évite le discard/TRIM complet.
        return ["mkfs.btrfs", "-f", "-K", part]

    if fs_type == "ntfs":
        need_cmd("mkfs.ntfs", "Paquet habituel : ntfs-3g")
        # Formatage rapide NTFS : -Q = quick format, -F = force.
        return ["mkfs.ntfs", "-Q", "-F", part]

    if fs_type == "fat32":
        cmd = shutil.which("mkfs.vfat") or shutil.which("mkfs.fat")
        if not cmd:
            need_cmd("mkfs.vfat", "Paquet habituel : dosfstools")
        return [cmd, "-F", "32", part]

    if fs_type == "exfat":
        cmd = shutil.which("mkfs.exfat") or shutil.which("mkexfatfs")
        if not cmd:
            need_cmd("mkfs.exfat", "Paquet habituel : exfatprogs")
        return [cmd, part]

    if fs_type == "swap":
        need_cmd("mkswap", "Paquet habituel : util-linux")
        return ["mkswap", part]

    print(f"ERREUR : format non supporté : {fs_type}")
    sys.exit(1)


def reread_partitions(disk):
    subprocess.run(["partprobe", disk], text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["udevadm", "settle"], text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)


def show_list():
    data = read_lsblk()
    protected = detect_system_disks()

    print()
    print("LISTE DES DISQUES - FORMATAGE LABO")
    print("==================================")
    print()

    for disk in data.get("blockdevices", []):
        if disk.get("type") != "disk":
            continue

        disk_path = disk.get("path")
        size = disk.get("size") or ""
        model = disk.get("model") or ""
        status = "PROTÉGÉ SYSTÈME" if disk_path in protected else "TEST POSSIBLE"

        print(f"{disk_path} | {size} | {model} | {status}")

        children = disk.get("children") or []
        if not children:
            print("  └─ aucune partition")
            if disk_path not in protected:
                print(f"     table  : sudo python3 format.py -table {disk_path}=gpt")
                print(f"     create : sudo python3 format.py -create {disk_path}=xfs")
            print()
            continue

        for part in children:
            part_path = part.get("path")
            psize = part.get("size") or ""
            fstype = part.get("fstype") or "aucun"
            label = part.get("label") or ""
            uuid = part.get("uuid") or ""
            mounts = [m for m in (part.get("mountpoints") or []) if m]

            print(f"  └─ {part_path} | {psize} | type={fstype}")
            if label:
                print(f"     label : {label}")
            if uuid:
                print(f"     uuid  : {uuid}")
            if mounts:
                print(f"     état  : MONTÉE sur {', '.join(mounts)}")
            else:
                print("     état  : non montée")
                if disk_path not in protected:
                    print(f"     format : sudo python3 format.py -format {disk_path} {part_path}=xfs")
                    print(f"     delete : sudo python3 format.py -delete {disk_path} {part_path}")
        print()

    print("Formats supportés : xfs, ext4, btrfs, ntfs, fat32, exfat, swap")
    print("Tables supportées : gpt, msdos")
    print()


def create_empty_table(disk, table_type, swap_size=None):
    need_root()
    need_cmd("parted", "Paquet habituel : parted")

    disk = protect_disk_or_exit(disk)
    if disk_has_mounted_partitions(disk):
        print(f"ERREUR : {disk} contient une partition montée.")
        print("Démonte les partitions avant de recréer la table.")
        sys.exit(1)

    print()
    print(f"CRÉATION TABLE {table_type.upper()} : {disk}")
    print("====================================")
    print()

    r = subprocess.run(["parted", "-s", disk, "mklabel", table_type], text=True)
    if r.returncode != 0:
        print("ERREUR : création de table échouée.")
        sys.exit(r.returncode)

    reread_partitions(disk)

    if swap_size:
        print(f"Création d'une partition swap de {swap_size} en fin de disque...")
        r = subprocess.run([
            "parted", "-s", disk, "--", "mkpart", "primary", "linux-swap", f"-{swap_size}", "100%"
        ], text=True)
        if r.returncode != 0:
            print("ERREUR : création de la partition swap échouée.")
            sys.exit(r.returncode)
        reread_partitions(disk)
        swap_part = partition_path(disk, 1)
        if device_exists(swap_part):
            cmd = fs_command("swap", swap_part)
            r = subprocess.run(cmd, text=True)
            if r.returncode != 0:
                print("ERREUR : mkswap a échoué.")
                sys.exit(r.returncode)
            print(f"OK : partition swap créée : {swap_part}")

    print(f"OK : table {table_type} créée sur {disk}")
    print("Relance : python3 format.py -list")
    print()


def create_and_format(disk, table_type, fs_type, swap_size=None):
    need_root()
    need_cmd("parted", "Paquet habituel : parted")

    if fs_type is None:
        create_empty_table(disk, table_type, swap_size)
        return

    disk = protect_disk_or_exit(disk)
    if disk_has_mounted_partitions(disk):
        print(f"ERREUR : {disk} contient une partition montée.")
        print("Démonte les partitions avant de recréer la table.")
        sys.exit(1)

    print()
    print(f"CREATE {table_type.upper()} + {fs_type.upper()} : {disk}")
    print("====================================")
    print()

    r = subprocess.run(["parted", "-s", disk, "mklabel", table_type], text=True)
    if r.returncode != 0:
        print("ERREUR : création de table échouée.")
        sys.exit(r.returncode)

    if swap_size:
        # Partition data de 1MiB jusqu'à -swap_size, puis swap jusqu'à 100%.
        r = subprocess.run([
            "parted", "-s", disk, "--", "mkpart", "primary", "1MiB", f"-{swap_size}"
        ], text=True)
        if r.returncode != 0:
            print("ERREUR : création partition principale échouée.")
            sys.exit(r.returncode)

        r = subprocess.run([
            "parted", "-s", disk, "--", "mkpart", "primary", "linux-swap", f"-{swap_size}", "100%"
        ], text=True)
        if r.returncode != 0:
            print("ERREUR : création partition swap échouée.")
            sys.exit(r.returncode)
    else:
        r = subprocess.run([
            "parted", "-s", disk, "--", "mkpart", "primary", "1MiB", "100%"
        ], text=True)
        if r.returncode != 0:
            print("ERREUR : création partition principale échouée.")
            sys.exit(r.returncode)

    reread_partitions(disk)

    data_part = partition_path(disk, 1)
    if not device_exists(data_part):
        print(f"ERREUR : partition créée introuvable : {data_part}")
        print("Relance -list pour voir le nom réel.")
        sys.exit(1)

    print(f"Formatage {fs_type} de {data_part}...")
    cmd = fs_command(fs_type, data_part)
    r = subprocess.run(cmd, text=True)
    if r.returncode != 0:
        print("ERREUR : formatage partition principale échoué.")
        sys.exit(r.returncode)

    if swap_size:
        swap_part = partition_path(disk, 2)
        if device_exists(swap_part):
            print(f"Initialisation swap de {swap_part}...")
            cmd = fs_command("swap", swap_part)
            r = subprocess.run(cmd, text=True)
            if r.returncode != 0:
                print("ERREUR : mkswap échoué.")
                sys.exit(r.returncode)
            print(f"OK : swap créée : {swap_part}")

    print()
    print(f"OK : {disk} préparé.")
    print(f"Partition principale : {data_part} ({fs_type})")
    if swap_size:
        print(f"Swap : {partition_path(disk, 2)} ({swap_size})")
    print("Relance : python3 format.py -list")
    print()


def format_partition(disk, part, fs_type):
    need_root()
    disk = protect_disk_or_exit(disk)
    part = dev_path(part)
    ensure_part_belongs_to_disk(disk, part)

    if part_is_mounted(part):
        print(f"ERREUR : partition déjà montée : {part}")
        print("Démonte-la avant formatage.")
        sys.exit(1)

    print()
    print(f"FORMATAGE {fs_type.upper()} : {part}")
    print("==============================")
    print()

    cmd = fs_command(fs_type, part)
    r = subprocess.run(cmd, text=True)
    if r.returncode != 0:
        print("ERREUR : formatage échoué.")
        sys.exit(r.returncode)

    print()
    print(f"OK : {part} formatée en {fs_type}")
    print("Relance : python3 format.py -list")
    print()


def delete_partition(disk, part):
    need_root()
    need_cmd("parted", "Paquet habituel : parted")

    disk = protect_disk_or_exit(disk)
    part = dev_path(part)
    ensure_part_belongs_to_disk(disk, part)

    if part_is_mounted(part):
        print(f"ERREUR : partition montée : {part}")
        print("Démonte-la avant suppression.")
        sys.exit(1)

    number = partition_number(disk, part)

    print()
    print(f"SUPPRESSION PARTITION : {part}")
    print("================================")
    print()

    r = subprocess.run(["parted", "-s", disk, "rm", number], text=True)
    if r.returncode != 0:
        print("ERREUR : suppression de partition échouée.")
        sys.exit(r.returncode)

    reread_partitions(disk)
    print(f"OK : partition supprimée : {part}")
    print("Relance : python3 format.py -list")
    print()


def wipe_disk(disk):
    need_root()
    need_cmd("wipefs", "Paquet habituel : util-linux")

    disk = protect_disk_or_exit(disk)
    if disk_has_mounted_partitions(disk):
        print(f"ERREUR : {disk} contient une partition montée.")
        print("Démonte les partitions avant wipe.")
        sys.exit(1)

    print()
    print(f"WIPE SIGNATURES : {disk}")
    print("==============================")
    print()

    r = subprocess.run(["wipefs", "-a", disk], text=True)
    reread_partitions(disk)
    if r.returncode != 0:
        print("ERREUR : wipefs a échoué.")
        sys.exit(r.returncode)

    print(f"OK : signatures effacées sur {disk}")
    print("Relance : python3 format.py -list")
    print()


def usage():
    print()
    print("Usage :")
    print("  python3 format.py -list")
    print("  sudo python3 format.py -table /sda=gpt")
    print("  sudo python3 format.py -table /sda=msdos")
    print("  sudo python3 format.py -table /sda=gpt swap=1G")
    print("  sudo python3 format.py -create /sda=xfs")
    print("  sudo python3 format.py -create /sda=ext4 swap=1G")
    print("  sudo python3 format.py -format /sda /sda1=xfs")
    print("  sudo python3 format.py -delete /sda /sda1")
    print("  sudo python3 format.py -wipe /sda")
    print()
    print("Formats : xfs, ext4, btrfs, ntfs, fat32, exfat, swap")
    print("Tables  : gpt, msdos")
    print()


def main():
    if len(sys.argv) == 1:
        show_list()
        return

    action = sys.argv[1].lower()

    if action in {"-h", "--help", "help"}:
        usage()
        return

    if action in {"-list", "--list", "-liste", "liste", "list", "-l"}:
        show_list()
        return

    if action == "-table":
        disk, table_type, swap_size = parse_table_args(sys.argv[2:])
        create_empty_table(disk, table_type, swap_size)
        return

    if action == "-create":
        disk, table_type, fs_type, swap_size = parse_create_args(sys.argv[2:])
        create_and_format(disk, table_type, fs_type, swap_size)
        return

    if action == "-format":
        disk, part, fs_type = parse_format_args(sys.argv[2:])
        format_partition(disk, part, fs_type)
        return

    if action in {"-delete", "-del", "-remove", "-rm"}:
        if len(sys.argv) != 4:
            usage()
            sys.exit(1)
        delete_partition(sys.argv[2], sys.argv[3])
        return

    if action == "-wipe":
        if len(sys.argv) != 3:
            usage()
            sys.exit(1)
        wipe_disk(sys.argv[2])
        return

    print(f"ERREUR : action inconnue : {action}")
    usage()
    sys.exit(1)


if __name__ == "__main__":
    main()
