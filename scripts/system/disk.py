#!/usr/bin/env python3

# ============================================================
# disk.py - Gestion simple des montages Linux / fstab
# ============================================================
#
# LEXIQUE DES COMMANDES
#
# Afficher la liste des disques / partitions :
#   python3 disk.py
#   python3 disk.py -liste
#   python3 disk.py -list
#   python3 disk.py -l
#
# Ajouter une partition dans /etc/fstab pour montage automatique :
#   sudo python3 disk.py -install /sda /sda1 /mnt/cache
#   sudo python3 disk.py -install /sdb /sdb1 /mnt/disque1
#
# Monter une partition tout de suite à chaud :
#   sudo python3 disk.py -mount /sda /sda1 /mnt/cache
#   sudo python3 disk.py -mount /sdb /sdb1 /mnt/disque1
#
# Supprimer le montage automatique depuis /etc/fstab avec disque + partition :
#   sudo python3 disk.py -remove /sda /sda1
#   sudo python3 disk.py -remove /sdb /sdb1
#
# Supprimer le montage automatique depuis /etc/fstab avec le point de montage :
#   sudo python3 disk.py -remove /mnt/cache
#   sudo python3 disk.py -remove /mnt/disque1
#
# Aide :
#   python3 disk.py -h
#   python3 disk.py --help
#
# NOTES IMPORTANTES
#
# - Le script monte les partitions, pas les disques bruts.
#   Exemple correct : /sda /sda1
#   Exemple refusé   : /sda /mnt/cache
#
# - Pour /etc/fstab, le script utilise l'UUID de la partition.
#   C'est plus fiable que /dev/sda1, car les noms sda/sdb peuvent changer.
#
# - Avant chaque modification de /etc/fstab, le script crée une sauvegarde :
#   /etc/fstab.backup_YYYYMMDD_HHMMSS
#
# - Les lignes ajoutées dans /etc/fstab utilisent :
#   defaults,nofail,x-systemd.device-timeout=5s
#
# - Donc si un disque est absent, Linux ne doit pas bloquer le démarrage.
#
# - ATTENTION :
#   -install ajoute dans fstab.
#   -mount monte maintenant.
#   -remove enlève de fstab, mais ne démonte pas le disque déjà monté.
#
# Pour démonter à chaud :
#   sudo umount /mnt/cache
#   sudo umount /mnt/disque1
#
# ============================================================

import os
import sys
import json
import shutil
import subprocess
from datetime import datetime

FSTAB = "/etc/fstab"


def run(cmd, check=True):
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=check
    )


def need_root():
    if os.geteuid() != 0:
        print("ERREUR : lance le script en root.")
        print("Exemple : sudo python3 disk.py -install /sda /sda1 /mnt/cache")
        sys.exit(1)


def need_cmd(name):
    if shutil.which(name) is None:
        print(f"ERREUR : commande manquante : {name}")
        print("Je n'installe rien automatiquement.")
        sys.exit(1)


def dev_path(value):
    value = value.strip()

    if value.startswith("/dev/"):
        return value

    if value.startswith("/"):
        value = value[1:]

    return "/dev/" + value


def short_dev(value):
    value = dev_path(value)
    return os.path.basename(value)


def device_exists(dev):
    return os.path.exists(dev)


def is_disk(dev):
    try:
        r = run(["lsblk", "-dn", "-o", "TYPE", dev])
        return r.stdout.strip() == "disk"
    except subprocess.CalledProcessError:
        return False


def is_partition(dev):
    try:
        r = run(["lsblk", "-dn", "-o", "TYPE", dev])
        return r.stdout.strip() == "part"
    except subprocess.CalledProcessError:
        return False


def partition_belongs_to_disk(disk, part):
    disk_name = short_dev(disk)
    part = dev_path(part)

    try:
        r = run(["lsblk", "-no", "PKNAME", part])
        parent = r.stdout.strip()
        return parent == disk_name
    except subprocess.CalledProcessError:
        return False


def get_uuid(dev):
    try:
        r = run(["blkid", "-s", "UUID", "-o", "value", dev])
        return r.stdout.strip() or None
    except subprocess.CalledProcessError:
        return None


def get_fstype(dev):
    try:
        r = run(["blkid", "-s", "TYPE", "-o", "value", dev])
        return r.stdout.strip() or None
    except subprocess.CalledProcessError:
        return None


def read_lsblk():
    need_cmd("lsblk")
    r = run([
        "lsblk",
        "-J",
        "-o",
        "NAME,PATH,TYPE,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,MODEL"
    ])
    return json.loads(r.stdout)


def mountpoints_of(dev):
    try:
        r = run(["lsblk", "-nr", "-o", "MOUNTPOINTS", dev], check=False)
        return [x.strip() for x in r.stdout.splitlines() if x.strip()]
    except Exception:
        return []


def ensure_mountpoint(mountpoint):
    mountpoint = os.path.abspath(mountpoint)

    forbidden = {
        "/",
        "/boot",
        "/boot/efi",
        "/home",
        "/mnt",
        "/var",
        "/usr",
        "/etc",
        "/root",
        "/tmp",
        "/opt",
    }

    if mountpoint in forbidden:
        print(f"ERREUR : point de montage interdit : {mountpoint}")
        print("Choisis un dossier dédié, exemple : /mnt/cache ou /mnt/disque1")
        sys.exit(1)

    if not mountpoint.startswith("/mnt/"):
        print(f"ERREUR : point de montage refusé : {mountpoint}")
        print("Par sécurité, ce script accepte seulement les montages dans /mnt/")
        sys.exit(1)

    os.makedirs(mountpoint, exist_ok=True)
    return mountpoint


def backup_fstab():
    if not os.path.exists(FSTAB):
        print("ERREUR : /etc/fstab introuvable.")
        sys.exit(1)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{FSTAB}.backup_{stamp}"
    shutil.copy2(FSTAB, backup)
    return backup


def fstab_entries():
    entries = []

    if not os.path.exists(FSTAB):
        return entries

    with open(FSTAB, "r", encoding="utf-8") as f:
        for index, line in enumerate(f.readlines()):
            clean = line.strip()

            if not clean or clean.startswith("#"):
                continue

            parts = clean.split()

            if len(parts) >= 2:
                entries.append({
                    "index": index,
                    "line": line,
                    "source": parts[0],
                    "mountpoint": parts[1],
                    "parts": parts,
                })

    return entries


def fstab_contains_uuid(uuid):
    target = f"UUID={uuid}"
    return any(e["source"] == target for e in fstab_entries())


def fstab_contains_mountpoint(mountpoint):
    return any(e["mountpoint"] == mountpoint for e in fstab_entries())


def validate_disk_part(disk, part):
    disk = dev_path(disk)
    part = dev_path(part)

    if not device_exists(disk):
        print(f"ERREUR : disque introuvable : {disk}")
        sys.exit(1)

    if not device_exists(part):
        print(f"ERREUR : partition introuvable : {part}")
        sys.exit(1)

    if not is_disk(disk):
        print(f"ERREUR : ce n'est pas un disque brut : {disk}")
        sys.exit(1)

    if not is_partition(part):
        print(f"ERREUR : ce n'est pas une partition : {part}")
        print("Exemple valide : /sda /sda1 /mnt/cache")
        sys.exit(1)

    if not partition_belongs_to_disk(disk, part):
        print(f"ERREUR : {part} n'appartient pas à {disk}")
        print("Commande refusée pour éviter une erreur de disque.")
        sys.exit(1)

    return disk, part


def show_list():
    data = read_lsblk()

    print()
    print("LISTE DES DISQUES")
    print("=================")
    print()

    for disk in data.get("blockdevices", []):
        if disk.get("type") != "disk":
            continue

        disk_path = disk.get("path") or f"/dev/{disk.get('name', '')}"
        disk_name = disk.get("name", "")
        disk_size = disk.get("size") or ""
        disk_model = disk.get("model") or ""

        print(f"{disk_path} | disque | {disk_size} | {disk_model}")

        children = disk.get("children") or []

        if not children:
            print("  └─ aucune partition")
            print("     état : NON MONTABLE directement par sécurité")
            print("     raison : il faut créer/formater une partition avant montage")
            print()
            continue

        for part in children:
            part_path = part.get("path") or f"/dev/{part.get('name', '')}"
            part_name = part.get("name", "")
            part_size = part.get("size") or ""
            fstype = part.get("fstype") or ""
            label = part.get("label") or ""
            uuid = part.get("uuid") or ""
            mountpoints = part.get("mountpoints") or []

            if isinstance(mountpoints, str):
                mountpoints = [mountpoints]

            mounted = [m for m in mountpoints if m]

            print(f"  └─ {part_path} | partition | {part_size}")

            if label:
                print(f"     label : {label}")

            if fstype:
                print(f"     type  : {fstype}")
            else:
                print("     type  : aucun")

            if uuid:
                print(f"     uuid  : {uuid}")
            else:
                print("     uuid  : aucun")

            if mounted:
                print(f"     état  : DÉJÀ MONTÉE sur {', '.join(mounted)}")
                print(f"     remove fstab : sudo python3 disk.py -remove /{disk_name} /{part_name}")
            elif fstype and uuid:
                print("     état  : MONTABLE")
                print(f"     install : sudo python3 disk.py -install /{disk_name} /{part_name} /mnt/cache")
                print(f"     mount   : sudo python3 disk.py -mount   /{disk_name} /{part_name} /mnt/cache")
                print(f"     remove  : sudo python3 disk.py -remove  /{disk_name} /{part_name}")
            elif not fstype:
                print("     état  : NON MONTABLE")
                print("     raison : partition non formatée")
            elif not uuid:
                print("     état  : NON MONTABLE")
                print("     raison : pas d'UUID détecté")

        print()

    print("Rappel :")
    print("  -install = ajoute dans /etc/fstab pour le prochain démarrage")
    print("  -mount   = monte tout de suite à chaud")
    print("  -remove  = enlève le montage automatique de /etc/fstab")
    print()


def install_fstab(disk, part, mountpoint):
    need_root()
    need_cmd("blkid")

    disk, part = validate_disk_part(disk, part)
    mountpoint = ensure_mountpoint(mountpoint)

    uuid = get_uuid(part)

    if not uuid:
        print(f"ERREUR : impossible de récupérer l'UUID de {part}")
        print("La partition est peut-être non formatée.")
        sys.exit(1)

    fstype = get_fstype(part)

    if not fstype:
        print(f"ERREUR : impossible de détecter le type de système de fichiers de {part}")
        print("La partition est peut-être non formatée.")
        sys.exit(1)

    if fstab_contains_uuid(uuid):
        print(f"Déjà présent dans /etc/fstab : UUID={uuid}")
        sys.exit(0)

    if fstab_contains_mountpoint(mountpoint):
        print(f"ERREUR : le point de montage existe déjà dans /etc/fstab : {mountpoint}")
        print("Je ne modifie rien pour éviter de casser le démarrage.")
        sys.exit(1)

    backup = backup_fstab()

    line = f"UUID={uuid}  {mountpoint}  {fstype}  defaults,nofail,x-systemd.device-timeout=5s  0  0\n"

    with open(FSTAB, "a", encoding="utf-8") as f:
        f.write("\n# Ajouté par disk.py\n")
        f.write(line)

    subprocess.run(["systemctl", "daemon-reload"], check=False)

    print()
    print("OK : entrée ajoutée dans /etc/fstab")
    print(f"Sauvegarde créée : {backup}")
    print(f"Disque    : {disk}")
    print(f"Partition : {part}")
    print(f"UUID      : {uuid}")
    print(f"Type FS   : {fstype}")
    print(f"Montage   : {mountpoint}")
    print()
    print("Le disque sera bien monté au prochain démarrage.")
    print("Ligne sécurisée avec : nofail,x-systemd.device-timeout=5s")
    print()


def mount_now(disk, part, mountpoint):
    need_root()
    need_cmd("blkid")
    need_cmd("mount")

    disk, part = validate_disk_part(disk, part)
    mountpoint = ensure_mountpoint(mountpoint)

    uuid = get_uuid(part)

    if not uuid:
        print(f"ERREUR : impossible de récupérer l'UUID de {part}")
        print("La partition est peut-être non formatée.")
        sys.exit(1)

    fstype = get_fstype(part)

    if not fstype:
        print(f"ERREUR : impossible de détecter le type de système de fichiers de {part}")
        print("La partition est peut-être non formatée.")
        sys.exit(1)

    current = mountpoints_of(part)

    if current:
        print(f"INFO : {part} est déjà montée ici : {', '.join(current)}")
        sys.exit(0)

    result = subprocess.run(["mount", part, mountpoint], text=True)

    if result.returncode != 0:
        print("ERREUR : montage impossible.")
        print("Tu peux vérifier avec :")
        print("  dmesg | tail -50")
        sys.exit(result.returncode)

    print()
    print("OK : disque monté à chaud.")
    print(f"Disque    : {disk}")
    print(f"Partition : {part}")
    print(f"UUID      : {uuid}")
    print(f"Type FS   : {fstype}")
    print(f"Montage   : {mountpoint}")
    print()


def remove_fstab(args):
    need_root()

    uuid = None
    mountpoint = None

    # Forme 1 :
    # disk.py -remove /mnt/cache
    if len(args) == 1:
        mountpoint = os.path.abspath(args[0])

    # Forme 2 :
    # disk.py -remove /sda /sda1
    elif len(args) == 2:
        disk, part = validate_disk_part(args[0], args[1])
        uuid = get_uuid(part)

        if not uuid:
            print(f"ERREUR : impossible de récupérer l'UUID de {part}")
            print("Si la partition a été supprimée ou reformatée, utilise plutôt :")
            print("  sudo python3 disk.py -remove /mnt/cache")
            sys.exit(1)

    else:
        usage()
        sys.exit(1)

    if not os.path.exists(FSTAB):
        print("ERREUR : /etc/fstab introuvable.")
        sys.exit(1)

    with open(FSTAB, "r", encoding="utf-8") as f:
        lines = f.readlines()

    remove_indexes = set()

    for i, line in enumerate(lines):
        clean = line.strip()

        if not clean or clean.startswith("#"):
            continue

        parts = clean.split()

        match_uuid = False
        match_mountpoint = False

        if uuid and len(parts) >= 1 and parts[0] == f"UUID={uuid}":
            match_uuid = True

        if mountpoint and len(parts) >= 2 and parts[1] == mountpoint:
            match_mountpoint = True

        if match_uuid or match_mountpoint:
            remove_indexes.add(i)

            if i > 0 and lines[i - 1].strip().startswith("# Ajouté par disk.py"):
                remove_indexes.add(i - 1)

    if not remove_indexes:
        if uuid:
            print(f"Aucune ligne trouvée dans /etc/fstab pour UUID={uuid}")
        elif mountpoint:
            print(f"Aucune ligne trouvée dans /etc/fstab pour {mountpoint}")
        else:
            print("Aucune ligne trouvée dans /etc/fstab.")
        sys.exit(0)

    backup = backup_fstab()

    with open(FSTAB, "w", encoding="utf-8") as f:
        for i, line in enumerate(lines):
            if i not in remove_indexes:
                f.write(line)

    subprocess.run(["systemctl", "daemon-reload"], check=False)

    print()
    print("OK : montage automatique supprimé de /etc/fstab")
    print(f"Sauvegarde créée : {backup}")

    if uuid:
        print(f"UUID supprimé : {uuid}")

    if mountpoint:
        print(f"Point de montage : {mountpoint}")

    print()
    print("Le disque ne sera plus monté automatiquement au prochain démarrage.")
    print("Note : s’il est déjà monté maintenant, il reste monté jusqu’au démontage manuel.")
    print("Pour démonter maintenant : sudo umount /mnt/TON_POINT_DE_MONTAGE")
    print()


def usage():
    print()
    print("Usage :")
    print("  python3 disk.py")
    print("  python3 disk.py -liste")
    print("  sudo python3 disk.py -install /sda /sda1 /mnt/cache")
    print("  sudo python3 disk.py -mount   /sda /sda1 /mnt/cache")
    print("  sudo python3 disk.py -remove  /sda /sda1")
    print("  sudo python3 disk.py -remove  /mnt/cache")
    print()
    print("Commandes :")
    print("  -liste    affiche les disques, partitions, UUID et état montable/non montable")
    print("  -install  ajoute la partition dans /etc/fstab avec UUID")
    print("  -mount    monte la partition tout de suite, sans modifier fstab")
    print("  -remove   supprime l'entrée de montage automatique dans /etc/fstab")
    print()


def main():
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

    if action == "-install":
        if len(sys.argv) != 5:
            usage()
            sys.exit(1)

        install_fstab(sys.argv[2], sys.argv[3], sys.argv[4])
        sys.exit(0)

    if action == "-mount":
        if len(sys.argv) != 5:
            usage()
            sys.exit(1)

        mount_now(sys.argv[2], sys.argv[3], sys.argv[4])
        sys.exit(0)

    if action in ["-remove", "-rm", "-delete", "-del"]:
        remove_fstab(sys.argv[2:])
        sys.exit(0)

    print(f"ERREUR : action inconnue : {action}")
    usage()
    sys.exit(1)


if __name__ == "__main__":
    main()
