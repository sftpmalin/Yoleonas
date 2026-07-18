#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# PART 005 - Inventaire hôte, pools, réseaux, ISO, firmware et ttyd série



def byid_preference_score(name: str) -> Tuple[int, str]:
    """Score pour Ã©viter les doublons /dev/disk/by-id.

    Le mÃªme disque apparaÃ®t souvent deux fois : ata-... et wwn-...
    Pour l'UI on garde une seule ligne par vrai /dev/sdX, avec l'alias
    le plus parlant en prioritÃ©. Ã‡a Ã©vite de sÃ©lectionner le mÃªme disque
    deux fois dans la popup.
    """
    if name.startswith("ata-"):
        return (0, name)
    if name.startswith("nvme-"):
        return (1, name)
    if name.startswith("usb-"):
        return (2, name)
    if name.startswith("scsi-"):
        return (3, name)
    if name.startswith("wwn-"):
        return (4, name)
    if name.startswith("eui-"):
        return (5, name)
    return (9, name)


def _lsblk_value_has_mount(value: object) -> bool:
    if isinstance(value, list):
        return any(str(item or "").strip() for item in value)
    return bool(str(value or "").strip())


def _lsblk_node_has_mount(node: Dict[str, object]) -> bool:
    if _lsblk_value_has_mount(node.get("mountpoint")) or _lsblk_value_has_mount(node.get("mountpoints")):
        return True
    children = node.get("children") or []
    if isinstance(children, list):
        return any(_lsblk_node_has_mount(child) for child in children if isinstance(child, dict))
    return False


def block_device_has_mounts(real_device: str) -> bool:
    """Retourne True si le disque entier ou une de ses partitions est monté.

    Pour le passthrough d'un disque physique à une VM, on ne propose que les
    disques réellement libres. Un /dev/sdX non monté mais avec /dev/sdX1 monté
    doit donc être considéré comme indisponible.
    """
    real_device = str(real_device or "").strip()
    if not real_device:
        return True
    rc, out = run_cmd(["lsblk", "-J", "-o", "NAME,PATH,TYPE,MOUNTPOINT,MOUNTPOINTS", real_device], timeout=12)
    if rc != 0:
        # En cas de doute, on ne propose pas le disque dans la liste VM.
        return True
    try:
        data = json.loads(out or "{}")
        devices = data.get("blockdevices") or []
        if isinstance(devices, list):
            return any(_lsblk_node_has_mount(node) for node in devices if isinstance(node, dict))
    except Exception:
        return True
    return False


def list_host_byid_disks(conf: Dict[str, str]) -> List[Dict[str, str]]:
    byid = str(conf.get("HOST_DEV_BY_ID", "/dev/disk/by-id") or "/dev/disk/by-id").strip()
    by_real: Dict[str, Dict[str, str]] = {}
    aliases_by_real: Dict[str, List[str]] = {}
    try:
        names = os.listdir(byid)
    except Exception:
        return []

    allowed_prefixes = ("wwn-", "ata-", "scsi-", "nvme-", "usb-", "eui-")
    for name in sorted(names):
        if not name.startswith(allowed_prefixes) or "-part" in name:
            continue
        full = os.path.join(byid, name)
        try:
            real = os.path.realpath(full)
            if not os.path.exists(real) or not os.path.basename(real):
                continue
            base = os.path.basename(real)
            # Garde seulement les vrais disques entiers, pas les partitions.
            if not os.path.exists(os.path.join("/sys/class/block", base, "device")):
                continue
            # Pour une VM, un disque physique n'est proposable que s'il n'est
            # pas monté, ni directement ni via une de ses partitions.
            if block_device_has_mounts(real):
                continue

            size = ""
            sys_size = os.path.join("/sys/class/block", base, "size")
            try:
                with open(sys_size, "r", encoding="utf-8") as handle:
                    sectors = int(handle.read().strip())
                size = human_bytes(sectors * 512)
            except Exception:
                pass

            candidate = {"id": name, "path": full, "real": real, "size": size}
            aliases_by_real.setdefault(real, []).append(name)
            current = by_real.get(real)
            if current is None or byid_preference_score(name) < byid_preference_score(str(current.get("id", ""))):
                by_real[real] = candidate
        except Exception:
            continue

    rows = list(by_real.values())
    for row in rows:
        aliases = sorted(set(aliases_by_real.get(str(row.get("real", "")), [])), key=byid_preference_score)
        row["aliases"] = ", ".join(aliases)
        if len(aliases) > 1:
            row["alias_count"] = str(len(aliases))
    return sorted(rows, key=lambda item: (str(item.get("real", "")), byid_preference_score(str(item.get("id", "")))))


def _flatten_lsblk_nodes(nodes: object) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    if not isinstance(nodes, list):
        return rows
    for node in nodes:
        if not isinstance(node, dict):
            continue
        rows.append(node)
        rows.extend(_flatten_lsblk_nodes(node.get("children") or []))
    return rows


def _preferred_optical_path(real: str, fallback: str) -> str:
    real = os.path.realpath(str(real or "").strip())
    aliases = ["/dev/cdrom", "/dev/dvd", "/dev/bluray"]
    for alias in aliases:
        try:
            if os.path.exists(alias) and os.path.realpath(alias) == real:
                return alias
        except Exception:
            continue
    return fallback


def list_host_optical_drives(conf: Dict[str, str]) -> List[Dict[str, object]]:
    """Lecteurs CD/DVD/Blu-ray physiques utilisables comme disk device=cdrom.

    On les garde séparés des disques physiques : /dev/sr0 doit devenir un
    lecteur optique libvirt, pas un disque dur passthrough.
    """
    rc, out = run_cmd(["lsblk", "-J", "-o", "NAME,PATH,TYPE,MODEL,VENDOR,SIZE,RM,TRAN"], timeout=12)
    if rc != 0:
        return []
    try:
        data = json.loads(out or "{}")
    except Exception:
        return []

    qemu_user = ""
    try:
        qemu_user = detect_libvirt_qemu_user(conf)
    except Exception:
        qemu_user = ""

    rows: List[Dict[str, object]] = []
    seen: set = set()
    for node in _flatten_lsblk_nodes(data.get("blockdevices") or []):
        name = str(node.get("name") or "").strip()
        path = str(node.get("path") or "").strip() or (f"/dev/{name}" if name else "")
        kind = str(node.get("type") or "").strip().lower()
        if kind != "rom" and not re.fullmatch(r"sr[0-9]+", name):
            continue
        if not path or not os.path.exists(path):
            continue
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        stable_path = _preferred_optical_path(real, path)
        model = " ".join(str(x or "").strip() for x in [node.get("vendor"), node.get("model")] if str(x or "").strip())
        size = str(node.get("size") or "").strip()
        transport = str(node.get("tran") or "").strip()
        access_ok = user_can_access_path(qemu_user, stable_path, "r") if qemu_user else None
        label_bits = [stable_path, model, size, transport]
        rows.append({
            "path": stable_path,
            "real": real,
            "name": name,
            "model": model,
            "size": size,
            "transport": transport,
            "display": " - ".join(bit for bit in label_bits if bit),
            "qemu_user": qemu_user,
            "qemu_access": access_ok,
        })
    return sorted(rows, key=lambda row: str(row.get("path") or ""))


def do_vm_disk_action(conf: Dict[str, str], payload: Dict[str, object]) -> Tuple[Dict[str, object], int]:
    # CompatibilitÃ© avec l'ancien bouton "ajouter disque physique".
    data = dict(payload)
    action = str(data.get("action", "add") or "add")
    if action == "attach_physical":
        action = "add"
    data["action"] = action
    data["type"] = data.get("type") or "physical_disk"
    if "source" not in data and "path" in data:
        data["source"] = data.get("path")
    return do_vm_device_action(conf, data)


def parse_virsh_names(conf: Dict[str, str], *args: str) -> List[str]:
    rc, out = virsh(conf, *args, timeout=20)
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def pool_path_from_xml(xml_text: str) -> str:
    try:
        root = ET.fromstring(xml_text)
        return first_text(root, "./target/path")
    except Exception:
        return ""


def libvirt_size_to_bytes(value: object) -> int:
    """Convertit les tailles virsh du type '100.00 GiB' en octets."""
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return 0
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([KMGTPE]?i?B|[KMGTPE]B|[KMGTPE]?io|[KMGTPE]?o|B)?$", text, re.I)
    if not m:
        return 0
    number = float(m.group(1))
    unit = (m.group(2) or "B").lower()
    factors = {
        "b": 1, "o": 1,
        "kb": 1000, "kib": 1024, "ko": 1000, "kio": 1024,
        "mb": 1000 ** 2, "mib": 1024 ** 2, "mo": 1000 ** 2, "mio": 1024 ** 2,
        "gb": 1000 ** 3, "gib": 1024 ** 3, "go": 1000 ** 3, "gio": 1024 ** 3,
        "tb": 1000 ** 4, "tib": 1024 ** 4, "to": 1000 ** 4, "tio": 1024 ** 4,
        "pb": 1000 ** 5, "pib": 1024 ** 5, "po": 1000 ** 5, "pio": 1024 ** 5,
        "eb": 1000 ** 6, "eib": 1024 ** 6, "eo": 1000 ** 6, "eio": 1024 ** 6,
    }
    return int(number * factors.get(unit, 1))


def bytes_to_gib_ceil(value: int) -> int:
    gib = 1024 ** 3
    try:
        value = int(value or 0)
    except Exception:
        value = 0
    if value <= 0:
        return 0
    return max(1, (value + gib - 1) // gib)


def bytes_to_gio_text(value: int) -> str:
    """Retourne une capacité en Gio lisible pour l'interface.

    Exemple : 128103 Mio libvirt devient 125.10 Gio dans le champ,
    pas 128103. Le champ doit rester compréhensible pour l'humain.
    """
    try:
        size = int(value or 0) / float(1024 ** 3)
    except Exception:
        return ""
    if size <= 0:
        return ""
    if abs(size - round(size)) < 0.005:
        return str(int(round(size)))
    return f"{size:.2f}"


def storage_file_disk_usage(path: str) -> str:
    """Taille réellement consommée sur le disque hôte pour un fichier sparse/qcow2."""
    path = str(path or "").strip()
    if not path:
        return ""
    try:
        st = os.stat(path)
        blocks = int(getattr(st, "st_blocks", 0) or 0) * 512
        if blocks > 0:
            return human_bytes(blocks)
    except Exception:
        pass
    try:
        rc, out = run_cmd(["du", "-B1", "-s", path], timeout=12)
        if rc == 0:
            raw = str(out or "").split()[0]
            return human_bytes(int(raw))
    except Exception:
        pass
    return ""


def list_pool_volumes(conf: Dict[str, str], pool: str) -> List[Dict[str, str]]:
    rc, out = virsh(conf, "vol-list", pool, "--details", timeout=40)
    if rc != 0:
        return []
    rows: List[Dict[str, str]] = []
    for raw in out.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith(("Name", "Nom")) or set(stripped) == {"-"}:
            continue
        parts = re.split(r"\s{2,}", stripped)
        if len(parts) >= 2:
            capacity = parts[3] if len(parts) > 3 else ""
            allocation = parts[4] if len(parts) > 4 else ""
            capacity_bytes = libvirt_size_to_bytes(capacity)
            allocation_bytes = libvirt_size_to_bytes(allocation)
            remaining_bytes = max(capacity_bytes - allocation_bytes, 0) if capacity_bytes else 0
            path = parts[1] if len(parts) > 1 else ""
            capacity_gio = bytes_to_gio_text(capacity_bytes)
            rows.append({
                "name": parts[0],
                "path": path,
                "type": parts[2] if len(parts) > 2 else "",
                "capacity": (f"{capacity_gio} Gio" if capacity_gio else capacity),
                "capacity_raw": capacity,
                "capacity_bytes": str(capacity_bytes),
                "capacity_gib": capacity_gio or str(bytes_to_gib_ceil(capacity_bytes)),
                "capacity_gio": capacity_gio,
                "allocation": allocation,
                "allocation_bytes": str(allocation_bytes),
                "remaining": human_bytes(remaining_bytes) if remaining_bytes else "0 B",
                "remaining_bytes": str(remaining_bytes),
                "disk_usage": storage_file_disk_usage(path) or allocation,
            })
    return rows


def list_storage_pools(conf: Dict[str, str]) -> List[Dict[str, object]]:
    names = parse_virsh_names(conf, "pool-list", "--all", "--name")
    pools: List[Dict[str, object]] = []
    for name in names:
        rc, info_text = virsh(conf, "pool-info", name, timeout=20)
        info = parse_key_values(info_text) if rc == 0 else {}
        rcx, xml_text = virsh(conf, "pool-dumpxml", name, timeout=20)
        path = pool_path_from_xml(xml_text) if rcx == 0 else ""
        pools.append({
            "name": name,
            "state": info.get("State", ""),
            "autostart": info.get("Autostart", ""),
            "persistent": info.get("Persistent", ""),
            "capacity": info.get("Capacity", ""),
            "allocation": info.get("Allocation", ""),
            "available": info.get("Available", ""),
            "path": path,
            "volumes": list_pool_volumes(conf, name),
        })
    return sorted(pools, key=lambda x: str(x.get("name", "")).lower())


def network_xml_info(xml_text: str) -> Dict[str, str]:
    out = {"bridge": "", "forward": "", "domain": ""}
    try:
        root = ET.fromstring(xml_text)
        out["bridge"] = node_attr(root.find("bridge"), "name", "")
        fwd = root.find("forward")
        out["forward"] = node_attr(fwd, "mode", "") if fwd is not None else "isolÃ©"
        out["domain"] = node_attr(root.find("domain"), "name", "")
    except Exception:
        pass
    return out


def list_libvirt_networks(conf: Dict[str, str]) -> List[Dict[str, str]]:
    names = parse_virsh_names(conf, "net-list", "--all", "--name")
    rows: List[Dict[str, str]] = []
    for name in names:
        rc, info_text = virsh(conf, "net-info", name, timeout=20)
        info = parse_key_values(info_text) if rc == 0 else {}
        rcx, xml_text = virsh(conf, "net-dumpxml", name, timeout=20)
        xml_info = network_xml_info(xml_text) if rcx == 0 else {}
        rows.append({
            "name": name,
            "active": info.get("Active", ""),
            "persistent": info.get("Persistent", ""),
            "autostart": info.get("Autostart", ""),
            "bridge": xml_info.get("bridge", ""),
            "forward": xml_info.get("forward", ""),
            "domain": xml_info.get("domain", ""),
        })
    return sorted(rows, key=lambda x: x.get("name", "").lower())


def list_pci_devices(conf: Dict[str, str]) -> List[Dict[str, str]]:
    lspci = str(conf.get("LSPCI_BIN", "lspci") or "lspci").strip()
    rc, out = run_cmd([lspci, "-Dnn"], timeout=15)
    rows: List[Dict[str, str]] = []
    if rc != 0:
        return rows
    for raw in out.splitlines():
        m = re.match(r"^([0-9a-fA-F:.]+)\s+(.+)$", raw.strip())
        if m:
            rows.append({"address": m.group(1), "label": m.group(2)})
    return rows


def list_usb_devices() -> List[Dict[str, str]]:
    rc, out = run_cmd(["lsusb"], timeout=10)
    rows: List[Dict[str, str]] = []
    if rc != 0:
        return rows
    for raw in out.splitlines():
        m = re.search(r"ID\s+([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s*(.*)$", raw)
        if m:
            rows.append({"vendor": m.group(1), "product": m.group(2), "label": m.group(3).strip(), "raw": raw.strip()})
    return rows


def list_host_interfaces() -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    root = "/sys/class/net"
    try:
        names = sorted(os.listdir(root))
    except Exception:
        return rows

    for name in names:
        if name == "lo" or name.startswith(("vnet", "virbr", "docker", "br-")):
            # virbr reste visible cÃ´tÃ© rÃ©seaux libvirt ; ici on garde surtout les interfaces hÃ´te/direct.
            pass
        try:
            path = os.path.join(root, name)
            kind = "bridge" if os.path.isdir(os.path.join(path, "bridge")) else "interface"
            state = ""
            mac = ""
            try:
                with open(os.path.join(path, "operstate"), "r", encoding="utf-8") as handle:
                    state = handle.read().strip()
            except Exception:
                pass
            try:
                with open(os.path.join(path, "address"), "r", encoding="utf-8") as handle:
                    mac = handle.read().strip()
            except Exception:
                pass
            rows.append({"name": name, "kind": kind, "state": state, "mac": mac})
        except Exception:
            continue
    return rows


def list_host_bridges() -> List[Dict[str, str]]:
    bridges: List[Dict[str, str]] = []
    for item in list_host_interfaces():
        if item.get("kind") == "bridge":
            bridges.append(item)

    # Si des bridges standards existent mais n'ont pas Ã©tÃ© reconnus comme bridges, on les ajoute quand mÃªme.
    try:
        names = set(os.listdir("/sys/class/net"))
    except Exception:
        names = set()
    known = {str(x.get("name") or "") for x in bridges}
    for guess in ("br0", "virbr0"):
        if guess in names and guess not in known:
            bridges.append({"name": guess, "kind": "bridge", "state": "", "mac": ""})
    return sorted(bridges, key=lambda x: str(x.get("name") or "").lower())


def _dedupe_existing_dirs(paths: List[str]) -> List[str]:
    rows: List[str] = []
    seen: set = set()
    for item in paths:
        path = os.path.abspath(os.path.expanduser(os.path.expandvars(str(item or "").strip())))
        if not path or not os.path.isdir(path):
            continue
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        rows.append(path)
    return rows


def _pool_looks_like_iso_library(pool: Dict[str, object]) -> bool:
    text = f"{pool.get('name', '')} {pool.get('path', '')}".lower()
    return "iso" in text or "isos" in text


def list_iso_files(conf: Dict[str, str], pools: Optional[List[Dict[str, object]]] = None) -> List[Dict[str, str]]:
    # Source explicite historique + pools libvirt qui ressemblent à une
    # bibliothèque ISO. Ça permet à une pool nommée "iso" dans /mnt/user/isos
    # d'apparaître automatiquement, sans scanner tout un gros pool de disques VM.
    root_candidates = [str(conf.get("ISO_DEFAULT_DIR", DEFAULT_CONFIG["ISO_DEFAULT_DIR"]) or "").strip()]
    pool_rows = pools if pools is not None else list_storage_pools(conf)
    for pool in pool_rows or []:
        if not _pool_looks_like_iso_library(pool):
            continue
        root_candidates.append(str(pool.get("path") or ""))

    roots = _dedupe_existing_dirs(root_candidates)
    out: List[Dict[str, str]] = []
    seen_paths: set = set()
    for root in roots:
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in files:
                if fn.lower().endswith((".iso", ".img")):
                    path = os.path.join(base, fn)
                    real = os.path.realpath(path)
                    if real in seen_paths:
                        continue
                    seen_paths.add(real)
                    try:
                        size = human_bytes(os.path.getsize(path))
                    except Exception:
                        size = ""
                    out.append({"name": fn, "path": path, "size": size})
            if len(out) > 500:
                break
    return sorted(out, key=lambda x: x["path"].lower())


def firmware_nvram_template(loader_path: str) -> str:
    base = os.path.basename(loader_path)
    folder = os.path.dirname(loader_path)
    candidates: List[str] = []
    if "CODE" in base:
        stem = base.replace("CODE", "VARS")
        stem = stem.replace(".secboot.strictnx", "").replace(".secboot", "")
        candidates.append(os.path.join(folder, stem))
    if base.upper().startswith("OVMF"):
        candidates.extend([
            "/usr/share/OVMF/OVMF_VARS_4M.fd",
            "/usr/share/OVMF/OVMF_VARS.fd",
            "/usr/share/ovmf/OVMF_VARS.fd",
        ])
    if base.upper().startswith("AAVMF") or "AAVMF" in loader_path.upper():
        candidates.extend([
            "/usr/share/AAVMF/AAVMF_VARS.fd",
            "/usr/share/AAVMF/AAVMF_VARS.ms.fd",
            "/usr/share/qemu-efi-aarch64/QEMU_EFI_VARS.fd",
        ])
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return ""


def firmware_arch_label(path: str) -> str:
    low = path.lower()
    if "aavmf" in low or "aarch64" in low or "arm64" in low:
        return "aarch64"
    if "arm" in low and "ovmf" not in low:
        return "arm"
    return "x86_64"


def list_firmware_options(conf: Dict[str, str]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = [
        {"id": "bios", "mode": "bios", "label": "BIOS legacy / SeaBIOS", "arch": "x86_64", "loader": "", "nvram": "", "secure_boot": False},
        {"id": "uefi", "mode": "uefi", "label": "UEFI auto", "arch": "auto", "loader": "", "nvram": "", "secure_boot": False},
    ]
    roots = [
        "/usr/share/OVMF",
        "/usr/share/ovmf",
        "/usr/share/AAVMF",
        "/usr/share/qemu-efi-aarch64",
        "/usr/share/qemu-efi-arm",
    ]
    seen = {row["id"] for row in rows}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for base, _dirs, files in os.walk(root):
            for fn in files:
                low = fn.lower()
                if not low.endswith((".fd", ".bin")):
                    continue
                if "vars" in low:
                    continue
                if not any(token in low for token in ("ovmf", "aavmf", "qemu_efi", "code", "edk2")):
                    continue
                path = os.path.join(base, fn)
                arch = firmware_arch_label(path)
                nvram = firmware_nvram_template(path)
                secure = "secboot" in low or "secure" in low or ".ms." in low
                variants: List[Tuple[str, str]] = [(nvram, "")]
                if "ovmf" in path.lower():
                    for vars_path, vars_label in (
                        ("/usr/share/OVMF/OVMF_VARS_4M.ms.fd", "MS keys / Windows"),
                        ("/usr/share/OVMF/OVMF_VARS_4M.snakeoil.fd", "snakeoil test keys"),
                    ):
                        if os.path.exists(vars_path):
                            variants.append((vars_path, vars_label))
                for variant_nvram, variant_label in variants:
                    variant_secure = secure or ".ms." in os.path.basename(variant_nvram).lower()
                    label_bits = [fn]
                    if variant_nvram:
                        label_bits.append(os.path.basename(variant_nvram))
                    if variant_secure:
                        label_bits.append("Secure Boot")
                    if variant_label:
                        label_bits.append(variant_label)
                    if arch != "x86_64":
                        label_bits.append(arch)
                    item_id = "loader:" + path + ("|vars:" + variant_nvram if variant_nvram else "")
                    if item_id in seen:
                        continue
                    seen.add(item_id)
                    rows.append({
                        "id": item_id,
                        "mode": "loader",
                        "label": " - ".join(label_bits),
                        "arch": arch,
                        "loader": path,
                        "nvram": variant_nvram,
                        "secure_boot": variant_secure,
                    })
    return rows




def _read_thread_siblings(cpu_id: int) -> str:
    path = f"/sys/devices/system/cpu/cpu{cpu_id}/topology/thread_siblings_list"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except Exception:
        return ""


def list_host_cpus(conf: Dict[str, str]) -> List[Dict[str, object]]:
    """Inventaire des CPU logiques de l'hôte pour le pinning VM.

    Source principale : lscpu -e=CPU,CORE,SOCKET,NODE.
    Sur un Ryzen 7 1700 on obtient typiquement les paires :
    core 0 => CPU 0 + 8, core 1 => CPU 1 + 9, etc.
    """
    rc, out = run_cmd(["lscpu", "-e=CPU,CORE,SOCKET,NODE"], timeout=8)
    rows: List[Dict[str, object]] = []
    if rc == 0:
        for raw in out.splitlines():
            line = raw.strip()
            if not line or not re.match(r"^\d+\s+\d+\s+\d+\s+\d+", line):
                continue
            parts = line.split()
            try:
                cpu_id = int(parts[0])
                core_id = int(parts[1])
                socket_id = int(parts[2])
                node_id = int(parts[3])
            except Exception:
                continue
            rows.append({
                "cpu": cpu_id,
                "core": core_id,
                "socket": socket_id,
                "node": node_id,
                "siblings": _read_thread_siblings(cpu_id),
            })

    if not rows:
        # Fallback très simple si lscpu manque.
        root = "/sys/devices/system/cpu"
        try:
            names = sorted(os.listdir(root), key=lambda n: int(n[3:]) if n.startswith("cpu") and n[3:].isdigit() else 999999)
        except Exception:
            names = []
        for name in names:
            if not name.startswith("cpu") or not name[3:].isdigit():
                continue
            cpu_id = int(name[3:])
            rows.append({"cpu": cpu_id, "core": cpu_id, "socket": 0, "node": 0, "siblings": _read_thread_siblings(cpu_id)})

    # Ajoute la liste des CPU frères par cœur physique, plus fiable/visible dans l'UI.
    by_core: Dict[Tuple[int, int, int], List[int]] = {}
    for row in rows:
        key = (int(row.get("socket", 0)), int(row.get("node", 0)), int(row.get("core", 0)))
        by_core.setdefault(key, []).append(int(row.get("cpu", 0)))
    for row in rows:
        key = (int(row.get("socket", 0)), int(row.get("node", 0)), int(row.get("core", 0)))
        row["core_cpus"] = sorted(by_core.get(key, []))
        row["core_key"] = f"S{key[0]}N{key[1]}C{key[2]}"

    return sorted(rows, key=lambda item: (int(item.get("socket", 0)), int(item.get("node", 0)), int(item.get("core", 0)), int(item.get("cpu", 0))))


def _render_node_pci_from_by_path(path: str) -> str:
    by_path = "/dev/dri/by-path"
    real_path = os.path.realpath(path)
    try:
        names = sorted(os.listdir(by_path))
    except Exception:
        return ""
    for name in names:
        if not name.endswith("-render"):
            continue
        candidate = os.path.join(by_path, name)
        try:
            if os.path.realpath(candidate) != real_path:
                continue
        except Exception:
            continue
        match = re.search(r"pci-([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7])-render", name)
        if match:
            return match.group(1)
    return ""


def _user_primary_groups(user: str) -> List[str]:
    if not user:
        return []
    rc, out = run_cmd(["id", "-nG", user], timeout=8)
    if rc != 0:
        return []
    return [item.strip() for item in out.split() if item.strip()]


def _stat_owner_group_mode(path: str) -> Tuple[str, str, str]:
    try:
        import grp
        import pwd
        st = os.stat(path)
        owner = pwd.getpwuid(st.st_uid).pw_name
        group = grp.getgrgid(st.st_gid).gr_name
        mode = format(st.st_mode & 0o777, "04o")
        return owner, group, mode
    except Exception:
        return "", "", ""


def list_render_nodes(conf: Dict[str, str]) -> List[Dict[str, object]]:
    base = "/dev/dri"
    try:
        names = sorted(os.listdir(base), key=lambda item: int(re.sub(r"\D+", "", item) or "0"))
    except Exception:
        return []

    qemu_user = ""
    qemu_groups: List[str] = []
    try:
        qemu_user = detect_libvirt_qemu_user(conf)
        qemu_groups = _user_primary_groups(qemu_user)
    except Exception:
        qemu_user = ""

    preferred_path = ""
    try:
        preferred_path = preferred_rendernode_for_egl(conf)
    except Exception:
        preferred_path = ""

    rows: List[Dict[str, object]] = []
    for name in names:
        if not re.fullmatch(r"renderD[0-9]+", name):
            continue
        path = os.path.join(base, name)
        if not os.path.exists(path):
            continue
        pci = _render_node_pci_from_by_path(path)
        label = lspci_label(conf, pci) if pci else name
        owner, group, mode = _stat_owner_group_mode(path)
        access_ok = user_can_access_path(qemu_user, path, "rw") if qemu_user else False
        warnings: List[str] = []
        if group and group != "render":
            warnings.append(f"{path} est dans le groupe {group}, attendu souvent render.")
        if qemu_user and not access_ok:
            warnings.append(f"L'utilisateur libvirt/QEMU {qemu_user} n'a pas acces en lecture/ecriture a {path}.")
        if qemu_user and group == "render" and "render" not in qemu_groups:
            warnings.append(f"L'utilisateur {qemu_user} n'est pas dans le groupe render.")
        display = " - ".join(part for part in [label, f"({pci})" if pci else "", path] if part)
        rows.append({
            "path": path,
            "node": name,
            "pci": pci,
            "label": label,
            "display": display,
            "owner": owner,
            "group": group,
            "mode": mode,
            "preferred": path == preferred_path,
            "qemu_user": qemu_user,
            "qemu_access": access_ok,
            "warnings": warnings,
        })
    return rows


def collect_vm_extra(conf: Dict[str, str]) -> Dict[str, object]:
    pools = list_storage_pools(conf)
    return {
        "pools": pools,
        "networks": list_libvirt_networks(conf),
        "host_disks": list_host_byid_disks(conf),
        "host_optical_drives": list_host_optical_drives(conf),
        "host_interfaces": list_host_interfaces(),
        "host_bridges": list_host_bridges(),
        "pci_devices": list_pci_devices(conf),
        "usb_devices": list_usb_devices(),
        "host_cpus": list_host_cpus(conf),
        "render_nodes": list_render_nodes(conf),
        "isos": list_iso_files(conf, pools),
        "firmware_options": list_firmware_options(conf),
    }


def vm_ttyd_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return slug[:80] or "vm"


def vm_ttyd_pid_file(name: str) -> str:
    return os.path.join("/tmp", f"yoleo_vm_serial_ttyd_{vm_ttyd_slug(name)}.pid")


def vm_ttyd_port_file(name: str) -> str:
    return os.path.join("/tmp", f"yoleo_vm_serial_ttyd_{vm_ttyd_slug(name)}.port")


def vm_ttyd_log_file(name: str) -> str:
    return os.path.join("/tmp", f"yoleo_vm_serial_ttyd_{vm_ttyd_slug(name)}.log")


def vm_ttyd_read_int(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return int(str(handle.read()).strip() or "0")
    except Exception:
        return 0


def vm_ttyd_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def vm_ttyd_port_is_free(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", int(port)))
        return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


def vm_ttyd_find_free_port(conf: Dict[str, str]) -> int:
    start = max(1024, conf_int(conf, "VM_SERIAL_TTYD_BASE_PORT", 7820))
    count = max(1, conf_int(conf, "VM_SERIAL_TTYD_PORT_COUNT", 40))
    for port in range(start, start + count):
        if vm_ttyd_port_is_free(port):
            return port
    raise RuntimeError(f"Aucun port libre pour ttyd VM entre {start} et {start + count - 1}.")


def vm_ttyd_url(port: int) -> str:
    try:
        import terminal as yoleo_terminal
        return yoleo_terminal.ttyd_url_for_port(yoleo_terminal.get_config(), int(port))
    except Exception:
        host = "127.0.0.1"
        if has_request_context():
            host = request.host.split(":", 1)[0]
        return f"//{host}:{int(port)}"


def vm_terminal_conf() -> Dict[str, str]:
    try:
        import terminal as yoleo_terminal
        return yoleo_terminal.get_config()
    except Exception:
        return {}


def vm_ttyd_binary(term_conf: Dict[str, str]) -> str:
    candidate = str(term_conf.get("TERMINAL_BIN_RESOLVED") or "").strip()
    if candidate and os.path.isfile(candidate):
        return candidate
    for candidate in (
        shutil.which("ttyd") or "",
        os.path.abspath(os.path.join(_NAS_MODULE_DIR, "..", "bin", "ttyd.x86_64")),
        os.path.abspath(os.path.join(_NAS_MODULE_DIR, "..", "bin", "ttyd.aarch64")),
        "/usr/bin/ttyd",
        "/usr/local/bin/ttyd",
    ):
        if candidate and os.path.isfile(candidate):
            return candidate
    return str(term_conf.get("TERMINAL_BIN_RESOLVED") or "ttyd")


def start_vm_serial_ttyd(conf: Dict[str, str], name: str) -> Dict[str, object]:
    name = clean_vm_name(name)
    vm = collect_one_vm(conf, name)
    if vm.get("error"):
        raise RuntimeError(str(vm.get("error") or "Impossible de lire la VM."))
    if not vm_looks_running_for_console(conf, vm):
        raise RuntimeError("La console série est disponible seulement quand la VM est démarrée.")
    if not vm.get("has_virtio_serial"):
        raise RuntimeError("Cette VM n'a pas de console virtio série dans son XML.")

    pid_file = vm_ttyd_pid_file(name)
    port_file = vm_ttyd_port_file(name)
    pid = vm_ttyd_read_int(pid_file)
    port = vm_ttyd_read_int(port_file)
    if pid and port and vm_ttyd_process_running(pid):
        return {"ok": True, "title": f"Console série VM · {name}", "url": vm_ttyd_url(port), "pid": pid, "port": port, "reused": True}

    for path in (pid_file, port_file):
        try:
            os.unlink(path)
        except OSError:
            pass

    term_conf = vm_terminal_conf()
    ttyd_bin = vm_ttyd_binary(term_conf)
    if not os.path.isfile(ttyd_bin):
        raise RuntimeError(f"Binaire ttyd introuvable : {ttyd_bin}")
    try:
        os.chmod(ttyd_bin, 0o755)
    except OSError:
        pass

    port = vm_ttyd_find_free_port(conf)
    base_path = ""
    try:
        import terminal as yoleo_terminal
        base_path = yoleo_terminal.ttyd_base_path_for_port(term_conf, int(port))
    except Exception:
        base_path = ""
    title = f"Console série VM · {name}"
    theme = json.dumps({
        "background": str(term_conf.get("TERMINAL_THEME_BACKGROUND") or "#000000"),
        "foreground": str(term_conf.get("TERMINAL_THEME_FOREGROUND") or "#00ff00"),
        "cursor": str(term_conf.get("TERMINAL_THEME_CURSOR") or "#ffffff"),
    }, separators=(",", ":"))
    uri = str(conf.get("LIBVIRT_URI", "") or "").strip()
    connect = (" -c " + shlex.quote(uri)) if uri else ""
    quoted_name = shlex.quote(name)
    script = (
        "clear; "
        f"echo '=== Console série virtio : {name} ==='; "
        "echo 'Quitter virsh console : Ctrl+]'; "
        "echo; "
        f"{shlex.quote(str(conf.get('VIRSH_BIN', 'virsh') or 'virsh'))}{connect} console {quoted_name}; "
        "code=$?; "
        "echo; echo '--- Console série terminée ---'; "
        "echo \"Code retour: $code\"; "
        "exec /bin/bash -l"
    )
    cmd = [
        ttyd_bin,
        "-p", str(port),
        "-i", "0.0.0.0",
        "-W",
        "-t", f"theme={theme}",
        "-t", f"titleFixed={title}",
        "-t", "enableReconnect=true",
        "-t", "disableLeaveAlert=true",
    ]
    if base_path:
        cmd.extend(["-b", base_path])
    cmd.extend(["/bin/bash", "-lc", script])
    log_file = vm_ttyd_log_file(name)
    with open(log_file, "a", encoding="utf-8", errors="replace") as log:
        log.write("\n\n=== Démarrage ttyd console série VM ===\n")
        log.write("Commande: " + " ".join(shlex.quote(x) for x in cmd) + "\n")
        log.flush()
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            cwd="/root" if os.path.isdir("/root") else "/",
        )

    with open(pid_file, "w", encoding="utf-8") as handle:
        handle.write(str(proc.pid))
    with open(port_file, "w", encoding="utf-8") as handle:
        handle.write(str(port))
    time.sleep(0.35)
    if not vm_ttyd_process_running(proc.pid):
        raise RuntimeError(f"ttyd console VM s'est arrêté juste après le démarrage. Log : {log_file}")
    return {"ok": True, "title": title, "url": vm_ttyd_url(port), "pid": proc.pid, "port": port, "reused": False}
