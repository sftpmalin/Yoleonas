#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# PART 006 - Actions pools de stockage et réseaux libvirt



def _storage_positive_int(value: object, label: str, min_value: int = 1, max_value: int = 262144) -> int:
    raw = str(value or "").strip().replace(",", ".")
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(?:g|gb|gib|go|gio)?$", raw, re.I)
    if not m:
        raise ValueError(f"{label} invalide.")
    out = int(float(m.group(1)))
    if out < min_value or out > max_value:
        raise ValueError(f"{label} invalide : valeur attendue entre {min_value} et {max_value} Gio.")
    return out


def _format_gio_from_bytes(value: int) -> str:
    try:
        size = int(value or 0) / float(1024 ** 3)
    except Exception:
        return "0"
    if abs(size - round(size)) < 0.005:
        return str(int(round(size)))
    return f"{size:.2f}"


def _storage_resize_size_to_bytes(value: object, label: str, min_gio: float = 1, max_gio: float = 262144) -> Tuple[int, str]:
    raw = str(value or "").strip().replace(",", ".")
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(b|o|k|kb|kib|ko|kio|m|mb|mib|mo|mio|g|gb|gib|go|gio|t|tb|tib|to|tio)?$", raw, re.I)
    if not m:
        raise ValueError(f"{label} invalide.")
    number = float(m.group(1))
    unit = (m.group(2) or "gio").lower()
    aliases = {"k": "kio", "m": "mio", "g": "gio", "t": "tio"}
    unit = aliases.get(unit, unit)
    # Pour l'interface, Go/GB/G/Gio signifient volontairement Gio.
    # Objectif : l'utilisateur tape 125 ou 125,10, pas une valeur en Mio.
    factors = {
        "b": 1, "o": 1,
        "kb": 1000, "kib": 1024, "ko": 1000, "kio": 1024,
        "mb": 1000 ** 2, "mib": 1024 ** 2, "mo": 1000 ** 2, "mio": 1024 ** 2,
        "gb": 1024 ** 3, "gib": 1024 ** 3, "go": 1024 ** 3, "gio": 1024 ** 3,
        "tb": 1024 ** 4, "tib": 1024 ** 4, "to": 1024 ** 4, "tio": 1024 ** 4,
    }
    requested_bytes = int(number * factors.get(unit, 1024 ** 3))
    min_bytes = int(float(min_gio) * (1024 ** 3))
    max_bytes = int(float(max_gio) * (1024 ** 3))
    if requested_bytes < min_bytes or requested_bytes > max_bytes:
        raise ValueError(f"{label} invalide : valeur attendue entre {min_gio:g} et {max_gio:g} Gio.")
    return requested_bytes, _format_gio_from_bytes(requested_bytes)


def _storage_disk_format(value: object) -> str:
    fmt = str(value or "qcow2").strip().lower()
    return "raw" if fmt == "raw" else "qcow2"


def _storage_allocation_mode(value: object) -> str:
    mode = str(value or "thin").strip().lower()
    if mode in {"full", "prealloc", "preallocated", "complet", "reserve", "reserved"}:
        return "full"
    return "thin"


def _storage_volume_name_with_extension(name: str, disk_format: str) -> str:
    volume = clean_volume_name(name)
    lower = volume.lower()
    if not lower.endswith((".qcow2", ".raw", ".img")):
        volume = f"{volume}.{'raw' if disk_format == 'raw' else 'qcow2'}"
    return clean_volume_name(volume)


def _storage_volume_usage(conf: Dict[str, str], pool: str, volume: str) -> Tuple[List[str], str]:
    path = ""
    try:
        path = volume_path_in_pool(conf, pool, volume)
    except Exception:
        path = ""
    if not path:
        return [], path
    used_by: List[str] = []
    try:
        vms, _summary, _error = collect_inventory(conf)
        real_path = os.path.realpath(path)
        for vm in vms or []:
            for disk in vm.get("disks", []) or []:
                source = str(disk.get("source", "") or "")
                if source == path or (source and os.path.realpath(source) == real_path):
                    used_by.append(str(vm.get("name", "") or "VM inconnue"))
                    break
    except Exception:
        used_by = []
    return sorted(set(used_by)), path


def do_storage_volume_create(conf: Dict[str, str], payload: Dict[str, object]) -> Tuple[Dict[str, object], int]:
    pool = clean_pool_name(str(payload.get("pool", "") or payload.get("pool_name", "") or ""))
    disk_format = _storage_disk_format(payload.get("format"))
    volume = _storage_volume_name_with_extension(str(payload.get("volume", "") or payload.get("name", "") or ""), disk_format)
    size_gb = _storage_positive_int(payload.get("size_gb", payload.get("size", "")), "Taille du disque", 1, 262144)
    allocation_mode = _storage_allocation_mode(payload.get("allocation"))

    ensure_pool_active(conf, pool)
    if pool_volume_exists(conf, pool, volume):
        return {"ok": False, "message": f"Le volume existe deja dans la pool {pool} : {volume}"}, 409

    args = [
        "vol-create-as",
        pool,
        volume,
        f"{size_gb}G",
        "--format",
        disk_format,
    ]
    if allocation_mode == "full":
        args.extend(["--allocation", f"{size_gb}G"])
    else:
        # Mode dynamique/sparse : le disque annonce sa capacite max,
        # mais le fichier ne consomme pas tout de suite cette taille sur le pool.
        args.extend(["--allocation", "0"])

    rc, out = virsh(conf, *args, timeout=max(conf_int(conf, "ACTION_TIMEOUT", 90), 240))
    if rc != 0:
        return {"ok": False, "message": out.strip() or f"Creation du volume {pool}/{volume} impossible."}, 500

    virsh(conf, "pool-refresh", pool, timeout=60)
    try:
        path = volume_path_in_pool(conf, pool, volume)
    except Exception:
        path = ""
    alloc_label = "prealloue / reserve" if allocation_mode == "full" else "dynamique / sparse"
    vm_log(conf, f"Volume cree {pool}/{volume} size={size_gb}G format={disk_format} allocation={allocation_mode} path={path}")
    return {
        "ok": True,
        "message": f"Disque virtuel cree : {pool}/{volume} ({size_gb} Gio, {disk_format}, {alloc_label})",
        "pool": pool,
        "volume": volume,
        "path": path,
    }, 200


def do_storage_volume_delete(conf: Dict[str, str], payload: Dict[str, object]) -> Tuple[Dict[str, object], int]:
    pool = clean_pool_name(str(payload.get("pool", "") or payload.get("pool_name", "") or ""))
    volume = clean_volume_name(str(payload.get("volume", "") or payload.get("name", "") or ""))
    ensure_pool_active(conf, pool)
    used_by, path = _storage_volume_usage(conf, pool, volume)
    if used_by:
        return {"ok": False, "message": "Ce disque virtuel est encore attaché à : " + ", ".join(used_by) + ". Retire d'abord le périphérique de la VM avant de supprimer le fichier."}, 409
    rc, out = virsh(conf, "vol-delete", "--pool", pool, volume, timeout=max(conf_int(conf, "ACTION_TIMEOUT", 90), 90))
    if rc != 0:
        return {"ok": False, "message": out.strip() or f"Suppression du volume {pool}/{volume} impossible."}, 500
    virsh(conf, "pool-refresh", pool, timeout=60)
    vm_log(conf, f"Volume supprime {pool}/{volume}")
    return {"ok": True, "message": f"Disque virtuel supprime : {pool}/{volume}", "pool": pool, "volume": volume}, 200


def do_storage_volume_resize(conf: Dict[str, str], payload: Dict[str, object]) -> Tuple[Dict[str, object], int]:
    pool = clean_pool_name(str(payload.get("pool", "") or payload.get("pool_name", "") or ""))
    volume = clean_volume_name(str(payload.get("volume", "") or payload.get("name", "") or ""))
    requested_bytes, requested_gio = _storage_resize_size_to_bytes(
        payload.get("size_gio", payload.get("size_gb", payload.get("size", ""))),
        "Nouvelle taille du disque",
        1,
        262144,
    )
    ensure_pool_active(conf, pool)

    current = None
    for row in list_pool_volumes(conf, pool):
        if str(row.get("name", "")) == volume:
            current = row
            break
    if current is None:
        return {"ok": False, "message": f"Volume introuvable dans la pool {pool} : {volume}"}, 404

    current_path = str(current.get("path") or current.get("target") or "")
    current_name = str(current.get("name") or volume or "")
    lower_volume = current_name.lower()
    lower_path = current_path.lower()
    allowed_ext = (".qcow2", ".raw", ".img", ".vmdk")
    if lower_volume.endswith(".iso") or lower_path.endswith(".iso") or not (lower_volume.endswith(allowed_ext) or lower_path.endswith(allowed_ext)):
        return {"ok": False, "message": f"Redimensionnement refusé : {pool}/{volume} n'est pas un disque virtuel redimensionnable."}, 400

    current_bytes = int(str(current.get("capacity_bytes") or "0") or "0")
    current_gio = _format_gio_from_bytes(current_bytes)
    if current_bytes <= 0:
        return {"ok": False, "message": f"Capacité actuelle illisible pour {pool}/{volume}. Redimensionnement refusé."}, 400
    if requested_bytes <= current_bytes:
        return {"ok": False, "message": f"Nouvelle taille trop petite : {requested_gio} Gio demandé, {current_gio} Gio actuel."}, 400

    rc, out = virsh(conf, "vol-resize", "--pool", pool, volume, f"{requested_bytes}B", timeout=max(conf_int(conf, "ACTION_TIMEOUT", 90), 240))
    if rc != 0:
        return {"ok": False, "message": out.strip() or f"Redimensionnement du volume {pool}/{volume} impossible."}, 500
    virsh(conf, "pool-refresh", pool, timeout=60)
    vm_log(conf, f"Volume agrandi {pool}/{volume}: {current_gio} Gio -> {requested_gio} Gio")
    return {"ok": True, "message": f"Disque virtuel agrandi : {pool}/{volume} -> {requested_gio} Gio", "pool": pool, "volume": volume}, 200


def define_dir_pool_xml(name: str, path: str) -> str:
    root = ET.Element("pool", {"type": "dir"})
    ET.SubElement(root, "name").text = name
    target = ET.SubElement(root, "target")
    ET.SubElement(target, "path").text = path
    return xml_to_string(root)


def do_storage_action(conf: Dict[str, str], payload: Dict[str, object]) -> Tuple[Dict[str, object], int]:
    action = str(payload.get("action", "") or "").strip().lower()
    if action in {"volume_create", "create_volume", "vol_create"}:
        return do_storage_volume_create(conf, payload)
    if action in {"volume_delete", "delete_volume", "vol_delete"}:
        return do_storage_volume_delete(conf, payload)
    if action in {"volume_resize", "resize_volume", "vol_resize"}:
        return do_storage_volume_resize(conf, payload)

    name = clean_pool_name(str(payload.get("name", "") or "")) if payload.get("name") else ""
    if action == "add":
        name = clean_pool_name(str(payload.get("name", "") or ""))
        path = clean_abs_path(str(payload.get("path", "") or ""), "chemin pool")
        if str(payload.get("create_dir", "") or "").lower() in {"1", "true", "yes", "on"}:
            os.makedirs(path, exist_ok=True)
        elif not os.path.isdir(path):
            return {"ok": False, "message": f"Le dossier de pool n'existe pas : {path}"}, 400

        rc_existing, _existing = virsh(conf, "pool-info", name, timeout=20)
        if rc_existing == 0:
            return {"ok": False, "message": f"La pool libvirt existe deja : {name}"}, 409

        xml_path = write_temp_xml(define_dir_pool_xml(name, path), "vm-pool-")
        try:
            rc, out = virsh(conf, "pool-define", xml_path, timeout=60)
        finally:
            try:
                os.unlink(xml_path)
            except OSError:
                pass
        if rc != 0:
            return {"ok": False, "message": out.strip() or "pool-define a echoue"}, 500

        build_rc, build_out = virsh(conf, "pool-build", name, timeout=60)
        if build_rc != 0 and build_out.strip():
            vm_log(conf, f"Pool {name}: pool-build non bloquant rc={build_rc}: {build_out.strip()}")

        start_rc, start_out = virsh(conf, "pool-start", name, timeout=60)
        if start_rc != 0:
            return {"ok": False, "message": start_out.strip() or f"Pool {name} definie mais impossible a demarrer."}, 500

        if str(payload.get("autostart", "1") or "1").lower() in {"1", "true", "yes", "on"}:
            auto_rc, auto_out = virsh(conf, "pool-autostart", name, timeout=30)
            if auto_rc != 0:
                return {"ok": False, "message": auto_out.strip() or f"Pool {name} demarree, mais autostart impossible."}, 500
        vm_log(conf, f"Pool ajoutee {name} -> {path}")
        return {"ok": True, "message": f"Pool {name} creee : {path}"}, 200

    if not name:
        return {"ok": False, "message": "Nom de pool manquant."}, 400

    if action == "add":
        name = clean_pool_name(str(payload.get("name", "") or ""))
        path = clean_abs_path(str(payload.get("path", "") or ""), "chemin pool")
        if str(payload.get("create_dir", "") or "").lower() in {"1", "true", "yes", "on"}:
            os.makedirs(path, exist_ok=True)
        rc, out = virsh(conf, "pool-define-as", name, "dir", "--target", path, timeout=60)
        if rc != 0:
            return {"ok": False, "message": out.strip() or "pool-define-as a Ã©chouÃ©"}, 500
        virsh(conf, "pool-build", name, timeout=60)
        virsh(conf, "pool-start", name, timeout=60)
        if str(payload.get("autostart", "1") or "1").lower() in {"1", "true", "yes", "on"}:
            virsh(conf, "pool-autostart", name, timeout=30)
        vm_log(conf, f"Pool ajoutÃ© {name} -> {path}")
        return {"ok": True, "message": f"Pool {name} crÃ©Ã© : {path}"}, 200
    mapping = {
        "start": ["pool-start", name],
        "stop": ["pool-destroy", name],
        "refresh": ["pool-refresh", name],
        "autostart": ["pool-autostart", name],
        "noautostart": ["pool-autostart", name, "--disable"],
        "delete": ["pool-undefine", name],
    }
    if action not in mapping:
        return {"ok": False, "message": f"Action pool inconnue : {action}"}, 400
    if action == "delete":
        virsh(conf, "pool-destroy", name, timeout=60)
    rc, out = virsh(conf, *mapping[action], timeout=60)
    if rc == 0:
        vm_log(conf, f"Pool {name}: {action}")
        return {"ok": True, "message": out.strip() or f"Pool {name}: {action} OK"}, 200
    return {"ok": False, "message": out.strip() or f"Action pool {action} Ã©chouÃ©e"}, 500


def define_network_xml(name: str, mode: str, bridge: str = "") -> str:
    root = ET.Element("network")
    ET.SubElement(root, "name").text = name
    if mode == "bridge":
        ET.SubElement(root, "forward", {"mode": "bridge"})
        ET.SubElement(root, "bridge", {"name": bridge or "br0"})
    elif mode == "isolated":
        ET.SubElement(root, "bridge", {"name": bridge or f"virbr-{name[:8]}"})
    else:
        ET.SubElement(root, "forward", {"mode": "nat"})
        ET.SubElement(root, "bridge", {"name": bridge or f"virbr-{name[:8]}"})
    return xml_to_string(root)


def do_network_action(conf: Dict[str, str], payload: Dict[str, object]) -> Tuple[Dict[str, object], int]:
    action = str(payload.get("action", "") or "").strip().lower()
    name = clean_network_name(str(payload.get("name", "") or "")) if payload.get("name") else ""
    if action == "add":
        name = clean_network_name(str(payload.get("name", "") or ""))
        mode = str(payload.get("mode", "nat") or "nat").strip().lower()
        bridge = str(payload.get("bridge", "") or "").strip()
        xml_text = define_network_xml(name, mode, bridge)
        path = write_temp_xml(xml_text, "vm-net-")
        try:
            rc, out = virsh(conf, "net-define", path, timeout=60)
        finally:
            try: os.unlink(path)
            except OSError: pass
        if rc != 0:
            return {"ok": False, "message": out.strip() or "net-define a Ã©chouÃ©"}, 500
        virsh(conf, "net-start", name, timeout=60)
        if str(payload.get("autostart", "1") or "1").lower() in {"1", "true", "yes", "on"}:
            virsh(conf, "net-autostart", name, timeout=30)
        vm_log(conf, f"RÃ©seau ajoutÃ© {name} mode={mode} bridge={bridge}")
        return {"ok": True, "message": f"RÃ©seau {name} crÃ©Ã©"}, 200
    mapping = {
        "start": ["net-start", name],
        "stop": ["net-destroy", name],
        "autostart": ["net-autostart", name],
        "noautostart": ["net-autostart", name, "--disable"],
        "delete": ["net-undefine", name],
    }
    if action not in mapping:
        return {"ok": False, "message": f"Action rÃ©seau inconnue : {action}"}, 400
    if action == "delete":
        virsh(conf, "net-destroy", name, timeout=60)
    rc, out = virsh(conf, *mapping[action], timeout=60)
    if rc == 0:
        vm_log(conf, f"RÃ©seau {name}: {action}")
        return {"ok": True, "message": out.strip() or f"RÃ©seau {name}: {action} OK"}, 200
    return {"ok": False, "message": out.strip() or f"Action rÃ©seau {action} Ã©chouÃ©e"}, 500
