#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# PART 002 - Inventaire VM, lecture XML, actions simples et export XML



def _expand_cpu_cpuset(cpuset: str) -> List[int]:
    """Convertit une valeur cpuset libvirt simple en liste d'ID CPU.

    Gère les formes courantes : "4", "4,5,12", "4-7,12-15".
    Les exclusions cpuset avancées ne sont pas utilisées par l'interface Yoleo.
    """
    out: List[int] = []
    seen: set[int] = set()
    for raw in re.split(r"[,\s]+", str(cpuset or "").strip()):
        part = raw.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a, b = [int(x) for x in part.split("-", 1)]
            except Exception:
                continue
            if a > b:
                a, b = b, a
            for value in range(a, b + 1):
                if value not in seen:
                    seen.add(value)
                    out.append(value)
            continue
        try:
            value = int(part)
        except Exception:
            continue
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def parse_domain_xml(conf: Dict[str, str], xml_text: str) -> Dict[str, object]:
    root = ET.fromstring(xml_text)
    os_node = root.find("./os")
    os_type = root.find("./os/type")
    memory_node = root.find("memory")
    current_memory_node = root.find("currentMemory")
    vcpu_node = root.find("vcpu")
    cpu_node = root.find("cpu")

    memory_bytes = memory_to_bytes(first_text(root, "memory"), node_attr(memory_node, "unit", "KiB"))
    current_memory_bytes = memory_to_bytes(first_text(root, "currentMemory"), node_attr(current_memory_node, "unit", "KiB"))

    disks: List[Dict[str, str]] = []
    for disk in root.findall("./devices/disk"):
        source = disk.find("source")
        target = disk.find("target")
        driver = disk.find("driver")
        boot = disk.find("boot")
        src = ""
        src_kind = ""
        if source is not None:
            for key in ("file", "dev", "dir", "name", "volume"):
                if source.get(key):
                    src = source.get(key) or ""
                    src_kind = key
                    break
        disks.append({
            "device": node_attr(disk, "device", "disk"),
            "type": node_attr(disk, "type", ""),
            "source": src,
            "source_kind": src_kind,
            "target": node_attr(target, "dev", ""),
            "bus": node_attr(target, "bus", ""),
            "driver": node_attr(driver, "name", ""),
            "format": node_attr(driver, "type", ""),
            "cache": node_attr(driver, "cache", ""),
            "discard": node_attr(driver, "discard", ""),
            "readonly": "oui" if disk.find("readonly") is not None else "",
            "boot_order": node_attr(boot, "order", ""),
            "size": disk_size_label(src) if src_kind in {"file", "dev"} else "",
        })

    nics: List[Dict[str, str]] = []
    for iface in root.findall("./devices/interface"):
        source = iface.find("source")
        model = iface.find("model")
        mac = iface.find("mac")
        target = iface.find("target")
        boot = iface.find("boot")
        source_value = ""
        source_kind = ""
        if source is not None:
            for key in ("bridge", "network", "dev", "mode"):
                if source.get(key):
                    source_value = source.get(key) or ""
                    source_kind = key
                    break
        nics.append({
            "type": node_attr(iface, "type", ""),
            "source": source_value,
            "source_kind": source_kind,
            "model": node_attr(model, "type", ""),
            "mac": node_attr(mac, "address", ""),
            "target": node_attr(target, "dev", ""),
            "boot_order": node_attr(boot, "order", ""),
        })

    graphics: List[Dict[str, str]] = []
    egl_rendernode = ""
    for gfx in root.findall("./devices/graphics"):
        gfx_type = node_attr(gfx, "type", "")
        gfx_rendernode = node_attr(gfx.find("gl"), "rendernode", "")
        if gfx_type == "egl-headless" and not egl_rendernode:
            egl_rendernode = gfx_rendernode
        graphics.append({
            "type": gfx_type,
            "kind": gfx_type,
            # port = VNC brut. Selon libvirt, ce n'est pas forcÃ©ment le port Ã  mettre
            # dans /wsproxy/. Le port noVNC est souvent dans l'attribut websocket.
            "port": node_attr(gfx, "port", ""),
            "websocket": node_attr(gfx, "websocket", ""),
            "listen": node_attr(gfx, "listen", ""),
            "tls_port": node_attr(gfx, "tlsPort", ""),
            "autoport": node_attr(gfx, "autoport", ""),
            "rendernode": gfx_rendernode,
        })

    videos: List[Dict[str, str]] = []
    for video in root.findall("./devices/video"):
        model = video.find("model")
        accel = model.find("acceleration") if model is not None else None
        model_type = node_attr(model, "type", "")
        accel3d = node_attr(accel, "accel3d", "").strip().lower() in {"yes", "on", "true", "1"}
        videos.append({
            "type": model_type,
            "model": model_type,
            "vram": node_attr(model, "vram", ""),
            "ram": node_attr(model, "ram", ""),
            "heads": node_attr(model, "heads", ""),
            "primary": node_attr(model, "primary", ""),
            "accel3d": "yes" if accel3d else "",
            "rendernode": egl_rendernode if accel3d else "",
        })

    hostdevs: List[Dict[str, str]] = []
    for hostdev in root.findall("./devices/hostdev"):
        htype = node_attr(hostdev, "type", "")
        item = {
            "mode": node_attr(hostdev, "mode", ""),
            "type": htype,
            "managed": node_attr(hostdev, "managed", ""),
            "address": "",
            "vendor": "",
            "product": "",
            "label": "",
        }
        if htype == "usb":
            vendor = node_attr(hostdev.find("./source/vendor"), "id", "").lower().replace("0x", "")
            product = node_attr(hostdev.find("./source/product"), "id", "").lower().replace("0x", "")
            item.update({
                "vendor": vendor,
                "product": product,
                "label": f"USB {vendor}:{product}".strip(),
            })
        else:
            source_addr = hostdev.find("./source/address")
            pci_addr = pci_address_from_xml(source_addr)
            item.update({
                "address": pci_addr,
                "label": lspci_label(conf, pci_addr),
            })
        hostdevs.append(item)

    channels: List[Dict[str, str]] = []
    for channel in root.findall("./devices/channel"):
        target = channel.find("target")
        source = channel.find("source")
        channels.append({
            "type": node_attr(channel, "type", ""),
            "target_name": node_attr(target, "name", ""),
            "target_type": node_attr(target, "type", ""),
            "source_path": node_attr(source, "path", ""),
        })

    consoles: List[Dict[str, str]] = []
    for console in root.findall("./devices/console"):
        target = console.find("target")
        source = console.find("source")
        consoles.append({
            "type": node_attr(console, "type", ""),
            "target_type": node_attr(target, "type", ""),
            "target_port": node_attr(target, "port", ""),
            "source_path": node_attr(source, "path", ""),
        })

    controllers: List[Dict[str, str]] = []
    for ctrl in root.findall("./devices/controller"):
        controllers.append({
            "type": node_attr(ctrl, "type", ""),
            "model": node_attr(ctrl, "model", ""),
            "index": node_attr(ctrl, "index", ""),
        })

    tpms: List[Dict[str, str]] = []
    for tpm in root.findall("./devices/tpm"):
        backend = tpm.find("backend")
        tpms.append({
            "model": node_attr(tpm, "model", ""),
            "backend_type": node_attr(backend, "type", ""),
            "version": node_attr(backend, "version", ""),
        })

    boot_devs = [node_attr(boot, "dev", "") for boot in root.findall("./os/boot") if node_attr(boot, "dev", "")]

    vcpu_pins: List[Dict[str, str]] = []
    pinned_host_cpus: List[int] = []
    pinned_seen: set[int] = set()
    for pin in root.findall("./cputune/vcpupin"):
        vcpu_id = node_attr(pin, "vcpu", "")
        cpuset = node_attr(pin, "cpuset", "")
        if not vcpu_id and not cpuset:
            continue
        vcpu_pins.append({"vcpu": vcpu_id, "cpuset": cpuset})
        for cpu_id in _expand_cpu_cpuset(cpuset):
            if cpu_id not in pinned_seen:
                pinned_seen.add(cpu_id)
                pinned_host_cpus.append(cpu_id)

    secure_boot_feature = root.find("./os/firmware/feature[@name='secure-boot']")
    secure_boot_enabled = node_attr(secure_boot_feature, "enabled", "").lower() in {"yes", "on", "true", "1"}
    if not secure_boot_enabled:
        secure_boot_enabled = node_attr(root.find("./os/loader"), "secure", "").lower() in {"yes", "on", "true", "1"}

    cpu_mode = node_attr(cpu_node, "mode", "").strip()
    cpu_mode_l = cpu_mode.lower()
    cpu_model = first_text(root, "./cpu/model").strip()
    topology_node = root.find("./cpu/topology")
    topo_sockets = node_attr(topology_node, "sockets", "")
    topo_cores = node_attr(topology_node, "cores", "")
    topo_threads = node_attr(topology_node, "threads", "")
    try:
        topo_total = int(topo_sockets or "0") * int(topo_cores or "0") * int(topo_threads or "0")
    except Exception:
        topo_total = 0
    if topo_sockets and topo_cores and topo_threads:
        topology_label = f"{topo_sockets} socket(s) / {topo_cores} coeur(s) / {topo_threads} thread(s)"
        if topo_total:
            topology_label += f" = {topo_total} vCPU"
        if topo_sockets == "1" and topo_threads == "1":
            topology_mode = "cores"
        elif topo_cores == "1" and topo_threads == "1":
            topology_mode = "sockets"
        else:
            topology_mode = "custom"
    else:
        topology_label = "non defini (libvirt choisit)"
        topology_mode = "cores"
    virt_type = node_attr(root, "type", "").strip()
    if cpu_mode_l == "host-passthrough":
        cpu_choice = "host-passthrough"
        cpu_label = "CPU hote direct"
    elif cpu_mode_l == "host-model":
        cpu_choice = "host-model"
        cpu_label = "CPU hote compatible"
    elif cpu_mode_l == "custom" and cpu_model.lower() in {"qemu64", "amd64"}:
        cpu_choice = "emulated"
        cpu_label = f"CPU logiciel emule ({cpu_model})"
    elif cpu_mode_l or cpu_model:
        cpu_choice = "custom"
        cpu_label = "CPU XML conserve"
        if cpu_mode:
            cpu_label += f" mode={cpu_mode}"
        if cpu_model:
            cpu_label += f" ({cpu_model})"
    else:
        cpu_choice = ""
        cpu_label = "CPU XML par defaut"
    if virt_type:
        cpu_label += f" / {virt_type}"

    return {
        "xml": xml_text,
        "uuid": first_text(root, "uuid"),
        "title": first_text(root, "title"),
        "description": first_text(root, "description"),
        "arch": node_attr(os_type, "arch", ""),
        "machine": node_attr(os_type, "machine", ""),
        "os_type": (os_type.text or "").strip() if os_type is not None and os_type.text else "",
        "virt_type": virt_type,
        "emulator": first_text(root, "./devices/emulator"),
        "firmware": node_attr(os_node, "firmware", ""),
        "secure_boot_enabled": secure_boot_enabled,
        "loader": first_text(root, "./os/loader"),
        "nvram": first_text(root, "./os/nvram"),
        "boot_devs": boot_devs,
        "vcpu": (vcpu_node.text or "").strip() if vcpu_node is not None and vcpu_node.text else "",
        "vcpu_current": node_attr(vcpu_node, "current", ""),
        "vcpu_pins": vcpu_pins,
        "cpu_pinset": ",".join(str(x) for x in pinned_host_cpus),
        "memory": human_bytes(memory_bytes),
        "current_memory": human_bytes(current_memory_bytes or memory_bytes),
        "cpu_mode": cpu_mode,
        "cpu_model": cpu_model,
        "cpu_choice": cpu_choice,
        "cpu_label": cpu_label,
        "cpu_topology": {
            "sockets": topo_sockets,
            "cores": topo_cores,
            "threads": topo_threads,
            "total": topo_total,
            "label": topology_label,
            "mode": topology_mode,
        },
        "cpu_topology_mode": topology_mode,
        "cpu_topology_sockets": topo_sockets,
        "cpu_topology_label": topology_label,
        "hyperv_enabled": root.find("./features/hyperv") is not None,
        "disks": disks,
        "nics": nics,
        "graphics": graphics,
        "videos": videos,
        "hostdevs": hostdevs,
        "channels": channels,
        "consoles": consoles,
        "controllers": controllers,
        "tpms": tpms,
        "has_qemu_agent": any("qemu.guest_agent" in (c.get("target_name") or "") for c in channels),
        "has_virtio_serial": any(str(c.get("target_type") or "").lower() == "virtio" for c in consoles),
        "has_pci": bool(hostdevs),
        "has_virtual_video": bool(videos),
        "has_tpm": bool(tpms),
    }


def list_vm_names(conf: Dict[str, str]) -> Tuple[List[str], Optional[str]]:
    rc, out = virsh(conf, "list", "--all", "--name", timeout=15)
    if rc != 0:
        return [], out.strip() or "virsh list a Ã©chouÃ©."
    names = [line.strip() for line in out.splitlines() if line.strip()]
    return sorted(names, key=str.lower), None


def get_vm_state(conf: Dict[str, str], name: str) -> str:
    rc, out = virsh(conf, "domstate", name, "--reason", timeout=15)
    if rc == 0 and out.strip():
        return out.strip().replace("\n", " ")
    rc, out = virsh(conf, "dominfo", name, timeout=15)
    if rc == 0:
        info = parse_key_values(out)
        return info.get("State", "unknown")
    return "unknown"


def state_class(state: str) -> str:
    low = (state or "").strip().lower()

    # Sorties virsh en anglais quand LC_ALL=C, plus quelques libellÃ©s FR
    # au cas oÃ¹ un vieux cache/log ou une autre commande renvoie du localisÃ©.
    running_words = (
        "running", "en cours", "actif", "active",
        "execution", "exÃ©cution", "demarre", "dÃ©marrÃ©", "demarree", "dÃ©marrÃ©e",
        "marche", "lancÃ©e", "lancee",
    )
    paused_words = ("paused", "pmsuspended", "suspended", "pause", "suspendu")
    stopped_words = (
        "shut off", "shutoff", "shut", "off", "crashed",
        "fermÃ©", "fermee", "arrÃªtÃ©", "arrete", "Ã©teint", "eteint",
        "stopped", "inactive", "dead", "poweroff", "powered off",
    )

    if any(word in low for word in running_words):
        return "running"
    if any(word in low for word in paused_words):
        return "paused"
    if any(word in low for word in stopped_words):
        return "stopped"

    # virsh domstate --reason peut donner "unknown" aprÃ¨s "shut off" selon les versions.
    # "unknown" seul reste unknown, mais "fermÃ© inconnu" est captÃ© plus haut.
    return "unknown"


def collect_one_vm(conf: Dict[str, str], name: str) -> Dict[str, object]:
    item: Dict[str, object] = {
        "name": name,
        "state": "unknown",
        "state_class": "unknown",
        "error": "",
    }

    rc_info, info_text = virsh(conf, "dominfo", name, timeout=15)
    info = parse_key_values(info_text) if rc_info == 0 else {}
    item["info"] = info

    state = get_vm_state(conf, name)
    item["state"] = state
    item["state_class"] = state_class(state)
    item["running"] = item["state_class"] == "running"

    rc_xml, xml_text = virsh(conf, "dumpxml", name, timeout=25)
    if rc_xml != 0:
        item["error"] = xml_text.strip() or "Impossible de lire le XML."
        return item

    try:
        parsed = parse_domain_xml(conf, xml_text)
        item.update(parsed)
    except Exception as exc:
        item["error"] = f"Erreur lecture XML : {exc}"

    # ComplÃ©ments dominfo, utiles quand le XML ne donne pas tout.
    if info:
        item.setdefault("uuid", info.get("UUID", ""))
        item["id"] = info.get("Id", "-")
        item["autostart"] = info.get("Autostart", "")
        item["persistent"] = info.get("Persistent", "")
        if not item.get("memory"):
            item["memory"] = info.get("Max memory", "")
        if not item.get("current_memory"):
            item["current_memory"] = info.get("Used memory", "")
        if not item.get("vcpu"):
            item["vcpu"] = info.get("CPU(s)", "")

    return item



def _read_proc_cpu_totals() -> Optional[Tuple[int, int]]:
    """Retourne (total_ticks, idle_ticks) depuis /proc/stat, ou None si indisponible."""
    try:
        with open("/proc/stat", "r", encoding="utf-8", errors="ignore") as fh:
            first = fh.readline().strip().split()
        if not first or first[0] != "cpu":
            return None
        values = [int(x) for x in first[1:]]
        if len(values) < 4:
            return None
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        return total, idle
    except Exception:
        return None


def _host_cpu_percent(sample_delay: float = 0.06) -> Optional[float]:
    """Petit échantillon CPU local pour l'affichage Ajax du résumé VM."""
    first = _read_proc_cpu_totals()
    if not first:
        return None
    try:
        time.sleep(max(0.02, min(float(sample_delay), 0.20)))
    except Exception:
        time.sleep(0.06)
    second = _read_proc_cpu_totals()
    if not second:
        return None
    total_delta = second[0] - first[0]
    idle_delta = second[1] - first[1]
    if total_delta <= 0:
        return None
    used = max(0.0, min(100.0, 100.0 * (1.0 - (idle_delta / total_delta))))
    return round(used, 1)


def _host_memory_values() -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """Retourne (total, used, available) en octets depuis /proc/meminfo."""
    try:
        values: Dict[str, int] = {}
        with open("/proc/meminfo", "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if ":" not in line:
                    continue
                key, raw = line.split(":", 1)
                parts = raw.strip().split()
                if not parts:
                    continue
                try:
                    values[key] = int(parts[0]) * 1024
                except Exception:
                    continue
        total = values.get("MemTotal")
        available = values.get("MemAvailable")
        if total is None:
            return None, None, None
        if available is None:
            available = values.get("MemFree", 0) + values.get("Buffers", 0) + values.get("Cached", 0)
        used = max(0, total - int(available or 0))
        return total, used, int(available or 0)
    except Exception:
        return None, None, None


def collect_host_live_stats() -> Dict[str, object]:
    """Stats hôte légères pour le résumé VM, mises à jour en Ajax sans redessiner le tableau."""
    cpu = _host_cpu_percent()
    ram_total, ram_used, ram_available = _host_memory_values()
    ram_percent: Optional[float] = None
    if ram_total and ram_used is not None:
        ram_percent = round((ram_used / ram_total) * 100.0, 1)
    return {
        "host_cpu_percent": cpu if cpu is not None else "",
        "host_cpu_label": f"{cpu:.0f}%" if cpu is not None else "—",
        "host_ram_total_bytes": ram_total or 0,
        "host_ram_used_bytes": ram_used or 0,
        "host_ram_available_bytes": ram_available or 0,
        "host_ram_total_label": human_bytes(ram_total) if ram_total is not None else "—",
        "host_ram_used_label": human_bytes(ram_used) if ram_used is not None else "—",
        "host_ram_available_label": human_bytes(ram_available) if ram_available is not None else "—",
        "host_ram_percent": ram_percent if ram_percent is not None else "",
        "host_ram_percent_label": f"{ram_percent:.0f}%" if ram_percent is not None else "—",
    }


def collect_inventory(conf: Dict[str, str]) -> Tuple[List[Dict[str, object]], Dict[str, object], Optional[str]]:
    names, error = list_vm_names(conf)
    vms = [collect_one_vm(conf, name) for name in names] if not error else []
    summary = {
        "total": len(vms),
        "running": sum(1 for vm in vms if vm.get("state_class") == "running"),
        "stopped": sum(1 for vm in vms if vm.get("state_class") == "stopped"),
        "paused": sum(1 for vm in vms if vm.get("state_class") == "paused"),
        "pci": sum(1 for vm in vms if vm.get("has_pci")),
        "qemu_agent": sum(1 for vm in vms if vm.get("has_qemu_agent")),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "virsh": conf.get("VIRSH_BIN", "virsh"),
        "uri": conf.get("LIBVIRT_URI", ""),
    }
    summary.update(collect_host_live_stats())
    return vms, summary, error


def action_message(action: str) -> str:
    return {
        "start": "DÃ©marrage demandÃ©.",
        "shutdown": "ArrÃªt propre demandÃ©.",
        "reboot": "RedÃ©marrage propre demandÃ©.",
        "suspend": "Mise en pause demandÃ©e.",
        "resume": "Reprise demandÃ©e.",
        "destroy": "ArrÃªt forcÃ© demandÃ©.",
        "reset": "Reset forcÃ© demandÃ©.",
        "delete": "Suppression demandÃ©e.",
        "remove": "Suppression demandÃ©e.",
        "undefine": "Suppression demandÃ©e.",
    }.get(action, "Action demandÃ©e.")


def _vm_start_error_is_kvm_unavailable(message: str) -> bool:
    low = (message or "").lower()
    return "kvm" in low and (
        "could not access kvm" in low
        or "failed to initialize kvm" in low
        or "dev/kvm" in low
        or "no such file or directory" in low
        or "permission denied" in low
    )


def _vm_start_error_message(message: str) -> str:
    text = (message or "").strip()
    if _vm_start_error_is_kvm_unavailable(text):
        friendly = (
            "Impossible de demarrer la VM : KVM indisponible ou inaccessible. "
            "Verifiez que la virtualisation est activee dans le BIOS, que les modules KVM "
            "sont charges et que /dev/kvm appartient au groupe kvm. "
            "La configuration CPU de la VM n'a pas ete modifiee."
        )
        return friendly + ("\n\nDetail libvirt/QEMU :\n" + text if text else "")
    return text


def _vm_action_state_payload(conf: Dict[str, str], name: str, ok: bool, message: str) -> Dict[str, object]:
    payload: Dict[str, object] = {
        "ok": ok,
        "message": message,
        "state": "unknown",
        "state_class": "unknown",
        "running": False,
    }
    names, err = list_vm_names(conf)
    if err:
        payload["state_error"] = err
        return payload
    if name not in names:
        payload["state_error"] = f"VM introuvable apres action : {name}"
        return payload
    vm = collect_one_vm(conf, name)
    payload["vm"] = vm
    payload["state"] = vm.get("state", "unknown")
    payload["state_class"] = vm.get("state_class", "unknown")
    payload["running"] = bool(vm.get("running"))
    if vm.get("error"):
        payload["state_error"] = vm.get("error")
    return payload


def do_vm_action(conf: Dict[str, str], name: str, action: str) -> Tuple[Dict[str, object], int]:
    name = clean_vm_name(name)
    action = (action or "").strip().lower()
    names, err = list_vm_names(conf)
    if err:
        return {"ok": False, "message": err}, 500
    if name not in names:
        return {"ok": False, "message": f"VM introuvable : {name}"}, 404

    if action in {"delete", "remove", "undefine"}:
        current_state = get_vm_state(conf, name)
        current_class = state_class(current_state)
        if current_class != "stopped":
            return {
                "ok": False,
                "message": f"Suppression refusÃ©e : la VM doit Ãªtre arrÃªtÃ©e. Ã‰tat actuel : {current_state or 'unknown'}.",
            }, 409

        try:
            backup_path = backup_domain_xml(conf, name, "delete")
        except Exception as exc:
            return {"ok": False, "message": f"Suppression refusÃ©e : sauvegarde XML impossible : {exc}"}, 500

        timeout = max(conf_int(conf, "ACTION_TIMEOUT", 90), 90)
        rc, out = virsh(conf, "undefine", name, timeout=timeout)
        message = (out or "").strip()

        # Certaines VM UEFI/libvirt refusent undefine sans --nvram.
        # On tente seulement ce fallback quand libvirt le demande clairement.
        if rc != 0 and any(word in message.lower() for word in ("nvram", "uefi")):
            rc, out = virsh(conf, "undefine", name, "--nvram", timeout=timeout)
            message = (out or "").strip()

        if rc == 0:
            return {
                "ok": True,
                "message": f"VM supprimÃ©e de libvirt. XML sauvegardÃ© : {backup_path}. Les disques ne sont pas effacÃ©s.",
                "backup": backup_path,
            }, 200
        return {
            "ok": False,
            "message": message or f"virsh undefine a Ã©chouÃ©. XML sauvegardÃ© : {backup_path}",
            "backup": backup_path,
        }, 500

    mapping = {
        "start": ["start", name],
        "shutdown": ["shutdown", name],
        "reboot": ["reboot", name],
        "suspend": ["suspend", name],
        "resume": ["resume", name],
        "destroy": ["destroy", name],
        "reset": ["reset", name],
    }
    if action not in mapping:
        return {"ok": False, "message": f"Action refusÃ©e : {action}"}, 400

    rc, out = virsh(conf, *mapping[action], timeout=int(conf.get("ACTION_TIMEOUT", "90") or "90"))
    message = (out or "").strip()
    if action == "start" and rc != 0:
        message = _vm_start_error_message(message)
    if rc == 0:
        return _vm_action_state_payload(conf, name, True, message or action_message(action)), 200
    message = message or f"virsh {action} a echoue."
    payload = _vm_action_state_payload(conf, name, False, message)
    payload["error"] = message
    return payload, 500
    return {"ok": False, "message": message or f"virsh {action} a Ã©chouÃ©."}, 500

def first_disk_dir_from_vm(vm: Dict[str, object]) -> str:
    for disk in vm.get("disks", []) or []:
        if not isinstance(disk, dict):
            continue
        src = str(disk.get("source", "") or "")
        if src and os.path.isabs(src) and os.path.exists(os.path.dirname(src)):
            return os.path.dirname(src)
    return ""


def export_vm_xml(conf: Dict[str, str], name: str) -> Tuple[Dict[str, object], int]:
    name = clean_vm_name(name)
    names, err = list_vm_names(conf)
    if err:
        return {"ok": False, "message": err}, 500
    if name not in names:
        return {"ok": False, "message": f"VM introuvable : {name}"}, 404

    vm = collect_one_vm(conf, name)
    xml_text = str(vm.get("xml", "") or "")
    if not xml_text:
        return {"ok": False, "message": f"XML introuvable pour {name}"}, 500

    base_dir = conf.get("XML_EXPORT_DIR", DEFAULT_CONFIG["XML_EXPORT_DIR"]).strip() or DEFAULT_CONFIG["XML_EXPORT_DIR"]
    export_dir = ""
    if conf.get("XML_EXPORT_MODE", "first_disk_dir").strip().lower() == "first_disk_dir":
        export_dir = first_disk_dir_from_vm(vm)
    if not export_dir:
        export_dir = os.path.join(base_dir, safe_filename(name))

    try:
        os.makedirs(export_dir, exist_ok=True)
        path = os.path.join(export_dir, safe_filename(name) + ".xml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(xml_text)
            if not xml_text.endswith("\n"):
                handle.write("\n")
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
        return {"ok": True, "message": f"XML exportÃ© : {path}", "path": path}, 200
    except Exception as exc:
        return {"ok": False, "message": f"Export XML impossible : {exc}"}, 500


def dump_vm_xml_text(conf: Dict[str, str], name: str) -> Tuple[bool, str, int]:
    """Retourne le XML libvirt actuel d'une VM existante."""
    name = clean_vm_name(name)
    names, err = list_vm_names(conf)
    if err:
        return False, err, 500
    if name not in names:
        return False, f"VM introuvable : {name}", 404
    rc, xml_text = virsh(conf, "dumpxml", name, timeout=25)
    if rc != 0:
        return False, (xml_text or "").strip() or "dumpxml a echoue", 500
    return True, xml_text, 200


def export_vm_xml_to_folder(conf: Dict[str, str], name: str, folder: str) -> Tuple[Dict[str, object], int]:
    """Exporte NOM_VM.xml dans un dossier choisi explicitement par l'utilisateur."""
    raw_folder = str(folder or "").strip()
    if not raw_folder:
        return {"ok": False, "message": "Dossier d'export manquant."}, 400
    folder = os.path.abspath(os.path.expanduser(os.path.expandvars(raw_folder)))
    if not os.path.isdir(folder):
        return {"ok": False, "message": f"Dossier introuvable : {folder}"}, 404

    ok, xml_text, status = dump_vm_xml_text(conf, name)
    if not ok:
        return {"ok": False, "message": xml_text}, status

    safe = safe_filename(name)
    path = os.path.join(folder, safe + ".xml")
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(xml_text)
            if not xml_text.endswith("\n"):
                handle.write("\n")
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
        vm_log(conf, f"XML exporté sur serveur : {name} -> {path}")
        return {"ok": True, "message": f"XML exporté : {path}", "path": path, "name": name}, 200
    except Exception as exc:
        return {"ok": False, "message": f"Export XML impossible : {exc}"}, 500


def _domain_name_from_xml(xml_text: str) -> str:
    try:
        root = ET.fromstring(xml_text or "")
    except Exception as exc:
        raise ValueError(f"XML illisible : {exc}")
    if root.tag != "domain":
        raise ValueError("XML refusé : la racine doit être <domain>.")
    name_node = root.find("name")
    name = (name_node.text if name_node is not None else "") or ""
    name = name.strip()
    if not name:
        raise ValueError("XML refusé : balise <name> manquante.")
    clean_vm_name(name)
    return name


def import_vm_xml_text(conf: Dict[str, str], xml_text: str, source_label: str = "") -> Tuple[Dict[str, object], int]:
    """Déclare une VM libvirt depuis un XML fourni par le serveur ou le PC client."""
    xml_text = str(xml_text or "")
    if not xml_text.strip():
        return {"ok": False, "message": "XML vide."}, 400
    if len(xml_text.encode("utf-8", errors="ignore")) > 8 * 1024 * 1024:
        return {"ok": False, "message": "XML trop volumineux pour une définition VM."}, 413

    try:
        name = _domain_name_from_xml(xml_text)
    except Exception as exc:
        return {"ok": False, "message": str(exc)}, 400

    names, err = list_vm_names(conf)
    if err:
        return {"ok": False, "message": err}, 500
    if name in names:
        return {"ok": False, "message": f"Une VM existe déjà avec ce nom : {name}"}, 409

    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".xml", prefix="yoleo-vm-import-", delete=False) as handle:
            handle.write(xml_text)
            if not xml_text.endswith("\n"):
                handle.write("\n")
            tmp_path = handle.name
        rc, out = virsh(conf, "define", tmp_path, timeout=max(conf_int(conf, "ACTION_TIMEOUT", 90), 90))
        if rc != 0:
            return {"ok": False, "message": (out or "").strip() or f"virsh define a échoué pour {name}"}, 500
        vm_log(conf, f"VM importée depuis XML : {name}" + (f" ({source_label})" if source_label else ""))
        return {"ok": True, "message": f"VM importée : {name}", "name": name}, 200
    except Exception as exc:
        return {"ok": False, "message": f"Import XML impossible : {exc}"}, 500
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def import_vm_xml_from_server(conf: Dict[str, str], path: str) -> Tuple[Dict[str, object], int]:
    raw_path = str(path or "").strip()
    if not raw_path:
        return {"ok": False, "message": "Chemin XML manquant."}, 400
    path = os.path.abspath(os.path.expanduser(os.path.expandvars(raw_path)))
    if not os.path.isfile(path):
        return {"ok": False, "message": f"Fichier XML introuvable : {path}"}, 404
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            xml_text = handle.read(8 * 1024 * 1024 + 1)
    except Exception as exc:
        return {"ok": False, "message": f"Lecture XML impossible : {exc}"}, 500
    return import_vm_xml_text(conf, xml_text, path)
