def json_response(ok: bool, message: str, **extra: Any):
    payload = {"ok": ok, "message": message}
    payload.update(extra)
    return jsonify(payload), (200 if ok else 400)


def allowed_mount_prefixes(conf: Dict[str, str]) -> List[str]:
    raw = str(conf.get("MOUNT_ALLOWED_PREFIXES") or "/mnt,/media,/srv,/data")
    out: List[str] = []
    for item in raw.split(","):
        item = item.strip().rstrip("/")
        if item and item.startswith("/") and item not in {"/", "/boot", "/etc", "/usr", "/var", "/dev", "/proc", "/sys", "/run"}:
            out.append(item)
    return out or ["/mnt"]


def safe_real_mount_path(conf: Dict[str, str], path: str) -> Tuple[bool, str, str]:
    path = os.path.normpath(str(path or "").strip())
    if not path.startswith("/"):
        return False, path, "Le chemin de montage doit être absolu."
    forbidden = {"/", "/boot", "/etc", "/usr", "/var", "/dev", "/proc", "/sys", "/run", "/root"}
    if path in forbidden:
        return False, path, "Chemin de montage système refusé."
    prefixes = allowed_mount_prefixes(conf)
    if not any(path == p or path.startswith(p + "/") for p in prefixes):
        return False, path, "Chemin refusé. Racines autorisées : " + ", ".join(prefixes)
    return True, path, ""


def block_name_from_path(target: str) -> str:
    target = str(target or "").strip()
    if not SAFE_BLOCK_PATH_RE.match(target):
        return ""
    real = os.path.realpath(target)
    name = os.path.basename(real)
    if name and os.path.exists(os.path.join("/sys/class/block", name, "dev")):
        return name
    return ""


def parent_disk_name(block_name: str) -> str:
    if not block_name:
        return ""
    # Méthode sysfs fiable : /sys/class/block/sda1 -> .../block/sda/sda1
    try:
        real = os.path.realpath(os.path.join("/sys/class/block", block_name))
        parent = os.path.basename(os.path.dirname(real))
        if parent and parent != "block" and os.path.exists(os.path.join("/sys/class/block", parent, "dev")):
            # Si block_name est déjà un disque, le parent n'est pas un disque bloc valide différent.
            if parent != block_name and partition_name_matches(parent, block_name):
                return parent
    except Exception:
        pass

    if re.fullmatch(r"nvme\d+n\d+p\d+", block_name):
        return re.sub(r"p\d+$", "", block_name)
    if re.fullmatch(r"mmcblk\d+p\d+", block_name):
        return re.sub(r"p\d+$", "", block_name)
    if re.fullmatch(r"(sd|hd|vd|xvd)[a-z]+\d+", block_name):
        return re.sub(r"\d+$", "", block_name)
    return block_name


def target_mount_items(conf: Dict[str, str], target: str) -> List[Dict[str, str]]:
    name = block_name_from_path(target)
    if not name:
        return []
    parent = parent_disk_name(name)
    names = [name]
    if parent and parent != name:
        names.insert(0, parent)
    mountinfo = read_mountinfo(conf)
    btrfs_by_name = btrfs_mounts_by_devname(mountinfo)
    mounts_by_name = disk_mounts_for_names(mountinfo, names, btrfs_by_name)
    items: List[Dict[str, str]] = []
    for n in names:
        items.extend(mounts_by_name.get(n, []))
    return dedupe_mount_items(items)


def mount_paths_for_target(conf: Dict[str, str], target: str) -> List[str]:
    paths: List[str] = []
    for item in target_mount_items(conf, target):
        mount = item.get("mount") or ""
        if mount and mount not in paths:
            paths.append(mount)
    # D'abord les montages profonds/bind, puis le montage principal.
    return sorted(paths, key=lambda p: len(p.rstrip("/").split("/")), reverse=True)


def system_disk_names(conf: Dict[str, str]) -> set[str]:
    mountinfo = read_mountinfo(conf)
    btrfs_by_name = btrfs_mounts_by_devname(mountinfo)
    out: set[str] = set()
    for disk_name in list_disk_names(conf):
        parts = list_partitions(str(conf.get("SYS_BLOCK_PATH") or "/sys/block"), disk_name)
        names = [disk_name] + parts
        mounts_by_name = disk_mounts_for_names(mountinfo, names, btrfs_by_name)
        if is_system_or_boot_disk(disk_name, parts, mounts_by_name):
            out.add(disk_name)
    return out


def target_is_protected(conf: Dict[str, str], target: str) -> bool:
    name = block_name_from_path(target)
    if not name:
        return True
    parent = parent_disk_name(name)
    if parent in system_disk_names(conf):
        return True
    for mount in mount_paths_for_target(conf, target):
        if mount == "/" or mount.startswith("/boot"):
            return True
    return False


def require_safe_target(conf: Dict[str, str], target: str, allow_disk: bool = True, allow_part: bool = True) -> Tuple[bool, str, str]:
    target = str(target or "").strip()
    if not target.startswith("/dev/"):
        return False, target, "Périphérique invalide."
    if SAFE_DISK_RE.match(target):
        if not allow_disk:
            return False, target, "Action autorisée uniquement sur une partition."
    elif SAFE_PART_RE.match(target):
        if not allow_part:
            return False, target, "Action autorisée uniquement sur un disque."
    else:
        return False, target, "Nom de périphérique refusé."
    name = block_name_from_path(target)
    if not name:
        return False, target, "Périphérique introuvable dans /sys/class/block."
    if target_is_protected(conf, target):
        return False, target, "Disque système/boot protégé : action refusée."
    return True, target, ""


def bool_from_payload(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "oui"}


def parse_blkid_export(target: str) -> Dict[str, str]:
    """Sonde réellement un périphérique bloc, sans utiliser le cache blkid.

    Important pour Maintenance/RAID : /dev/md127 peut être recréé avec un
    nouveau volume logique, alors que le cache blkid/udev garde encore une
    ancienne étiquette XFS/ext4. L'option -p force une lecture de probe et
    évite d'afficher un vieux TYPE fantôme dans l'interface.
    """
    if not SAFE_BLOCK_PATH_RE.match(target or ""):
        return {}
    blkid_bin = shutil.which("blkid") or "blkid"
    rc, out = run_cmd([blkid_bin, "-p", "-o", "export", target], timeout=8)
    if rc != 0:
        # rc=2 signifie simplement : aucune signature détectée. On ne retombe
        # volontairement pas sur `blkid -o export`, car cette commande peut
        # ressortir une ancienne entrée du cache blkid pour le même /dev/mdX.
        return {}
    data: Dict[str, str] = {}
    for raw in (out or "").splitlines():
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key and value:
            data[key] = value
    return data


def forget_persistent_block_meta(conf: Dict[str, str], target: str) -> None:
    """Supprime les anciennes métadonnées persistantes d'un bloc.

    Utile quand un /dev/mdX vient d'être recréé : l'ancien cache JSON peut
    encore contenir TYPE=xfs pour le même nom de périphérique alors que le
    volume logique actuel n'est pas formaté.
    """
    if not SAFE_BLOCK_PATH_RE.match(target or ""):
        return
    try:
        data = read_disk_log(conf)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    meta_map = data.get("block_meta")
    if not isinstance(meta_map, dict):
        return
    changed = False
    for key in block_meta_cache_keys(target):
        if key in meta_map:
            meta_map.pop(key, None)
            changed = True
    if changed:
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            write_disk_log(conf, data)
        except Exception:
            pass


def clear_disk_runtime_caches() -> None:
    _TEMP_CACHE.clear()
    _HEALTH_CACHE.clear()
    _USAGE_CACHE.clear()


def refresh_udev_best_effort(target: str = "") -> None:
    # Après un formatage, les liens /dev/disk/by-* peuvent mettre une seconde à revenir.
    if shutil.which("udevadm"):
        run_cmd(["udevadm", "settle"], timeout=8)
    if target and shutil.which("partprobe"):
        run_cmd(["partprobe", target], timeout=8)




def raid_maintenance_volumes(conf: Dict[str, str]) -> List[Dict[str, Any]]:
    """Expose les volumes logiques RAID utilisables par la page Maintenance.

    La page RAID crée/supprime seulement les volumes logiques. Le formatage,
    la vérification et le montage restent ici. On garde donc uniquement les
    volumes qui ont un vrai périphérique bloc Linux (/dev/mdX), afin de
    réutiliser les mêmes garde-fous que les disques/partitions classiques.
    """
    list_fn = globals().get("list_raid_volumes")
    if not callable(list_fn):
        return []

    rows: List[Dict[str, Any]] = []
    try:
        volumes = list_fn()
    except Exception:
        volumes = []

    for volume in volumes or []:
        engine = str(volume.get("engine") or "").strip().lower()
        device = str(volume.get("device") or "").strip()
        if not device.startswith("/dev/"):
            # Les pools ZFS purs n'ont pas de bloc /dev/mdX à formater/monter
            # avec les actions génériques de Maintenance. On les laisse à la
            # page RAID/logique dédiée.
            continue
        if not SAFE_BLOCK_PATH_RE.match(device):
            continue
        name = str(volume.get("name") or os.path.basename(device) or "volume").strip()
        alias = str(volume.get("alias_device") or "").strip()
        mountpoint = str(volume.get("mountpoint") or "").strip()
        mounts_raw = volume.get("mounts") or []
        mounts = [str(m).strip() for m in mounts_raw if str(m or "").strip()]
        if mountpoint and mountpoint not in mounts:
            mounts.insert(0, mountpoint)

        # Source de vérité pour Maintenance : probe direct du volume logique.
        # On ignore volontairement volume["fstype"] quand il vient de la page RAID
        # ou d'un inventaire précédent, car /dev/md127 peut être recréé et garder
        # un ancien TYPE fantôme dans les caches.
        live_meta = parse_blkid_export(device)
        if live_meta:
            save_persistent_block_meta(conf, device, live_meta)
        else:
            forget_persistent_block_meta(conf, device)
        fstype = str(live_meta.get("type") or "").strip()
        label = str(live_meta.get("label") or "").strip()
        uuid = str(live_meta.get("uuid") or "").strip()
        row: Dict[str, Any] = {
            "type": "raid_volume",
            "engine": engine or "raid",
            "name": name,
            "logical_name": name,
            "display_device": device,
            "device": device,
            "path": device,
            "alias_device": alias,
            "model": (engine.upper() if engine else "RAID") + " volume logique",
            "serial": alias,
            "size": volume.get("size") or "—",
            "fstype": fstype,
            "blkid_fstype": fstype,
            "label": label,
            "uuid": uuid,
            "mount": mountpoint,
            "mountpoint": mountpoint,
            "mounts": mounts,
            "mounted": bool(mountpoint),
            "maintenance_protected": False,
            "protected": False,
        }
        row.update(disk_maintenance_tmux_state(device))
        rows.append(row)

    return sorted(rows, key=lambda item: str(item.get("device") or item.get("name") or ""))

def augment_maintenance_data(data: Dict[str, Any]) -> Dict[str, Any]:
    conf = get_config()
    protected = system_disk_names(conf)
    for disk in data.get("disks", []) or []:
        device = str(disk.get("device") or "")
        disk_name = block_name_from_path(device)
        disk["maintenance_protected"] = bool(disk.get("is_system_disk") or disk_name in protected)
        if device:
            disk.update(disk_maintenance_tmux_state(device))
        disk_meta = parse_blkid_export(device)
        if disk_meta:
            save_persistent_block_meta(conf, device, disk_meta)
            disk["blkid_fstype"] = disk_meta.get("type", "")
            disk["blkid_label"] = disk_meta.get("label", "")
            disk["blkid_uuid"] = disk_meta.get("uuid", "")
        else:
            # En Maintenance, une absence de signature live doit gagner contre
            # les vieux caches : un disque/volume wipé doit redevenir "—".
            forget_persistent_block_meta(conf, device)
            for key in ("blkid_fstype", "fstype", "fs_type", "filesystem", "blkid_label", "label", "blkid_uuid", "uuid"):
                disk[key] = ""
        for part in disk.get("parts", []) or []:
            target = str(part.get("path") or "")
            if target:
                part.update(disk_maintenance_tmux_state(target))
            meta = parse_blkid_export(target)
            if meta:
                save_persistent_block_meta(conf, target, meta)
                part["fstype"] = meta.get("type", "")
                part["label"] = meta.get("label", "")
                part["uuid"] = meta.get("uuid", "")
            else:
                forget_persistent_block_meta(conf, target)
                for key in ("blkid_fstype", "fstype", "fs_type", "filesystem", "blkid_label", "label", "blkid_uuid", "uuid"):
                    part[key] = ""
            part["protected"] = disk["maintenance_protected"]
            part["mounted"] = bool(part.get("mount"))
    data["maintenance"] = True
    data["raid_maintenance_volumes"] = raid_maintenance_volumes(conf)
    data["allowed_mount_prefixes"] = allowed_mount_prefixes(conf)
    data["mount_default_options"] = str(conf.get("MOUNT_DEFAULT_OPTIONS") or "defaults,nofail,noatime")
    return data


def fstab_path(conf: Dict[str, str]) -> str:
    return str(conf.get("FSTAB_FILE") or "/etc/fstab")


def fstab_backup_and_write(conf: Dict[str, str], lines: List[str]) -> None:
    path = fstab_path(conf)
    try:
        if os.path.exists(path):
            stamp = time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(path, f"{path}.bak.{stamp}")
    except Exception:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines).rstrip() + "\n")
    os.replace(tmp, path)


def add_or_replace_fstab_entry(conf: Dict[str, str], target: str, mountpoint: str, fstype: str, options: str) -> Tuple[bool, str]:
    path = fstab_path(conf)
    if not os.path.exists(path):
        return False, f"{path} introuvable."
    meta = parse_blkid_export(target)
    if meta:
        save_persistent_block_meta(conf, target, meta)
    uuid = meta.get("uuid", "")
    fs_type = fstype if fstype and fstype != "auto" else meta.get("type", "auto")
    spec = f"UUID={uuid}" if uuid else target
    options = options or str(conf.get("MOUNT_DEFAULT_OPTIONS") or "defaults,nofail,noatime")
    new_line = f"{spec}\t{mountpoint}\t{fs_type}\t{options}\t0\t0"

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            current = handle.read().splitlines()
    except Exception as exc:
        return False, f"Lecture fstab impossible : {exc}"

    kept: List[str] = []
    for line in current:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            kept.append(line)
            continue
        parts = stripped.split()
        # Remplace l'ancienne ligne si elle pointe le même UUID/device ou le même point de montage.
        if len(parts) >= 2 and (parts[0] in {spec, target} or parts[1] == mountpoint):
            continue
        kept.append(line)

    kept.append(new_line)
    try:
        fstab_backup_and_write(conf, kept)
    except Exception as exc:
        return False, f"Écriture fstab impossible : {exc}"
    return True, "Entrée fstab ajoutée/mise à jour."


def remove_fstab_entries_for_target(conf: Dict[str, str], target: str, mountpoints: List[str]) -> Tuple[bool, str]:
    """Supprime du fstab les lignes qui correspondent au périphérique ou à ses points de montage.

    Utilisé uniquement quand l'utilisateur coche l'option persistante dans la pop-up
    de démontage. Le démontage simple reste temporaire.
    """
    path = fstab_path(conf)
    if not os.path.exists(path):
        return False, f"{path} introuvable."

    meta = parse_blkid_export(target)
    if meta:
        save_persistent_block_meta(conf, target, meta)
    specs = {target}
    uuid = meta.get("uuid", "")
    label = meta.get("label", "")
    if uuid:
        specs.add(f"UUID={uuid}")
        specs.add(f"/dev/disk/by-uuid/{uuid}")
    if label:
        specs.add(f"LABEL={label}")
        specs.add(f"/dev/disk/by-label/{label}")

    mount_set = {str(m or "").strip() for m in mountpoints if str(m or "").strip()}

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            current = handle.read().splitlines()
    except Exception as exc:
        return False, f"Lecture fstab impossible : {exc}"

    kept: List[str] = []
    removed = 0
    for line in current:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            kept.append(line)
            continue
        parts = stripped.split()
        if len(parts) >= 2 and (parts[0] in specs or parts[1] in mount_set):
            removed += 1
            continue
        kept.append(line)

    if not removed:
        return True, "Aucune entrée fstab correspondante à supprimer."

    try:
        fstab_backup_and_write(conf, kept)
    except Exception as exc:
        return False, f"Écriture fstab impossible : {exc}"

    if shutil.which("systemctl"):
        run_cmd(["systemctl", "daemon-reload"], timeout=12)

    return True, f"{removed} entrée(s) fstab supprimée(s)."


def mount_dir_removal_allowed(conf: Dict[str, str], mountpoint: str) -> Tuple[bool, str, str]:
    """Valide qu'un dossier de point de montage peut être supprimé sans danger.

    Cette suppression ne concerne que le dossier vide qui servait de point de montage,
    jamais le contenu du disque. On refuse les chemins système, les racines autorisées
    elles-mêmes, les liens symboliques, les montages encore actifs et les chemins
    sensibles de type /mnt/user ou /mnt/user0.
    """
    ok, path, error = safe_real_mount_path(conf, mountpoint)
    if not ok:
        return False, path, error

    path = os.path.normpath(path)
    prefixes = allowed_mount_prefixes(conf)

    if path in prefixes:
        return False, path, "Suppression refusée : racine de montage autorisée."

    sensitive_prefixes = ("/mnt/user", "/mnt/user0")
    if any(path == p or path.startswith(p + "/") for p in sensitive_prefixes):
        return False, path, "Suppression refusée : chemin mergerfs/user protégé."

    if os.path.islink(path):
        return False, path, "Suppression refusée : le point de montage est un lien symbolique."

    if os.path.ismount(path):
        return False, path, "Suppression refusée : le dossier est encore un point de montage actif."

    if not os.path.exists(path):
        return True, path, "Dossier de montage déjà absent."

    if not os.path.isdir(path):
        return False, path, "Suppression refusée : le chemin n'est pas un dossier."

    try:
        entries = os.listdir(path)
    except Exception as exc:
        return False, path, f"Impossible de lire le dossier : {exc}"

    if entries:
        return False, path, "Suppression refusée : dossier non vide."

    return True, path, ""


def remove_mount_dirs_if_safe(conf: Dict[str, str], mountpoints: List[str]) -> Tuple[bool, str]:
    """Supprime les dossiers de montage vides après un démontage réussi."""
    cleaned: List[str] = []
    seen: set[str] = set()
    for mountpoint in mountpoints:
        path = os.path.normpath(str(mountpoint or "").strip())
        if not path or path in seen:
            continue
        seen.add(path)
        cleaned.append(path)

    if not cleaned:
        return False, "Aucun point de montage connu à supprimer."

    messages: List[str] = []
    # Dossiers profonds d'abord, au cas où plusieurs points de montage existent.
    for mountpoint in sorted(cleaned, key=lambda p: len(p.rstrip("/").split("/")), reverse=True):
        ok, path, error = mount_dir_removal_allowed(conf, mountpoint)
        if not ok:
            return False, f"{path}: {error}"
        if error:
            messages.append(f"{path}: {error}")
            continue
        try:
            os.rmdir(path)
            messages.append(f"Dossier de montage supprimé : {path}")
        except Exception as exc:
            return False, f"{path}: suppression impossible : {exc}"

    return True, " ".join(messages) if messages else "Aucun dossier supprimé."


def make_mkfs_command(target: str, fstype: str, label: str, quick: bool) -> Tuple[bool, List[str], str]:
    fstype = str(fstype or "").strip().lower()
    label = str(label or "").strip()
    if not SAFE_LABEL_RE.match(label):
        return False, [], "Label refusé : caractères autorisés A-Z, 0-9, espace, point, tiret, underscore."

    if fstype == "xfs":
        binary = shutil.which("mkfs.xfs")
        if not binary:
            return False, [], "Formatage XFS impossible : mkfs.xfs est introuvable. Installe le paquet xfsprogs depuis le bouton Installer les services."
        # XFS n'a pas de mode quick séparé comme ext4/ntfs : mkfs.xfs -f crée les métadonnées directement et reste normalement rapide.
        cmd = [binary, "-f"]
        if label:
            cmd += ["-L", label]
        cmd.append(target)
        return True, cmd, ""
    if fstype == "ext4":
        binary = shutil.which("mkfs.ext4") or "mkfs.ext4"
        cmd = [binary, "-F"]
        if quick:
            cmd += ["-E", "lazy_itable_init=1,lazy_journal_init=1"]
        if label:
            cmd += ["-L", label]
        cmd.append(target)
        return True, cmd, ""
    if fstype == "btrfs":
        binary = shutil.which("mkfs.btrfs") or "mkfs.btrfs"
        cmd = [binary, "-f"]
        if label:
            cmd += ["-L", label]
        cmd.append(target)
        return True, cmd, ""
    if fstype == "exfat":
        binary = shutil.which("mkfs.exfat") or "mkfs.exfat"
        cmd = [binary]
        if label:
            cmd += ["-n", label]
        cmd.append(target)
        return True, cmd, ""
    if fstype == "ntfs":
        binary = shutil.which("mkfs.ntfs") or shutil.which("mkntfs") or "mkfs.ntfs"
        cmd = [binary, "-F"]
        if quick:
            cmd.append("-Q")
        if label:
            cmd += ["-L", label]
        cmd.append(target)
        return True, cmd, ""
    if fstype in {"vfat", "fat32"}:
        binary = shutil.which("mkfs.vfat") or "mkfs.vfat"
        cmd = [binary, "-F", "32"]
        if label:
            cmd += ["-n", label[:11]]
        cmd.append(target)
        return True, cmd, ""
    return False, [], "Format non supporté. Choisis xfs, ext4, btrfs, exfat, ntfs ou vfat."




def target_tree_targets_for_wipe(conf: Dict[str, str], target: str) -> List[str]:
    """Retourne le bloc demandé + ses partitions directes pour un wipe complet.

    Pour préparer un disque au RAID, il faut pouvoir nettoyer le disque parent
    même s'il contient encore une table de partitions. Les partitions sont
    traitées avant le disque parent pour enlever aussi les signatures XFS/ext4
    visibles sur les anciens enfants.
    """
    target = str(target or "").strip()
    targets: List[str] = [target]
    if not SAFE_DISK_RE.match(target):
        return targets
    name = block_name_from_path(target)
    if not name:
        return targets
    for part_name in list_partitions(str(conf.get("SYS_BLOCK_PATH") or "/sys/block"), name):
        part_path = "/dev/" + part_name
        if SAFE_BLOCK_PATH_RE.match(part_path) and part_path not in targets:
            targets.append(part_path)
    return targets


def mount_paths_for_wipe_target(conf: Dict[str, str], target: str) -> List[str]:
    paths: List[str] = []
    for item in target_tree_targets_for_wipe(conf, target):
        for mount in mount_paths_for_target(conf, item):
            if mount and mount not in paths:
                paths.append(mount)
    return paths


def remove_fstab_for_wipe_targets(conf: Dict[str, str], targets: List[str]) -> List[str]:
    messages: List[str] = []
    for item in targets:
        try:
            ok_fstab, msg = remove_fstab_entries_for_target(conf, item, mount_paths_for_target(conf, item))
            if msg and (ok_fstab or "introuvable" not in msg.lower()):
                messages.append(f"fstab {item}: {msg}")
        except Exception as exc:
            messages.append(f"fstab {item}: {exc}")
    return messages


def wipe_device_signatures(conf: Dict[str, str], target: str) -> Tuple[bool, str]:
    wipefs_bin = shutil.which("wipefs")
    if not wipefs_bin:
        return False, "Commande absente: wipefs"

    targets = target_tree_targets_for_wipe(conf, target)
    outputs: List[str] = []
    outputs.extend(remove_fstab_for_wipe_targets(conf, targets))

    # Nettoyage enfants d'abord, puis disque parent.
    ordered = list(reversed(targets[1:])) + [targets[0]]
    for item in ordered:
        if not os.path.exists(item):
            continue
        rc, out = run_cmd([wipefs_bin, "-a", "-f", item], timeout=90)
        outputs.append((f"$ wipefs -a -f {item}\n" + (out or "")).strip())
        if rc != 0:
            return False, "\n\n".join(outputs)[-4000:]
        forget_persistent_block_meta(conf, item)

    # Sur un disque complet, on efface aussi les métadonnées GPT de secours si sgdisk existe.
    if SAFE_DISK_RE.match(target):
        sgdisk_bin = shutil.which("sgdisk")
        if sgdisk_bin and os.path.exists(target):
            rc, out = run_cmd([sgdisk_bin, "--zap-all", target], timeout=90)
            outputs.append((f"$ sgdisk --zap-all {target}\n" + (out or "")).strip())
            # Certaines versions retournent une erreur si le disque n'avait déjà plus de GPT.
            # Le wipefs précédent reste la source principale, donc on garde ce retour en sortie.

    if shutil.which("partprobe"):
        rc, out = run_cmd(["partprobe", target], timeout=20)
        if out:
            outputs.append((f"$ partprobe {target}\n" + out).strip())
    elif shutil.which("blockdev") and SAFE_DISK_RE.match(target):
        rc, out = run_cmd(["blockdev", "--rereadpt", target], timeout=20)
        if out:
            outputs.append((f"$ blockdev --rereadpt {target}\n" + out).strip())

    refresh_udev_best_effort(target)
    clear_disk_runtime_caches()
    return True, "\n\n".join([x for x in outputs if x])[-4000:]


@disk_bp.route("/disk/api/action/wipe", methods=["POST"])
def disk_wipe_api():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    ok, target, error = require_safe_target(conf, str(payload.get("target") or ""), allow_disk=True, allow_part=True)
    if not ok:
        return json_response(False, error)

    mounts = mount_paths_for_wipe_target(conf, target)
    if mounts:
        return json_response(False, "Le disque/volume est monté : démonte tout avant le wipe.", output="\n".join(mounts)[-2000:])

    active_state = disk_maintenance_tmux_state(target)
    if active_state.get("maintenance_tmux_active"):
        return json_response(False, "Une session tmux est déjà active pour ce volume : " + str(active_state.get("maintenance_tmux_session") or ""))

    ok_wipe, output = wipe_device_signatures(conf, target)
    if not ok_wipe:
        detail = ""
        for line in reversed((output or "").splitlines()):
            line = line.strip()
            if line:
                detail = line
                break
        msg = "Wipe impossible pour " + target + "."
        if detail:
            msg += " " + detail[:240]
        return json_response(False, msg, output=output)

    return json_response(True, "Wipe terminé. Le disque devrait maintenant pouvoir réapparaître côté RAID après actualisation.", output=output)

@disk_bp.route("/disk/api/maintenance")
def disk_maintenance_api():
    return jsonify(augment_maintenance_data(collect_disks()))


@disk_bp.route("/disk/api/action/power-toggle", methods=["POST"])
def disk_power_toggle_api():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    ok, device, error = require_safe_target(conf, str(payload.get("device") or ""), allow_disk=True, allow_part=False)
    if not ok:
        return json_response(False, error)

    name = block_name_from_path(device)
    rota = read_text(os.path.join(sysfs_disk_base(str(conf.get("SYS_BLOCK_PATH") or "/sys/block"), name), "queue", "rotational"))
    is_hdd = rota == "1"
    if not is_hdd:
        return json_response(False, "Action veille/réveil réservée aux HDD.")

    state, _label = read_power_state(conf, device, is_hdd=True)
    if state == "standby":
        rc, out = run_cmd(["dd", f"if={device}", "of=/dev/null", "bs=512", "count=1", "iflag=direct"], timeout=12)
        if rc != 0:
            rc, out = run_cmd(["dd", f"if={device}", "of=/dev/null", "bs=512", "count=1"], timeout=12)
        clear_disk_runtime_caches()
        return json_response(rc == 0, "Réveil demandé." if rc == 0 else "Réveil impossible.", output=out[-1200:])

    hdparm = which_or_config(conf, "HDPARM_BIN", "hdparm")
    rc, out = run_cmd([hdparm, "-y", device], timeout=8)
    clear_disk_runtime_caches()
    return json_response(rc == 0, "Mise en veille demandée." if rc == 0 else "Mise en veille impossible.", output=out[-1200:])


@disk_bp.route("/disk/api/action/unmount", methods=["POST"])
def disk_unmount_api():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    ok, target, error = require_safe_target(conf, str(payload.get("target") or ""), allow_disk=True, allow_part=True)
    if not ok:
        return json_response(False, error)

    remove_fstab = bool_from_payload(payload.get("remove_fstab"))
    remove_mount_dir = bool_from_payload(payload.get("remove_mount_dir"))
    mounts = mount_paths_for_target(conf, target)

    if not mounts and not remove_fstab and not remove_mount_dir:
        return json_response(True, "Déjà démonté.")
    if remove_mount_dir and not mounts:
        return json_response(False, "Aucun point de montage actuel à supprimer. Monte/démonte depuis la ligne montée, ou supprime le dossier à la main si besoin.")

    outputs: List[str] = []
    for mount in mounts:
        if mount == "/" or mount.startswith("/boot"):
            return json_response(False, "Montage système/boot refusé.")
        rc, out = run_cmd(["umount", mount], timeout=20)
        outputs.append(f"$ umount {mount}\n{out}".strip())
        if rc != 0:
            return json_response(False, f"Démontage impossible : {mount}", output="\n\n".join(outputs)[-2000:])

    fstab_msg = ""
    if remove_fstab:
        ok_fstab, fstab_msg = remove_fstab_entries_for_target(conf, target, mounts)
        if not ok_fstab:
            return json_response(False, "Démonté à chaud, mais suppression fstab impossible : " + fstab_msg, output="\n\n".join(outputs)[-2000:])
        if fstab_msg:
            outputs.append(fstab_msg)

    mount_dir_msg = ""
    if remove_mount_dir:
        ok_mount_dir, mount_dir_msg = remove_mount_dirs_if_safe(conf, mounts)
        if not ok_mount_dir:
            return json_response(False, "Démonté à chaud, mais suppression du dossier de montage impossible : " + mount_dir_msg, output="\n\n".join(outputs)[-2000:])
        if mount_dir_msg:
            outputs.append(mount_dir_msg)

    clear_disk_runtime_caches()
    extras = " ".join(x for x in [fstab_msg, mount_dir_msg] if x)
    msg = "Démontage terminé." + (" " + extras if extras else "")
    return json_response(True, msg, output="\n\n".join(outputs)[-2500:])


@disk_bp.route("/disk/api/action/mount", methods=["POST"])
def disk_mount_api():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    ok, target, error = require_safe_target(conf, str(payload.get("target") or ""), allow_disk=True, allow_part=True)
    if not ok:
        return json_response(False, error)

    mountpoint_raw = str(payload.get("mountpoint") or "").strip()
    path_ok, mountpoint, path_error = safe_real_mount_path(conf, mountpoint_raw)
    if not path_ok:
        return json_response(False, path_error)

    create_dir = bool_from_payload(payload.get("create_dir"))
    add_fstab = bool_from_payload(payload.get("add_fstab"))
    fstype = str(payload.get("fstype") or "auto").strip().lower() or "auto"
    options = str(payload.get("options") or conf.get("MOUNT_DEFAULT_OPTIONS") or "defaults,nofail,noatime").strip()
    allowed_fs = {"auto", "xfs", "ext4", "btrfs", "exfat", "ntfs", "vfat", "fat32"}
    if fstype not in allowed_fs:
        return json_response(False, "Type de filesystem refusé.")
    if fstype == "fat32":
        fstype = "vfat"

    if not os.path.exists(mountpoint):
        if not create_dir:
            return json_response(False, "Le dossier n'existe pas. Coche 'Créer le dossier'.")
        try:
            os.makedirs(mountpoint, exist_ok=True)
        except Exception as exc:
            return json_response(False, f"Création du dossier impossible : {exc}")
    if not os.path.isdir(mountpoint):
        return json_response(False, "Le chemin existe mais n'est pas un dossier.")

    cmd = ["mount"]
    if fstype != "auto":
        cmd += ["-t", fstype]
    if options:
        cmd += ["-o", options]
    cmd += [target, mountpoint]

    outputs: List[str] = []
    fstab_msg = ""

    if add_fstab:
        # Quand l'utilisateur demande le démarrage automatique, on écrit d'abord
        # /etc/fstab, puis on monte à chaud depuis cette nouvelle ligne.
        # Comme ça le comportement immédiat et le comportement au reboot restent identiques.
        ok_fstab, fstab_msg = add_or_replace_fstab_entry(conf, target, mountpoint, fstype, options)
        if not ok_fstab:
            return json_response(False, "fstab non modifié : " + fstab_msg)
        outputs.append(fstab_msg)

        if shutil.which("systemctl"):
            rc_reload, out_reload = run_cmd(["systemctl", "daemon-reload"], timeout=12)
            if out_reload:
                outputs.append(("$ systemctl daemon-reload\n" + out_reload).strip())
            if rc_reload != 0:
                return json_response(False, "fstab écrit, mais rechargement systemd impossible.", output="\n\n".join(outputs)[-2500:])

        if os.path.ismount(mountpoint):
            outputs.append(f"Déjà monté à chaud : {mountpoint}")
            rc = 0
            out = ""
        else:
            # Avec une ligne fstab valide, mount <point> utilise le UUID/type/options
            # réellement enregistrés. C'est plus cohérent que de monter avant d'écrire fstab.
            rc, out = run_cmd(["mount", mountpoint], timeout=25)
            outputs.append((f"$ mount {mountpoint}\n" + (out or "")).strip())
            if rc != 0:
                # Fallback défensif : si mount <point> échoue pour une raison liée au parsing
                # fstab, on tente quand même le montage direct demandé par la pop-up.
                rc_direct, out_direct = run_cmd(cmd, timeout=25)
                outputs.append(("$ " + " ".join(shlex.quote(x) for x in cmd) + "\n" + (out_direct or "")).strip())
                if rc_direct != 0:
                    return json_response(False, "fstab écrit, mais montage à chaud impossible.", output="\n\n".join(outputs)[-3000:])
    else:
        rc, out = run_cmd(cmd, timeout=25)
        outputs.append(("$ " + " ".join(shlex.quote(x) for x in cmd) + "\n" + (out or "")).strip())
        if rc != 0:
            return json_response(False, "Montage impossible.", output="\n\n".join(outputs)[-3000:])

    clear_disk_runtime_caches()
    success_msg = "Montage à chaud terminé." if add_fstab else "Montage terminé."
    if fstab_msg:
        success_msg += " " + fstab_msg
    return json_response(True, success_msg, output="\n\n".join(outputs)[-3000:])


def label_limit_for_fstype(fstype: str) -> int:
    fs = str(fstype or "").strip().lower()
    if fs == "xfs":
        return 12
    if fs in {"ext2", "ext3", "ext4"}:
        return 16
    if fs in {"vfat", "fat", "fat32", "msdos"}:
        return 11
    if fs == "exfat":
        return 15
    if fs == "ntfs":
        return 128
    if fs == "btrfs":
        return 32
    return 32


def make_label_command(target: str, fstype: str, label: str) -> Tuple[bool, List[str], str]:
    fstype = str(fstype or "").strip().lower()
    label = str(label or "").strip()
    if not label:
        return False, [], "Label vide refusé."
    if not SAFE_LABEL_RE.match(label):
        return False, [], "Label invalide. Utilise lettres, chiffres, espaces, point, tiret ou underscore."
    limit = label_limit_for_fstype(fstype)
    if len(label) > limit:
        return False, [], f"Label trop long pour {fstype or 'ce filesystem'} : {limit} caractères maximum."

    if fstype in {"ext2", "ext3", "ext4"}:
        binary = shutil.which("e2label") or "e2label"
        return True, [binary, target, label], ""
    if fstype == "xfs":
        binary = shutil.which("xfs_admin") or "xfs_admin"
        return True, [binary, "-L", label, target], ""
    if fstype == "btrfs":
        binary = shutil.which("btrfs") or "btrfs"
        return True, [binary, "filesystem", "label", target, label], ""
    if fstype == "exfat":
        binary = shutil.which("exfatlabel") or "exfatlabel"
        return True, [binary, target, label], ""
    if fstype == "ntfs":
        binary = shutil.which("ntfslabel") or "ntfslabel"
        return True, [binary, target, label], ""
    if fstype in {"vfat", "fat", "fat32", "msdos"}:
        binary = shutil.which("fatlabel") or shutil.which("dosfslabel") or "fatlabel"
        return True, [binary, target, label], ""
    return False, [], "Modification de label non supportée pour ce filesystem. Formats supportés : xfs, ext2/3/4, btrfs, exfat, ntfs, vfat."


@disk_bp.route("/disk/api/action/label", methods=["POST"])
def disk_label_api():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    ok, target, error = require_safe_target(conf, str(payload.get("target") or ""), allow_disk=True, allow_part=True)
    if not ok:
        return json_response(False, error)

    mounts = mount_paths_for_target(conf, target)
    if mounts:
        return json_response(False, "Le périphérique est monté : démonte-le avant de modifier son label.", output="\n".join(mounts)[-1200:])

    meta = parse_blkid_export(target)
    fstype = str(meta.get("type") or payload.get("fstype") or "").strip().lower()
    if not fstype:
        return json_response(False, "Aucun filesystem détecté : impossible de modifier le label.")

    label = str(payload.get("label") or "").strip()
    ok_cmd, cmd, cmd_error = make_label_command(target, fstype, label)
    if not ok_cmd:
        return json_response(False, cmd_error)

    rc, out = run_cmd(cmd, timeout=45)
    refresh_udev_best_effort(target)
    if rc == 0:
        meta = parse_blkid_export(target)
        if meta:
            save_persistent_block_meta(conf, target, meta)
        clear_disk_runtime_caches()
        return json_response(True, f"Label modifié : {label}", output=(out or "")[-2000:], label=label, fstype=fstype)

    detail = ""
    for line in reversed((out or "").splitlines()):
        line = line.strip()
        if line:
            detail = line
            break
    msg = f"Modification du label impossible pour {target}."
    if detail:
        msg += " " + detail[:240]
    return json_response(False, msg, output=(out or "")[-3000:])


@disk_bp.route("/disk/api/action/format", methods=["POST"])
def disk_format_api():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    ok, target, error = require_safe_target(conf, str(payload.get("target") or ""), allow_disk=True, allow_part=True)
    if not ok:
        return json_response(False, error)

    if mount_paths_for_target(conf, target):
        return json_response(False, "Le périphérique est monté : démonte-le avant de formater.")

    fstype = str(payload.get("fstype") or "xfs").strip().lower()
    label = str(payload.get("label") or "").strip()
    quick = bool_from_payload(payload.get("quick"))
    ok_cmd, cmd, cmd_error = make_mkfs_command(target, fstype, label, quick)
    if not ok_cmd:
        return json_response(False, cmd_error)

    # Formatage rapide : action courte, on garde le subprocess classique.
    # Formatage lent / normal : session tmux détachée + vrai terminal ttyd, comme la vérification.
    active_state = disk_maintenance_tmux_state(target)
    if quick:
        if active_state.get("maintenance_tmux_active"):
            return json_response(False, "Une session tmux est déjà active pour ce volume : " + str(active_state.get("maintenance_tmux_session") or ""))
        rc, out = run_cmd(cmd, timeout=1800)
        refresh_udev_best_effort(target)
        if rc == 0:
            meta = parse_blkid_export(target)
            if meta:
                save_persistent_block_meta(conf, target, meta)
        clear_disk_runtime_caches()
        if rc == 0:
            return json_response(True, "Formatage rapide terminé.", output=(out or "")[-3000:])
        detail = ""
        for line in reversed((out or "").splitlines()):
            line = line.strip()
            if line:
                detail = line
                break
        message = f"Formatage {fstype} impossible."
        if detail:
            message += " " + detail[:240]
        return json_response(False, message, output=(out or "")[-3000:])

    ok_tmux, session, tmux_msg, created = start_disk_format_tmux(conf, target, fstype, label, cmd)
    if not ok_tmux:
        return json_response(False, tmux_msg)

    ok_ttyd, url, port, ttyd_msg = start_ttyd_for_tmux_session(session)
    if not ok_ttyd:
        return json_response(False, tmux_msg + "\n" + ttyd_msg, session=session)

    return jsonify({
        "ok": True,
        "message": tmux_msg + "\n" + ttyd_msg,
        "session": session,
        "created": created,
        "terminal_url": url,
        "terminal_port": port,
        "mode": "format",
    })



# ---------------------------------------------------------------------------
# Vérification disque dans tmux + vrai terminal ttyd
# ---------------------------------------------------------------------------

def disk_operation_base_name(target: str) -> str:
    base = os.path.basename(str(target or "").strip()) or "disk"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", base).strip("-") or "disk"


def disk_check_session_name(target: str) -> str:
    return f"{disk_operation_base_name(target)}-check"


def disk_format_session_name(target: str) -> str:
    return f"{disk_operation_base_name(target)}-format"


def disk_maintenance_sessions_for_target(target: str) -> List[Dict[str, str]]:
    sessions: List[Dict[str, str]] = []
    for kind, label, session in (
        ("format", "Formatage", disk_format_session_name(target)),
        ("check", "Vérification", disk_check_session_name(target)),
    ):
        if tmux_session_exists(session):
            sessions.append({"kind": kind, "label": label, "session": session})
    return sessions


def disk_maintenance_tmux_state(target: str) -> Dict[str, Any]:
    sessions = disk_maintenance_sessions_for_target(target)
    active = sessions[0] if sessions else {}
    return {
        "maintenance_tmux_active": bool(active),
        "maintenance_tmux_kind": active.get("kind", ""),
        "maintenance_tmux_label": active.get("label", ""),
        "maintenance_tmux_session": active.get("session", ""),
        "maintenance_tmux_sessions": sessions,
    }


def tmux_session_exists(session: str) -> bool:
    if not shutil.which("tmux"):
        return False
    rc, _out = run_cmd(["tmux", "has-session", "-t", session], timeout=5)
    return rc == 0


def terminal_module_conf() -> Dict[str, str]:
    data = {
        "TERMINAL_PORT": "7681",
        "TERMINAL_LISTEN": "0.0.0.0",
        "TERMINAL_BIN_DIR": "../bin",
        "TERMINAL_BIN_X86_64": "ttyd.x86_64",
        "TERMINAL_BIN_AARCH64": "ttyd.aarch64",
        "TERMINAL_LOG_FILE": "/var/log/terminal/terminal.log",
        "TERMINAL_THEME_BACKGROUND": "#000000",
        "TERMINAL_THEME_FOREGROUND": "#00ff00",
        "TERMINAL_THEME_CURSOR": "#ffffff",
    }
    for candidate in (
        os.environ.get("TERMINAL_CONFIG_PATH", "").strip(),
        nas_conf_file("terminal.conf"),
        os.path.join(_NAS_MODULE_DIR, "..", "conf", "terminal.conf"),
        os.path.join(_NAS_MODULE_DIR, "conf", "terminal.conf"),
    ):
        if candidate and os.path.exists(candidate):
            data.update(read_config_file(candidate))
            break
    return data


def terminal_resolve_path(value: str) -> str:
    value = strip_quotes(str(value or "")).strip()
    if not value:
        return value
    value = os.path.expanduser(os.path.expandvars(value))
    if os.path.isabs(value):
        return os.path.abspath(value)
    return os.path.abspath(os.path.join(_NAS_MODULE_DIR, value))


def ttyd_binary_from_terminal_conf(conf: Dict[str, str]) -> str:
    try:
        machine = os.uname().machine.lower()
    except Exception:
        machine = ""
    preferred = conf.get("TERMINAL_BIN_AARCH64" if machine in {"aarch64", "arm64"} else "TERMINAL_BIN_X86_64", "")
    names = [preferred, "ttyd", "ttyd.x86_64", "ttyd.aarch64"]
    bin_dirs = [
        terminal_resolve_path(conf.get("TERMINAL_BIN_DIR", "../bin")),
        os.path.abspath(os.path.join(_NAS_MODULE_DIR, "..", "bin")),
        os.path.abspath(os.path.join(_NAS_MODULE_DIR, "bin")),
    ]
    seen_dirs: set[str] = set()
    for directory in bin_dirs:
        if not directory or directory in seen_dirs:
            continue
        seen_dirs.add(directory)
        for name in names:
            if not name:
                continue
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
    found = shutil.which("ttyd")
    return found or os.path.join(bin_dirs[0], preferred or "ttyd.x86_64")


def disk_check_port(session: str, terminal_conf: Dict[str, str]) -> int:
    try:
        base = int(str(terminal_conf.get("TERMINAL_PORT") or "7681").strip())
    except Exception:
        base = 7681
    # Port stable par session, assez loin du terminal principal.
    offset = 100 + (sum(ord(ch) for ch in session) % 200)
    return base + offset


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def disk_check_pid_file(session: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", session)
    return f"/tmp/flask_disk_check_ttyd_{safe}.pid"


def read_pid_file(path: str) -> int:
    try:
        return int(read_text(path) or "0")
    except Exception:
        return 0


def check_terminal_url(port: int) -> str:
    try:
        import terminal as yoleo_terminal
        return yoleo_terminal.ttyd_url_for_port(yoleo_terminal.get_config(), int(port))
    except Exception:
        # request.host = serveur:5055 ; ttyd écoute sur son propre port.
        host = (request.host or "").split(":", 1)[0] or request.environ.get("SERVER_NAME") or "127.0.0.1"
        scheme = request.scheme or "http"
        return f"{scheme}://{host}:{port}/"


def terminal_theme_args(conf: Dict[str, str]) -> List[str]:
    """Options ttyd reprises de terminal.conf pour la popup de vérification.

    ttyd accepte les options xterm.js avec -t key=value. On garde les
    couleurs dans terminal.conf comme source unique, puis Disk les réutilise
    sans modifier le module Terminal.
    """
    def color(key: str, default: str) -> str:
        value = strip_quotes(str(conf.get(key) or default)).strip() or default
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
            return value
        return default

    theme = {
        "background": color("TERMINAL_THEME_BACKGROUND", "#000000"),
        "foreground": color("TERMINAL_THEME_FOREGROUND", "#00ff00"),
        "cursor": color("TERMINAL_THEME_CURSOR", "#ffffff"),
    }
    return ["-t", "theme=" + json.dumps(theme, separators=(",", ":"))]


def disk_check_command(conf: Dict[str, str], target: str, mode: str) -> str:
    mode = str(mode or "fsck").strip().lower()
    fs_type = ""
    try:
        meta = cached_block_meta(conf, target, allow_live_read=True)
        fs_type = str(meta.get("type") or "").strip().lower()
    except Exception:
        fs_type = ""
    qtarget = shlex.quote(target)

    if mode in {"surface", "badblocks"}:
        tool = shutil.which("badblocks") or "badblocks"
        return (
            "echo 'Vérification surface lecture seule : badblocks -sv'; "
            f"echo 'Cible : {qtarget}'; "
            "echo; "
            f"exec {shlex.quote(tool)} -sv {qtarget}"
        )

    if fs_type == "xfs":
        tool = shutil.which("xfs_repair") or "xfs_repair"
        return (
            "echo 'Vérification XFS lecture seule : xfs_repair -n'; "
            f"echo 'Cible : {qtarget}'; "
            "echo; "
            f"exec {shlex.quote(tool)} -n {qtarget}"
        )

    if fs_type == "btrfs":
        tool = shutil.which("btrfs") or "btrfs"
        return (
            "echo 'Vérification BTRFS lecture seule : btrfs check --readonly'; "
            f"echo 'Cible : {qtarget}'; "
            "echo; "
            f"exec {shlex.quote(tool)} check --readonly {qtarget}"
        )

    tool = shutil.which("fsck") or "fsck"
    return (
        "echo 'Vérification filesystem : fsck -f -C 0'; "
        f"echo 'Cible : {qtarget}'; "
        "echo; "
        f"exec {shlex.quote(tool)} -f -C 0 {qtarget}"
    )


def start_disk_check_tmux(conf: Dict[str, str], target: str, mode: str) -> Tuple[bool, str, str, bool]:
    if not shutil.which("tmux"):
        return False, "", "tmux introuvable. Installe le paquet tmux.", False

    session = disk_check_session_name(target)
    if tmux_session_exists(session):
        return True, session, f"Session tmux déjà active : {session}", False

    command = disk_check_command(conf, target, mode)
    script = f"""
set -Eeuo pipefail
clear
echo "============================================================"
echo " Vérification disque Flask System"
echo " Session tmux : {session}"
echo " Démarrage    : $(date '+%F %T')"
echo "============================================================"
echo
{command}
rc=$?
echo
echo "============================================================"
echo " Fin vérification : $(date '+%F %T')"
echo " Code retour      : $rc"
echo " La session tmux va se fermer automatiquement dans 20 secondes."
echo "============================================================"
sleep 20
exit "$rc"
"""
    rc, out = run_cmd(["tmux", "new-session", "-d", "-s", session, "/bin/bash", "-lc", script], timeout=10)
    if rc != 0:
        return False, session, out or "Création session tmux impossible.", False
    return True, session, f"Session tmux créée : {session}", True



def make_disk_format_script(target: str, fstype: str, label: str, cmd: List[str], session: str) -> str:
    command = " ".join(shlex.quote(str(part)) for part in cmd)
    qtarget = shlex.quote(target)
    qfstype = shlex.quote(fstype)
    qlabel = shlex.quote(label) if label else "''"
    return f"""
set -Eeuo pipefail
clear
echo "============================================================"
echo " Formatage disque Flask System"
echo " Session tmux : {session}"
echo " Cible        : {target}"
echo " Format       : {fstype}"
echo " Label        : {label or '-'}"
echo " Démarrage    : $(date '+%F %T')"
echo "============================================================"
echo
echo "Commande : {command}"
echo
{command}
rc=$?
echo
if command -v udevadm >/dev/null 2>&1; then
  echo ">>> udevadm settle"
  udevadm settle || true
fi
if command -v partprobe >/dev/null 2>&1; then
  echo ">>> partprobe {qtarget}"
  partprobe {qtarget} || true
fi
echo
echo "============================================================"
echo " Fin formatage : $(date '+%F %T')"
echo " Code retour   : $rc"
echo " La session tmux va se fermer automatiquement dans 20 secondes."
echo "============================================================"
sleep 20
exit "$rc"
"""


def start_disk_format_tmux(conf: Dict[str, str], target: str, fstype: str, label: str, cmd: List[str]) -> Tuple[bool, str, str, bool]:
    if not shutil.which("tmux"):
        return False, "", "tmux introuvable. Installe le paquet tmux.", False

    check_session = disk_check_session_name(target)
    if tmux_session_exists(check_session):
        return False, check_session, f"Vérification déjà active pour ce volume : {check_session}", False

    session = disk_format_session_name(target)
    if tmux_session_exists(session):
        return True, session, f"Session tmux déjà active : {session}", False

    script = make_disk_format_script(target, fstype, label, cmd, session)
    rc, out = run_cmd(["tmux", "new-session", "-d", "-s", session, "/bin/bash", "-lc", script], timeout=10)
    if rc != 0:
        return False, session, out or "Création session tmux impossible.", False
    return True, session, f"Session tmux créée : {session}", True


def start_ttyd_for_tmux_session(session: str) -> Tuple[bool, str, int, str]:
    conf = terminal_module_conf()
    port = disk_check_port(session, conf)
    pid_path = disk_check_pid_file(session)
    pid = read_pid_file(pid_path)
    if process_is_running(pid):
        return True, check_terminal_url(port), port, f"Terminal déjà actif sur le port {port}."

    ttyd_bin = ttyd_binary_from_terminal_conf(conf)
    if not os.path.isfile(ttyd_bin):
        return False, "", port, f"Binaire ttyd introuvable : {ttyd_bin}"

    try:
        os.chmod(ttyd_bin, 0o755)
    except OSError:
        pass

    listen = strip_quotes(str(conf.get("TERMINAL_LISTEN") or "0.0.0.0").strip()) or "0.0.0.0"
    base_path = ""
    try:
        import terminal as yoleo_terminal
        base_path = yoleo_terminal.ttyd_base_path_for_port(conf, int(port))
    except Exception:
        base_path = ""
    log_file = terminal_resolve_path(conf.get("TERMINAL_LOG_FILE", "/var/log/terminal/terminal.log"))
    os.makedirs(os.path.dirname(log_file) or "/tmp", exist_ok=True)
    os.makedirs(os.path.dirname(pid_path) or "/tmp", exist_ok=True)

    attach_cmd = f"tmux attach-session -t {shlex.quote(session)}"
    cmd = [
        ttyd_bin,
        "-p", str(port),
        "-i", listen,
        "-W",
        "-t", "disableLeaveAlert=true",
        *terminal_theme_args(conf),
        "-t", f"titleFixed=Disk maintenance {session}",
        "-m", "2",
    ]
    if base_path:
        cmd.extend(["-b", base_path])
    cmd.extend(["/bin/sh", "-lc", attach_cmd])

    with open(log_file, "a", encoding="utf-8", errors="replace") as log:
        log.write("\n\n=== Démarrage terminal maintenance disque ===\n")
        log.write("Session tmux: " + session + "\n")
        log.write("Commande: " + " ".join(shlex.quote(x) for x in cmd) + "\n")
        log.flush()
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    with open(pid_path, "w", encoding="utf-8") as handle:
        handle.write(str(proc.pid))
    time.sleep(0.25)
    if not process_is_running(proc.pid):
        return False, "", port, f"ttyd s'est arrêté immédiatement. Log : {log_file}"
    return True, check_terminal_url(port), port, f"Terminal lancé sur le port {port}."


@disk_bp.route("/disk/api/action/check/start", methods=["POST"])
def disk_check_start_api():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    ok, target, error = require_safe_target(conf, str(payload.get("target") or ""), allow_disk=True, allow_part=True)
    if not ok:
        return json_response(False, error)

    if mount_paths_for_target(conf, target):
        return json_response(False, "Vérification refusée : démonte d'abord ce volume.")

    mode = str(payload.get("mode") or "fsck").strip().lower()
    if mode not in {"fsck", "surface", "badblocks"}:
        mode = "fsck"

    ok_tmux, session, tmux_msg, created = start_disk_check_tmux(conf, target, mode)
    if not ok_tmux:
        return json_response(False, tmux_msg)

    ok_ttyd, url, port, ttyd_msg = start_ttyd_for_tmux_session(session)
    if not ok_ttyd:
        return json_response(False, tmux_msg + "\n" + ttyd_msg, session=session)

    return jsonify({
        "ok": True,
        "message": tmux_msg + "\n" + ttyd_msg,
        "session": session,
        "created": created,
        "terminal_url": url,
        "terminal_port": port,
        "mode": mode,
    })


@disk_bp.route("/disk/api/action/tmux/view", methods=["POST"])
def disk_tmux_view_api():
    payload = request.get_json(silent=True) or {}
    conf = get_config()
    ok, target, error = require_safe_target(conf, str(payload.get("target") or ""), allow_disk=True, allow_part=True)
    if not ok:
        return json_response(False, error)

    wanted_session = str(payload.get("session") or "").strip()
    sessions = disk_maintenance_sessions_for_target(target)
    if wanted_session:
        allowed = {item.get("session") for item in sessions}
        if wanted_session not in allowed:
            return json_response(False, "Cette session tmux n'est plus active pour ce volume.")
        session = wanted_session
        kind = next((item.get("kind", "") for item in sessions if item.get("session") == session), "")
    elif sessions:
        session = sessions[0]["session"]
        kind = sessions[0].get("kind", "")
    else:
        return json_response(False, "Aucune session tmux active pour ce volume.")

    ok_ttyd, url, port, ttyd_msg = start_ttyd_for_tmux_session(session)
    if not ok_ttyd:
        return json_response(False, ttyd_msg, session=session)
    return jsonify({
        "ok": True,
        "message": ttyd_msg,
        "session": session,
        "terminal_url": url,
        "terminal_port": port,
        "mode": kind,
    })


@disk_bp.route("/disk/api/dirs")
def disk_dirs_api():
    conf = get_config()
    raw_path = str(request.args.get("path") or "/mnt")
    path_ok, path, error = safe_real_mount_path(conf, raw_path)
    if not path_ok:
        # Pour le navigateur, on retombe sur la première racine autorisée au lieu de planter.
        path = allowed_mount_prefixes(conf)[0]
    if not os.path.exists(path):
        path = allowed_mount_prefixes(conf)[0]
    if not os.path.isdir(path):
        path = os.path.dirname(path) or allowed_mount_prefixes(conf)[0]

    entries: List[Dict[str, str]] = []
    try:
        for name in sorted(os.listdir(path), key=lambda s: s.lower()):
            full = os.path.join(path, name)
            if os.path.isdir(full) and not os.path.islink(full):
                entries.append({"name": name, "path": full})
    except Exception as exc:
        return json_response(False, f"Lecture dossier impossible : {exc}")

    parent = os.path.dirname(path.rstrip("/")) or "/"
    prefixes = allowed_mount_prefixes(conf)
    can_parent = any(parent == p or parent.startswith(p + "/") for p in prefixes)
    return jsonify({"ok": True, "path": path, "parent": parent if can_parent else "", "entries": entries, "allowed_prefixes": prefixes})

# ---------------------------------------------------------------------------
# Onglets MargeFS / Veille / Logs
# ---------------------------------------------------------------------------
