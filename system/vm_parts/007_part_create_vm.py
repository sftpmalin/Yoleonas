#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# PART 007 - Création de VM via virt-install



# ==========================================================
# CrÃ©ation VM propre via virt-install (virt-manager officiel)
# ==========================================================
SAFE_NEW_VM_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


def clean_new_vm_name(name: str) -> str:
    name = (name or "").strip()
    if not name or not SAFE_NEW_VM_RE.match(name):
        raise ValueError("Nom de VM invalide. Utilise lettres, chiffres, tiret, underscore, point ou deux-points, sans espace.")
    return name


def payload_bool(payload: Dict[str, object], key: str, default: bool = False) -> bool:
    value = payload.get(key)
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "oui"}


def positive_int(value: object, label: str, min_value: int, max_value: int) -> int:
    try:
        out = int(str(value or "").strip())
    except Exception:
        raise ValueError(f"{label} invalide.")
    if out < min_value or out > max_value:
        raise ValueError(f"{label} invalide : valeur attendue entre {min_value} et {max_value}.")
    return out


def first_existing_bridge() -> str:
    bridges = list_host_bridges()
    for pref in ("br0", "virbr0"):
        if any(str(item.get("name") or "") == pref for item in bridges):
            return pref
    return str(bridges[0].get("name") or "") if bridges else ""


def default_vm_disk_path(conf: Dict[str, str], name: str) -> str:
    base = str(conf.get("STORAGE_DEFAULT_DIR", DEFAULT_CONFIG["STORAGE_DEFAULT_DIR"]) or DEFAULT_CONFIG["STORAGE_DEFAULT_DIR"]).strip()
    if not base:
        base = DEFAULT_CONFIG["STORAGE_DEFAULT_DIR"]
    return os.path.join(base, safe_filename(name) + ".qcow2")


def clean_volume_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("Nom de volume disque invalide.")
    if name.startswith(".") or "/" in name or "\x00" in name or ".." in name:
        raise ValueError("Nom de volume disque invalide : pas de /, .. ou caractÃ¨re nul.")
    if not re.fullmatch(r"[A-Za-z0-9_.:+-]{1,160}", name):
        raise ValueError("Nom de volume disque invalide. Utilise lettres, chiffres, tiret, underscore, point ou deux-points.")
    return name


def pool_target_path(conf: Dict[str, str], pool: str) -> str:
    pool = clean_pool_name(pool)
    rc, xml_text = virsh(conf, "pool-dumpxml", pool, timeout=20)
    if rc != 0:
        raise ValueError(f"Pool libvirt introuvable ou illisible : {pool}")
    path = pool_path_from_xml(xml_text)
    if not path:
        raise ValueError(f"Pool {pool} sans chemin cible lisible. Utilise le mode chemin manuel pour ce type de pool.")
    return clean_abs_path(path, f"chemin pool {pool}")


def ensure_pool_active(conf: Dict[str, str], pool: str) -> None:
    pool = clean_pool_name(pool)
    rc, info_text = virsh(conf, "pool-info", pool, timeout=20)
    if rc != 0:
        raise ValueError(f"Pool libvirt introuvable : {pool}")
    info = parse_key_values(info_text)
    state = str(info.get("State", "") or "").strip().lower()
    if state not in {"running", "active"}:
        rc_start, out_start = virsh(conf, "pool-start", pool, timeout=60)
        if rc_start != 0:
            raise ValueError((out_start or f"Pool {pool} inactive et impossible Ã  dÃ©marrer.").strip())


def volume_path_in_pool(conf: Dict[str, str], pool: str, volume: str) -> str:
    pool = clean_pool_name(pool)
    volume = clean_volume_name(volume)
    ensure_pool_active(conf, pool)
    rc, out = virsh(conf, "vol-path", "--pool", pool, volume, timeout=20)
    if rc != 0 or not out.strip():
        raise ValueError(f"Volume {volume} introuvable dans la pool {pool}.")
    return validate_existing_file(out.strip().splitlines()[0], f"volume {pool}/{volume}")


def pool_volume_exists(conf: Dict[str, str], pool: str, volume: str) -> bool:
    pool = clean_pool_name(pool)
    volume = clean_volume_name(volume)
    rc, _out = virsh(conf, "vol-info", "--pool", pool, volume, timeout=20)
    return rc == 0


def create_volume_in_pool(conf: Dict[str, str], pool: str, volume: str, size_gb: int, disk_format: str) -> str:
    pool = clean_pool_name(pool)
    volume = clean_volume_name(volume)
    ensure_pool_active(conf, pool)
    if pool_volume_exists(conf, pool, volume):
        raise ValueError(f"Le volume existe deja dans la pool {pool} : {volume}")
    fmt = "raw" if disk_format == "raw" else "qcow2"
    rc, out = virsh(
        conf,
        "vol-create-as",
        pool,
        volume,
        f"{size_gb}G",
        "--format",
        fmt,
        timeout=max(conf_int(conf, "ACTION_TIMEOUT", 90), 180),
    )
    if rc != 0:
        raise RuntimeError(out.strip() or f"Creation du volume {pool}/{volume} impossible.")
    return volume_path_in_pool(conf, pool, volume)


def delete_volume_from_pool(conf: Dict[str, str], pool: str, volume: str) -> None:
    try:
        pool = clean_pool_name(pool)
        volume = clean_volume_name(volume)
        virsh(conf, "vol-delete", "--pool", pool, volume, timeout=60)
    except Exception:
        pass


def disk_path_from_pool(conf: Dict[str, str], pool: str, volume_name: str) -> str:
    pool = clean_pool_name(pool)
    volume_name = clean_volume_name(volume_name)
    ensure_pool_active(conf, pool)
    base = pool_target_path(conf, pool)
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, volume_name)


def validate_existing_file(path: str, label: str) -> str:
    path = clean_abs_path(path, label)
    if not os.path.exists(path):
        raise ValueError(f"{label} introuvable : {path}")
    if not os.path.isfile(path) and not path.startswith("/dev/"):
        raise ValueError(f"{label} invalide : ce n'est pas un fichier utilisable.")
    return path



def virt_install_bin(conf: Dict[str, str]) -> str:
    return str(conf.get("VIRT_INSTALL_BIN", DEFAULT_CONFIG.get("VIRT_INSTALL_BIN", "virt-install")) or "virt-install").strip()


def setfacl_bin(conf: Dict[str, str]) -> str:
    return str(conf.get("SETFACL_BIN", DEFAULT_CONFIG.get("SETFACL_BIN", "setfacl")) or "setfacl").strip()


def detect_libvirt_qemu_user(conf: Dict[str, str]) -> str:
    explicit = str(conf.get("QEMU_USER", "") or "").strip()
    candidates = []
    if explicit:
        candidates.append(explicit)
    for user in ("libvirt-qemu", "qemu"):
        if user not in candidates:
            candidates.append(user)
    for user in candidates:
        rc, _out = run_cmd(["id", "-u", user], timeout=5)
        if rc == 0:
            return user
    return explicit


def user_can_access_path(user: str, path: str, mode: str) -> bool:
    if not user or not path:
        return False
    flags = [f"-{char}" for char in str(mode or "").strip().lstrip("-") if char in "rwx"]
    if not flags:
        flags = ["-e"]
    ok = True
    for flag in flags:
        rc, _out = run_cmd(["runuser", "-u", user, "--", "test", flag, path], timeout=8)
        if rc != 0:
            ok = False
            break
    if ok:
        return True
    # Fallback si runuser n'est pas disponible dans l'environnement minimal.
    test_script = " && ".join("test " + shlex.quote(flag) + " " + shlex.quote(path) for flag in flags)
    rc, _out = run_cmd(["su", "-s", "/bin/sh", user, "-c", test_script], timeout=8)
    return rc == 0


def apply_libvirt_acl(conf: Dict[str, str], user: str, path: str, rights: str) -> Tuple[bool, str]:
    bin_path = setfacl_bin(conf)
    rc, out = run_cmd([bin_path, "-m", f"u:{user}:{rights}", path], timeout=20)
    if rc == 0:
        return True, out
    return False, out.strip() or f"{bin_path} a Ã©chouÃ© sur {path}"


def path_parent_chain(path: str) -> List[str]:
    out: List[str] = []
    current = os.path.abspath(os.path.realpath(os.path.dirname(path)))
    while current and current != os.path.dirname(current):
        out.append(current)
        current = os.path.dirname(current)
    out.reverse()
    return out


def ensure_libvirt_qemu_can_read_file(conf: Dict[str, str], path: str, label: str) -> None:
    """PrÃ©pare les droits minimaux pour que le processus QEMU de libvirt lise un ISO.

    Flask/root peut voir le fichier, mais la VM tourne souvent sous l'utilisateur
    libvirt-qemu. Sans droit de traversÃ©e sur le dossier parent, virt-install crÃ©e
    la VM puis Ã©choue avec "Cannot open ... Permission denied". On corrige par ACL
    utilisateur, sans ouvrir les droits en 777 et sans changer le propriÃ©taire.
    """
    if path.startswith("/dev/"):
        return
    if not conf_bool(conf, "VM_CREATE_AUTO_FIX_LIBVIRT_ACL", DEFAULT_CONFIG["VM_CREATE_AUTO_FIX_LIBVIRT_ACL"]):
        return
    user = detect_libvirt_qemu_user(conf)
    if not user:
        vm_log(conf, f"AccÃ¨s libvirt non vÃ©rifiÃ© pour {label} : utilisateur QEMU introuvable.")
        return

    changed: List[str] = []
    errors: List[str] = []
    for directory in path_parent_chain(path):
        if user_can_access_path(user, directory, "x"):
            continue
        ok, msg = apply_libvirt_acl(conf, user, directory, "rx")
        if ok:
            changed.append(directory)
        else:
            errors.append(f"{directory}: {msg}")

    if not user_can_access_path(user, path, "r"):
        ok, msg = apply_libvirt_acl(conf, user, path, "r")
        if ok:
            changed.append(path)
        else:
            errors.append(f"{path}: {msg}")

    if changed:
        vm_log(conf, f"ACL libvirt appliquÃ©es pour {label} ({user}) : " + ", ".join(changed))

    if errors or not user_can_access_path(user, path, "r"):
        detail = " ; ".join(errors) if errors else "droits encore insuffisants aprÃ¨s setfacl"
        raise ValueError(
            f"{label} n'est pas lisible par libvirt/QEMU ({user}) : {path}. "
            f"Erreur droits : {detail}. Installe le paquet acl si setfacl manque, "
            f"ou donne Ã  {user} le droit de traverser les dossiers parents et de lire ce fichier."
        )


def iso_staging_enabled(conf: Dict[str, str]) -> bool:
    mode = str(conf.get("VM_CREATE_ISO_STAGING", "auto") or "auto").strip().lower()
    return mode not in {"0", "false", "no", "off", "non", "disabled"}


def iso_path_needs_staging(conf: Dict[str, str], path: str) -> bool:
    mode = str(conf.get("VM_CREATE_ISO_STAGING", "auto") or "auto").strip().lower()
    if mode in {"1", "true", "yes", "on", "oui", "always"}:
        return True
    if mode in {"0", "false", "no", "off", "non", "disabled"}:
        return False
    real = os.path.abspath(os.path.realpath(path))
    safe_roots = [
        "/var/lib/libvirt",
        str(conf.get("STORAGE_DEFAULT_DIR", "") or ""),
        str(conf.get("ISO_DEFAULT_DIR", "") or ""),
    ]
    for root in safe_roots:
        root = str(root or "").strip()
        if root and real.startswith(os.path.abspath(os.path.realpath(root)).rstrip("/") + "/"):
            return False
    return True


def stage_iso_for_libvirt(conf: Dict[str, str], source: str) -> str:
    source = validate_existing_file(source, "ISO")
    if not iso_staging_enabled(conf) or not iso_path_needs_staging(conf, source):
        return source

    staging_dir = str(conf.get("VM_CREATE_ISO_STAGING_DIR", DEFAULT_CONFIG["VM_CREATE_ISO_STAGING_DIR"]) or "").strip()
    staging_dir = resolve_module_path(staging_dir, DEFAULT_CONFIG["VM_CREATE_ISO_STAGING_DIR"])
    os.makedirs(staging_dir, exist_ok=True)

    stat = os.stat(source)
    digest = hashlib.sha256(f"{os.path.abspath(source)}:{stat.st_size}:{int(stat.st_mtime)}".encode("utf-8", "replace")).hexdigest()[:16]
    base = safe_filename(os.path.basename(source))
    if not base.lower().endswith((".iso", ".img")):
        base += ".iso"
    staged = os.path.join(staging_dir, f"{digest}-{base}")

    try:
        if os.path.exists(staged):
            dst_stat = os.stat(staged)
            if dst_stat.st_size == stat.st_size and int(dst_stat.st_mtime) == int(stat.st_mtime):
                return staged
        tmp = staged + ".tmp"
        shutil.copy2(source, tmp)
        os.replace(tmp, staged)
        os.chmod(staged, 0o644)
        vm_log(conf, f"ISO stagee pour libvirt : {source} -> {staged}")
    finally:
        try:
            if os.path.exists(staged + ".tmp"):
                os.unlink(staged + ".tmp")
        except OSError:
            pass
    return staged


def prepare_install_iso(conf: Dict[str, str], source: str) -> str:
    source = validate_existing_file(source, "ISO")
    try:
        ensure_libvirt_qemu_can_read_file(conf, source, "ISO")
    except Exception as exc:
        vm_log(conf, f"Controle ACL ISO non bloquant : {exc}")
        if not iso_staging_enabled(conf):
            raise
    return stage_iso_for_libvirt(conf, source)


def build_network_arg(payload: Dict[str, object], conf: Dict[str, str]) -> str:
    kind = str(payload.get("network_kind") or conf.get("VM_CREATE_DEFAULT_NETWORK_KIND") or "bridge").strip().lower()
    source = str(payload.get("network_source") or conf.get("VM_CREATE_DEFAULT_NETWORK_SOURCE") or "").strip()
    model = str(payload.get("network_model") or conf.get("VM_CREATE_DEFAULT_NETWORK_MODEL") or "virtio").strip() or "virtio"
    if kind in {"none", "aucun", "off"}:
        return "none"
    if kind == "network":
        source = source or "default"
        if not SAFE_NET_RE.match(source):
            raise ValueError("Nom de rÃ©seau libvirt invalide.")
        return f"network={source},model={model}"
    if kind == "direct":
        if not source or not re.match(r"^[A-Za-z0-9_.:-]+$", source):
            raise ValueError("Interface directe invalide.")
        return f"type=direct,source={source},source_mode=bridge,model={model}"
    # bridge par dÃ©faut : br0 si possible, sinon premier bridge dÃ©tectÃ©, sinon virbr0.
    source = source or first_existing_bridge() or "br0"
    if not re.match(r"^[A-Za-z0-9_.:-]+$", source):
        raise ValueError("Nom de bridge invalide.")
    return f"bridge={source},model={model}"


def build_virt_install_command(conf: Dict[str, str], payload: Dict[str, object]) -> Tuple[str, List[str], Dict[str, object]]:
    name = clean_new_vm_name(str(payload.get("name") or ""))
    names, err = list_vm_names(conf)
    if err:
        raise RuntimeError(err)
    if name in names:
        raise ValueError(f"Une VM existe dÃ©jÃ  avec ce nom : {name}")

    mode = str(payload.get("mode") or "iso").strip().lower()
    if mode not in {"iso", "import"}:
        raise ValueError("Mode de crÃ©ation inconnu.")

    storage_mode = str(payload.get("disk_storage_mode") or conf.get("VM_CREATE_DEFAULT_STORAGE_MODE") or "pool").strip().lower()
    if storage_mode not in {"pool", "path"}:
        raise ValueError("Mode stockage invalide : pool ou chemin manuel.")

    memory_gb_default = conf.get("VM_CREATE_DEFAULT_MEMORY_GB", "4")
    disk_gb_default = conf.get("VM_CREATE_DEFAULT_DISK_GB", "64")
    vcpus = positive_int(payload.get("vcpus") or conf.get("VM_CREATE_DEFAULT_VCPUS", "2"), "vCPU", 1, 256)
    memory_gb = positive_int(payload.get("memory_gb") or memory_gb_default, "RAM Go", 1, 4096)
    disk_gb = positive_int(payload.get("disk_gb") or disk_gb_default, "Taille disque Go", 1, 1048576)

    disk_format = str(payload.get("disk_format") or conf.get("VM_CREATE_DEFAULT_DISK_FORMAT") or "qcow2").strip().lower()
    if disk_format not in {"qcow2", "raw"}:
        raise ValueError("Format disque invalide : qcow2 ou raw uniquement.")
    disk_bus = str(payload.get("disk_bus") or conf.get("VM_CREATE_DEFAULT_DISK_BUS") or "virtio").strip().lower()
    if disk_bus not in {"virtio", "sata", "scsi", "ide", "usb", "nvme"}:
        raise ValueError("Bus disque invalide.")

    extra_disk_args: List[str] = []
    physical_disk_path = str(payload.get("physical_disk_path") or "").strip()
    physical_disk_bus = str(payload.get("physical_disk_bus") or "sata").strip().lower()
    if disk_bus == "nvme" or (physical_disk_path and physical_disk_bus == "nvme"):
        require_virtual_nvme_disk_support(conf)
    if physical_disk_path:
        physical_disk_path = clean_abs_path(physical_disk_path, "disque physique")
        if not os.path.exists(physical_disk_path):
            raise ValueError(f"Disque physique introuvable : {physical_disk_path}")
        if physical_disk_bus not in {"virtio", "sata", "scsi", "usb", "nvme"}:
            raise ValueError("Bus du disque physique invalide.")
        physical_serial = ",serial=yoleo-extra-nvme" if physical_disk_bus == "nvme" else ""
        extra_disk_args.append(f"path={physical_disk_path},format=raw,bus={physical_disk_bus},cache=none,discard=unmap{physical_serial}")

    raw_pci_devices = payload.get("pci_devices") or []
    if isinstance(raw_pci_devices, str):
        pci_devices = [x.strip() for x in raw_pci_devices.split(",") if x.strip()]
    elif isinstance(raw_pci_devices, list):
        pci_devices = [str(x).strip() for x in raw_pci_devices if str(x).strip()]
    else:
        pci_devices = []
    for address in pci_devices:
        parse_pci_address(address)

    disk_pool = ""
    disk_volume = ""
    created_volume = False
    if storage_mode == "pool":
        disk_pool = clean_pool_name(str(payload.get("disk_pool") or conf.get("VM_CREATE_DEFAULT_STORAGE_POOL") or "default"))
        if mode == "import":
            disk_volume = clean_volume_name(str(payload.get("disk_volume") or payload.get("disk_volume_name") or ""))
            disk_path = volume_path_in_pool(conf, disk_pool, disk_volume)
        else:
            disk_volume = clean_volume_name(str(payload.get("disk_volume_name") or (safe_filename(name) + "." + disk_format)))
            disk_path = disk_path_from_pool(conf, disk_pool, disk_volume)
    else:
        disk_path = str(payload.get("disk_path") or "").strip() or default_vm_disk_path(conf, name)
        disk_path = clean_abs_path(disk_path, "chemin disque")

    os_variant = str(payload.get("os_variant") or conf.get("VM_CREATE_DEFAULT_OS_VARIANT") or "generic").strip() or "generic"
    graphics = str(payload.get("graphics") or conf.get("VM_CREATE_DEFAULT_GRAPHICS") or "vnc").strip().lower()
    if graphics not in {"spice", "vnc", "none"}:
        raise ValueError("Console graphique invalide : spice, vnc ou none.")
    video = str(payload.get("video") or conf.get("VM_CREATE_DEFAULT_VIDEO") or "bochs").strip().lower()
    if video not in {"virtio", "qxl", "vga", "cirrus", "bochs"}:
        raise ValueError("ModÃ¨le vidÃ©o invalide.")
    firmware_raw = str(payload.get("firmware") or conf.get("VM_CREATE_DEFAULT_FIRMWARE") or "bios").strip()
    firmware = firmware_raw.lower()
    firmware_loader = str(payload.get("firmware_loader") or "").strip()
    firmware_nvram_template = str(payload.get("firmware_nvram_template") or "").strip()
    firmware_arch = ""
    if firmware.startswith("loader:") and not firmware_loader:
        firmware_loader = firmware_raw.split(":", 1)[1].strip()
        firmware = "loader"
    elif firmware.startswith("loader:"):
        firmware = "loader"
    if firmware_loader:
        firmware_loader = validate_existing_file(firmware_loader, "firmware UEFI CODE")
        firmware = "loader"
        firmware_arch = firmware_arch_label(firmware_loader)
    if firmware_nvram_template:
        firmware_nvram_template = validate_existing_file(firmware_nvram_template, "firmware UEFI VARS")
    if firmware not in {"bios", "uefi", "loader"}:
        raise ValueError("Firmware invalide : BIOS, UEFI auto ou fichier firmware.")
    cpu_model = str(payload.get("cpu_model") or conf.get("VM_CREATE_DEFAULT_CPU") or "host-passthrough").strip()
    virtio_serial = payload_bool(payload, "virtio_serial", False)

    disk_serial = ",serial=yoleo-nvme0" if disk_bus == "nvme" else ""
    if mode == "import":
        if storage_mode == "pool":
            disk_arg = f"vol={disk_pool}/{disk_volume},format={disk_format},bus={disk_bus},cache=none,discard=unmap{disk_serial}"
        else:
            disk_path = validate_existing_file(disk_path, "disque existant")
            disk_arg = f"path={disk_path},format={disk_format},bus={disk_bus},cache=none,discard=unmap{disk_serial}"
    else:
        iso_path = prepare_install_iso(conf, str(payload.get("iso_path") or ""))
        if storage_mode == "pool":
            disk_path = create_volume_in_pool(conf, disk_pool, disk_volume, disk_gb, disk_format)
            created_volume = True
        parent = os.path.dirname(disk_path)
        os.makedirs(parent, exist_ok=True)
        if storage_mode != "pool" and os.path.exists(disk_path):
            raise ValueError(f"Le disque existe dÃ©jÃ , je refuse de l'Ã©craser : {disk_path}")
        disk_arg = f"vol={disk_pool}/{disk_volume},format={disk_format},bus={disk_bus},cache=none,discard=unmap{disk_serial}" if storage_mode == "pool" else f"path={disk_path},size={disk_gb},format={disk_format},bus={disk_bus},cache=none,discard=unmap{disk_serial}"

    net_arg = build_network_arg(payload, conf)
    gfx_arg = "none" if graphics == "none" else f"{graphics},listen=0.0.0.0"

    cmd = [virt_install_bin(conf)]
    uri = str(conf.get("LIBVIRT_URI", "") or "").strip()
    if uri:
        cmd.extend(["--connect", uri])
    cmd.extend([
        "--name", name,
        "--memory", str(memory_gb * 1024),
        "--vcpus", f"{vcpus},sockets=1,cores={vcpus},threads=1",
        "--disk", disk_arg,
        "--osinfo", os_variant,
        "--network", net_arg,
        "--graphics", gfx_arg,
        "--video", video,
        "--noautoconsole",
        "--wait", "0",
    ])
    for extra_disk_arg in extra_disk_args:
        cmd.extend(["--disk", extra_disk_arg])
    for pci_address in pci_devices:
        cmd.extend(["--hostdev", pci_address])
    if cpu_model:
        cmd.extend(["--cpu", cpu_model])
    if firmware == "loader":
        boot_parts = [f"loader={firmware_loader}", "loader.readonly=yes", "loader.type=pflash", "bootmenu.enable=on"]
        if firmware_nvram_template:
            boot_parts.append(f"nvram.template={firmware_nvram_template}")
        boot_arg = ",".join(boot_parts)
        if firmware_arch and firmware_arch not in {"x86_64", "auto"}:
            cmd.extend(["--arch", firmware_arch])
            if firmware_arch in {"aarch64", "arm64"}:
                cmd.extend(["--machine", "virt"])
    else:
        boot_arg = "uefi,bootmenu.enable=on" if firmware == "uefi" else "bootmenu.enable=on"
    cmd.extend(["--boot", boot_arg])
    if virtio_serial:
        cmd.extend(["--console", "pty,target.type=virtio"])
    if mode == "import":
        cmd.append("--import")
    else:
        cmd.extend(["--cdrom", iso_path])

    meta = {
        "name": name,
        "mode": mode,
        "storage_mode": storage_mode,
        "disk_pool": disk_pool,
        "disk_volume": disk_volume,
        "disk_path": disk_path,
        "memory_gb": memory_gb,
        "vcpus": vcpus,
        "network": net_arg,
        "graphics": graphics,
        "firmware": firmware,
        "firmware_loader": firmware_loader,
        "firmware_nvram_template": firmware_nvram_template,
        "virtio_serial": virtio_serial,
        "created_volume": created_volume,
        "physical_disk_path": physical_disk_path,
        "pci_devices": pci_devices,
    }
    return name, cmd, meta


def friendly_virt_install_error(out: str, name: str) -> str:
    text = (out or "").strip()
    if "Permission denied" in text and "Cannot open" in text:
        return (
            "CrÃ©ation VM Ã©chouÃ©e : libvirt/QEMU n'a pas le droit de lire un fichier demandÃ© "
            "(souvent l'ISO dans /mnt/user/... ou un dossier parent sans droit de traversÃ©e). "
            "J'ai gardÃ© le dÃ©tail technique ci-dessous.\n\n" + text
        )
    if "domain installation does not appear to have been successful" in text.lower():
        return "CrÃ©ation VM Ã©chouÃ©e pendant virt-install. DÃ©tail technique :\n\n" + text
    return text or f"virt-install a Ã©chouÃ© pour {name}"


def run_virt_install_with_compat(cmd: List[str], timeout: int) -> Tuple[int, str, List[str]]:
    rc, out = run_cmd(cmd, timeout=timeout)
    if rc == 0:
        return rc, out, cmd

    low = (out or "").lower()
    wants_compat = "osinfo" in low or "bootmenu.enable" in low or "unknown --boot options" in low
    if not wants_compat:
        return rc, out, cmd

    compat = list(cmd)
    changed = False
    for idx, value in enumerate(compat):
        if value == "--osinfo":
            compat[idx] = "--os-variant"
            changed = True
        elif value == "bootmenu.enable=on":
            compat[idx] = "menu=on"
            changed = True
        elif value == "uefi,bootmenu.enable=on":
            compat[idx] = "uefi,menu=on"
            changed = True

    if not changed:
        return rc, out, cmd

    rc2, out2 = run_cmd(compat, timeout=timeout)
    if rc2 == 0:
        return rc2, out2, compat
    return rc2, (out2 or "") + "\n\nPremier essai virt-install:\n" + (out or ""), compat


def do_create_vm(conf: Dict[str, str], payload: Dict[str, object]) -> Tuple[Dict[str, object], int]:
    name, cmd, meta = build_virt_install_command(conf, payload)
    timeout = conf_int(conf, "ACTION_TIMEOUT", 90)
    timeout = max(timeout, 180)
    vm_log(conf, "CrÃ©ation VM demandÃ©e : " + json.dumps(meta, ensure_ascii=False))
    rc, out, used_cmd = run_virt_install_with_compat(cmd, timeout=timeout)
    if rc != 0:
        if meta.get("created_volume") and meta.get("disk_pool") and meta.get("disk_volume"):
            delete_volume_from_pool(conf, str(meta.get("disk_pool") or ""), str(meta.get("disk_volume") or ""))
        vm_log(conf, f"CrÃ©ation VM {name} Ã©chouÃ©e rc={rc}: {out.strip()}")
        return {"ok": False, "message": friendly_virt_install_error(out, name), "name": name}, 500
    if used_cmd != cmd:
        vm_log(conf, f"VM {name}: virt-install relance en compatibilite ancienne syntaxe.")
    disk_pool = str(meta.get("disk_pool") or "").strip()
    if disk_pool:
        virsh(conf, "pool-refresh", disk_pool, timeout=60)
    if payload_bool(payload, "autostart", False):
        virsh(conf, "autostart", name, timeout=30)
    vm_log(conf, f"VM crÃ©Ã©e : {name}")
    return {"ok": True, "message": out.strip() or f"VM {name} crÃ©Ã©e. Ouvre la console pour installer l'OS.", "name": name}, 200
