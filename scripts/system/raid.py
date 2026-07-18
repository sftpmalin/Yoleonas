#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ============================================================
# raid.py - Montage simple d'un RAID / multi-device BTRFS existant
# ============================================================
#
# But :
#   Gérer un ancien RAID1 BTRFS déjà créé, sans formater, sans créer de RAID.
#
# Ce script NE FAIT PAS :
#   - pas de mkfs
#   - pas de wipefs
#   - pas de création RAID
#   - pas de suppression de disque
#
# Il fait seulement :
#   - scan BTRFS
#   - liste des fichiers BTRFS détectés
#   - mount à chaud
#   - ajout fstab par UUID
#   - remove fstab
#   - umount
#
# Commandes :
#   python3 raid.py -list
#   sudo python3 raid.py -scan
#   sudo python3 raid.py -mount /dev/sdc /mnt/raid1
#   sudo python3 raid.py -install /dev/sdc /mnt/raid1
#   sudo python3 raid.py -umount /mnt/raid1
#   sudo python3 raid.py -remove /mnt/raid1
#
# Auto-détection :
#   sudo python3 raid.py -mount-auto /mnt/raid1
#   sudo python3 raid.py -install-auto /mnt/raid1
#
# Notes :
#   Pour un BTRFS RAID1 multi-device, monter UN des devices suffit généralement,
#   après btrfs device scan. Exemple :
#     btrfs device scan
#     mount -t btrfs /dev/sdc /mnt/raid1
#
# ============================================================

from __future__ import annotations

import os
import sys
import json
import shutil
import subprocess
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

FSTAB = "/etc/fstab"
DEFAULT_OPTIONS = "defaults,noatime,compress=zstd:3,nofail,x-systemd.device-timeout=10s"


def run(cmd: Sequence[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def need_root() -> None:
    if os.geteuid() != 0:
        print("ERREUR : lance le script en root.")
        print("Exemple : sudo python3 raid.py -mount /dev/sdc /mnt/raid1")
        sys.exit(1)


def need_cmd(name: str) -> None:
    if shutil.which(name) is None:
        print(f"ERREUR : commande manquante : {name}")
        print("Installe si besoin : apt install -y btrfs-progs")
        sys.exit(1)


def dev_path(value: str) -> str:
    value = str(value or "").strip()
    if value.startswith("/dev/"):
        return value
    if value.startswith("/"):
        value = value[1:]
    return "/dev/" + value


def normalize_mountpoint(path: str) -> str:
    path = os.path.abspath(str(path or "").strip())

    forbidden = {"/", "/mnt", "/boot", "/boot/efi", "/home", "/var", "/usr", "/etc", "/root", "/tmp", "/opt"}

    if path in forbidden:
        print(f"ERREUR : point de montage interdit : {path}")
        print("Choisis un dossier dédié, exemple : /mnt/raid1")
        sys.exit(1)

    if not path.startswith("/mnt/"):
        print(f"ERREUR : point de montage refusé : {path}")
        print("Par sécurité, ce script accepte seulement les montages dans /mnt/")
        sys.exit(1)

    os.makedirs(path, exist_ok=True)
    return path


def btrfs_scan() -> None:
    need_cmd("btrfs")
    print("Scan BTRFS : btrfs device scan")
    r = run(["btrfs", "device", "scan"], check=False)
    if r.stdout.strip():
        print(r.stdout.strip())
    if r.stderr.strip():
        print(r.stderr.strip())
    if r.returncode != 0:
        print("ATTENTION : btrfs device scan a retourné une erreur, mais on continue.")


def lsblk_json() -> dict:
    need_cmd("lsblk")
    r = run(["lsblk", "-J", "-o", "NAME,PATH,TYPE,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,MODEL,SERIAL"])
    return json.loads(r.stdout)


def flatten_blockdevices(devices: List[dict]) -> List[dict]:
    out: List[dict] = []
    for dev in devices:
        out.append(dev)
        children = dev.get("children") or []
        if children:
            out.extend(flatten_blockdevices(children))
    return out


def btrfs_devices_from_lsblk() -> List[dict]:
    data = lsblk_json()
    flat = flatten_blockdevices(data.get("blockdevices", []))
    return [d for d in flat if str(d.get("fstype") or "").lower() == "btrfs"]


def group_btrfs_by_uuid() -> Dict[str, List[dict]]:
    groups: Dict[str, List[dict]] = {}
    for dev in btrfs_devices_from_lsblk():
        uuid = dev.get("uuid") or ""
        if not uuid:
            continue
        groups.setdefault(uuid, []).append(dev)
    return groups


def show_list() -> None:
    print()
    print("LSBLK BTRFS")
    print("===========")
    print()

    groups = group_btrfs_by_uuid()

    if not groups:
        print("Aucun périphérique BTRFS détecté par lsblk.")
    else:
        for uuid, devs in groups.items():
            print(f"UUID BTRFS : {uuid}")
            for dev in devs:
                path = dev.get("path") or f"/dev/{dev.get('name')}"
                size = dev.get("size") or ""
                typ = dev.get("type") or ""
                label = dev.get("label") or ""
                mounts = dev.get("mountpoints") or []
                if isinstance(mounts, str):
                    mounts = [mounts]
                mounts = [m for m in mounts if m]
                print(f"  - {path} | {typ} | {size} | label={label or '-'} | mount={', '.join(mounts) if mounts else '-'}")
            print()

    print("BTRFS FILESYSTEM SHOW")
    print("=====================")
    need_cmd("btrfs")
    r = run(["btrfs", "filesystem", "show"], check=False)
    if r.stdout.strip():
        print(r.stdout.rstrip())
    if r.stderr.strip():
        print(r.stderr.rstrip())
    if not r.stdout.strip() and not r.stderr.strip():
        print("Aucune sortie btrfs filesystem show.")

    print()
    print("Commandes utiles :")
    print("  sudo python3 raid.py -mount /dev/sdc /mnt/raid1")
    print("  sudo python3 raid.py -install /dev/sdc /mnt/raid1")
    print("  sudo python3 raid.py -mount-auto /mnt/raid1")
    print()


def get_blkid_value(dev: str, key: str) -> Optional[str]:
    try:
        r = run(["blkid", "-s", key, "-o", "value", dev], check=False)
        value = r.stdout.strip()
        return value or None
    except Exception:
        return None


def validate_btrfs_device(dev: str) -> Tuple[str, str]:
    dev = dev_path(dev)

    if not os.path.exists(dev):
        print(f"ERREUR : périphérique introuvable : {dev}")
        sys.exit(1)

    fstype = get_blkid_value(dev, "TYPE")
    uuid = get_blkid_value(dev, "UUID")

    if fstype != "btrfs":
        print(f"ERREUR : {dev} n'est pas détecté comme BTRFS.")
        print(f"Type détecté : {fstype or 'aucun'}")
        print("Vérifie avec : blkid /dev/sdc /dev/sde")
        sys.exit(1)

    if not uuid:
        print(f"ERREUR : aucun UUID BTRFS détecté sur {dev}")
        sys.exit(1)

    return dev, uuid


def find_best_btrfs_device() -> Tuple[str, str, List[dict]]:
    groups = group_btrfs_by_uuid()

    if not groups:
        print("ERREUR : aucun BTRFS détecté.")
        print("Teste : blkid")
        sys.exit(1)

    # Préfère un groupe multi-device, typique RAID1 BTRFS.
    best_uuid = ""
    best_devs: List[dict] = []

    for uuid, devs in groups.items():
        if len(devs) > len(best_devs):
            best_uuid = uuid
            best_devs = devs

    if not best_devs:
        print("ERREUR : aucun device BTRFS utilisable.")
        sys.exit(1)

    first = best_devs[0].get("path") or f"/dev/{best_devs[0].get('name')}"
    return first, best_uuid, best_devs


def is_mountpoint(path: str) -> bool:
    r = run(["findmnt", "-rn", "--mountpoint", path, "-o", "TARGET"], check=False)
    return r.returncode == 0 and bool(r.stdout.strip())


def mount_btrfs(dev: str, mountpoint: str) -> None:
    need_root()
    need_cmd("btrfs")
    need_cmd("mount")
    need_cmd("findmnt")

    dev, uuid = validate_btrfs_device(dev)
    mountpoint = normalize_mountpoint(mountpoint)

    btrfs_scan()

    if is_mountpoint(mountpoint):
        print(f"INFO : déjà monté : {mountpoint}")
        return

    print()
    print("MONTAGE BTRFS")
    print("=============")
    print(f"Device     : {dev}")
    print(f"UUID       : {uuid}")
    print(f"Montage    : {mountpoint}")
    print(f"Options    : {DEFAULT_OPTIONS}")
    print()

    r = subprocess.run(["mount", "-t", "btrfs", "-o", DEFAULT_OPTIONS, dev, mountpoint], text=True)

    if r.returncode != 0:
        print("ERREUR : montage BTRFS impossible.")
        print("Essaye de voir le détail avec :")
        print("  dmesg | tail -80")
        print("  btrfs filesystem show")
        sys.exit(r.returncode)

    print("OK : BTRFS monté.")
    print(f"Teste : ls -la {mountpoint}")


def mount_auto(mountpoint: str) -> None:
    dev, uuid, devs = find_best_btrfs_device()
    print("Auto-détection BTRFS :")
    print(f"  UUID choisi : {uuid}")
    print("  Devices :")
    for d in devs:
        print(f"    - {d.get('path') or d.get('name')} {d.get('size') or ''}")
    print()
    mount_btrfs(dev, mountpoint)


def backup_fstab() -> str:
    if not os.path.exists(FSTAB):
        print("ERREUR : /etc/fstab introuvable.")
        sys.exit(1)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{FSTAB}.backup_raid_{stamp}"
    shutil.copy2(FSTAB, backup)
    return backup


def read_fstab_lines() -> List[str]:
    if not os.path.exists(FSTAB):
        return []
    with open(FSTAB, "r", encoding="utf-8") as f:
        return f.readlines()


def fstab_has_mountpoint(mountpoint: str) -> bool:
    for line in read_fstab_lines():
        clean = line.strip()
        if not clean or clean.startswith("#"):
            continue
        parts = clean.split()
        if len(parts) >= 2 and parts[1] == mountpoint:
            return True
    return False


def fstab_has_uuid(uuid: str) -> bool:
    target = f"UUID={uuid}"
    for line in read_fstab_lines():
        clean = line.strip()
        if not clean or clean.startswith("#"):
            continue
        parts = clean.split()
        if parts and parts[0] == target:
            return True
    return False


def install_fstab(dev: str, mountpoint: str) -> None:
    need_root()
    need_cmd("btrfs")
    need_cmd("blkid")

    dev, uuid = validate_btrfs_device(dev)
    mountpoint = normalize_mountpoint(mountpoint)

    if fstab_has_mountpoint(mountpoint):
        print(f"ERREUR : {mountpoint} existe déjà dans /etc/fstab.")
        print("Supprime d'abord avec : sudo python3 raid.py -remove " + mountpoint)
        sys.exit(1)

    if fstab_has_uuid(uuid):
        print(f"INFO : UUID={uuid} existe déjà dans /etc/fstab.")
        print("Je ne rajoute pas de doublon.")
        return

    backup = backup_fstab()

    line = f"UUID={uuid}  {mountpoint}  btrfs  {DEFAULT_OPTIONS}  0  0\n"

    with open(FSTAB, "a", encoding="utf-8") as f:
        f.write("\n# Ajouté par raid.py - BTRFS multi-device / RAID existant\n")
        f.write(line)

    subprocess.run(["systemctl", "daemon-reload"], check=False)

    print()
    print("OK : entrée BTRFS ajoutée dans /etc/fstab")
    print(f"Sauvegarde : {backup}")
    print(f"Device     : {dev}")
    print(f"UUID       : {uuid}")
    print(f"Montage    : {mountpoint}")
    print()
    print("Pour monter maintenant sans reboot :")
    print(f"  sudo mount {mountpoint}")
    print("ou :")
    print(f"  sudo python3 raid.py -mount {dev} {mountpoint}")


def install_auto(mountpoint: str) -> None:
    dev, uuid, devs = find_best_btrfs_device()
    print("Auto-détection BTRFS pour fstab :")
    print(f"  UUID choisi : {uuid}")
    print("  Devices :")
    for d in devs:
        print(f"    - {d.get('path') or d.get('name')} {d.get('size') or ''}")
    print()
    install_fstab(dev, mountpoint)


def remove_fstab(mountpoint: str) -> None:
    need_root()
    mountpoint = normalize_mountpoint(mountpoint)

    lines = read_fstab_lines()
    remove = set()
    found = False

    for i, line in enumerate(lines):
        clean = line.strip()
        if not clean or clean.startswith("#"):
            continue

        parts = clean.split()
        if len(parts) >= 3 and parts[1] == mountpoint and parts[2] == "btrfs":
            found = True
            remove.add(i)
            if i > 0 and lines[i - 1].strip().startswith("# Ajouté par raid.py"):
                remove.add(i - 1)

    if not found:
        print(f"Aucune ligne BTRFS trouvée dans /etc/fstab pour {mountpoint}")
        return

    backup = backup_fstab()

    with open(FSTAB, "w", encoding="utf-8") as f:
        for i, line in enumerate(lines):
            if i not in remove:
                f.write(line)

    subprocess.run(["systemctl", "daemon-reload"], check=False)

    print()
    print("OK : ligne BTRFS supprimée de /etc/fstab")
    print(f"Sauvegarde : {backup}")
    print(f"Montage    : {mountpoint}")


def umount_btrfs(mountpoint: str) -> None:
    need_root()
    mountpoint = normalize_mountpoint(mountpoint)

    if not is_mountpoint(mountpoint):
        print(f"INFO : {mountpoint} n'est pas monté.")
        return

    r = subprocess.run(["umount", mountpoint], text=True)
    if r.returncode != 0:
        print("ERREUR : umount impossible.")
        print("Si le dossier est occupé : cd / puis recommence.")
        print("Diagnostic :")
        print(f"  fuser -vm {mountpoint}")
        sys.exit(r.returncode)

    print(f"OK : démonté : {mountpoint}")


def usage() -> None:
    print()
    print("Usage :")
    print("  python3 raid.py -list")
    print("  sudo python3 raid.py -scan")
    print("  sudo python3 raid.py -mount /dev/sdc /mnt/raid1")
    print("  sudo python3 raid.py -mount-auto /mnt/raid1")
    print("  sudo python3 raid.py -install /dev/sdc /mnt/raid1")
    print("  sudo python3 raid.py -install-auto /mnt/raid1")
    print("  sudo python3 raid.py -umount /mnt/raid1")
    print("  sudo python3 raid.py -remove /mnt/raid1")
    print()


def main() -> None:
    if len(sys.argv) == 1:
        show_list()
        sys.exit(0)

    action = sys.argv[1].lower()

    if action in ("-h", "--help", "help"):
        usage()
        sys.exit(0)

    if action in ("-list", "-liste", "-l", "list"):
        show_list()
        sys.exit(0)

    if action in ("-scan", "scan"):
        need_root()
        btrfs_scan()
        sys.exit(0)

    if action in ("-mount", "mount"):
        if len(sys.argv) != 4:
            usage()
            sys.exit(1)
        mount_btrfs(sys.argv[2], sys.argv[3])
        sys.exit(0)

    if action in ("-mount-auto", "mount-auto"):
        if len(sys.argv) != 3:
            usage()
            sys.exit(1)
        mount_auto(sys.argv[2])
        sys.exit(0)

    if action in ("-install", "install"):
        if len(sys.argv) != 4:
            usage()
            sys.exit(1)
        install_fstab(sys.argv[2], sys.argv[3])
        sys.exit(0)

    if action in ("-install-auto", "install-auto"):
        if len(sys.argv) != 3:
            usage()
            sys.exit(1)
        install_auto(sys.argv[2])
        sys.exit(0)

    if action in ("-umount", "-unmount", "umount", "unmount"):
        if len(sys.argv) != 3:
            usage()
            sys.exit(1)
        umount_btrfs(sys.argv[2])
        sys.exit(0)

    if action in ("-remove", "-rm", "-delete", "remove"):
        if len(sys.argv) != 3:
            usage()
            sys.exit(1)
        remove_fstab(sys.argv[2])
        sys.exit(0)

    print(f"ERREUR : action inconnue : {action}")
    usage()
    sys.exit(1)


if __name__ == "__main__":
    main()
