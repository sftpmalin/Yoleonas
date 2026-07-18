#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# PART 009 - Gestion VM nouvelle interface

import tempfile
from flask import url_for


VM_GESTION_TITLE = (
    "VM - Gestion",
    "Nouvelle interface de gestion VM : edition par informations et peripheriques.",
)


def _vmg_int(value: object, label: str, min_value: int, max_value: int) -> int:
    try:
        out = int(str(value or "").strip())
    except Exception:
        raise ValueError(f"{label} invalide.")
    if out < min_value or out > max_value:
        raise ValueError(f"{label} invalide : valeur attendue entre {min_value} et {max_value}.")
    return out


def _vmg_supported_virt_types(conf: Dict[str, str]) -> List[str]:
    """Retourne les types de virtualisation réellement exposés par libvirt.

    Sur certains hôtes ou conteneurs de test, libvirt voit seulement `qemu` et
    refuse `kvm`. La création minimale doit donc s'adapter au moteur disponible
    au lieu de forcer `type="kvm"`.
    """
    try:
        rc, out = virsh(conf, "capabilities", timeout=20)
        if rc != 0 or not out.strip():
            return []
        root = ET.fromstring(out)
    except Exception:
        return []

    found: List[str] = []
    for node in root.findall(".//domain"):
        dtype = str(node.attrib.get("type") or "").strip().lower()
        if dtype and dtype not in found:
            found.append(dtype)
    return found


def _vmg_pick_virt_type(conf: Dict[str, str]) -> str:
    forced = str(conf.get("VM_CREATE_MINIMAL_VIRT_TYPE", "") or "").strip().lower()
    if forced in {"kvm", "qemu"}:
        return forced

    supported = _vmg_supported_virt_types(conf)
    if "kvm" in supported:
        return "kvm"
    if "qemu" in supported:
        return "qemu"
    return "kvm"


def _vmg_machine_type(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"", "pc", "i440fx", "i440", "pc-i440fx", "pc_i440fx"}:
        return "pc"
    if text in {"q35", "pc-q35", "pc_q35"}:
        return "q35"
    raise ValueError("Type de machine invalide. Choisis i440FX ou Q35.")




def _vmg_firmware_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"", "uefi", "efi", "auto", "uefi-auto", "uefi_auto"}:
        return "uefi"
    if text in {"bios", "seabios", "legacy"}:
        return "bios"
    raise ValueError("Firmware invalide. Choisis BIOS ou UEFI auto.")


def _vmg_domain_xml(conf: Dict[str, str], name: str, memory_gb: int, vcpus: int, virt_type: str = "kvm", machine_type: str = "pc", firmware_mode: str = "uefi") -> str:
    """Construit une VM minimale libvirt, sans disque, sans réseau et sans console.

    Cette route sert seulement à créer l'entrée VM propre dans libvirt. Le vrai
    matériel sera ajouté ensuite dans /vm/gestion/vm/<nom> avec l'éditeur unique.
    """
    virt_type = "qemu" if str(virt_type or "").strip().lower() == "qemu" else "kvm"
    machine_type = _vmg_machine_type(machine_type)
    firmware_mode = _vmg_firmware_mode(firmware_mode)
    domain = ET.Element("domain", {"type": virt_type})
    ET.SubElement(domain, "name").text = name
    ET.SubElement(domain, "memory", {"unit": "MiB"}).text = str(memory_gb * 1024)
    ET.SubElement(domain, "currentMemory", {"unit": "MiB"}).text = str(memory_gb * 1024)
    ET.SubElement(domain, "vcpu", {"placement": "static"}).text = str(vcpus)

    os_node = ET.SubElement(domain, "os")
    if firmware_mode == "uefi":
        # UEFI auto : on laisse libvirt choisir le firmware OVMF disponible.
        os_node.set("firmware", "efi")
    # Le type de machine doit être choisi à la création : libvirt ne le convertit
    # pas proprement après coup quand des contrôleurs existent déjà.
    ET.SubElement(os_node, "type", {"arch": "x86_64", "machine": machine_type}).text = "hvm"
    ET.SubElement(os_node, "boot", {"dev": "hd"})

    features = ET.SubElement(domain, "features")
    ET.SubElement(features, "acpi")
    ET.SubElement(features, "apic")

    # host-passthrough n'est fiable qu'en KVM. En pur qemu, on laisse libvirt
    # choisir un modèle basique pour éviter de bloquer la création de la fiche VM.
    cpu_model = str(conf.get("VM_CREATE_DEFAULT_CPU", "host-passthrough") or "host-passthrough").strip()
    cpu_node = None
    if virt_type == "kvm" and cpu_model:
        if cpu_model == "host-passthrough":
            cpu_node = ET.SubElement(domain, "cpu", {"mode": "host-passthrough", "check": "none"})
        else:
            cpu_node = ET.SubElement(domain, "cpu", {"mode": "custom", "match": "exact", "check": "partial"})
            ET.SubElement(cpu_node, "model", {"fallback": "allow"}).text = cpu_model
    if cpu_node is None:
        cpu_node = ET.SubElement(domain, "cpu")
    ET.SubElement(cpu_node, "topology", {"sockets": "1", "cores": str(vcpus), "threads": "1"})

    ET.SubElement(domain, "clock", {"offset": "localtime"})
    ET.SubElement(domain, "on_poweroff").text = "destroy"
    ET.SubElement(domain, "on_reboot").text = "restart"
    ET.SubElement(domain, "on_crash").text = "destroy"

    devices = ET.SubElement(domain, "devices")
    pci_root_model = "pcie-root" if machine_type == "q35" else "pci-root"
    ET.SubElement(devices, "controller", {"type": "pci", "index": "0", "model": pci_root_model})
    ET.SubElement(devices, "memballoon", {"model": "virtio"})

    xml_text = ET.tostring(domain, encoding="unicode")
    return "<?xml version='1.0' encoding='UTF-8'?>\n" + xml_text + "\n"


def _vmg_define_xml(conf: Dict[str, str], xml_text: str) -> Tuple[int, str]:
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".xml", prefix="yoleo-vm-min-", delete=False) as handle:
            handle.write(xml_text)
            tmp_path = handle.name
        return virsh(conf, "define", tmp_path, timeout=max(conf_int(conf, "ACTION_TIMEOUT", 90), 90))
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _vmg_should_retry_qemu(out: str) -> bool:
    low = (out or "").lower()
    return "kvm" in low and ("not support" in low or "ne prend pas en charge" in low or "unsupported" in low)


def do_create_minimal_vm(conf: Dict[str, str], payload: Dict[str, object]) -> Tuple[Dict[str, object], int]:
    name = clean_new_vm_name(str(payload.get("name") or ""))
    vcpus = _vmg_int(payload.get("vcpus") or conf.get("VM_CREATE_DEFAULT_VCPUS", "2"), "vCPU", 1, 256)
    memory_gb = _vmg_int(payload.get("memory_gb") or conf.get("VM_CREATE_DEFAULT_MEMORY_GB", "4"), "RAM Go", 1, 4096)
    machine_type = _vmg_machine_type(payload.get("machine_type") or conf.get("VM_CREATE_DEFAULT_MACHINE_TYPE") or "q35")
    firmware_mode = _vmg_firmware_mode(payload.get("firmware") or payload.get("firmware_mode") or conf.get("VM_CREATE_DEFAULT_FIRMWARE") or "uefi")

    names, err = list_vm_names(conf)
    if err:
        return {"ok": False, "message": err}, 500
    if name in names:
        return {"ok": False, "message": f"Une VM existe déjà avec ce nom : {name}"}, 409

    virt_type = _vmg_pick_virt_type(conf)
    xml_text = _vmg_domain_xml(conf, name, memory_gb, vcpus, virt_type=virt_type, machine_type=machine_type, firmware_mode=firmware_mode)
    rc, out = _vmg_define_xml(conf, xml_text)

    # Sécurité : si libvirt annonce mal ses capacités ou si l'hôte n'a pas KVM,
    # on crée quand même la fiche VM en type qemu au lieu de bloquer la pop-up.
    if rc != 0 and virt_type == "kvm" and _vmg_should_retry_qemu(out):
        vm_log(conf, f"Création VM minimale {name}: KVM refusé, nouvel essai en type qemu.")
        virt_type = "qemu"
        xml_text = _vmg_domain_xml(conf, name, memory_gb, vcpus, virt_type=virt_type, machine_type=machine_type, firmware_mode=firmware_mode)
        rc, out = _vmg_define_xml(conf, xml_text)

    if rc != 0:
        vm_log(conf, f"Création VM minimale {name} échouée rc={rc}: {out.strip()}")
        return {"ok": False, "message": out.strip() or f"virsh define a échoué pour {name}"}, 500

    vm_log(conf, f"VM minimale créée : {name} ({vcpus} vCPU, {memory_gb} Go RAM, virt={virt_type}, machine={machine_type}, firmware={firmware_mode})")
    return {
        "ok": True,
        "message": f"VM minimale {name} créée. Ajoute maintenant les périphériques depuis Gestion.",
        "name": name,
        "redirect": f"/vm/gestion/vm/{name}",
    }, 200


def _vmg_short(value: object, max_len: int = 42) -> str:
    text = str(value or "").strip()
    if len(text) <= max_len:
        return text
    return "..." + text[-max_len:]


def _vmg_device_item(kind: str, key: str, icon: str, label: str, subtitle: str, data: Dict[str, object]) -> Dict[str, object]:
    return {
        "kind": kind,
        "key": key,
        "icon": icon,
        "label": label,
        "subtitle": subtitle,
        "data": data or {},
    }


def build_gestion_device_menu(vm: Optional[Dict[str, object]]) -> List[Dict[str, object]]:
    if not vm:
        return []
    devices: List[Dict[str, object]] = []

    disk_count = 0
    cd_count = 0
    physical_count = 0
    for disk in vm.get("disks", []) or []:
        if not isinstance(disk, dict):
            continue
        device = str(disk.get("device") or "disk").lower()
        source = str(disk.get("source") or "")
        target = str(disk.get("target") or "")
        if device == "cdrom":
            cd_count += 1
            label = f"Lecteur CD-ROM {target or cd_count}"
            icon = "💿"
            kind = "cdrom"
        elif source.startswith("/dev/"):
            physical_count += 1
            label = f"Disque physique {target or physical_count}"
            icon = "🧱"
            kind = "physical_disk"
        else:
            disk_count += 1
            label = f"Disque virtuel {target or disk_count}"
            icon = "💽"
            kind = "disk"
        subtitle = " - ".join(x for x in [str(disk.get("bus") or ""), str(disk.get("format") or ""), _vmg_short(source)] if x)
        devices.append(_vmg_device_item(kind, f"disk-{len(devices)+1}", icon, label, subtitle, disk))

    for idx, nic in enumerate(vm.get("nics", []) or [], start=1):
        if not isinstance(nic, dict):
            continue
        mac = str(nic.get("mac") or "")
        source = str(nic.get("source") or "")
        label = f"Carte reseau {idx}" + (f" - {mac}" if mac else "")
        subtitle = " - ".join(x for x in [str(nic.get("type") or ""), source, str(nic.get("model") or "")] if x)
        devices.append(_vmg_device_item("nic", f"nic-{idx}", "🌐", label, subtitle, nic))

    for idx, hostdev in enumerate(vm.get("hostdevs", []) or [], start=1):
        if not isinstance(hostdev, dict):
            continue
        address = str(hostdev.get("address") or "")
        dev_type = str(hostdev.get("type") or "hostdev")
        vendor = str(hostdev.get("vendor") or "")
        product = str(hostdev.get("product") or "")
        if dev_type == "usb":
            label = f"Hote USB {vendor}:{product}" if vendor and product else f"Hote USB {idx}"
            subtitle = str(hostdev.get("label") or "")
            kind = "usb"
            icon = "🔌"
        else:
            label = f"PCI passthrough {address or idx}"
            subtitle = " - ".join(x for x in [address, str(hostdev.get("label") or "")] if x)
            kind = "pci"
            icon = "🧩"
        devices.append(_vmg_device_item(kind, f"hostdev-{idx}", icon, label, subtitle, hostdev))

    for idx, gfx in enumerate(vm.get("graphics", []) or [], start=1):
        if not isinstance(gfx, dict):
            continue
        if str(gfx.get("type") or "") == "egl-headless":
            continue
        label = f"Affichage {idx}"
        subtitle = " - ".join(x for x in [str(gfx.get("type") or ""), "port " + str(gfx.get("port")) if gfx.get("port") else "", "ws " + str(gfx.get("websocket")) if gfx.get("websocket") else ""] if x)
        devices.append(_vmg_device_item("graphics", f"graphics-{idx}", "🖥️", label, subtitle, gfx))

    for idx, video in enumerate(vm.get("videos", []) or [], start=1):
        if not isinstance(video, dict):
            continue
        label = f"Video {idx}"
        subtitle = " - ".join(
            x
            for x in [
                str(video.get("type") or ""),
                "3D" if video.get("accel3d") else "",
                str(video.get("rendernode") or ""),
                str(video.get("primary") or ""),
            ]
            if x
        )
        devices.append(_vmg_device_item("video", f"video-{idx}", "🎞️", label, subtitle, video))

    for idx, console in enumerate(vm.get("consoles", []) or [], start=1):
        if not isinstance(console, dict):
            continue
        label = f"Console serie {idx}"
        subtitle = " - ".join(x for x in [str(console.get("target_type") or ""), str(console.get("source_path") or "")] if x)
        devices.append(_vmg_device_item("console", f"console-{idx}", "⌨️", label, subtitle, console))

    for idx, channel in enumerate(vm.get("channels", []) or [], start=1):
        if not isinstance(channel, dict):
            continue
        label = f"Canal {idx}"
        subtitle = " - ".join(x for x in [str(channel.get("target_name") or ""), str(channel.get("source_path") or "")] if x)
        devices.append(_vmg_device_item("channel", f"channel-{idx}", "🔗", label, subtitle, channel))

    for idx, controller in enumerate(vm.get("controllers", []) or [], start=1):
        if not isinstance(controller, dict):
            continue
        ctype = str(controller.get("type") or "")
        label = f"Controleur {ctype or idx}"
        subtitle = " - ".join(x for x in [str(controller.get("model") or ""), "index " + str(controller.get("index")) if controller.get("index") else ""] if x)
        devices.append(_vmg_device_item("controller", f"controller-{idx}", "🧬", label, subtitle, controller))

    for idx, tpm in enumerate(vm.get("tpms", []) or [], start=1):
        if not isinstance(tpm, dict):
            continue
        label = f"TPM {idx}"
        subtitle = " - ".join(x for x in [str(tpm.get("model") or ""), str(tpm.get("backend_type") or ""), str(tpm.get("version") or "")] if x)
        devices.append(_vmg_device_item("tpm", f"tpm-{idx}", "🔐", label, subtitle, tpm))

    return devices


def _render_vm_gestion(mode: str = "main", name: str = ""):
    conf = get_config()
    vms, summary, error = collect_inventory(conf)
    current_vm: Optional[Dict[str, object]] = None
    if name:
        clean_name = clean_vm_name(name)
        current_vm = next((vm for vm in vms if str(vm.get("name") or "") == clean_name), None)
        if current_vm is None and not error:
            abort(404, f"VM introuvable : {clean_name}")
    devices = build_gestion_device_menu(current_vm)
    extra = collect_vm_extra(conf)
    nvme_virtual_disk = virtual_nvme_disk_status(conf)
    return render_template(
        "vm_gestion.html",
        conf=conf,
        vms=vms,
        summary=summary,
        error=error,
        mode=mode,
        current_vm=current_vm,
        devices=devices,
        nvme_virtual_disk=nvme_virtual_disk,
        vm_current_title=VM_GESTION_TITLE,
        refresh_seconds=int(conf.get("REFRESH_SECONDS", "8") or "8"),
        page_icon_override="🖥️",
        **extra,
    )


@vm_bp.route("/vm/gestion")
@vm_bp.route("/vm/gestion/")
def vm_gestion_home():
    return redirect("/vm/gestion/main")


@vm_bp.route("/vm/gestion/main")
def vm_gestion_main():
    return _render_vm_gestion("main")


@vm_bp.route("/vm/gestion/vm/<name>")
def vm_gestion_vm(name: str):
    return _render_vm_gestion("vm", name)


@vm_bp.route("/vm/gestion/create", methods=["POST"])
def vm_gestion_create():
    conf = get_config()
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    try:
        result, status = do_create_minimal_vm(conf, payload)
        return jsonify(result), status
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500
