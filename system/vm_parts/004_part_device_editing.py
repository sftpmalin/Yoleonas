#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# PART 004 - Modification VM, disques, cartes réseau et périphériques




# =============================================================================
# Extensions onglets VM / libvirt host : pools, rÃ©seaux, matÃ©riel, logs
# =============================================================================

SAFE_POOL_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
SAFE_NET_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
SAFE_TARGET_RE = re.compile(r"^(?:[a-z]{2,4}[0-9]*|nvme[0-9]+n[0-9]+)$")
SAFE_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")
SAFE_PCI_RE = re.compile(r"^(?:[0-9a-fA-F]{4}:)?[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")
SAFE_USB_ID_RE = re.compile(r"^[0-9a-fA-F]{4}$")
NVME_VIRTUAL_DISK_MIN_VERSION = (11, 5, 0)
NVME_TARGET_RE = re.compile(r"^nvme([0-9]+)n([1-9][0-9]*)$")


def libvirt_version_info(conf: Dict[str, str]) -> Tuple[Optional[Tuple[int, int, int]], str]:
    """Return the local libvirt version without relying on distribution names."""
    rc, out = virsh(conf, "--version", timeout=10)
    raw = (out or "").strip()
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", raw)
    if rc != 0 or not match:
        return None, raw
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)), raw


def virtual_nvme_disk_status(conf: Dict[str, str]) -> Dict[str, object]:
    """Describe whether this libvirt supports the standard emulated NVMe XML."""
    version, raw = libvirt_version_info(conf)
    required = ".".join(str(part) for part in NVME_VIRTUAL_DISK_MIN_VERSION)
    return {
        "supported": bool(version and version >= NVME_VIRTUAL_DISK_MIN_VERSION),
        "version": ".".join(str(part) for part in version) if version else (raw or "inconnue"),
        "required": required,
    }


def require_virtual_nvme_disk_support(conf: Dict[str, str]) -> None:
    status = virtual_nvme_disk_status(conf)
    if not status["supported"]:
        raise ValueError(
            f"NVMe virtuel requiert libvirt {status['required']} ou plus recent "
            f"(version installee : {status['version']})."
        )


def nvme_disk_serial(target: str) -> str:
    """Generate one stable controller serial for a libvirt NVMe target."""
    match = NVME_TARGET_RE.fullmatch((target or "").strip().lower())
    if not match:
        raise ValueError("Target NVMe invalide. Exemple attendu : nvme0n1.")
    return f"yoleo-nvme{int(match.group(1))}"


def vm_log_path(conf: Dict[str, str]) -> str:
    path = resolve_module_path(str(conf.get("VM_LOG_FILE", "") or DEFAULT_CONFIG["VM_LOG_FILE"]), DEFAULT_CONFIG["VM_LOG_FILE"])
    return path or resolve_module_path(DEFAULT_CONFIG["VM_LOG_FILE"])


def vm_log(conf: Dict[str, str], message: str) -> None:
    try:
        path = vm_log_path(conf)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + message.rstrip() + "\n")
    except Exception:
        pass


def require_vm_stopped(conf: Dict[str, str], name: str) -> Tuple[bool, str]:
    state = get_vm_state(conf, name)
    cls = state_class(state)
    if cls in {"running", "paused"}:
        return False, "Pour modifier le materiel persistant, arrete la VM avant. Libvirt evitera un XML bancal."
    # Si virsh renvoie un Ã©tat inconnu mais pas running/paused, on ne bloque pas bÃªtement
    # l'Ã©dition : libvirt refusera de toute faÃ§on si la VM est rÃ©ellement active.
    return True, ""


def clean_pool_name(name: str) -> str:
    name = (name or "").strip()
    if not name or not SAFE_POOL_RE.match(name):
        raise ValueError("Nom de pool invalide.")
    return name


def clean_network_name(name: str) -> str:
    name = (name or "").strip()
    if not name or not SAFE_NET_RE.match(name):
        raise ValueError("Nom de reseau invalide.")
    return name


def clean_abs_path(path: str, label: str = "chemin") -> str:
    path = (path or "").strip()
    if not path or "\x00" in path or not os.path.isabs(path):
        raise ValueError(f"{label} invalide : il faut un chemin absolu.")
    return path


def parse_pci_address(addr: str) -> Dict[str, str]:
    addr = (addr or "").strip()
    if not SAFE_PCI_RE.match(addr):
        raise ValueError("Adresse PCI invalide. Exemple : 0000:01:00.0")
    if addr.count(":") == 1:
        addr = "0000:" + addr
    domain, bus, rest = addr.split(":")
    slot, function = rest.split(".")
    return {
        "domain": "0x" + domain.lower().zfill(4),
        "bus": "0x" + bus.lower().zfill(2),
        "slot": "0x" + slot.lower().zfill(2),
        "function": "0x" + function.lower(),
    }


def domain_xml_root(conf: Dict[str, str], name: str) -> ET.Element:
    rc, xml_text = virsh(conf, "dumpxml", name, timeout=25)
    if rc != 0:
        raise RuntimeError(xml_text.strip() or "dumpxml a echoue")
    return ET.fromstring(xml_text)


def backup_domain_xml(conf: Dict[str, str], name: str, reason: str = "edit") -> str:
    root = domain_xml_root(conf, name)
    xml_text = xml_to_string(root)
    base = resolve_module_path(str(conf.get("XML_BACKUP_DIR", "") or DEFAULT_CONFIG["XML_BACKUP_DIR"]), DEFAULT_CONFIG["XML_BACKUP_DIR"])
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, f"{safe_filename(name)}_{time.strftime('%Y%m%d_%H%M%S')}_{safe_filename(reason)}.xml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(xml_text)
        if not xml_text.endswith("\n"):
            handle.write("\n")
    return path


def write_temp_xml(xml_text: str, prefix: str = "vm-device-") -> str:
    import tempfile
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=".xml")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(xml_text)
        if not xml_text.endswith("\n"):
            handle.write("\n")
    return path


def virsh_device(conf: Dict[str, str], name: str, action: str, fragment_xml: str, live: bool = False) -> Tuple[int, str]:
    path = write_temp_xml(fragment_xml)
    try:
        args = [action, name, path, "--config"]
        if live and conf_bool(conf, "ALLOW_LIVE_DEVICE_CHANGES", "0"):
            args.append("--live")
        return virsh(conf, *args, timeout=int(conf.get("ACTION_TIMEOUT", "90") or "90"))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def define_domain_root(conf: Dict[str, str], root: ET.Element) -> Tuple[int, str]:
    path = write_temp_xml(xml_to_string(root), "vm-define-")
    try:
        return virsh(conf, "define", path, timeout=max(conf_int(conf, "ACTION_TIMEOUT", 90), 90))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass




def _vm_positive_int_or_none(value: object, label: str, max_value: int = 256) -> Optional[int]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if not re.fullmatch(r"[0-9]{1,3}", raw):
        raise ValueError(f"{label} invalide.")
    number = int(raw)
    if number < 1 or number > max_value:
        raise ValueError(f"{label} invalide : valeur attendue entre 1 et {max_value}.")
    return number


def _vm_parse_cpu_pinset(value: object) -> List[int]:
    raw = str(value or "").strip()
    if not raw:
        return []
    out: List[int] = []
    seen: set[int] = set()
    for part in re.split(r"[,\s]+", raw):
        part = part.strip()
        if not part:
            continue
        if not re.fullmatch(r"[0-9]{1,4}", part):
            raise ValueError(f"CPU hote invalide : {part}")
        number = int(part)
        if number < 0 or number > 4095:
            raise ValueError(f"CPU hote invalide : {part}")
        if number not in seen:
            seen.add(number)
            out.append(number)
    return out


def _vm_cpu_mode_payload(value: object) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "software": "emulated",
        "emule": "emulated",
        "emulated": "emulated",
        "qemu64": "emulated",
        "amd64": "emulated",
        "hardware": "host-passthrough",
        "host": "host-passthrough",
        "host-passthrough": "host-passthrough",
        "passthrough": "host-passthrough",
        "host-model": "host-model",
        "model": "host-model",
    }
    if raw in {"", "keep"}:
        return ""
    if raw not in aliases:
        raise ValueError("Mode CPU invalide.")
    return aliases[raw]


def _vm_cpu_topology_mode_payload(value: object) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "": "",
        "keep": "",
        "auto": "cores",
        "core": "cores",
        "cores": "cores",
        "windows": "cores",
        "socket": "sockets",
        "sockets": "sockets",
        "legacy": "sockets",
        "custom": "custom",
        "manual": "custom",
    }
    if raw not in aliases:
        raise ValueError("Topologie CPU invalide.")
    return aliases[raw]


def _vm_current_cpu_choice(root: ET.Element) -> str:
    cpu_node = root.find("cpu")
    if cpu_node is None:
        return ""
    mode = node_attr(cpu_node, "mode", "").strip().lower()
    model = first_text(root, "./cpu/model").strip().lower()
    if mode == "host-passthrough":
        return "host-passthrough"
    if mode == "host-model":
        return "host-model"
    if mode == "custom" and model in {"qemu64", "amd64"}:
        return "emulated"
    if mode or model:
        return "custom"
    return ""


def _vm_vcpu_max_from_root(root: ET.Element, fallback: Optional[int] = None) -> int:
    vcpu_node = root.find("vcpu")
    raw = (vcpu_node.text or "").strip() if vcpu_node is not None and vcpu_node.text else ""
    try:
        value = int(raw or str(fallback or 1))
    except Exception:
        value = fallback or 1
    return max(1, min(value, 256))


def _vm_insert_cpu_node(root: ET.Element) -> ET.Element:
    cpu_node = ET.Element("cpu")
    children = list(root)
    for marker in ("clock", "on_poweroff", "on_reboot", "on_crash", "pm", "devices"):
        node = root.find(marker)
        if node is not None:
            try:
                root.insert(children.index(node), cpu_node)
                return cpu_node
            except ValueError:
                pass
    root.append(cpu_node)
    return cpu_node


def set_domain_cpu_topology(root: ET.Element, mode: str, sockets_raw: object, vcpu_max: Optional[int] = None) -> bool:
    mode = _vm_cpu_topology_mode_payload(mode)
    if not mode:
        return False
    total = vcpu_max or _vm_vcpu_max_from_root(root)
    total = max(1, min(int(total), 256))

    if mode == "cores":
        sockets = 1
        cores = total
    elif mode == "sockets":
        sockets = total
        cores = 1
    else:
        sockets = _vm_positive_int_or_none(sockets_raw, "Sockets CPU", max_value=256) or 1
        if sockets > total:
            raise ValueError(f"Sockets CPU invalide : {sockets} socket(s) pour {total} vCPU.")
        if total % sockets != 0:
            raise ValueError(f"Topologie CPU invalide : {total} vCPU ne se divisent pas en {sockets} socket(s).")
        cores = total // sockets
    threads = 1

    cpu_node = root.find("cpu")
    if cpu_node is None:
        cpu_node = _vm_insert_cpu_node(root)
    topology = cpu_node.find("topology")
    if topology is None:
        topology = ET.SubElement(cpu_node, "topology")
    wanted = {"sockets": str(sockets), "cores": str(cores), "threads": str(threads)}
    changed = False
    for key, value in wanted.items():
        if topology.get(key) != value:
            topology.set(key, value)
            changed = True
    return changed


def set_domain_cpu_mode(root: ET.Element, wanted: str) -> bool:
    wanted = _vm_cpu_mode_payload(wanted)
    if not wanted:
        return False
    needs_kvm_type = wanted in {"host-passthrough", "host-model"} and root.get("type") != "kvm"
    if _vm_current_cpu_choice(root) == wanted and not needs_kvm_type:
        return False

    cpu_node = root.find("cpu")
    if cpu_node is None:
        if wanted == "emulated":
            return False
        cpu_node = _vm_insert_cpu_node(root)

    keep_children = [child for child in list(cpu_node) if child.tag in {"topology", "numa", "cache"}]
    cpu_node.attrib.clear()
    for child in list(cpu_node):
        cpu_node.remove(child)

    if wanted in {"host-passthrough", "host-model"}:
        if root.get("type") != "kvm":
            root.set("type", "kvm")
    elif wanted == "emulated":
        # Une VM creee avant l'activation SVM peut etre en qemu logiciel.
        # On garde qemu uniquement quand l'utilisateur demande explicitement le CPU emule.
        if root.get("type") not in {"qemu", "kvm"}:
            root.set("type", "qemu")

    if wanted == "host-passthrough":
        cpu_node.set("mode", "host-passthrough")
        cpu_node.set("check", "none")
    elif wanted == "host-model":
        cpu_node.set("mode", "host-model")
        cpu_node.set("check", "partial")
    else:
        cpu_node.set("mode", "custom")
        cpu_node.set("match", "exact")
        cpu_node.set("check", "partial")
        model = ET.SubElement(cpu_node, "model", {"fallback": "allow"})
        model.text = "qemu64"

    for child in keep_children:
        cpu_node.append(child)
    return True


def set_domain_vcpu_and_pinning(root: ET.Element, current: Optional[int], maximum: Optional[int], pinset: List[int], pinning_requested: bool) -> bool:
    """Met à jour <vcpu> et <cputune>/<vcpupin> directement dans le XML.

    Cela évite le bug classique virsh setvcpus --config qui refuse 3 vCPU
    quand le domaine persistant a encore un maximum à 2.
    """
    if pinset:
        # Dans l'interface graphique, la sélection des CPU hôte est la source
        # de vérité : 8 cases cochées = 8 vCPU actifs.
        current = len(pinset)
        if maximum is None or maximum < current:
            maximum = current
    if current is None and maximum is None and not pinning_requested:
        return False

    vcpu_node = root.find("vcpu")
    if vcpu_node is None:
        vcpu_node = ET.SubElement(root, "vcpu", {"placement": "static"})

    old_text = (vcpu_node.text or "").strip()
    old_current = node_attr(vcpu_node, "current", "")

    if maximum is None:
        try:
            maximum = int(old_text or old_current or str(current or 1))
        except Exception:
            maximum = current or 1
    if current is None:
        try:
            current = int(old_current or old_text or str(maximum))
        except Exception:
            current = maximum
    if current < 1 or maximum < 1:
        raise ValueError("vCPU invalide.")
    if current > maximum:
        maximum = current

    changed = False
    if vcpu_node.get("placement") != "static":
        vcpu_node.set("placement", "static")
        changed = True
    if old_text != str(maximum):
        vcpu_node.text = str(maximum)
        changed = True
    wanted_current_attr = "" if current == maximum else str(current)
    if wanted_current_attr:
        if old_current != wanted_current_attr:
            vcpu_node.set("current", wanted_current_attr)
            changed = True
    else:
        if "current" in vcpu_node.attrib:
            vcpu_node.attrib.pop("current", None)
            changed = True

    if pinning_requested:
        cputune = root.find("cputune")
        if cputune is None and pinset:
            cputune = ET.Element("cputune")
            # Place cputune juste après <cpu> si possible, sinon avant <clock>/<devices>.
            children = list(root)
            insert_at = len(children)
            for idx, child in enumerate(children):
                if child.tag == "cpu":
                    insert_at = idx + 1
                    break
                if child.tag in {"clock", "on_poweroff", "devices"}:
                    insert_at = idx
                    break
            root.insert(insert_at, cputune)
        if cputune is not None:
            for node in list(cputune.findall("vcpupin")):
                cputune.remove(node)
            if pinset:
                for guest_vcpu, host_cpu in enumerate(pinset[:current]):
                    ET.SubElement(cputune, "vcpupin", {"vcpu": str(guest_vcpu), "cpuset": str(host_cpu)})
            if len(list(cputune)) == 0:
                root.remove(cputune)
            changed = True

    return changed


def parse_memory_gb_value(value: object, label: str) -> Optional[int]:
    raw = str(value or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        gb = float(raw)
    except Exception:
        raise ValueError(f"{label} invalide.")
    if gb <= 0:
        raise ValueError(f"{label} invalide.")
    return int(gb * 1024 * 1024)


def set_domain_memory_kib(root: ET.Element, current_kib: Optional[int], max_kib: Optional[int]) -> bool:
    changed = False
    if max_kib is not None:
        node = root.find("memory")
        if node is None:
            node = ET.SubElement(root, "memory")
        if node.get("unit") != "KiB":
            node.set("unit", "KiB")
            changed = True
        next_text = str(max_kib)
        if (node.text or "").strip() != next_text:
            node.text = next_text
            changed = True
    if current_kib is not None:
        node = root.find("currentMemory")
        if node is None:
            node = ET.SubElement(root, "currentMemory")
        if node.get("unit") != "KiB":
            node.set("unit", "KiB")
            changed = True
        next_text = str(current_kib)
        if (node.text or "").strip() != next_text:
            node.text = next_text
            changed = True
    return changed


def non_secure_ovmf_path(path: str) -> str:
    if not path:
        return path
    low = path.lower()
    candidates: List[str] = []
    if low.endswith(".ms.fd"):
        candidates.append(path[:-6] + ".fd")
    if ".ms." in low:
        candidates.append(re.sub(r"\.ms(?=\.fd$)", "", path, flags=re.IGNORECASE))
    if "secboot" in low:
        candidates.append(re.sub("secboot", "", path, flags=re.IGNORECASE))
    if "secure" in low:
        candidates.append(re.sub("secure", "", path, flags=re.IGNORECASE))
    for candidate in candidates:
        while ".." in candidate:
            candidate = candidate.replace("..", ".")
        if candidate != path and os.path.isfile(candidate):
            return candidate
    return path


def existing_disk_targets(root: ET.Element) -> set:
    out = set()
    for target in root.findall("./devices/disk/target"):
        dev = node_attr(target, "dev", "")
        if dev:
            out.add(dev)
    return out


def next_disk_target(root: ET.Element, bus: str, device: str = "disk") -> str:
    used = existing_disk_targets(root)
    if bus == "nvme":
        for index in range(64):
            candidate = f"nvme{index}n1"
            if candidate not in used:
                return candidate
        raise RuntimeError("Aucun target disque NVMe libre trouve.")
    if bus == "virtio":
        prefix = "vd"
    elif bus in {"ide"}:
        prefix = "hd"
    else:
        prefix = "sd"
    for letter in "abcdefghijklmnopqrstuvwxyz":
        candidate = prefix + letter
        if candidate not in used:
            return candidate
    raise RuntimeError("Aucun target disque libre trouve.")


def disk_fragment(source: str, source_kind: str, device: str, bus: str, target: str, fmt: str = "", cache: str = "", discard: str = "") -> str:
    source = clean_abs_path(source, "source disque")
    device = device if device in {"disk", "cdrom"} else "disk"
    bus = bus if bus in {"virtio", "sata", "scsi", "ide", "usb", "nvme"} else "virtio"
    if target and not SAFE_TARGET_RE.match(target):
        raise ValueError("Target disque invalide.")
    root = ET.Element("disk", {"type": "block" if source_kind == "dev" else "file", "device": device})
    driver_attrs = {"name": "qemu"}
    if fmt:
        driver_attrs["type"] = fmt
    if cache:
        driver_attrs["cache"] = cache
    if discard:
        driver_attrs["discard"] = discard
    ET.SubElement(root, "driver", driver_attrs)
    ET.SubElement(root, "source", {"dev" if source_kind == "dev" else "file": source})
    ET.SubElement(root, "target", {"dev": target, "bus": bus})
    if bus == "nvme":
        ET.SubElement(root, "serial").text = nvme_disk_serial(target)
    if device == "cdrom":
        ET.SubElement(root, "readonly")
    return xml_to_string(root)


def nic_fragment(kind: str, source: str, model: str = "virtio", mac: str = "") -> str:
    kind = kind if kind in {"bridge", "network", "direct"} else "bridge"
    source = (source or "").strip()
    if not source:
        raise ValueError("Source reseau manquante.")
    model = model if model in {"virtio", "e1000", "e1000e", "rtl8139", "vmxnet3"} else "virtio"
    root = ET.Element("interface", {"type": "bridge" if kind == "bridge" else kind})
    if mac:
        if not SAFE_MAC_RE.match(mac):
            raise ValueError("Adresse MAC invalide.")
        ET.SubElement(root, "mac", {"address": mac.lower()})
    if kind == "bridge":
        ET.SubElement(root, "source", {"bridge": source})
    elif kind == "network":
        ET.SubElement(root, "source", {"network": source})
    else:
        ET.SubElement(root, "source", {"dev": source, "mode": "bridge"})
    ET.SubElement(root, "model", {"type": model})
    return xml_to_string(root)


def pci_fragment(address: str, managed: str = "yes") -> str:
    attrs = parse_pci_address(address)
    root = ET.Element("hostdev", {"mode": "subsystem", "type": "pci", "managed": "yes" if managed != "no" else "no"})
    source = ET.SubElement(root, "source")
    ET.SubElement(source, "address", attrs)
    return xml_to_string(root)


def usb_fragment(vendor: str, product: str, managed: str = "yes") -> str:
    vendor = (vendor or "").lower().replace("0x", "")
    product = (product or "").lower().replace("0x", "")
    if not SAFE_USB_ID_RE.match(vendor) or not SAFE_USB_ID_RE.match(product):
        raise ValueError("Vendor/Product USB invalides. Exemple : 046d / c52b")
    root = ET.Element("hostdev", {"mode": "subsystem", "type": "usb", "managed": "yes" if managed != "no" else "no"})
    source = ET.SubElement(root, "source")
    ET.SubElement(source, "vendor", {"id": "0x" + vendor})
    ET.SubElement(source, "product", {"id": "0x" + product})
    return xml_to_string(root)


def graphics_fragment(kind: str = "vnc", listen: str = "0.0.0.0") -> str:
    kind = kind if kind in {"vnc", "spice"} else "vnc"
    listen = (listen or "0.0.0.0").strip()
    root = ET.Element("graphics", {"type": kind, "port": "-1", "autoport": "yes", "listen": listen})
    ET.SubElement(root, "listen", {"type": "address", "address": listen})
    return xml_to_string(root)


def _video_accel3d_payload(value: object) -> bool:
    parsed = _vm_bool_payload(value)
    return bool(parsed)


def video_fragment(model: str = "bochs", heads: str = "1", primary: str = "yes", accel3d: object = False) -> str:
    raw_model = str(model or "bochs").strip().lower().replace("_", "-")
    accel3d_enabled = _video_accel3d_payload(accel3d) or raw_model in {"virtio-3d", "virtio3d"}
    model = "virtio" if raw_model in {"virtio-3d", "virtio3d"} else raw_model
    model = model if model in {"virtio", "qxl", "vga", "cirrus", "bochs"} else "bochs"
    if accel3d_enabled:
        model = "virtio"
    try:
        heads_int = max(1, min(16, int(str(heads or "1"))))
    except Exception:
        heads_int = 1
    root = ET.Element("video")
    attrs = {"type": model, "heads": str(heads_int)}
    if primary in {"yes", "no"}:
        attrs["primary"] = primary
    model_node = ET.SubElement(root, "model", attrs)
    if accel3d_enabled:
        ET.SubElement(model_node, "acceleration", {"accel3d": "yes"})
    return xml_to_string(root)


def _video3d_qemu_bin(conf: Dict[str, str]) -> str:
    return str(conf.get("QEMU_SYSTEM_BIN") or DEFAULT_CONFIG.get("QEMU_SYSTEM_BIN") or "qemu-system-x86_64").strip() or "qemu-system-x86_64"


def _video3d_qemu_has_virgl(conf: Dict[str, str]) -> Tuple[bool, str]:
    qemu_bin = _video3d_qemu_bin(conf)
    if shutil.which(qemu_bin) is None and not os.path.isabs(qemu_bin):
        return False, f"Commande QEMU introuvable : {qemu_bin}"

    rc_display, display_out = run_cmd([qemu_bin, "-display", "help"], timeout=10)
    display_ok = rc_display == 0 and "egl-headless" in (display_out or "")

    rc_device, device_out = run_cmd([qemu_bin, "-device", "virtio-vga-gl,help"], timeout=10)
    device_ok = rc_device == 0 and "virtio-vga-gl" in (device_out or "")

    if display_ok and device_ok:
        return True, "QEMU OpenGL/VirGL disponible."

    details = []
    if not display_ok:
        details.append("backend egl-headless absent")
    if not device_ok:
        details.append("peripherique virtio-vga-gl absent")
    return False, ", ".join(details) or "QEMU OpenGL/VirGL indisponible"


def _video3d_try_install_qemu_opengl(conf: Dict[str, str]) -> List[str]:
    messages: List[str] = []
    if not conf_bool(conf, "VM_VIDEO3D_AUTO_INSTALL_QEMU_OPENGL", "1"):
        return messages
    if os.geteuid() != 0:
        messages.append("Installation QEMU OpenGL ignoree : le service Flask ne tourne pas en root.")
        return messages
    if shutil.which("apt-get") is None:
        messages.append("Installation QEMU OpenGL ignoree : apt-get introuvable.")
        return messages

    packages = shlex.split(str(conf.get("VM_VIDEO3D_QEMU_OPENGL_PACKAGES", "") or DEFAULT_CONFIG.get("VM_VIDEO3D_QEMU_OPENGL_PACKAGES", "")))
    packages = [pkg for pkg in packages if pkg]
    if not packages:
        return messages

    rc, out = run_cmd(["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", *packages], timeout=180)
    if rc == 0:
        messages.append("Modules QEMU OpenGL/VirGL installes : " + ", ".join(packages))
    else:
        messages.append((out or "").strip() or "Installation QEMU OpenGL/VirGL echouee.")
    return messages


def _video3d_ensure_qemu_virgl(conf: Dict[str, str]) -> None:
    ok, detail = _video3d_qemu_has_virgl(conf)
    if ok:
        return

    install_messages = _video3d_try_install_qemu_opengl(conf)
    ok, detail_after = _video3d_qemu_has_virgl(conf)
    if ok:
        return

    hint = "Paquets Debian attendus : qemu-system-modules-opengl libvirglrenderer1."
    extra = (" ".join(install_messages)).strip()
    if extra:
        extra = " " + extra
    raise ValueError(f"L'acceleration 3D QEMU/VirGL n'est pas disponible ({detail_after or detail}). {hint}{extra}")


def _video3d_rendernode_pci(path: str) -> str:
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


def _video3d_rendernode_score(conf: Dict[str, str], path: str, qemu_user: str) -> Tuple[int, str]:
    pci = _video3d_rendernode_pci(path)
    label = (lspci_label(conf, pci) if pci else "").lower()
    prefer = str(conf.get("VM_VIDEO3D_PREFER_GPU", "intel") or "intel").strip().lower()
    name_match = re.search(r"renderD([0-9]+)$", path)
    index = int(name_match.group(1)) if name_match else 999
    score = index

    if prefer and prefer in label:
        score -= 1000
    elif prefer == "intel" and any(token in label for token in ("intel", "uhd", "iris", "raptor lake")):
        score -= 1000

    if "nvidia" in label:
        score += 500
    if qemu_user and user_can_access_path(qemu_user, path, "rw"):
        score -= 50

    return score, path


def preferred_rendernode_for_egl(conf: Dict[str, str]) -> str:
    base = "/dev/dri"
    try:
        names = sorted(os.listdir(base), key=lambda item: int(re.sub(r"\D+", "", item) or "0"))
    except Exception:
        return ""

    try:
        qemu_user = detect_libvirt_qemu_user(conf)
    except Exception:
        qemu_user = ""

    candidates: List[Tuple[int, str]] = []
    for name in names:
        if not re.fullmatch(r"renderD[0-9]+", name):
            continue
        path = os.path.join(base, name)
        if os.path.exists(path):
            candidates.append(_video3d_rendernode_score(conf, path, qemu_user))

    if not candidates:
        return ""
    return sorted(candidates)[0][1]


def _video3d_ensure_render_access(conf: Dict[str, str], rendernode: str) -> None:
    try:
        qemu_user = detect_libvirt_qemu_user(conf)
    except Exception:
        qemu_user = ""
    if not qemu_user:
        raise ValueError("Utilisateur libvirt/QEMU introuvable pour verifier les droits du GPU de rendu.")

    if user_can_access_path(qemu_user, rendernode, "rw"):
        return

    if not conf_bool(conf, "VM_VIDEO3D_AUTO_REPAIR", "1"):
        raise ValueError(
            f"GPU de rendu inaccessible par libvirt/QEMU : {rendernode}. "
            f"L'utilisateur {qemu_user} doit avoir acces au groupe render ou aux droits du render node."
        )
    if os.geteuid() != 0:
        raise ValueError(
            f"GPU de rendu inaccessible par libvirt/QEMU : {rendernode}. "
            "Reparation automatique impossible car le service Flask ne tourne pas en root."
        )

    messages: List[str] = []
    groups: List[str] = []
    for group in ("render", "video"):
        rc, _out = run_cmd(["getent", "group", group], timeout=5)
        if rc == 0:
            groups.append(group)
    if groups:
        rc, out = run_cmd(["usermod", "-aG", ",".join(groups), qemu_user], timeout=20)
        messages.append(out.strip() if out.strip() else f"{qemu_user} ajoute aux groupes {', '.join(groups)}.")

    setfacl = str(conf.get("SETFACL_BIN") or DEFAULT_CONFIG.get("SETFACL_BIN") or "setfacl").strip() or "setfacl"
    if shutil.which(setfacl) is not None or os.path.isabs(setfacl):
        rc, out = run_cmd([setfacl, "-m", f"u:{qemu_user}:rw", rendernode], timeout=20)
        messages.append(out.strip() if out.strip() else f"ACL immediate appliquee sur {rendernode}.")

    if user_can_access_path(qemu_user, rendernode, "rw"):
        return

    details = " ".join(message for message in messages if message).strip()
    if details:
        details = " " + details
    raise ValueError(
        f"GPU de rendu inaccessible par libvirt/QEMU : {rendernode}. "
        f"L'utilisateur {qemu_user} doit avoir acces au groupe render ou aux droits du render node.{details}"
    )


def validate_rendernode_for_egl(conf: Dict[str, str], rendernode: str) -> str:
    rendernode = str(rendernode or "").strip()
    if not rendernode or rendernode == "auto":
        rendernode = preferred_rendernode_for_egl(conf)
    if not rendernode:
        raise ValueError("Aucun GPU de rendu /dev/dri/renderD* detecte pour Virtio 3D.")
    if not re.fullmatch(r"/dev/dri/renderD[0-9]+", rendernode):
        raise ValueError("GPU de rendu invalide : chemin attendu /dev/dri/renderDxxx.")
    if not os.path.exists(rendernode):
        raise ValueError(f"GPU de rendu introuvable : {rendernode}")
    _video3d_ensure_qemu_virgl(conf)
    _video3d_ensure_render_access(conf, rendernode)
    return rendernode


def sync_egl_headless_graphics(conf: Dict[str, str], root: ET.Element, enabled: bool, rendernode: str = "") -> None:
    devices = root.find("devices")
    if devices is None:
        raise ValueError("Bloc devices introuvable dans le XML.")
    for gfx in list(devices.findall("graphics")):
        if node_attr(gfx, "type", "") == "egl-headless":
            devices.remove(gfx)
    if not enabled:
        return
    rendernode = validate_rendernode_for_egl(conf, rendernode)
    gfx = ET.Element("graphics", {"type": "egl-headless"})
    if rendernode:
        ET.SubElement(gfx, "gl", {"rendernode": rendernode})
    devices.append(gfx)


def serial_console_fragment(target_type: str = "virtio") -> str:
    target_type = target_type if target_type in {"virtio", "serial", "isa-serial"} else "virtio"
    root = ET.Element("console", {"type": "pty"})
    ET.SubElement(root, "target", {"type": target_type})
    return xml_to_string(root)


def controller_fragment(ctype: str = "usb", model: str = "") -> str:
    ctype = ctype if ctype in {"usb", "sata", "scsi", "virtio-serial"} else "usb"
    default_model = {"usb": "qemu-xhci", "sata": "ich9-ahci", "scsi": "virtio-scsi", "virtio-serial": ""}.get(ctype, "")
    model = (model or default_model).strip()
    root = ET.Element("controller", {"type": ctype})
    if model:
        root.set("model", model)
    return xml_to_string(root)


def machine_kind_from_xml(machine: str) -> str:
    return "q35" if "q35" in str(machine or "").lower() else "pc"


def set_machine_type(root: ET.Element, machine_type: str) -> bool:
    wanted = "q35" if str(machine_type or "").strip().lower() == "q35" else "pc"
    os_type = root.find("./os/type")
    if os_type is None:
        os_node = root.find("os")
        if os_node is None:
            os_node = ET.SubElement(root, "os")
        os_type = ET.SubElement(os_node, "type", {"arch": "x86_64"})
        os_type.text = "hvm"
    current = machine_kind_from_xml(node_attr(os_type, "machine", ""))
    if current == wanted:
        return False
    os_type.set("machine", wanted)
    root_controller_model = "pcie-root" if wanted == "q35" else "pci-root"
    devices = root.find("devices")
    if devices is not None:
        for ctrl in devices.findall("controller"):
            if node_attr(ctrl, "type", "") == "pci" and node_attr(ctrl, "index", "0") in {"", "0"}:
                ctrl.set("model", root_controller_model)
                break
        else:
            ET.SubElement(devices, "controller", {"type": "pci", "index": "0", "model": root_controller_model})
    return True


def set_hyperv_features(root: ET.Element, enabled: bool) -> bool:
    features = root.find("features")
    if features is None:
        features = ET.SubElement(root, "features")
    current = features.find("hyperv") is not None
    if current == enabled:
        return False
    for node in list(features.findall("hyperv")):
        features.remove(node)
    if enabled:
        hyperv = ET.SubElement(features, "hyperv", {"mode": "custom"})
        ET.SubElement(hyperv, "relaxed", {"state": "on"})
        ET.SubElement(hyperv, "vapic", {"state": "on"})
        ET.SubElement(hyperv, "spinlocks", {"state": "on", "retries": "8191"})
        vmport = features.find("vmport")
        if vmport is None:
            ET.SubElement(features, "vmport", {"state": "off"})
        else:
            vmport.set("state", "off")
    return True


def channel_fragment(name: str = "org.qemu.guest_agent.0", target_type: str = "virtio") -> str:
    name = (name or "org.qemu.guest_agent.0").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", name):
        raise ValueError("Nom de channel invalide.")
    target_type = target_type if target_type in {"virtio"} else "virtio"
    root = ET.Element("channel", {"type": "unix"})
    ET.SubElement(root, "target", {"type": target_type, "name": name})
    return xml_to_string(root)


def tpm_fragment(model: str = "tpm-crb", version: str = "2.0") -> str:
    model = model if model in {"tpm-crb", "tpm-tis"} else "tpm-crb"
    version = version if version in {"1.2", "2.0"} else "2.0"
    root = ET.Element("tpm", {"model": model})
    ET.SubElement(root, "backend", {"type": "emulator", "version": version})
    return xml_to_string(root)


def find_disk_fragment(root: ET.Element, target: str = "", source: str = "") -> str:
    for disk in root.findall("./devices/disk"):
        tgt = node_attr(disk.find("target"), "dev", "")
        src = ""
        source_node = disk.find("source")
        if source_node is not None:
            for key in ("file", "dev", "dir", "name", "volume"):
                if source_node.get(key):
                    src = source_node.get(key) or ""
                    break
        if target:
            if tgt == target:
                return xml_to_string(disk)
            continue
        if source and src == source:
            return xml_to_string(disk)
    raise ValueError("Disque/ISO introuvable dans le XML.")


def find_nic_fragment(root: ET.Element, mac: str = "", target: str = "") -> str:
    for iface in root.findall("./devices/interface"):
        cur_mac = node_attr(iface.find("mac"), "address", "")
        cur_target = node_attr(iface.find("target"), "dev", "")
        if (mac and cur_mac.lower() == mac.lower()) or (target and cur_target == target):
            return xml_to_string(iface)
    raise ValueError("Carte reseau introuvable dans le XML.")


def find_hostdev_fragment(root: ET.Element, kind: str, address: str = "", vendor: str = "", product: str = "") -> str:
    for hostdev in root.findall("./devices/hostdev"):
        if node_attr(hostdev, "type", "") != kind:
            continue
        if kind == "pci":
            current = pci_address_from_xml(hostdev.find("./source/address"))
            wanted = address if address.count(":") == 2 else "0000:" + address
            if current.lower() == wanted.lower():
                return xml_to_string(hostdev)
        if kind == "usb":
            v = node_attr(hostdev.find("./source/vendor"), "id", "").lower().replace("0x", "")
            p = node_attr(hostdev.find("./source/product"), "id", "").lower().replace("0x", "")
            if v == vendor.lower().replace("0x", "") and p == product.lower().replace("0x", ""):
                return xml_to_string(hostdev)
    raise ValueError("Peripherique introuvable dans le XML.")


def find_graphics_fragment(root: ET.Element, kind: str = "", port: str = "") -> str:
    for gfx in root.findall("./devices/graphics"):
        if kind and node_attr(gfx, "type", "") != kind:
            continue
        if port and node_attr(gfx, "port", "") != port:
            continue
        return xml_to_string(gfx)
    raise ValueError("Console graphique introuvable dans le XML.")


def find_video_fragment(root: ET.Element, model: str = "") -> str:
    for video in root.findall("./devices/video"):
        cur_model = node_attr(video.find("model"), "type", "")
        if not model or cur_model == model:
            return xml_to_string(video)
    raise ValueError("Carte video introuvable dans le XML.")


def find_console_fragment(root: ET.Element, target_type: str = "") -> str:
    for console in root.findall("./devices/console"):
        cur_type = node_attr(console.find("target"), "type", "")
        if not target_type or cur_type == target_type:
            return xml_to_string(console)
    raise ValueError("Console serie introuvable dans le XML.")


def find_controller_fragment(root: ET.Element, ctype: str = "", model: str = "", index: str = "") -> str:
    for ctrl in root.findall("./devices/controller"):
        if ctype and node_attr(ctrl, "type", "") != ctype:
            continue
        if model and node_attr(ctrl, "model", "") != model:
            continue
        if index and node_attr(ctrl, "index", "") != index:
            continue
        return xml_to_string(ctrl)
    raise ValueError("Controleur introuvable dans le XML.")


def find_channel_fragment(root: ET.Element, target_name: str = "") -> str:
    for channel in root.findall("./devices/channel"):
        cur_name = node_attr(channel.find("target"), "name", "")
        if not target_name or cur_name == target_name:
            return xml_to_string(channel)
    raise ValueError("Channel introuvable dans le XML.")


def find_tpm_fragment(root: ET.Element, model: str = "") -> str:
    for tpm in root.findall("./devices/tpm"):
        if not model or node_attr(tpm, "model", "") == model:
            return xml_to_string(tpm)
    raise ValueError("TPM introuvable dans le XML.")


def remove_define_only_device(root: ET.Element, dtype: str, payload: Dict[str, object]) -> None:
    devices = root.find("devices")
    if devices is None:
        raise ValueError("Bloc devices introuvable dans le XML.")
    if dtype == "video":
        raw_type = str(payload.get("type", "") or "")
        wanted = str(payload.get("model", "") or payload.get("video_model", "") or (raw_type if raw_type != "video" else ""))
        for node in list(devices.findall("video")):
            cur = node_attr(node.find("model"), "type", "")
            if not wanted or cur == wanted:
                devices.remove(node)
                return
        raise ValueError("Carte video introuvable dans le XML.")
    if dtype == "tpm":
        wanted = str(payload.get("model", "") or "")
        for node in list(devices.findall("tpm")):
            if not wanted or node_attr(node, "model", "") == wanted:
                devices.remove(node)
                return
        raise ValueError("TPM introuvable dans le XML.")
    if dtype == "graphics":
        raw_type = str(payload.get("type", "") or "")
        wanted = str(payload.get("kind", "") or payload.get("graphics_type", "") or (raw_type if raw_type != "graphics" else ""))
        for node in list(devices.findall("graphics")):
            if not wanted or node_attr(node, "type", "") == wanted:
                devices.remove(node)
                return
        raise ValueError("Affichage introuvable dans le XML.")
    raise ValueError(f"Type non gere en define XML : {dtype}")


def remove_device_node(root: ET.Element, dtype: str, payload: Dict[str, object]) -> None:
    devices = root.find("devices")
    if devices is None:
        raise ValueError("Bloc devices introuvable dans le XML.")
    if dtype in {"video", "tpm", "graphics"}:
        remove_define_only_device(root, dtype, payload)
        return
    if dtype in {"disk", "cdrom", "iso", "physical_disk"}:
        wanted_target = str(payload.get("target", "") or "")
        wanted_source = str(payload.get("source", "") or "")
        for disk in list(devices.findall("disk")):
            target = node_attr(disk.find("target"), "dev", "")
            source_node = disk.find("source")
            current_source = ""
            if source_node is not None:
                for key in ("file", "dev", "dir", "name", "volume"):
                    if source_node.get(key):
                        current_source = source_node.get(key) or ""
                        break
            if (wanted_target and target == wanted_target) or (wanted_source and current_source == wanted_source):
                devices.remove(disk)
                return
        raise ValueError("Disque introuvable dans le XML.")
    if dtype == "nic":
        wanted_mac = str(payload.get("mac", "") or "").lower()
        wanted_target = str(payload.get("target", "") or "")
        for iface in list(devices.findall("interface")):
            mac = node_attr(iface.find("mac"), "address", "").lower()
            target = node_attr(iface.find("target"), "dev", "")
            if (wanted_mac and mac == wanted_mac) or (wanted_target and target == wanted_target):
                devices.remove(iface)
                return
        raise ValueError("Carte reseau introuvable dans le XML.")
    if dtype == "pci":
        wanted = pci_address_attrs(str(payload.get("address", "") or ""))
        for hostdev in list(devices.findall("hostdev")):
            if node_attr(hostdev, "type", "") != "pci":
                continue
            addr = hostdev.find("./source/address")
            if addr is not None and all((addr.get(k) or "").lower() == v.lower() for k, v in wanted.items()):
                devices.remove(hostdev)
                return
        raise ValueError("PCI introuvable dans le XML.")
    if dtype == "usb":
        vendor = str(payload.get("vendor", "") or "").lower().replace("0x", "")
        product = str(payload.get("product", "") or "").lower().replace("0x", "")
        for hostdev in list(devices.findall("hostdev")):
            if node_attr(hostdev, "type", "") != "usb":
                continue
            cur_vendor = node_attr(hostdev.find("./source/vendor"), "id", "").lower().replace("0x", "")
            cur_product = node_attr(hostdev.find("./source/product"), "id", "").lower().replace("0x", "")
            if vendor == cur_vendor and product == cur_product:
                devices.remove(hostdev)
                return
        raise ValueError("USB introuvable dans le XML.")
    if dtype == "console":
        wanted = str(payload.get("target_type", "") or "")
        for console in list(devices.findall("console")):
            if not wanted or node_attr(console.find("target"), "type", "") == wanted:
                devices.remove(console)
                return
        raise ValueError("Console introuvable dans le XML.")
    if dtype == "channel":
        wanted = str(payload.get("target_name", "") or "")
        for channel in list(devices.findall("channel")):
            if not wanted or node_attr(channel.find("target"), "name", "") == wanted:
                devices.remove(channel)
                return
        raise ValueError("Channel introuvable dans le XML.")
    if dtype == "controller":
        wanted_type = str(payload.get("type", "") or payload.get("controller_type", "") or "")
        wanted_model = str(payload.get("model", "") or "")
        wanted_index = str(payload.get("index", "") or "")
        for ctrl in list(devices.findall("controller")):
            if wanted_type and node_attr(ctrl, "type", "") != wanted_type:
                continue
            if wanted_model and node_attr(ctrl, "model", "") != wanted_model:
                continue
            if wanted_index and node_attr(ctrl, "index", "") != wanted_index:
                continue
            devices.remove(ctrl)
            return
        raise ValueError("Controleur introuvable dans le XML.")
    raise ValueError(f"Type non gere en define XML : {dtype}")


def do_vm_device_action(conf: Dict[str, str], payload: Dict[str, object]) -> Tuple[Dict[str, object], int]:
    name = clean_vm_name(str(payload.get("name", "") or ""))
    action = str(payload.get("action", "") or "").strip().lower()
    dtype = str(payload.get("type", "") or "").strip().lower()
    names, err = list_vm_names(conf)
    if err:
        return {"ok": False, "message": err}, 500
    if name not in names:
        return {"ok": False, "message": f"VM introuvable : {name}"}, 404
    ok_stop, stop_msg = require_vm_stopped(conf, name)
    if not ok_stop and not conf_bool(conf, "ALLOW_LIVE_DEVICE_CHANGES", "0"):
        return {"ok": False, "message": stop_msg}, 409

    backup = backup_domain_xml(conf, name, f"{action}_{dtype}")
    root = domain_xml_root(conf, name)
    live = str(payload.get("live", "") or "").lower() in {"1", "true", "yes", "on"}
    define_only = dtype in {"video", "tpm", "graphics"}

    try:
        if action in {"add", "edit"}:
            if action == "edit" and not define_only:
                try:
                    fragment = build_new_device_fragment(conf, root, dtype, payload)
                    rc, out = virsh_device(conf, name, "update-device", fragment, live=live)
                    if rc == 0:
                        vm_log(conf, f"VM {name}: edit {dtype} OK backup={backup}")
                        return {"ok": True, "message": f"Materiel {dtype} mis a jour. Backup XML : {backup}", "backup": backup}, 200
                except Exception:
                    pass
            if action == "edit":
                old = dict(payload.get("old") or {}) if isinstance(payload.get("old"), dict) else {}
                try:
                    if define_only or dtype in {"video", "tpm", "graphics"}:
                        remove_device_node(root, dtype, old)
                    else:
                        old_fragment = build_existing_fragment(root, dtype, old)
                    rc, out = (0, "") if (define_only or dtype in {"video", "tpm", "graphics"}) else virsh_device(conf, name, "detach-device", old_fragment, live=live)
                    if rc != 0:
                        return {"ok": False, "message": out.strip() or "detach ancienne config a echoue", "backup": backup}, 500
                    if not define_only and dtype not in {"video", "tpm", "graphics"}:
                        root = domain_xml_root(conf, name)
                except Exception as exc:
                    return {"ok": False, "message": f"Impossible de retirer l'ancienne config : {exc}", "backup": backup}, 500
            if action == "add" and dtype in {"disk", "cdrom", "iso", "physical_disk"}:
                new_source = str(payload.get("source", "") or "").strip()
                if new_source:
                    try:
                        find_disk_fragment(root, source=new_source)
                        return {"ok": False, "message": f"Cette source est deja attachee a la VM : {new_source}", "backup": backup}, 409
                    except ValueError:
                        pass
            fragment = build_new_device_fragment(conf, root, dtype, payload)
            if define_only or dtype in {"video", "tpm", "graphics"}:
                devices = root.find("devices")
                if devices is None:
                    return {"ok": False, "message": "Bloc devices introuvable dans le XML.", "backup": backup}, 500
                if action == "add":
                    try:
                        remove_define_only_device(root, dtype, payload)
                    except ValueError:
                        if dtype == "video" and str(payload.get("primary", "yes") or "yes").lower() not in {"0", "false", "no", "off"}:
                            try:
                                remove_define_only_device(root, dtype, {})
                            except ValueError:
                                pass
                        elif dtype == "tpm":
                            try:
                                remove_define_only_device(root, dtype, {})
                            except ValueError:
                                pass
                devices.append(ET.fromstring(fragment))
                if dtype == "video":
                    sync_egl_headless_graphics(
                        conf,
                        root,
                        _video_accel3d_payload(payload.get("accel3d", False)) or str(payload.get("model", "") or "").strip().lower() in {"virtio3d", "virtio-3d"},
                        str(payload.get("rendernode", "") or ""),
                    )
                rc, out = define_domain_root(conf, root)
                if rc == 0:
                    vm_log(conf, f"VM {name}: {action} {dtype} OK backup={backup}")
                    return {"ok": True, "message": f"Materiel {dtype} ajoute/modifie. Backup XML : {backup}", "backup": backup}, 200
                return {"ok": False, "message": out.strip() or "virsh define a echoue", "backup": backup}, 500
            rc, out = virsh_device(conf, name, "attach-device", fragment, live=live)
            if rc == 0:
                vm_log(conf, f"VM {name}: {action} {dtype} OK backup={backup}")
                return {"ok": True, "message": f"Materiel {dtype} ajoute/modifie. Backup XML : {backup}", "backup": backup}, 200
            return {"ok": False, "message": out.strip() or "attach-device a echoue", "backup": backup}, 500

        if action in {"remove", "detach", "delete"}:
            if define_only or dtype in {"video", "tpm", "graphics"}:
                remove_device_node(root, dtype, payload)
                if dtype == "video":
                    sync_egl_headless_graphics(conf, root, False, "")
                rc, out = define_domain_root(conf, root)
                if rc == 0:
                    vm_log(conf, f"VM {name}: remove {dtype} OK backup={backup}")
                    return {"ok": True, "message": f"Materiel {dtype} retire. Backup XML : {backup}", "backup": backup}, 200
                return {"ok": False, "message": out.strip() or "virsh define a echoue", "backup": backup}, 500
            fragment = build_existing_fragment(root, dtype, payload)
            rc, out = virsh_device(conf, name, "detach-device", fragment, live=live)
            if rc == 0:
                vm_log(conf, f"VM {name}: remove {dtype} OK backup={backup}")
                return {"ok": True, "message": f"Materiel {dtype} retire. Backup XML : {backup}", "backup": backup}, 200
            return {"ok": False, "message": out.strip() or "detach-device a echoue", "backup": backup}, 500

        return {"ok": False, "message": f"Action materielle inconnue : {action}"}, 400
    except ValueError as exc:
        return {"ok": False, "message": str(exc), "backup": backup}, 400
    except Exception as exc:
        return {"ok": False, "message": str(exc), "backup": backup}, 500


def build_new_device_fragment(conf: Dict[str, str], root: ET.Element, dtype: str, payload: Dict[str, object]) -> str:
    if dtype in {"disk", "cdrom", "iso", "physical_disk"}:
        device = "cdrom" if dtype in {"cdrom", "iso"} else "disk"
        source = str(payload.get("source", "") or "")
        source_kind = "dev" if dtype == "physical_disk" or source.startswith("/dev/") else "file"
        bus = str(payload.get("bus", "") or ("sata" if device == "cdrom" else "virtio")).strip().lower()
        if bus == "nvme":
            require_virtual_nvme_disk_support(conf)
        target = str(payload.get("target", "") or "").strip() or next_disk_target(root, bus, device)
        fmt = str(payload.get("format", "") or ("raw" if device == "cdrom" or source_kind == "dev" else "qcow2")).strip()
        cache = str(payload.get("cache", "") or "").strip()
        discard = str(payload.get("discard", "") or "").strip()
        return disk_fragment(source, source_kind, device, bus, target, fmt=fmt, cache=cache, discard=discard)
    if dtype == "nic":
        return nic_fragment(str(payload.get("kind", "bridge") or "bridge"), str(payload.get("source", "") or ""), str(payload.get("model", "virtio") or "virtio"), str(payload.get("mac", "") or ""))
    if dtype == "pci":
        return pci_fragment(str(payload.get("address", "") or ""), str(payload.get("managed", "yes") or "yes"))
    if dtype == "usb":
        return usb_fragment(str(payload.get("vendor", "") or ""), str(payload.get("product", "") or ""), str(payload.get("managed", "yes") or "yes"))
    if dtype == "video":
        return video_fragment(
            str(payload.get("model", "bochs") or "bochs"),
            str(payload.get("heads", "1") or "1"),
            str(payload.get("primary", "yes") or "yes"),
            payload.get("accel3d", False),
        )
    if dtype == "console":
        return serial_console_fragment(str(payload.get("target_type", "virtio") or "virtio"))
    if dtype == "channel":
        return channel_fragment(str(payload.get("target_name", "org.qemu.guest_agent.0") or "org.qemu.guest_agent.0"), str(payload.get("target_type", "virtio") or "virtio"))
    if dtype == "tpm":
        return tpm_fragment(str(payload.get("model", "tpm-crb") or "tpm-crb"), str(payload.get("version", "2.0") or "2.0"))
    if dtype == "graphics":
        return graphics_fragment(str(payload.get("kind", "vnc") or "vnc"), str(payload.get("listen", "0.0.0.0") or "0.0.0.0"))
    if dtype == "controller":
        return controller_fragment(str(payload.get("controller_type", "usb") or "usb"), str(payload.get("model", "") or ""))
    raise ValueError(f"Type materiel non gere : {dtype}")


def build_existing_fragment(root: ET.Element, dtype: str, payload: Dict[str, object]) -> str:
    if dtype in {"disk", "cdrom", "iso", "physical_disk"}:
        return find_disk_fragment(root, str(payload.get("target", "") or ""), str(payload.get("source", "") or ""))
    if dtype == "nic":
        return find_nic_fragment(root, str(payload.get("mac", "") or ""), str(payload.get("target", "") or ""))
    if dtype == "pci":
        return find_hostdev_fragment(root, "pci", address=str(payload.get("address", "") or ""))
    if dtype == "usb":
        return find_hostdev_fragment(root, "usb", vendor=str(payload.get("vendor", "") or ""), product=str(payload.get("product", "") or ""))
    if dtype == "video":
        raw_type = str(payload.get("type", "") or "")
        return find_video_fragment(root, str(payload.get("model", "") or payload.get("video_model", "") or (raw_type if raw_type != "video" else "")))
    if dtype == "console":
        return find_console_fragment(root, str(payload.get("target_type", "") or ""))
    if dtype == "channel":
        return find_channel_fragment(root, str(payload.get("target_name", "") or ""))
    if dtype == "tpm":
        return find_tpm_fragment(root, str(payload.get("model", "") or ""))
    if dtype == "graphics":
        raw_type = str(payload.get("type", "") or "")
        return find_graphics_fragment(root, str(payload.get("kind", "") or payload.get("graphics_type", "") or (raw_type if raw_type != "graphics" else "")), str(payload.get("port", "") or ""))
    if dtype == "controller":
        return find_controller_fragment(root, str(payload.get("type", "") or payload.get("controller_type", "") or ""), str(payload.get("model", "") or ""), str(payload.get("index", "") or ""))
    raise ValueError(f"Type materiel non gere : {dtype}")



def _vm_bool_payload(value: object) -> Optional[bool]:
    text = str(value if value is not None else "").strip().lower()
    if text in {"1", "true", "yes", "on", "enable", "enabled", "active", "actif", "oui"}:
        return True
    if text in {"0", "false", "no", "off", "disable", "disabled", "inactive", "non"}:
        return False
    return None



def _vm_ensure_features(root: ET.Element) -> ET.Element:
    features = root.find("features")
    if features is not None:
        return features
    features = ET.Element("features")
    os_node = root.find("os")
    if os_node is not None:
        children = list(root)
        try:
            root.insert(children.index(os_node) + 1, features)
        except ValueError:
            root.append(features)
    else:
        root.append(features)
    return features


def _vm_set_hyperv(root: ET.Element, enabled: bool) -> bool:
    features = _vm_ensure_features(root)
    changed = False
    for node in list(features.findall("hyperv")):
        features.remove(node)
        changed = True
    if enabled:
        hyperv = ET.SubElement(features, "hyperv", {"mode": "custom"})
        ET.SubElement(hyperv, "relaxed", {"state": "on"})
        ET.SubElement(hyperv, "vapic", {"state": "on"})
        ET.SubElement(hyperv, "spinlocks", {"state": "on", "retries": "8191"})
        vmport = features.find("vmport")
        if vmport is None:
            ET.SubElement(features, "vmport", {"state": "off"})
        else:
            vmport.set("state", "off")
        changed = True
    return changed



def _vm_define_root(conf: Dict[str, str], root: ET.Element, prefix: str) -> Tuple[int, str]:
    path = write_temp_xml(xml_to_string(root), prefix)
    try:
        return virsh(conf, "define", path, timeout=max(conf_int(conf, "ACTION_TIMEOUT", 90), 90))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

def do_vm_update(conf: Dict[str, str], payload: Dict[str, object]) -> Tuple[Dict[str, object], int]:
    name = clean_vm_name(str(payload.get("name", "") or ""))
    names, err = list_vm_names(conf)
    if err:
        return {"ok": False, "message": err}, 500
    if name not in names:
        return {"ok": False, "message": f"VM introuvable : {name}"}, 404
    backup = backup_domain_xml(conf, name, "resources")
    messages: List[str] = [f"Backup XML : {backup}"]

    vcpu = str(payload.get("vcpu", "") or "").strip()
    vcpu_current_raw = str(payload.get("vcpu_current", "") or vcpu).strip()
    vcpu_max_raw = str(payload.get("vcpu_max", "") or "").strip()
    cpu_pinset_raw = str(payload.get("cpu_pinset", "") or "").strip()
    cpu_mode_raw = str(payload.get("cpu_mode", "") or "").strip()
    cpu_mode_changed = str(payload.get("cpu_mode_changed", "") or "").strip().lower() in {"1", "on", "true", "yes"}
    cpu_topology_mode_raw = str(payload.get("cpu_topology_mode", "") or "").strip()
    cpu_topology_sockets_raw = str(payload.get("cpu_topology_sockets", "") or "").strip()
    cpu_topology_requested = bool(cpu_topology_mode_raw and cpu_topology_mode_raw.lower() not in {"keep", "conserver"})
    cpu_pinning_requested = "cpu_pinset" in payload or str(payload.get("cpu_pinning", "") or "").strip().lower() in {"1", "on", "true", "yes"}
    memory_gb = str(payload.get("memory_gb", "") or "").strip().replace(",", ".")
    memory_min_gb = str(payload.get("memory_min_gb", "") or "").strip().replace(",", ".")
    memory_max_gb = str(payload.get("memory_max_gb", "") or "").strip().replace(",", ".")
    autostart = str(payload.get("autostart", "") or "").strip().lower()
    new_name_raw = str(payload.get("new_name", "") or "").strip()
    boot_order_raw = payload.get("boot_order", "")
    boot_items_raw = payload.get("boot_items", None)
    firmware_id = str(payload.get("firmware_id", "") or "").strip()
    firmware_mode = str(payload.get("firmware_mode", "") or "").strip().lower()
    firmware_loader = str(payload.get("firmware_loader", "") or "").strip()
    firmware_nvram_template = str(payload.get("firmware_nvram_template", "") or "").strip()
    secure_boot_enabled = _vm_bool_payload(payload.get("secure_boot", None))
    machine_type = str(payload.get("machine_type", "") or "").strip().lower()
    hyperv_raw = payload.get("hyperv", None)
    hyperv_enabled = _vm_bool_payload(hyperv_raw)

    if new_name_raw and new_name_raw != name:
        new_name = clean_new_vm_name(new_name_raw)
        if new_name in names:
            return {"ok": False, "message": f"Une VM existe deja avec ce nom : {new_name}"}, 409
        ok_stop, stop_msg = require_vm_stopped(conf, name)
        if not ok_stop:
            return {"ok": False, "message": "Arrete la VM avant de la renommer. " + stop_msg, "backup": backup}, 409
        rc, out = virsh(conf, "domrename", name, new_name, timeout=max(conf_int(conf, "ACTION_TIMEOUT", 90), 90))
        messages.append((out or "").strip() or f"VM renommee : {name} -> {new_name}")
        if rc != 0:
            return {"ok": False, "message": "\n".join(messages)}, 500
        vm_log(conf, f"VM {name}: renamed to {new_name} backup={backup}")
        name = new_name

    if vcpu_current_raw or vcpu_max_raw or cpu_pinning_requested or cpu_topology_requested:
        try:
            vcpu_current = _vm_positive_int_or_none(vcpu_current_raw, "vCPU actifs")
            vcpu_max = _vm_positive_int_or_none(vcpu_max_raw, "vCPU maximum")
            cpu_pinset = _vm_parse_cpu_pinset(cpu_pinset_raw)
            cpu_topology_mode = _vm_cpu_topology_mode_payload(cpu_topology_mode_raw)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}, 400
        try:
            root = domain_xml_root(conf, name)
            changed = set_domain_vcpu_and_pinning(root, vcpu_current, vcpu_max, cpu_pinset, cpu_pinning_requested)
            if cpu_topology_mode:
                final_vcpu_max = _vm_vcpu_max_from_root(root, vcpu_max or vcpu_current)
                if set_domain_cpu_topology(root, cpu_topology_mode, cpu_topology_sockets_raw, final_vcpu_max):
                    changed = True
            if changed:
                rc, out = define_domain_root(conf, root)
                label = f"vCPU={len(cpu_pinset) if cpu_pinset else (vcpu_current or vcpu_max or '')}"
                if cpu_pinset:
                    label += " / pinning=" + ",".join(str(x) for x in cpu_pinset)
                if cpu_topology_mode:
                    topology = root.find("./cpu/topology")
                    label += f" / topology={node_attr(topology, 'sockets', '?')}s/{node_attr(topology, 'cores', '?')}c/{node_attr(topology, 'threads', '?')}t"
                messages.append((out or "").strip() or label)
                if rc != 0:
                    return {"ok": False, "message": "\n".join(messages)}, 500
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}, 400

    if cpu_mode_changed and cpu_mode_raw:
        try:
            cpu_mode_wanted = _vm_cpu_mode_payload(cpu_mode_raw)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}, 400
        if cpu_mode_wanted:
            root = domain_xml_root(conf, name)
            if set_domain_cpu_mode(root, cpu_mode_wanted):
                ok_stop, stop_msg = require_vm_stopped(conf, name)
                if not ok_stop:
                    return {"ok": False, "message": "Arrete la VM avant de changer le modele CPU. " + stop_msg, "backup": backup}, 409
                rc, out = define_domain_root(conf, root)
                cpu_label = {
                    "emulated": "CPU logiciel emule qemu64",
                    "host-model": "CPU hote compatible",
                    "host-passthrough": "CPU hote direct",
                }.get(cpu_mode_wanted, "CPU")
                messages.append((out or "").strip() or cpu_label)
                if rc != 0:
                    return {"ok": False, "message": "\n".join(messages)}, 500

    if memory_gb and not memory_min_gb and not memory_max_gb:
        memory_min_gb = memory_gb
        memory_max_gb = memory_gb

    if memory_min_gb or memory_max_gb:
        try:
            current_kib = parse_memory_gb_value(memory_min_gb, "RAM min") if memory_min_gb else None
            max_kib = parse_memory_gb_value(memory_max_gb, "RAM max") if memory_max_gb else None
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}, 400
        if current_kib is not None and max_kib is not None and current_kib > max_kib:
            return {"ok": False, "message": "RAM min ne peut pas depasser RAM max."}, 400
        root = domain_xml_root(conf, name)
        if current_kib is not None and max_kib is None:
            memory_node = root.find("memory")
            max_kib = memory_to_bytes(first_text(root, "memory"), node_attr(memory_node, "unit", "KiB")) or current_kib
            if current_kib > max_kib:
                return {"ok": False, "message": "RAM min ne peut pas depasser la RAM max actuelle."}, 400
        if max_kib is not None and current_kib is None:
            current_node = root.find("currentMemory")
            current_kib = memory_to_bytes(first_text(root, "currentMemory"), node_attr(current_node, "unit", "KiB"))
            if current_kib is not None and current_kib > max_kib:
                return {"ok": False, "message": "RAM max ne peut pas etre inferieure a la RAM min actuelle."}, 400
        if set_domain_memory_kib(root, current_kib, max_kib):
            rc, out = define_domain_root(conf, root)
            messages.append((out or "").strip() or "RAM min/max OK")
            if rc != 0:
                return {"ok": False, "message": "\n".join(messages)}, 500

    if autostart in {"on", "off", "1", "0", "true", "false"}:
        if autostart in {"on", "1", "true"}:
            rc, out = virsh(conf, "autostart", name, timeout=30)
        else:
            rc, out = virsh(conf, "autostart", name, "--disable", timeout=30)
        messages.append((out or "").strip() or "autostart OK")
        if rc != 0:
            return {"ok": False, "message": "\n".join(messages)}, 500

    machine_wanted = machine_type if machine_type in {"pc", "i440fx", "q35"} else ""
    hyperv_set = hyperv_enabled is not None
    if machine_wanted or hyperv_set:
        root = domain_xml_root(conf, name)
        changed_xml = False
        if machine_wanted:
            changed_xml = set_machine_type(root, "q35" if machine_wanted == "q35" else "pc") or changed_xml
        if hyperv_set:
            changed_xml = set_hyperv_features(root, bool(hyperv_enabled)) or changed_xml
        if changed_xml:
            ok_stop, stop_msg = require_vm_stopped(conf, name)
            if not ok_stop:
                return {"ok": False, "message": stop_msg, "backup": backup}, 409
            rc, out = define_domain_root(conf, root)
            messages.append((out or "").strip() or "machine / Hyper-V OK")
            if rc != 0:
                return {"ok": False, "message": "\n".join(messages)}, 500

    if firmware_id or firmware_mode or firmware_loader:
        if firmware_id.startswith("loader:") and not firmware_loader:
            raw = firmware_id[len("loader:"):]
            parts = raw.split("|vars:", 1)
            firmware_loader = parts[0]
            if len(parts) > 1 and not firmware_nvram_template:
                firmware_nvram_template = parts[1]
            firmware_mode = "loader"
        if firmware_mode in {"", "bios"} and firmware_id in {"bios", ""}:
            firmware_mode = "bios"
        elif firmware_mode in {"", "uefi"} and firmware_id == "uefi":
            firmware_mode = "uefi"
        elif firmware_loader:
            firmware_mode = "loader"
        if firmware_mode not in {"bios", "uefi", "loader"}:
            return {"ok": False, "message": "Firmware invalide."}, 400

        if firmware_mode == "loader" and secure_boot_enabled is False:
            firmware_loader = non_secure_ovmf_path(firmware_loader)
            firmware_nvram_template = non_secure_ovmf_path(firmware_nvram_template)

        root = domain_xml_root(conf, name)
        os_node = root.find("os")
        if os_node is None:
            os_node = ET.SubElement(root, "os")
        for node_name in ("firmware", "loader", "nvram"):
            for node in list(os_node.findall(node_name)):
                os_node.remove(node)

        if firmware_mode == "bios":
            os_node.attrib.pop("firmware", None)
        elif firmware_mode == "uefi":
            os_node.set("firmware", "efi")
            if secure_boot_enabled is not None:
                firmware = ET.SubElement(os_node, "firmware")
                state = "yes" if secure_boot_enabled else "no"
                ET.SubElement(firmware, "feature", {"enabled": state, "name": "enrolled-keys"})
                ET.SubElement(firmware, "feature", {"enabled": state, "name": "secure-boot"})
        else:
            if not firmware_loader or not os.path.isfile(firmware_loader):
                return {"ok": False, "message": "Firmware UEFI CODE introuvable."}, 400
            os_node.attrib.pop("firmware", None)
            loader_attrs = {"readonly": "yes", "type": "pflash", "format": "raw"}
            if secure_boot_enabled:
                loader_attrs["secure"] = "yes"
            loader = ET.SubElement(os_node, "loader", loader_attrs)
            loader.text = firmware_loader
            if firmware_nvram_template:
                if not os.path.isfile(firmware_nvram_template):
                    return {"ok": False, "message": "Firmware UEFI VARS introuvable."}, 400
                ET.SubElement(os_node, "nvram", {"template": firmware_nvram_template})

        path = write_temp_xml(xml_to_string(root), "vm-firmware-")
        try:
            rc, out = virsh(conf, "define", path, timeout=max(conf_int(conf, "ACTION_TIMEOUT", 90), 90))
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        messages.append((out or "").strip() or "firmware OK")
        if rc != 0:
            return {"ok": False, "message": "\n".join(messages)}, 500

    if isinstance(boot_items_raw, list):
        boot_items = [x for x in boot_items_raw if isinstance(x, dict)]
    else:
        boot_items = []
    if boot_items:
        root = domain_xml_root(conf, name)
        os_node = root.find("os")
        if os_node is not None:
            for boot in list(os_node.findall("boot")):
                os_node.remove(boot)
        for node in list(root.findall("./devices/disk")) + list(root.findall("./devices/interface")):
            for boot in list(node.findall("boot")):
                node.remove(boot)

        def match_disk(item: Dict[str, object]) -> Optional[ET.Element]:
            wanted_target = str(item.get("target", "") or "")
            wanted_source = str(item.get("source", "") or "")
            for disk in root.findall("./devices/disk"):
                if wanted_target and node_attr(disk.find("target"), "dev", "") == wanted_target:
                    return disk
                source_node = disk.find("source")
                current_source = ""
                if source_node is not None:
                    for key in ("file", "dev", "dir", "name", "volume"):
                        if source_node.get(key):
                            current_source = source_node.get(key) or ""
                            break
                if wanted_source and current_source == wanted_source:
                    return disk
            return None

        def match_iface(item: Dict[str, object]) -> Optional[ET.Element]:
            wanted_mac = str(item.get("mac", "") or "").lower()
            wanted_target = str(item.get("target", "") or "")
            wanted_source = str(item.get("source", "") or "")
            for iface in root.findall("./devices/interface"):
                if wanted_mac and node_attr(iface.find("mac"), "address", "").lower() == wanted_mac:
                    return iface
                if wanted_target and node_attr(iface.find("target"), "dev", "") == wanted_target:
                    return iface
                source_node = iface.find("source")
                current_source = ""
                if source_node is not None:
                    for key in ("bridge", "network", "dev"):
                        if source_node.get(key):
                            current_source = source_node.get(key) or ""
                            break
                if wanted_source and current_source == wanted_source:
                    return iface
            return None

        for index, item in enumerate(boot_items, start=1):
            dtype = str(item.get("type", "") or "").strip().lower()
            if dtype in {"disk", "physical_disk", "cdrom", "iso"}:
                node = match_disk(item)
            elif dtype == "nic":
                node = match_iface(item)
            else:
                continue
            if node is None:
                return {"ok": False, "message": f"Peripherique de boot introuvable : {item}"}, 400
            ET.SubElement(node, "boot", {"order": str(index)})

        path = write_temp_xml(xml_to_string(root), "vm-boot-devices-")
        try:
            rc, out = virsh(conf, "define", path, timeout=max(conf_int(conf, "ACTION_TIMEOUT", 90), 90))
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        messages.append((out or "").strip() or "ordre de boot par peripherique OK")
        if rc != 0:
            return {"ok": False, "message": "\n".join(messages)}, 500

    if not boot_items and isinstance(boot_order_raw, list):
        boot_order = [str(x or "").strip().lower() for x in boot_order_raw if str(x or "").strip()]
    else:
        boot_order = [x.strip().lower() for x in re.split(r"[,\s]+", str(boot_order_raw or "")) if x.strip()]
    if not boot_items and boot_order:
        allowed_boot = {"hd", "cdrom", "network", "fd"}
        invalid = [x for x in boot_order if x not in allowed_boot]
        if invalid:
            return {"ok": False, "message": "Ordre de boot invalide : " + ", ".join(invalid)}, 400
        root = domain_xml_root(conf, name)
        os_node = root.find("os")
        if os_node is None:
            return {"ok": False, "message": "Bloc <os> introuvable dans le XML."}, 500
        for boot in list(os_node.findall("boot")):
            os_node.remove(boot)
        for dev in boot_order:
            ET.SubElement(os_node, "boot", {"dev": dev})
        path = write_temp_xml(xml_to_string(root), "vm-boot-")
        try:
            rc, out = virsh(conf, "define", path, timeout=max(conf_int(conf, "ACTION_TIMEOUT", 90), 90))
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        messages.append((out or "").strip() or "ordre de boot OK")
        if rc != 0:
            return {"ok": False, "message": "\n".join(messages)}, 500

    vm_log(conf, f"VM {name}: update resources")
    return {"ok": True, "message": "\n".join(messages), "name": name, "redirect": f"/vm/gestion/vm/{name}"}, 200
