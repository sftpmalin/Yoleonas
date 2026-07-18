# -*- coding: utf-8 -*-
"""Virtual Disk - montage ponctuel de disques VM via qemu-nbd.

Objectif : permettre de choisir un fichier disque virtuel (qcow2/raw/img/vdi/...),
de l'exposer sur un /dev/nbdX libre, puis de monter toutes ses partitions
montables dans des sous-dossiers du point de montage choisi. Le bouton croix
de l'UI démonte uniquement : il ne supprime jamais le fichier disque virtuel.
"""
from __future__ import annotations


VIRTUAL_DISK_STATE_VERSION = 1
VIRTUAL_DISK_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
VIRTUAL_DISK_BLOCK_RE = re.compile(r"^/dev/nbd\d+(p\d+)?$")


def virtualdisk_base_path(conf):
    raw = str(conf.get("VIRTUAL_DISK_BASE_PATH") or "/mnt/virtual_disk").strip() or "/mnt/virtual_disk"
    return os.path.normpath(os.path.expanduser(os.path.expandvars(raw)))


def virtualdisk_state_file(conf):
    raw = str(conf.get("VIRTUAL_DISK_STATE_FILE") or "/var/lib/yoleo/disk/virtual_disks.json").strip()
    return os.path.abspath(os.path.expanduser(os.path.expandvars(raw)))


def virtualdisk_allowed_extensions(conf):
    raw = str(conf.get("VIRTUAL_DISK_ALLOWED_EXTENSIONS") or ".qcow2,.qcow,.qed,.raw,.img,.vdi,.vmdk,.vhd,.vhdx")
    out = []
    for item in raw.split(","):
        ext = item.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        out.append(ext)
    return out or [".qcow2", ".raw", ".img"]


def virtualdisk_default_mount_options(conf, read_only=False):
    raw = str(conf.get("VIRTUAL_DISK_DEFAULT_OPTIONS") or "nosuid,nodev,noatime").strip()
    parts = ["ro" if read_only else "rw"]
    for item in raw.split(","):
        opt = item.strip()
        if not opt or opt in {"ro", "rw"}:
            continue
        parts.append(opt)
    return ",".join(parts)


def virtualdisk_safe_name(value):
    base = os.path.basename(str(value or "").strip())
    if "." in base:
        base = os.path.splitext(base)[0]
    base = VIRTUAL_DISK_ID_RE.sub("_", base).strip("._-")
    return base[:64] or "virtual_disk"


def virtualdisk_default_mountpoint(conf, image_path=""):
    return os.path.join(virtualdisk_base_path(conf), virtualdisk_safe_name(image_path or "disk"))


def virtualdisk_mountpoint_is_safe(conf, path, image_path=""):
    raw = str(path or "").strip()
    if not raw:
        raw = virtualdisk_default_mountpoint(conf, image_path)
    ok, resolved, err = safe_real_mount_path(conf, raw)
    if not ok:
        return False, resolved, err
    norm = os.path.normpath(resolved)
    forbidden = {"/mnt", "/media", "/srv", "/data", virtualdisk_base_path(conf).rstrip("/")}
    if norm in forbidden:
        return False, norm, "Choisis un sous-dossier de montage, pas la racine elle-même."
    if VIRTUAL_DISK_BLOCK_RE.match(norm):
        return False, norm, "Le point de montage ne doit pas être un périphérique bloc."
    return True, norm, ""


def virtualdisk_resolve_image(conf, image_path):
    raw = str(image_path or "").strip()
    if not raw:
        return False, "", "Choisis un fichier disque virtuel."
    path = os.path.abspath(os.path.expanduser(os.path.expandvars(raw)))
    if not os.path.isfile(path):
        return False, path, "Fichier disque virtuel introuvable."
    ext = os.path.splitext(path)[1].lower()
    if ext not in virtualdisk_allowed_extensions(conf):
        return False, path, "Extension refusée. Formats autorisés : " + ", ".join(virtualdisk_allowed_extensions(conf))
    return True, path, ""


def virtualdisk_row_id(image_path, mountpoint):
    seed = f"{image_path}|{mountpoint}"
    try:
        import hashlib
        digest = hashlib.sha1(seed.encode("utf-8", "replace")).hexdigest()[:12]
    except Exception:
        digest = VIRTUAL_DISK_ID_RE.sub("_", seed)[-12:]
    return "vdisk-" + digest


def virtualdisk_is_mountpoint(path):
    path = os.path.normpath(str(path or ""))
    if not path or path == ".":
        return False
    try:
        if os.path.ismount(path):
            return True
    except Exception:
        pass
    if shutil.which("mountpoint"):
        rc, _out = run_cmd(["mountpoint", "-q", path], timeout=5)
        return rc == 0
    return False



def virtualdisk_format_bytes(value):
    try:
        size = float(value or 0)
    except Exception:
        size = 0.0
    units = ["o", "Kio", "Mio", "Gio", "Tio", "Pio"]
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    if size >= 100:
        return f"{size:.0f} {units[idx]}"
    if size >= 10:
        return f"{size:.1f} {units[idx]}"
    return f"{size:.2f} {units[idx]}"


def virtualdisk_mount_usage(mountpoint):
    mountpoint = str(mountpoint or "")
    empty = {
        "total_bytes": 0,
        "used_bytes": 0,
        "free_bytes": 0,
        "total_human": "—",
        "used_human": "—",
        "free_human": "—",
        "use_percent": None,
    }
    if not mountpoint:
        return empty
    try:
        usage = shutil.disk_usage(mountpoint)
    except Exception:
        return empty
    total = int(getattr(usage, "total", 0) or 0)
    used = int(getattr(usage, "used", 0) or 0)
    free = int(getattr(usage, "free", 0) or 0)
    pct = round((used * 100.0 / total), 1) if total > 0 else None
    return {
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "total_human": virtualdisk_format_bytes(total),
        "used_human": virtualdisk_format_bytes(used),
        "free_human": virtualdisk_format_bytes(free),
        "use_percent": pct,
    }

def virtualdisk_read_state(conf):
    path = virtualdisk_state_file(conf)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            data = json.load(handle)
    except Exception:
        return {"version": VIRTUAL_DISK_STATE_VERSION, "mounts": []}
    if not isinstance(data, dict):
        return {"version": VIRTUAL_DISK_STATE_VERSION, "mounts": []}
    mounts = data.get("mounts")
    if not isinstance(mounts, list):
        mounts = []
    return {"version": data.get("version") or VIRTUAL_DISK_STATE_VERSION, "mounts": mounts}


def virtualdisk_write_state(conf, mounts):
    path = virtualdisk_state_file(conf)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"version": VIRTUAL_DISK_STATE_VERSION, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "mounts": mounts}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass


def virtualdisk_nbd_from_partition(source):
    source = str(source or "")
    m = re.match(r"^(/dev/nbd\d+)p\d+$", source)
    if m:
        return m.group(1)
    if re.match(r"^/dev/nbd\d+$", source):
        return source
    return ""


def virtualdisk_block_mounted(source, conf=None):
    source = str(source or "")
    if not source:
        return False
    conf = conf or get_config()
    try:
        real = os.path.realpath(source)
        src_name = os.path.basename(real)
    except Exception:
        src_name = os.path.basename(source)
    nbd = virtualdisk_nbd_from_partition(source)
    names = {src_name}
    if nbd:
        names.add(os.path.basename(nbd))
    for mounts in read_mountinfo(conf).values():
        for item in mounts:
            src = str(item.get("source") or "")
            if src == source or os.path.basename(src) in names:
                return True
    return False


def virtualdisk_refresh_row(conf, row):
    item = dict(row or {})
    image = str(item.get("image_path") or "")
    mountpoint = str(item.get("mountpoint") or "")
    nbd = str(item.get("nbd") or "")
    source = str(item.get("source") or "")
    mounted = bool(mountpoint and virtualdisk_is_mountpoint(mountpoint))
    source_mounted = bool(source and virtualdisk_block_mounted(source, conf))
    item["mounted"] = mounted
    item["source_mounted"] = source_mounted
    item["image_exists"] = os.path.isfile(image)
    item["name"] = item.get("name") or virtualdisk_safe_name(image)
    item["id"] = item.get("id") or virtualdisk_row_id(image, mountpoint)
    item["status_label"] = "monté" if mounted else ("source active" if source_mounted else "arrêté")
    item["nbd"] = nbd
    item.update(virtualdisk_mount_usage(mountpoint) if mounted else virtualdisk_mount_usage(""))
    return item


def virtualdisk_try_rmdir(path, outputs=None):
    """Nettoie un dossier vide uniquement.

    Règle volontaire : simple rmdir, jamais de rm -rf. Si le dossier n'est pas
    vide ou n'existe pas, on ignore silencieusement.
    """
    path = os.path.normpath(str(path or ""))
    if not path or path in {"/", "."}:
        return False
    try:
        os.rmdir(path)
        if outputs is not None:
            outputs.append("$ rmdir " + shlex.quote(path))
        return True
    except OSError:
        return False


def virtualdisk_row_is_stale(conf, row):
    """Détermine si une ligne JSON est seulement un reste d'ancien boot.

    Après redémarrage complet de la machine, qemu-nbd et les montages ont
    disparu, mais le fichier d'état JSON peut encore contenir les anciennes
    lignes. Le tableau ne doit pas les afficher comme profils arrêtés : ce
    module est un outil de montage ponctuel, pas une liste d'autostart.
    """
    if not isinstance(row, dict):
        return True
    if row.get("mounted") or row.get("source_mounted"):
        return False
    mountpoint = str(row.get("mountpoint") or "")
    source = str(row.get("source") or "")
    nbd = str(row.get("nbd") or virtualdisk_nbd_from_partition(source))
    if mountpoint and virtualdisk_is_mountpoint(mountpoint):
        return False
    if source and virtualdisk_block_mounted(source, conf):
        return False
    if nbd and virtualdisk_block_mounted(nbd, conf):
        return False
    return True


def virtualdisk_cleanup_stale_rows(conf, rows):
    """Supprime de l'état les lignes non montées et nettoie les dossiers vides.

    Ça corrige le cas visible après reboot complet : le JSON se rechargeait et
    montrait encore /dev/nbd0p1 en "arrêté", alors que Linux avait bien tout
    démonté.
    """
    kept = []
    changed = False
    for row in rows:
        if virtualdisk_row_is_stale(conf, row):
            changed = True
            virtualdisk_try_rmdir(row.get("mountpoint"))
            base = str(row.get("base_mountpoint") or "")
            mp = str(row.get("mountpoint") or "")
            if base and base != mp:
                virtualdisk_try_rmdir(base)
            continue
        kept.append(row)
    if changed:
        virtualdisk_save_rows(conf, kept)
    return kept


def virtualdisk_rows(conf, cleanup=True):
    state = virtualdisk_read_state(conf)
    rows = [virtualdisk_refresh_row(conf, row) for row in state.get("mounts", []) if isinstance(row, dict)]
    if cleanup:
        rows = virtualdisk_cleanup_stale_rows(conf, rows)
    rows.sort(key=lambda r: (str(r.get("mountpoint") or ""), str(r.get("image_path") or "")))
    return rows


def virtualdisk_save_rows(conf, rows):
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = {k: row.get(k) for k in [
            "id", "name", "image_path", "base_mountpoint", "mountpoint", "nbd", "source",
            "partition", "read_only", "mounted_at", "mount_options", "fstype"
        ] if row.get(k) not in {None, ""}}
        if item:
            clean.append(item)
    virtualdisk_write_state(conf, clean)


def virtualdisk_image_busy_processes(image_path):
    image_path = os.path.abspath(str(image_path or ""))
    if not image_path:
        return []
    busy = []
    if shutil.which("fuser"):
        rc, out = run_cmd(["fuser", image_path], timeout=8)
        if rc == 0 and out.strip():
            busy.append("fuser: " + out.strip())
    proc_root = "/proc"
    try:
        for pid in os.listdir(proc_root):
            if not pid.isdigit():
                continue
            cmdline_path = os.path.join(proc_root, pid, "cmdline")
            try:
                raw = open(cmdline_path, "rb").read().replace(b"\x00", b" ").decode("utf-8", "replace")
            except Exception:
                continue
            if image_path in raw:
                comm = read_text(os.path.join(proc_root, pid, "comm")) or "process"
                busy.append(f"PID {pid} {comm}")
    except Exception:
        pass
    return sorted(set(busy))


def virtualdisk_running_vm_users(image_path):
    image_path = os.path.abspath(str(image_path or ""))
    if not image_path or not shutil.which("virsh"):
        return []
    users = []
    rc, out = run_cmd(["virsh", "list", "--state-running", "--name"], timeout=10)
    if rc != 0:
        return users
    for domain in [line.strip() for line in (out or "").splitlines() if line.strip()]:
        rc_blk, out_blk = run_cmd(["virsh", "domblklist", domain, "--details"], timeout=10)
        if rc_blk != 0:
            continue
        for line in (out_blk or "").splitlines():
            if image_path in line or os.path.realpath(image_path) in line:
                users.append(domain)
                break
    return sorted(set(users))


def virtualdisk_refuse_if_busy(conf, image_path):
    rows = virtualdisk_rows(conf)
    for row in rows:
        if os.path.abspath(str(row.get("image_path") or "")) == image_path and row.get("mounted"):
            return False, f"Ce disque virtuel est déjà monté sur {row.get('mountpoint')}."
    vm_users = virtualdisk_running_vm_users(image_path)
    if vm_users:
        return False, "Disque utilisé par une VM en cours : " + ", ".join(vm_users)
    busy = virtualdisk_image_busy_processes(image_path)
    if busy:
        return False, "Fichier disque occupé : " + " ; ".join(busy[:4])
    return True, ""


def virtualdisk_mountpoint_available(mountpoint):
    if virtualdisk_is_mountpoint(mountpoint):
        return False, "Ce point de montage est déjà monté."
    if os.path.exists(mountpoint):
        if not os.path.isdir(mountpoint):
            return False, "Le point de montage existe mais n'est pas un dossier."
        try:
            entries = [x for x in os.listdir(mountpoint) if x not in {".", ".."}]
        except Exception as exc:
            return False, f"Lecture du point de montage impossible : {exc}"
        if entries:
            return False, "Le dossier de montage existe déjà et n'est pas vide. Choisis un dossier vide."
    return True, ""


def virtualdisk_modprobe_nbd():
    if os.path.exists("/sys/module/nbd"):
        return True, ""
    if not shutil.which("modprobe"):
        return False, "modprobe absent : impossible de charger le module nbd."
    rc, out = run_cmd(["modprobe", "nbd", "max_part=16"], timeout=15)
    text = "$ modprobe nbd max_part=16"
    if out:
        text += "\n" + out.strip()
    if rc != 0:
        return False, text
    return True, text


def virtualdisk_part_no(path):
    m = re.search(r"p(\d+)$", str(path or ""))
    return int(m.group(1)) if m else 0


def virtualdisk_nbd_partitions_sysfs(nbd):
    name = os.path.basename(str(nbd or ""))
    sys_block = os.path.join("/sys/block", name)
    parts = []
    try:
        for child in os.listdir(sys_block):
            if re.fullmatch(re.escape(name) + r"p\d+", child):
                parts.append("/dev/" + child)
    except Exception:
        pass
    return parts


def virtualdisk_nbd_partitions_lsblk(nbd):
    """Retourne les partitions vues par lsblk, en complément de /sys/block.

    Sur certains hôtes, juste après `qemu-nbd --connect`, /sys/block/nbdXp1
    apparaît avant /sys/block/nbdXp2/p3. Si on s'arrête au premier résultat,
    le module monte seulement la première partition. On croise donc sysfs et
    lsblk, puis on attend une liste stable avant de décider.
    """
    if not shutil.which("lsblk"):
        return []
    name = os.path.basename(str(nbd or ""))
    if not re.fullmatch(r"nbd\d+", name or ""):
        return []
    rc, out = run_cmd(["lsblk", "-rn", "-o", "NAME,TYPE", "/dev/" + name], timeout=8)
    if rc != 0:
        return []
    parts = []
    for raw in (out or "").splitlines():
        cols = raw.split()
        if len(cols) < 2:
            continue
        dev_name, dev_type = cols[0], cols[1]
        dev_name = dev_name.replace("├─", "").replace("└─", "").strip()
        if dev_type == "part" and re.fullmatch(re.escape(name) + r"p\d+", dev_name):
            parts.append("/dev/" + dev_name)
    return parts


def virtualdisk_nbd_partitions(nbd):
    parts = set(virtualdisk_nbd_partitions_sysfs(nbd))
    parts.update(virtualdisk_nbd_partitions_lsblk(nbd))
    return sorted(parts, key=virtualdisk_part_no)


def virtualdisk_refresh_partition_table(nbd):
    """Force/attend la relecture de table de partitions NBD.

    Ce n'est pas bloquant si un outil est absent : le module reste portable,
    mais quand `partprobe`, `partx` ou `blockdev` existent, ils aident beaucoup
    à éviter le cas aléatoire où seule p1 est visible au premier poll.
    """
    outputs = []
    if shutil.which("udevadm"):
        run_cmd(["udevadm", "settle"], timeout=10)
    if shutil.which("blockdev"):
        rc, out = run_cmd(["blockdev", "--rereadpt", nbd], timeout=10)
        if out and rc != 0:
            outputs.append(out.strip())
    if shutil.which("partprobe"):
        rc, out = run_cmd(["partprobe", nbd], timeout=10)
        if out and rc != 0:
            outputs.append(out.strip())
    if shutil.which("partx"):
        rc, out = run_cmd(["partx", "--update", nbd], timeout=10)
        if out and rc != 0:
            outputs.append(out.strip())
    if shutil.which("udevadm"):
        run_cmd(["udevadm", "settle"], timeout=10)
    return "\n".join(outputs)


def virtualdisk_wait_nbd_partitions_stable(nbd, timeout=10.0):
    """Attend que la liste des partitions NBD soit stable avant de monter.

    Ancien comportement : dès que p1 apparaissait, on partait au montage.
    Nouveau comportement : on continue à sonder pendant quelques secondes et on
    exige plusieurs lectures identiques, afin que p2/p3 aient le temps
    d'apparaître. Ça corrige le montage aléatoire 1 partition puis 2 partitions
    après démontage/remontage.
    """
    deadline = time.time() + float(timeout)
    last = []
    stable_hits = 0
    best = []
    while time.time() < deadline:
        current = virtualdisk_nbd_partitions(nbd)
        if len(current) > len(best):
            best = current[:]
        if current and current == last:
            stable_hits += 1
        else:
            stable_hits = 0
            last = current[:]
        # 4 lectures identiques espacées de 250 ms ~= 1 s de stabilité.
        if current and stable_hits >= 4:
            return current
        time.sleep(0.25)
    return best or last


def virtualdisk_nbd_is_free(nbd, conf):
    name = os.path.basename(str(nbd or ""))
    if not re.fullmatch(r"nbd\d+", name or ""):
        return False
    if virtualdisk_block_mounted("/dev/" + name, conf):
        return False
    for part in virtualdisk_nbd_partitions("/dev/" + name):
        if virtualdisk_block_mounted(part, conf):
            return False
    size_path = os.path.join("/sys/block", name, "size")
    try:
        size = int(read_text(size_path) or "0")
    except Exception:
        size = 0
    return size == 0


def virtualdisk_find_free_nbd(conf):
    ok, msg = virtualdisk_modprobe_nbd()
    if not ok:
        return False, "", msg
    candidates = []
    try:
        for name in os.listdir("/sys/block"):
            if re.fullmatch(r"nbd\d+", name):
                candidates.append("/dev/" + name)
    except Exception:
        candidates = []
    candidates.sort(key=lambda p: int(re.search(r"\d+", p).group(0)) if re.search(r"\d+", p) else 0)
    for nbd in candidates:
        if virtualdisk_nbd_is_free(nbd, conf):
            return True, nbd, msg
    return False, "", (msg + "\n" if msg else "") + "Aucun /dev/nbdX libre."


def virtualdisk_probe_block(target):
    """Sonde un bloc NBD sans réutiliser les garde-fous des vrais disques hôte.

    `parse_blkid_export()` est volontairement limité aux périphériques physiques
    de Maintenance (/dev/sdX, /dev/nvmeX, /dev/mdX...). Un disque virtuel exposé
    par qemu-nbd arrive en /dev/nbd0p1, /dev/nbd0p2, etc. Si on passe par la
    fonction Maintenance, elle renvoie donc `{}` avant même d'appeler blkid, ce
    qui faisait croire à tort que toutes les partitions du disque virtuel étaient
    sans signature filesystem.
    """
    target = str(target or "")
    if not VIRTUAL_DISK_BLOCK_RE.match(target):
        return {}
    blkid_bin = shutil.which("blkid") or "blkid"
    rc, out = run_cmd([blkid_bin, "-p", "-o", "export", target], timeout=8)
    if rc != 0:
        return {}
    data = {}
    for raw in (out or "").splitlines():
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key and value:
            data[key] = value
    return data


def virtualdisk_partition_label(source, nbd):
    source = str(source or "")
    nbd = str(nbd or "")
    m = re.search(r"p(\d+)$", source)
    if m:
        return "p" + m.group(1)
    if source == nbd:
        return "disk"
    safe = VIRTUAL_DISK_ID_RE.sub("_", os.path.basename(source)).strip("._-")
    return safe or "partition"


def virtualdisk_select_mount_sources(conf, nbd):
    refresh_msg = virtualdisk_refresh_partition_table(nbd)
    partitions = virtualdisk_wait_nbd_partitions_stable(nbd, timeout=10.0)

    # Si le disque a une table de partitions, on monte les partitions, pas le
    # disque entier. Si c'est une image raw avec un filesystem direct, on monte
    # /dev/nbdX dans le sous-dossier "disk".
    candidates = partitions or [nbd]
    skipped = []
    sources = []
    if refresh_msg:
        skipped.append(refresh_msg)
    for source in candidates:
        meta = virtualdisk_probe_block(source)
        fstype = str(meta.get("type") or "").lower()
        if not fstype:
            skipped.append(f"{source}: aucune signature filesystem")
            continue
        if fstype in {"swap", "linux_raid_member", "lvm2_member", "crypto_luks", "zfs_member"}:
            skipped.append(f"{source}: {fstype} non monté automatiquement")
            continue
        sources.append({
            "source": source,
            "fstype": fstype,
            "partition": virtualdisk_partition_label(source, nbd),
        })
    if sources:
        return True, sources, ""
    return False, [], "Aucune partition montable trouvée. " + " ; ".join(skipped[:8])


def virtualdisk_connect_image(conf, image_path, read_only=False):
    ok_nbd, nbd, nbd_msg = virtualdisk_find_free_nbd(conf)
    if not ok_nbd:
        return False, "", nbd_msg
    qemu_nbd = shutil.which("qemu-nbd") or shutil.which("qemu-nbd-static")
    if not qemu_nbd:
        return False, "", "qemu-nbd absent. Installe qemu-utils pour monter des disques virtuels."
    cmd = [qemu_nbd]
    if read_only:
        cmd.append("--read-only")
    cmd.extend(["--connect", nbd, image_path])
    rc, out = run_cmd(cmd, timeout=30)
    text = "$ " + " ".join(shlex.quote(x) for x in cmd)
    if out:
        text += "\n" + out.strip()
    if nbd_msg:
        text = nbd_msg + "\n" + text
    if rc != 0:
        return False, "", text
    return True, nbd, text


def virtualdisk_disconnect_nbd(nbd):
    nbd = str(nbd or "")
    if not re.fullmatch(r"/dev/nbd\d+", nbd):
        return True, ""
    qemu_nbd = shutil.which("qemu-nbd") or shutil.which("qemu-nbd-static")
    if not qemu_nbd:
        return False, "qemu-nbd absent : déconnexion NBD impossible."
    rc, out = run_cmd([qemu_nbd, "--disconnect", nbd], timeout=20)
    text = "$ " + " ".join(shlex.quote(x) for x in [qemu_nbd, "--disconnect", nbd])
    if out:
        text += "\n" + out.strip()
    return rc == 0, text


def virtualdisk_mount_image(conf, image_path, mountpoint, read_only=False):
    outputs = []
    ok_busy, busy_msg = virtualdisk_refuse_if_busy(conf, image_path)
    if not ok_busy:
        return False, busy_msg, ""
    ok_mp, msg_mp = virtualdisk_mountpoint_available(mountpoint)
    if not ok_mp:
        return False, msg_mp, ""
    try:
        os.makedirs(mountpoint, exist_ok=True)
    except Exception as exc:
        return False, f"Création du dossier impossible : {exc}", ""

    ok_connect, nbd, msg_connect = virtualdisk_connect_image(conf, image_path, read_only=read_only)
    outputs.append(msg_connect)
    if not ok_connect:
        try:
            os.rmdir(mountpoint)
        except OSError:
            pass
        return False, "Connexion qemu-nbd impossible.", "\n".join(outputs)[-8000:]

    ok_sources, sources, msg_sources = virtualdisk_select_mount_sources(conf, nbd)
    if not ok_sources:
        ok_disc, msg_disc = virtualdisk_disconnect_nbd(nbd)
        outputs.extend([msg_sources, msg_disc])
        try:
            os.rmdir(mountpoint)
        except OSError:
            pass
        return False, "Aucune partition montable dans ce disque virtuel.", "\n".join([x for x in outputs if x])[-8000:]

    options = virtualdisk_default_mount_options(conf, read_only=read_only)
    mounted_rows = []
    created_dirs = []
    image_name = virtualdisk_safe_name(image_path)
    mounted_at = time.strftime("%Y-%m-%d %H:%M:%S")

    for entry in sources:
        source = str(entry.get("source") or "")
        fstype = str(entry.get("fstype") or "")
        partition = str(entry.get("partition") or virtualdisk_partition_label(source, nbd))
        part_mountpoint = os.path.join(mountpoint, partition)
        try:
            os.makedirs(part_mountpoint, exist_ok=True)
            created_dirs.append(part_mountpoint)
        except Exception as exc:
            outputs.append(f"Création du sous-dossier impossible {part_mountpoint}: {exc}")
            break

        cmd = ["mount", "-o", options, source, part_mountpoint]
        rc, out = run_cmd(cmd, timeout=30)
        text = "$ " + " ".join(shlex.quote(x) for x in cmd)
        if out:
            text += "\n" + out.strip()
        outputs.append(text)
        if rc != 0 or not virtualdisk_is_mountpoint(part_mountpoint):
            outputs.append(f"Échec montage {source} sur {part_mountpoint}")
            break

        mounted_rows.append({
            "id": virtualdisk_row_id(image_path, part_mountpoint),
            "name": f"{image_name}/{partition}",
            "image_path": image_path,
            "base_mountpoint": mountpoint,
            "mountpoint": part_mountpoint,
            "nbd": nbd,
            "source": source,
            "partition": partition,
            "read_only": bool(read_only),
            "mounted_at": mounted_at,
            "mount_options": options,
            "fstype": fstype,
        })

    if len(mounted_rows) != len(sources):
        remaining_rows = []
        for row in reversed(mounted_rows):
            mp = str(row.get("mountpoint") or "")
            if mp and virtualdisk_is_mountpoint(mp):
                rc_um, out_um = run_cmd(["umount", mp], timeout=30)
                text = "$ umount " + shlex.quote(mp)
                if out_um:
                    text += "\n" + out_um.strip()
                outputs.append(text)
                if rc_um != 0 or virtualdisk_is_mountpoint(mp):
                    remaining_rows.append(row)

        if remaining_rows:
            remaining_mountpoints = {str(row.get("mountpoint") or "") for row in remaining_rows}
            for path in reversed(created_dirs):
                if path in remaining_mountpoints:
                    continue
                try:
                    os.rmdir(path)
                except OSError:
                    pass
            existing = virtualdisk_rows(conf)
            remaining_ids = {str(row.get("id") or "") for row in remaining_rows}
            rows = [r for r in existing if str(r.get("id") or "") not in remaining_ids]
            rows.extend(virtualdisk_refresh_row(conf, row) for row in remaining_rows)
            virtualdisk_save_rows(conf, rows)
            outputs.append(f"NBD conservé : {len(remaining_rows)} partition(s) encore montée(s).")
            return False, "Montage partiel nettoyé incomplet : certaines partitions restent montées.", "\n".join([x for x in outputs if x])[-8000:]

        ok_disc, msg_disc = virtualdisk_disconnect_nbd(nbd)
        if msg_disc:
            outputs.append(msg_disc)
        if not ok_disc:
            return False, "Montage partiel nettoyé, mais déconnexion NBD impossible.", "\n".join([x for x in outputs if x])[-8000:]
        for path in reversed(created_dirs):
            try:
                os.rmdir(path)
            except OSError:
                pass
        try:
            os.rmdir(mountpoint)
        except OSError:
            pass
        return False, "Montage d'une partition impossible. Rien n'a été conservé monté.", "\n".join([x for x in outputs if x])[-8000:]

    existing = virtualdisk_rows(conf)
    new_ids = {str(r.get("id") or "") for r in mounted_rows}
    rows = [r for r in existing if str(r.get("id") or "") not in new_ids]
    rows.extend(mounted_rows)
    virtualdisk_save_rows(conf, rows)
    count = len(mounted_rows)
    return True, f"Virtual Disk monté : {count} partition(s).", "\n".join([x for x in outputs if x])[-8000:]


def virtualdisk_unmount_row(conf, row):
    item = virtualdisk_refresh_row(conf, row)
    mountpoint = str(item.get("mountpoint") or "")
    base_mountpoint = str(item.get("base_mountpoint") or "")
    nbd = str(item.get("nbd") or virtualdisk_nbd_from_partition(item.get("source") or ""))
    outputs = []
    if mountpoint and virtualdisk_is_mountpoint(mountpoint):
        rc, out = run_cmd(["umount", mountpoint], timeout=30)
        text = "$ umount " + shlex.quote(mountpoint)
        if out:
            text += "\n" + out.strip()
        outputs.append(text)
        if rc != 0:
            return False, "Démontage impossible.", "\n".join(outputs)[-8000:]
    else:
        outputs.append(f"Déjà démonté : {mountpoint}")

    if mountpoint and virtualdisk_is_mountpoint(mountpoint):
        return False, "Sécurité : le point de montage est encore actif.", "\n".join(outputs)[-8000:]

    current_rows = virtualdisk_rows(conf)
    remaining_same_nbd = []
    for other in current_rows:
        if str(other.get("id") or "") == str(item.get("id") or ""):
            continue
        if nbd and str(other.get("nbd") or virtualdisk_nbd_from_partition(other.get("source") or "")) == nbd:
            refreshed = virtualdisk_refresh_row(conf, other)
            if refreshed.get("mounted") or refreshed.get("source_mounted"):
                remaining_same_nbd.append(refreshed)

    if remaining_same_nbd:
        outputs.append(f"NBD conservé : {len(remaining_same_nbd)} autre(s) partition(s) encore montée(s).")
    else:
        ok_disc, msg_disc = virtualdisk_disconnect_nbd(nbd)
        if msg_disc:
            outputs.append(msg_disc)
        if not ok_disc:
            return False, "Démonté, mais déconnexion NBD impossible.", "\n".join(outputs)[-8000:]

    # Demande explicite : simple rmdir seulement. Pas de rm -rf. Pas d'échec bloquant.
    if mountpoint:
        virtualdisk_try_rmdir(mountpoint, outputs)
    if base_mountpoint and base_mountpoint != mountpoint:
        virtualdisk_try_rmdir(base_mountpoint, outputs)

    rows = [r for r in current_rows if str(r.get("id") or "") != str(item.get("id") or "")]
    virtualdisk_save_rows(conf, rows)
    return True, "Virtual Disk démonté.", "\n".join([x for x in outputs if x])[-8000:]


def virtualdisk_summary(conf):
    rows = virtualdisk_rows(conf)
    return rows, {
        "profiles": len(rows),
        "mounted": sum(1 for r in rows if r.get("mounted")),
        "readonly": sum(1 for r in rows if r.get("read_only")),
    }


@disk_bp.route("/disk/api/virtual_disk")
def disk_virtual_disk_api():
    conf = get_config()
    rows, summary = virtualdisk_summary(conf)
    return jsonify({
        "ok": True,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_path": virtualdisk_base_path(conf),
        "default_mountpoint": virtualdisk_default_mountpoint(conf, "disk"),
        "allowed_extensions": virtualdisk_allowed_extensions(conf),
        "summary": summary,
        "rows": rows,
    })


@disk_bp.route("/disk/api/action/virtual_disk/mount", methods=["POST"])
def disk_virtual_disk_mount_api():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    ok_image, image_path, image_error = virtualdisk_resolve_image(conf, payload.get("image_path"))
    if not ok_image:
        return json_response(False, image_error)
    ok_mp, mountpoint, mp_error = virtualdisk_mountpoint_is_safe(conf, payload.get("mountpoint"), image_path)
    if not ok_mp:
        return json_response(False, mp_error)
    read_only = bool_from_payload(payload.get("read_only"))
    ok_mount, message, output = virtualdisk_mount_image(conf, image_path, mountpoint, read_only=read_only)
    return json_response(ok_mount, message, output=output)


@disk_bp.route("/disk/api/action/virtual_disk/unmount", methods=["POST"])
def disk_virtual_disk_unmount_api():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    wanted_id = str(payload.get("id") or "").strip()
    wanted_mount = str(payload.get("mountpoint") or "").strip()
    rows = virtualdisk_rows(conf)
    row = None
    if wanted_id:
        row = next((r for r in rows if str(r.get("id") or "") == wanted_id), None)
    if row is None and wanted_mount:
        row = next((r for r in rows if str(r.get("mountpoint") or "") == wanted_mount), None)
    if row is None:
        # Idempotence volontaire : après un reboot complet, l'API/listing peut
        # avoir déjà purgé la ligne JSON obsolète avant que l'utilisateur clique
        # sur la croix affichée par un ancien rendu Ajax. Dans ce cas, ce n'est
        # pas une erreur bloquante : on confirme le nettoyage pour que l'UI se
        # rafraîchisse et que le module ne reste jamais coincé sur une ligne
        # fantôme.
        if wanted_id or wanted_mount:
            return json_response(True, "Virtual Disk déjà démonté.", output="Ligne obsolète déjà nettoyée.")
        return json_response(False, "Montage Virtual Disk introuvable.")
    ok_unmount, message, output = virtualdisk_unmount_row(conf, row)
    return json_response(ok_unmount, message, output=output)
