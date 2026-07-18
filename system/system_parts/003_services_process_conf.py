import copy
import threading

SAFE_SERVICE_RE = re.compile(r"^[A-Za-z0-9_.@:\\-]+\.service$")

REFRESH_SECONDS = get_conf_int("SYSTEMCTL_REFRESH_SECONDS", 5)
MAX_OUTPUT_CHARS = get_conf_int("SYSTEMCTL_MAX_OUTPUT_CHARS", 5000)
PROCESS_REFRESH_SECONDS = get_conf_int("PROCESS_REFRESH_SECONDS", 3)
SYSTEM_LOG_LINES = get_conf_int("SYSTEM_LOG_LINES", 180)
SYSTEM_LOG_DEFAULT_UNIT = get_conf_str("SYSTEM_LOG_DEFAULT_UNIT", "")
SYSTEM_OVERVIEW_CACHE_SECONDS = get_conf_int("SYSTEM_OVERVIEW_CACHE_SECONDS", 300)

HARDWARE_INVENTORY_CACHE_SECONDS = get_conf_int("HARDWARE_INVENTORY_CACHE_SECONDS", 300)
HARDWARE_INVENTORY_CACHE: Dict[str, Any] | None = None
HARDWARE_INVENTORY_CACHE_TS = 0.0

SYSTEM_OVERVIEW_STATIC_LOCK = threading.RLock()
SYSTEM_OVERVIEW_STATIC_CACHE: Dict[str, Any] | None = None
SYSTEM_OVERVIEW_STATIC_REFRESHING = False


def now_label() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def systemctl_bin() -> str:
    return shutil.which("systemctl") or "systemctl"


def journalctl_bin() -> str:
    return shutil.which("journalctl") or "journalctl"


def run_cmd(cmd: List[str], timeout: int = 20) -> Tuple[int, str]:
    try:
        completed = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return completed.returncode, completed.stdout or ""
    except subprocess.TimeoutExpired:
        return 124, "Timeout"
    except FileNotFoundError:
        return 127, f"Commande absente: {cmd[0]}"
    except Exception as exc:
        return 1, str(exc)


def json_response(ok: bool, message: str, **extra: Any):
    payload: Dict[str, Any] = {"ok": bool(ok), "message": message}
    payload.update(extra)
    return jsonify(payload)


def clean_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    text = text or ""
    if len(text) > limit:
        return text[-limit:]
    return text


def valid_service_name(service: str) -> bool:
    return bool(SAFE_SERVICE_RE.fullmatch(service or ""))


def service_error(service: str) -> str:
    if not service:
        return "Service manquant."
    return "Nom de service refusé."


def parse_unit_files(text: str) -> Dict[str, Dict[str, Any]]:
    """
    systemctl list-unit-files --type=service --no-legend --no-pager

    Exemples :
      docker.service enabled enabled
      rsync.service disabled enabled
      systemd-fsck-root.service static -
    """
    units: Dict[str, Dict[str, Any]] = {}

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue

        parts = line.split()
        if len(parts) < 2:
            continue

        name = parts[0].strip()
        if name.startswith("●"):
            name = name.lstrip("●").strip()

        if not name.endswith(".service"):
            continue

        state = parts[1].strip()
        preset = parts[2].strip() if len(parts) >= 3 else ""

        units[name] = {
            "name": name,
            "unit_file_state": state,
            "unit_file_preset": preset,
        }

    return units


def parse_units(text: str) -> Dict[str, Dict[str, Any]]:
    """
    systemctl list-units --type=service --all --plain --no-legend --no-pager

    Colonnes :
      UNIT LOAD ACTIVE SUB DESCRIPTION
    """
    units: Dict[str, Dict[str, Any]] = {}

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue

        if line.startswith("●"):
            line = line[1:].strip()

        parts = line.split(None, 4)
        if len(parts) < 4:
            continue

        name = parts[0].strip()
        if not name.endswith(".service"):
            continue

        load_state = parts[1].strip()
        active_state = parts[2].strip()
        sub_state = parts[3].strip()
        description = parts[4].strip() if len(parts) >= 5 else ""

        units[name] = {
            "name": name,
            "load_state": load_state,
            "active_state": active_state,
            "sub_state": sub_state,
            "description": description,
        }

    return units


def show_one_service(service: str) -> Dict[str, str]:
    """
    Complète les infos d'un service précis sans multiplier les appels pour tout le tableau.
    Utile après une action ou pour les détails.
    """
    if not valid_service_name(service):
        return {}

    cmd = [
        systemctl_bin(),
        "show",
        service,
        "--no-pager",
        "--property=Id,Description,LoadState,ActiveState,SubState,UnitFileState,UnitFilePreset,MainPID,FragmentPath",
    ]
    rc, out = run_cmd(cmd, timeout=8)
    if rc != 0:
        return {}

    data: Dict[str, str] = {}
    for raw in out.splitlines():
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        data[key] = value

    return data


def fr_active_label(active: str, sub: str = "") -> str:
    active = (active or "unknown").lower()
    sub = (sub or "").lower()

    if active == "active":
        if sub == "running":
            return "Actif / tourne"
        if sub:
            return f"Actif / {sub}"
        return "Actif"
    if active == "inactive":
        if sub == "dead":
            return "Arrêté"
        if sub:
            return f"Inactif / {sub}"
        return "Inactif"
    if active == "failed":
        return "Erreur"
    if active == "activating":
        return "Démarrage..."
    if active == "deactivating":
        return "Arrêt..."
    if active == "reloading":
        return "Rechargement..."
    return active or "Inconnu"


def fr_enabled_label(state: str) -> str:
    state = (state or "unknown").lower()

    labels = {
        "enabled": "Activé auto",
        "enabled-runtime": "Auto runtime",
        "linked": "Lié",
        "linked-runtime": "Lié runtime",
        "disabled": "Désactivé",
        "static": "Statique",
        "indirect": "Indirect",
        "generated": "Généré",
        "transient": "Temporaire",
        "masked": "Masqué",
        "masked-runtime": "Masqué runtime",
        "alias": "Alias",
        "bad": "Invalide",
        "not-found": "Introuvable",
        "unknown": "Inconnu",
    }
    return labels.get(state, state or "Inconnu")


def active_class(active: str, sub: str = "") -> str:
    active = (active or "").lower()
    sub = (sub or "").lower()

    if active == "active":
        return "active"
    if active == "failed":
        return "bad"
    if active in {"activating", "reloading"}:
        return "warn"
    if active == "deactivating":
        return "warn"
    if active == "inactive" and sub == "dead":
        return "inactive"
    if active == "inactive":
        return "inactive"
    return "unknown"


def enabled_class(state: str) -> str:
    state = (state or "").lower()

    if state in {"enabled", "enabled-runtime"}:
        return "enabled"
    if state in {"disabled"}:
        return "disabled"
    if state in {"masked", "masked-runtime", "bad", "not-found"}:
        return "bad"
    if state in {"static", "indirect", "generated", "transient", "alias"}:
        return "static"
    if state in {"linked", "linked-runtime"}:
        return "enabled"
    return "unknown"


def can_enable(state: str) -> bool:
    state = (state or "").lower()
    return state in {"disabled", "indirect", "linked-runtime", "masked-runtime"}


def can_disable(state: str) -> bool:
    state = (state or "").lower()
    return state in {"enabled", "enabled-runtime", "linked", "linked-runtime", "indirect"}


def can_start(load_state: str, unit_file_state: str) -> bool:
    load_state = (load_state or "").lower()
    unit_file_state = (unit_file_state or "").lower()
    return load_state not in {"masked", "not-found", "bad"} and unit_file_state not in {"masked", "masked-runtime", "bad", "not-found"}


def can_stop(active_state: str) -> bool:
    return (active_state or "").lower() in {"active", "activating", "reloading"}


def short_unit_name(name: str) -> str:
    if name.endswith(".service"):
        return name[:-8]
    return name


def row_sort_key(row: Dict[str, Any]) -> Tuple[int, str]:
    active = row.get("active_state") or ""
    enabled = row.get("unit_file_state") or ""

    if active == "failed":
        prio = 0
    elif active == "active":
        prio = 1
    elif enabled in {"enabled", "enabled-runtime"}:
        prio = 2
    else:
        prio = 3

    return prio, str(row.get("name") or "").lower()


def collect_services() -> Dict[str, Any]:
    sysctl = systemctl_bin()

    rc_files, out_files = run_cmd(
        [sysctl, "list-unit-files", "--type=service", "--no-legend", "--no-pager"],
        timeout=20,
    )
    rc_units, out_units = run_cmd(
        [sysctl, "list-units", "--type=service", "--all", "--plain", "--no-legend", "--no-pager"],
        timeout=20,
    )

    if rc_files == 127 or rc_units == 127:
        return {
            "ok": False,
            "message": "systemctl est introuvable sur cet hôte.",
            "generated_at": now_label(),
            "services": [],
            "summary": {},
            "output": clean_output((out_files or "") + "\n" + (out_units or "")),
        }

    unit_files = parse_unit_files(out_files)
    units = parse_units(out_units)

    names = sorted(set(unit_files.keys()) | set(units.keys()))
    rows: List[Dict[str, Any]] = []

    for name in names:
        base = unit_files.get(name, {})
        runtime = units.get(name, {})

        unit_file_state = str(base.get("unit_file_state") or runtime.get("unit_file_state") or "unknown")
        unit_file_preset = str(base.get("unit_file_preset") or "")
        load_state = str(runtime.get("load_state") or "not-loaded")
        active_state = str(runtime.get("active_state") or "inactive")
        sub_state = str(runtime.get("sub_state") or "dead")
        description = str(runtime.get("description") or "")

        # Si le service n'est pas actuellement chargé, on évite d'afficher un faux "dead"
        # trop agressif : il existe bien, mais systemd ne l'a pas en mémoire active.
        if name not in units:
            load_state = "not-loaded"
            active_state = "inactive"
            sub_state = "dead"

        row = {
            "name": name,
            "short_name": short_unit_name(name),
            "description": description or "—",
            "load_state": load_state,
            "active_state": active_state,
            "sub_state": sub_state,
            "active_label": fr_active_label(active_state, sub_state),
            "active_class": active_class(active_state, sub_state),
            "unit_file_state": unit_file_state,
            "unit_file_label": fr_enabled_label(unit_file_state),
            "unit_file_class": enabled_class(unit_file_state),
            "unit_file_preset": unit_file_preset or "—",
            "can_start": can_start(load_state, unit_file_state),
            "can_stop": can_stop(active_state),
            "can_restart": can_start(load_state, unit_file_state),
            "can_enable": can_enable(unit_file_state),
            "can_disable": can_disable(unit_file_state),
            "can_disable_now": can_disable(unit_file_state) or can_stop(active_state),
        }
        rows.append(row)

    rows.sort(key=row_sort_key)

    summary = {
        "total": len(rows),
        "active": sum(1 for r in rows if r["active_state"] == "active"),
        "running": sum(1 for r in rows if r["sub_state"] == "running"),
        "failed": sum(1 for r in rows if r["active_state"] == "failed"),
        "enabled": sum(1 for r in rows if str(r["unit_file_state"]).startswith("enabled")),
        "disabled": sum(1 for r in rows if r["unit_file_state"] == "disabled"),
        "static": sum(1 for r in rows if r["unit_file_state"] == "static"),
    }

    return {
        "ok": rc_files == 0 or rc_units == 0,
        "message": "OK" if (rc_files == 0 or rc_units == 0) else "Lecture systemctl incomplète.",
        "generated_at": now_label(),
        "services": rows,
        "summary": summary,
        "errors": {
            "list_unit_files_rc": rc_files,
            "list_units_rc": rc_units,
            "list_unit_files_output": clean_output(out_files, 1200) if rc_files != 0 else "",
            "list_units_output": clean_output(out_units, 1200) if rc_units != 0 else "",
        },
    }


def action_command(action: str, service: str) -> Tuple[bool, List[str], str, int]:
    sysctl = systemctl_bin()

    if action == "start":
        return True, [sysctl, "start", service], "Démarrage demandé.", 60
    if action == "stop":
        return True, [sysctl, "stop", service], "Arrêt demandé.", 60
    if action == "restart":
        return True, [sysctl, "restart", service], "Redémarrage demandé.", 90
    if action == "enable":
        return True, [sysctl, "enable", service], "Démarrage automatique activé.", 60
    if action == "disable":
        return True, [sysctl, "disable", service], "Démarrage automatique désactivé.", 60
    if action == "disable-now":
        return True, [sysctl, "disable", "--now", service], "Service arrêté et désactivé au démarrage.", 90

    return False, [], "Action refusée.", 0


# --------------------------------------------------
# INFOS HOTE / PROCESSUS / LOGS
# --------------------------------------------------
def _read_first_existing(paths: List[str], default: str = "—") -> str:
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                value = handle.read().strip()
            if value:
                return value
        except Exception:
            pass
    return default


def _read_os_release() -> Dict[str, str]:
    data: Dict[str, str] = {}
    try:
        with open("/etc/os-release", "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                data[key] = value.strip().strip('"').strip("'")
    except Exception:
        pass
    return data


def _seconds_to_label(seconds: float) -> str:
    try:
        seconds = int(max(0, seconds))
    except Exception:
        seconds = 0
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}j {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _hardware_cmd_missing(command: str) -> Dict[str, Any]:
    return {
        "available": False,
        "generated_at": now_label(),
        "error": f"Commande absente: {command}",
        "rows": [],
    }


def collect_pci_devices(limit: int = 120, include_display: bool = False) -> Dict[str, Any]:
    """Inventaire PCI local via lspci.

    La page Info hôte a déjà un tableau GPU dédié. Par défaut on garde donc
    les autres cartes PCI pour éviter le doublon : réseau, SATA/NVMe, USB,
    audio, DVB/tuners, contrôleurs, etc.
    """
    lspci = shutil.which("lspci")
    if not lspci:
        return _hardware_cmd_missing("lspci")

    rc, out = run_cmd([lspci, "-Dnn"], timeout=8)
    rows: List[Dict[str, Any]] = []
    display_classes = {"vga compatible controller", "3d controller", "display controller"}

    if rc == 0:
        for raw in (out or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            slot, rest = (line.split(None, 1) + [""])[:2]
            category, name = (rest.split(": ", 1) + [""])[:2] if ": " in rest else ("—", rest or "—")
            category_norm = category.strip().lower()
            if not include_display and category_norm in display_classes:
                continue
            ids = re.findall(r"\[([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\]", line)
            rows.append({
                "slot": slot,
                "class": category.strip() or "—",
                "name": name.strip() or "—",
                "id": ids[-1] if ids else "—",
            })
            if len(rows) >= limit:
                break

    return {
        "available": rc == 0,
        "generated_at": now_label(),
        "error": "" if rc == 0 else clean_output(out, 800),
        "count": len(rows),
        "rows": rows,
    }


def collect_usb_devices(limit: int = 160) -> Dict[str, Any]:
    """Inventaire USB local via lsusb : claviers, souris, tuners, ponts USB, etc."""
    lsusb = shutil.which("lsusb")
    if not lsusb:
        return _hardware_cmd_missing("lsusb")

    rc, out = run_cmd([lsusb], timeout=8)
    rows: List[Dict[str, Any]] = []
    pattern = re.compile(r"^Bus\s+(?P<bus>\d+)\s+Device\s+(?P<device>\d+):\s+ID\s+(?P<id>[0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s*(?P<name>.*)$")

    if rc == 0:
        for raw in (out or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            match = pattern.match(line)
            if match:
                rows.append({
                    "bus": match.group("bus"),
                    "device": match.group("device"),
                    "id": match.group("id"),
                    "name": (match.group("name") or "—").strip() or "—",
                })
            else:
                rows.append({"bus": "—", "device": "—", "id": "—", "name": line})
            if len(rows) >= limit:
                break

    return {
        "available": rc == 0,
        "generated_at": now_label(),
        "error": "" if rc == 0 else clean_output(out, 800),
        "count": len(rows),
        "rows": rows,
    }


def collect_hardware_inventory(force_reload: bool = False) -> Dict[str, Any]:
    """Cache léger pour éviter de relancer lspci/lsusb toutes les 5 secondes."""
    global HARDWARE_INVENTORY_CACHE, HARDWARE_INVENTORY_CACHE_TS
    now = time.time()
    if (
        not force_reload
        and isinstance(HARDWARE_INVENTORY_CACHE, dict)
        and now - HARDWARE_INVENTORY_CACHE_TS < HARDWARE_INVENTORY_CACHE_SECONDS
    ):
        return copy.deepcopy(HARDWARE_INVENTORY_CACHE)

    data = {
        "generated_at": now_label(),
        "pci": collect_pci_devices(),
        "usb": collect_usb_devices(),
    }
    HARDWARE_INVENTORY_CACHE = copy.deepcopy(data)
    HARDWARE_INVENTORY_CACHE_TS = now
    return data


def collect_host_info() -> Dict[str, Any]:
    os_release = _read_os_release()
    route_info = get_default_route_info()
    iface = route_info.get("iface") or "-"
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    hardware_inventory = collect_hardware_inventory()

    cpu_model = _read_first_existing(["/proc/cpuinfo"], "")
    model_name = "—"
    for raw in cpu_model.splitlines():
        if raw.lower().startswith("model name") and ":" in raw:
            model_name = raw.split(":", 1)[1].strip()
            break

    return {
        "generated_at": now_label(),
        "time": time.strftime("%H:%M:%S"),
        "date": time.strftime("%d/%m/%Y"),
        "hostname": platform.node(),
        "fqdn": platform.node(),
        "os": os_release.get("PRETTY_NAME") or platform.platform(),
        "debian_version": _read_first_existing(["/etc/debian_version"], "—"),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "boot_time": time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(psutil.boot_time())),
        "uptime": _seconds_to_label(time.time() - psutil.boot_time()),
        "board_vendor": _read_first_existing(["/sys/class/dmi/id/board_vendor"], "—"),
        "board_name": _read_first_existing(["/sys/class/dmi/id/board_name"], "—"),
        "bios_version": _read_first_existing(["/sys/class/dmi/id/bios_version"], "—"),
        "cpu_model": model_name,
        "cpu_cores_physical": psutil.cpu_count(logical=False) or 0,
        "cpu_cores_logical": psutil.cpu_count(logical=True) or 0,
        "ram_total": get_size_str(mem.total),
        "swap_total": get_size_str(swap.total),
        "default_iface": iface,
        "default_gateway": route_info.get("gateway") or "-",
        "local_ip": get_iface_ipv4(iface),
        "iface_state": get_iface_state(iface),
        "iface_speed": get_iface_speed(iface),
        "config_path": loaded_config or "",
        "config_dir": loaded_config_dir or "",
        "pci_devices": hardware_inventory.get("pci") or {"available": False, "rows": []},
        "usb_devices": hardware_inventory.get("usb") or {"available": False, "rows": []},
    }



def disk_top_conf_path() -> str:
    env_path = os.environ.get("DISK_TOP_CONF", "").strip()
    if env_path:
        return os.path.abspath(os.path.expanduser(os.path.expandvars(env_path)))
    roots = _project_root_candidates()
    candidates = [os.path.join(root, "conf", "disk_top.conf") for root in roots]
    candidates.extend([nas_conf_file("disk_top.conf"), "disk_top.conf"])
    for candidate in _unique_existing_order(candidates):
        if os.path.exists(candidate):
            return os.path.abspath(candidate)
    root = roots[0] if roots else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.abspath(os.path.join(root, "conf", "disk_top.conf"))


def disk_top_norm(path: str) -> str:
    value = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path or "").strip())))
    return "" if value == "." else value


def disk_top_read_config() -> Dict[str, List[str]]:
    path = disk_top_conf_path()
    mounts: List[str] = []
    usages: List[str] = []
    if not os.path.exists(path):
        return {"mounts": mounts, "usages": usages}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or line.startswith(";") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip().lower()
                mount = disk_top_norm(value.strip().strip('"'))
                if not mount:
                    continue
                if key.startswith(("mount", "path")) and mount not in mounts:
                    mounts.append(mount)
                elif key.startswith(("usage", "space", "watch", "monitor")) and mount not in usages:
                    usages.append(mount)
    except Exception:
        return {"mounts": mounts, "usages": usages}
    return {"mounts": mounts, "usages": usages}


def disk_top_read_mounts() -> List[str]:
    return disk_top_read_config().get("mounts") or []


def disk_top_read_usage_mounts() -> List[str]:
    return disk_top_read_config().get("usages") or []


def disk_top_findmnt_map() -> Dict[str, Dict[str, str]]:
    rows: Dict[str, Dict[str, str]] = {}
    code, output = run_cmd(["findmnt", "-rn", "-o", "TARGET,SOURCE,FSTYPE"], timeout=8)
    if code != 0:
        return rows
    for raw in (output or "").splitlines():
        parts = raw.split(None, 2)
        if not parts:
            continue
        target = disk_top_norm(parts[0])
        if not target:
            continue
        rows[target] = {"source": parts[1] if len(parts) > 1 else "—", "fstype": parts[2] if len(parts) > 2 else "—"}
    return rows


def disk_top_label(path: str) -> str:
    cleaned = str(path or "").rstrip("/")
    return os.path.basename(cleaned) or cleaned or "Montage"


def disk_top_usage_row(path: str, mounted: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    info = mounted.get(path) or {}
    exists = os.path.exists(path)
    is_mount = bool(info) or os.path.ismount(path)
    row: Dict[str, Any] = {
        "path": path,
        "label": disk_top_label(path),
        "ok": False,
        "exists": exists,
        "is_mount": is_mount,
        "source": info.get("source") or "—",
        "fstype": info.get("fstype") or "—",
        "percent": 0,
        "used": "—",
        "free": "—",
        "total": "—",
        "status": "missing",
        "status_label": "Absent",
    }
    if not is_mount:
        if exists:
            row["status"] = "folder"
            row["status_label"] = "Non monte"
        return row
    try:
        usage = psutil.disk_usage(path)
    except Exception as exc:
        row["status"] = "error"
        row["status_label"] = str(exc)[:80] or "Erreur"
        return row
    row.update({
        "ok": True,
        "status": "ok",
        "status_label": "OK",
        "percent": usage.percent,
        "used": get_size_str(usage.used),
        "free": get_size_str(usage.free),
        "total": get_size_str(usage.total),
    })
    return row


def collect_disk_top_status() -> Dict[str, Any]:
    disk_top_config = disk_top_read_config()
    configured = disk_top_config.get("mounts") or []
    usage_configured = disk_top_config.get("usages") or []
    mounted = disk_top_findmnt_map()
    rows: List[Dict[str, Any]] = []
    usage_rows: List[Dict[str, Any]] = []
    ok_count = 0
    warn_count = 0
    fail_count = 0
    for path in configured:
        info = mounted.get(path) or {}
        exists = os.path.exists(path)
        is_mount = bool(info) or os.path.ismount(path)
        try:
            has_local_entries = exists and os.path.isdir(path) and (not is_mount) and bool(os.listdir(path))
        except Exception:
            has_local_entries = False
        if is_mount:
            status = "ok"
            label = "OK"
            ok_count += 1
        elif exists:
            status = "folder_with_data" if has_local_entries else "folder"
            label = "Dossier local" if has_local_entries else "Dossier"
            warn_count += 1
        else:
            status = "missing"
            label = "Absent"
            fail_count += 1
        rows.append({
            "path": path,
            "ok": is_mount,
            "exists": exists,
            "is_mount": is_mount,
            "source": info.get("source") or "—",
            "fstype": info.get("fstype") or "—",
            "status": status,
            "status_label": label,
        })
    for path in usage_configured:
        usage_rows.append(disk_top_usage_row(path, mounted))
    total = len(rows)
    state = "empty"
    label = "Non configuré"
    if total:
        if fail_count or warn_count:
            state = "warning" if not fail_count else "error"
            label = "Erreur" if fail_count else "Attention"
        else:
            state = "ok"
            label = "OK"
    return {
        "configured": total,
        "total": total,
        "ok": ok_count,
        "warn": warn_count,
        "fail": fail_count,
        "state": state,
        "label": label,
        "config_path": disk_top_conf_path(),
        "rows": rows,
        "usage_rows": usage_rows,
        "usage_total": len(usage_rows),
        "usage_ok": len([row for row in usage_rows if row.get("ok")]),
    }


def _read_hwmon_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except Exception:
        return ""


def _fan_number_from_path(path: str) -> int:
    match = re.search(r"fan(\d+)_input$", os.path.basename(path or ""))
    if not match:
        return 0
    try:
        return int(match.group(1))
    except Exception:
        return 0


def _rpm_from_text(value: str):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return max(0, int(round(float(value))))
    except Exception:
        return None


def collect_fans() -> Dict[str, Any]:
    """Lit les ventilateurs actifs exposés par Linux via hwmon, avec fallback psutil.

    Les cartes mères exposent généralement les RPM sous /sys/class/hwmon.
    Pour l'accueil Yoleo, on masque les headers non branchés / arrêtés à 0 RPM :
    seuls les vrais ventilateurs actifs sont affichés.
    """
    rows: List[Dict[str, Any]] = []

    try:
        for hwmon in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            chip = _read_hwmon_text(os.path.join(hwmon, "name")) or os.path.basename(hwmon)
            for fan_input in sorted(glob.glob(os.path.join(hwmon, "fan*_input")), key=_fan_number_from_path):
                fan_num = _fan_number_from_path(fan_input)
                if fan_num <= 0:
                    continue
                rpm = _rpm_from_text(_read_hwmon_text(fan_input))
                # Le contrôleur Nuvoton/MSI expose souvent tous les headers possibles.
                # Les entrées à 0 RPM correspondent généralement à des prises non utilisées :
                # elles encombrent l'accueil et ne sont pas utiles au diagnostic quotidien.
                if rpm is None or rpm <= 0:
                    continue
                label = _read_hwmon_text(os.path.join(hwmon, f"fan{fan_num}_label")) or f"FAN {fan_num}"
                fault = _read_hwmon_text(os.path.join(hwmon, f"fan{fan_num}_fault"))
                status = "unknown"
                if fault == "1":
                    status = "fault"
                elif rpm is None:
                    status = "unknown"
                elif rpm <= 0:
                    status = "stopped"
                else:
                    status = "ok"
                rows.append({
                    "id": f"{chip}:{fan_num}",
                    "label": str(label).strip() or f"FAN {fan_num}",
                    "index": fan_num,
                    "rpm": rpm,
                    "rpm_label": f"{rpm} RPM" if rpm is not None else "—",
                    "chip": chip,
                    "fault": fault == "1",
                    "status": status,
                    "source": "hwmon",
                })
    except Exception:
        rows = []

    if not rows:
        try:
            sensors = psutil.sensors_fans() or {}
            for chip, fans in sensors.items():
                for idx, fan in enumerate(fans or [], start=1):
                    rpm = _rpm_from_text(getattr(fan, "current", None))
                    if rpm is None or rpm <= 0:
                        continue
                    label = getattr(fan, "label", "") or f"FAN {idx}"
                    rows.append({
                        "id": f"{chip}:{idx}",
                        "label": str(label).strip() or f"FAN {idx}",
                        "index": idx,
                        "rpm": rpm,
                        "rpm_label": f"{rpm} RPM" if rpm is not None else "—",
                        "chip": chip,
                        "fault": False,
                        "status": "stopped" if rpm == 0 else ("ok" if rpm else "unknown"),
                        "source": "psutil",
                    })
        except Exception:
            rows = []

    rows.sort(key=lambda row: (str(row.get("chip") or ""), int(row.get("index") or 0), str(row.get("label") or "")))
    return {
        "available": bool(rows),
        "count": len(rows),
        "rows": rows,
        "source": "hwmon" if rows and rows[0].get("source") == "hwmon" else ("psutil" if rows else ""),
    }



def _virsh_lines(args: List[str], timeout: int = 8) -> Tuple[bool, List[str]]:
    """Retourne les lignes utiles de virsh, sans lever d'exception.

    Utilisé uniquement pour le résumé VM de l'accueil. On reste volontairement
    sur virsh CLI pour éviter d'ajouter une dépendance Python libvirt.
    """
    virsh = shutil.which("virsh")
    if not virsh:
        return False, []
    rc, out = run_cmd([virsh, "-c", "qemu:///system", *args], timeout=timeout)
    if rc != 0:
        return False, []
    lines = [line.strip() for line in (out or "").splitlines() if line.strip()]
    return True, lines


def collect_vm_summary() -> Dict[str, Any]:
    """Compte les VM libvirt pour l'accueil.

    Total = toutes les VM définies dans libvirt.
    running = VM réellement en exécution.
    stopped = total - running, pour garder une lecture simple côté tableau NAS.
    """
    ok_all, all_names = _virsh_lines(["list", "--all", "--name"])
    if not ok_all:
        return {"available": False, "total": 0, "running": 0, "stopped": 0}

    ok_running, running_names = _virsh_lines(["list", "--state-running", "--name"])
    if not ok_running:
        running_names = []

    total = len([name for name in all_names if name])
    running = len([name for name in running_names if name])
    stopped = max(0, total - running)
    return {
        "available": True,
        "total": total,
        "running": running,
        "stopped": stopped,
    }


def _home_read_kv_file(path: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or line.startswith(";") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                data[key.strip().upper()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return data


def _home_resolve_conf_path(value: str, base_path: str = "") -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    value = os.path.expanduser(os.path.expandvars(value))
    if os.path.isabs(value):
        return value
    if base_path:
        return os.path.abspath(os.path.join(os.path.dirname(base_path), value))
    return os.path.abspath(value)


def _build_cache_candidates() -> List[str]:
    candidates: List[str] = []
    for conf_name in ("builds.conf", "build.conf", "registry.conf"):
        conf_path = nas_conf_file(conf_name)
        conf = _home_read_kv_file(conf_path)
        cache_path = conf.get("BUILD_CACHE_FILE", "")
        if cache_path:
            candidates.append(_home_resolve_conf_path(cache_path, conf_path))
    candidates.append(nas_conf_file("build.jdom"))

    out: List[str] = []
    seen = set()
    for raw in candidates:
        path = os.path.abspath(os.path.expanduser(os.path.expandvars(str(raw or ""))))
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def collect_build_dashboard_summary() -> Dict[str, Any]:
    """Résumé léger du module Build pour l'accueil.

    On lit seulement le cache Build existant pour éviter qu'un affichage de la
    page d'accueil rescane les Dockerfiles, les TAR ou le registre.
    """
    for path in _build_cache_candidates():
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            continue
        except Exception as exc:
            return {
                "available": False,
                "label": "Cache illisible",
                "message": str(exc),
                "total": 0,
                "projects": 0,
                "tars": 0,
                "to_build": 0,
                "to_push": 0,
                "meta_missing": 0,
                "updated_at": "",
            }

        builds = payload.get("builds") if isinstance(payload, dict) else []
        if not isinstance(builds, list):
            builds = []
        summary = payload.get("summary") if isinstance(payload, dict) and isinstance(payload.get("summary"), dict) else {}

        to_build = sum(1 for item in builds if isinstance(item, dict) and item.get("can_build"))
        to_push = sum(1 for item in builds if isinstance(item, dict) and item.get("can_import"))
        meta_missing = int(summary.get("meta_missing") or sum(1 for item in builds if isinstance(item, dict) and item.get("meta_missing")))
        total = int(summary.get("total") or len(builds))
        projects = int(summary.get("projects") or sum(1 for item in builds if isinstance(item, dict) and item.get("has_context")))
        tars = int(summary.get("tars") or sum(1 for item in builds if isinstance(item, dict) and item.get("tar_exists")))

        if total <= 0:
            label = "Cache vide"
        elif to_build <= 0 and to_push <= 0 and meta_missing <= 0:
            label = "Tout est à jour"
        elif to_build > 0 and to_push > 0:
            label = f"{to_build} build · {to_push} registre"
        elif to_build > 0:
            label = f"{to_build} à builder"
        elif to_push > 0:
            label = f"{to_push} à envoyer"
        else:
            label = f"{meta_missing} base à compléter"

        return {
            "available": True,
            "label": label,
            "message": "",
            "total": total,
            "projects": projects,
            "tars": tars,
            "to_build": to_build,
            "to_push": to_push,
            "meta_missing": meta_missing,
            "updated_at": str(payload.get("updated_at") or "") if isinstance(payload, dict) else "",
        }

    return {
        "available": False,
        "label": "Cache vide",
        "message": "Mets à jour le cache Build.",
        "total": 0,
        "projects": 0,
        "tars": 0,
        "to_build": 0,
        "to_push": 0,
        "meta_missing": 0,
        "updated_at": "",
    }


def _overview_static_defaults(error: str = "") -> Dict[str, Any]:
    return {
        "host": {"hostname": "System", "os": error, "kernel": ""},
        "network": {"iface": "-", "ip": "-", "gateway": "-", "state": "unknown", "speed": "—"},
        "services": {},
        "docker": {"total": 0, "running": 0, "available": False},
        "vms": {"total": 0, "running": 0, "stopped": 0, "available": False},
        "build": {"available": False, "label": "Cache vide", "total": 0, "projects": 0, "tars": 0, "to_build": 0, "to_push": 0, "meta_missing": 0, "updated_at": ""},
        "mounts": {"available": False, "rows": [], "usage_rows": [], "count": 0},
        "fans": {"available": False, "count": 0, "rows": [], "source": ""},
    }


def _collect_overview_static_parts() -> Dict[str, Any]:
    route_info = get_default_route_info()
    iface = route_info.get("iface") or "-"

    docker_info = {"total": 0, "running": 0, "available": docker is not None}
    if docker is not None:
        try:
            containers = docker.from_env().containers.list(all=True)
            docker_info = {
                "total": len(containers),
                "running": len([x for x in containers if x.status == "running"]),
                "available": True,
            }
        except Exception:
            docker_info["available"] = False

    services = collect_services()
    service_summary = services.get("summary") or {}

    return {
        "host": collect_host_info(),
        "network": {
            "iface": iface,
            "ip": get_iface_ipv4(iface),
            "gateway": route_info.get("gateway") or "-",
            "state": get_iface_state(iface),
            "speed": get_iface_speed(iface),
        },
        "services": service_summary,
        "docker": docker_info,
        "vms": collect_vm_summary(),
        "mounts": collect_disk_top_status(),
        "fans": collect_fans(),
    }


def _overview_static_refresh_worker() -> None:
    global SYSTEM_OVERVIEW_STATIC_CACHE, SYSTEM_OVERVIEW_STATIC_REFRESHING
    try:
        data = _collect_overview_static_parts()
        with SYSTEM_OVERVIEW_STATIC_LOCK:
            SYSTEM_OVERVIEW_STATIC_CACHE = {"ts": time.time(), "data": copy.deepcopy(data)}
            SYSTEM_OVERVIEW_STATIC_REFRESHING = False
    except Exception:
        with SYSTEM_OVERVIEW_STATIC_LOCK:
            SYSTEM_OVERVIEW_STATIC_REFRESHING = False


def overview_static_parts_load(force_reload: bool = False) -> Dict[str, Any]:
    global SYSTEM_OVERVIEW_STATIC_CACHE, SYSTEM_OVERVIEW_STATIC_REFRESHING

    start_background_refresh = False
    with SYSTEM_OVERVIEW_STATIC_LOCK:
        if not force_reload and isinstance(SYSTEM_OVERVIEW_STATIC_CACHE, dict):
            cached_data = SYSTEM_OVERVIEW_STATIC_CACHE.get("data")
            cached_ts = float(SYSTEM_OVERVIEW_STATIC_CACHE.get("ts", 0) or 0)
            if isinstance(cached_data, dict):
                if time.time() - cached_ts > SYSTEM_OVERVIEW_CACHE_SECONDS and not SYSTEM_OVERVIEW_STATIC_REFRESHING:
                    SYSTEM_OVERVIEW_STATIC_REFRESHING = True
                    start_background_refresh = True
                data = copy.deepcopy(cached_data)
            else:
                data = _overview_static_defaults()

            if start_background_refresh:
                threading.Thread(
                    target=_overview_static_refresh_worker,
                    name="system-overview-cache-refresh",
                    daemon=True,
                ).start()
            return data

        if SYSTEM_OVERVIEW_STATIC_REFRESHING and isinstance(SYSTEM_OVERVIEW_STATIC_CACHE, dict):
            cached_data = SYSTEM_OVERVIEW_STATIC_CACHE.get("data")
            if isinstance(cached_data, dict):
                return copy.deepcopy(cached_data)

        SYSTEM_OVERVIEW_STATIC_REFRESHING = True

    try:
        data = _collect_overview_static_parts()
    except Exception as exc:
        data = _overview_static_defaults(str(exc))

    with SYSTEM_OVERVIEW_STATIC_LOCK:
        SYSTEM_OVERVIEW_STATIC_CACHE = {"ts": time.time(), "data": copy.deepcopy(data)}
        SYSTEM_OVERVIEW_STATIC_REFRESHING = False
        return copy.deepcopy(data)


def collect_overview() -> Dict[str, Any]:
    mem = psutil.virtual_memory()
    load1, load5, load15 = (0.0, 0.0, 0.0)
    try:
        load1, load5, load15 = psutil.getloadavg()
    except Exception:
        pass

    root_usage = None
    try:
        root_usage = psutil.disk_usage("/")
    except Exception:
        root_usage = None

    preferred_mount = "/mnt/user" if os.path.exists("/mnt/user") else "/"
    try:
        main_disk = psutil.disk_usage(preferred_mount)
    except Exception:
        main_disk = root_usage
        preferred_mount = "/"

    static_parts = overview_static_parts_load()
    static_defaults = _overview_static_defaults()

    return {
        "generated_at": now_label(),
        "time": time.strftime("%H:%M:%S"),
        "date": time.strftime("%A %d/%m/%Y"),
        "host": static_parts.get("host") or static_defaults["host"],
        "cpu": {"percent": psutil.cpu_percent(interval=None), "load1": round(load1, 2), "load5": round(load5, 2), "load15": round(load15, 2)},
        "ram": {"percent": mem.percent, "used": get_size_str(mem.used), "total": get_size_str(mem.total)},
        "disk": {
            "mount": preferred_mount,
            "percent": main_disk.percent if main_disk else 0,
            "used": get_size_str(main_disk.used) if main_disk else "—",
            "total": get_size_str(main_disk.total) if main_disk else "—",
        },
        "network": static_parts.get("network") or static_defaults["network"],
        "services": static_parts.get("services") or static_defaults["services"],
        "docker": static_parts.get("docker") or static_defaults["docker"],
        "vms": static_parts.get("vms") or static_defaults["vms"],
        "build": collect_build_dashboard_summary(),
        "processes": {"total": len(psutil.pids())},
        "mounts": collect_disk_top_status(),
        "fans": static_parts.get("fans") or static_defaults["fans"],
        "uptime": _seconds_to_label(time.time() - psutil.boot_time()),
    }


try:
    overview_static_parts_load(force_reload=True)
    print("Cache accueil systeme charge en memoire.")
except Exception as exc:
    print(f"Cache accueil systeme non charge : {exc}")


def collect_processes(limit: int = 120, query: str = "", sort: str = "cpu") -> Dict[str, Any]:
    try:
        limit = max(10, min(500, int(limit)))
    except Exception:
        limit = 120
    query = (query or "").strip().lower()
    sort = (sort or "cpu").strip().lower()

    rows: List[Dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "ppid", "name", "username", "status", "cpu_percent", "memory_percent", "memory_info", "create_time", "cmdline"]):
        try:
            info = proc.info
            cmdline = " ".join(info.get("cmdline") or [])
            name = info.get("name") or ""
            username = info.get("username") or ""
            haystack = f"{info.get('pid')} {name} {username} {cmdline}".lower()
            if query and query not in haystack:
                continue
            rss = 0
            try:
                rss = int((info.get("memory_info") or {}).rss)  # type: ignore[attr-defined]
            except Exception:
                try:
                    rss = int(info.get("memory_info").rss)  # type: ignore[union-attr]
                except Exception:
                    rss = 0
            rows.append({
                "pid": info.get("pid") or 0,
                "ppid": info.get("ppid") or 0,
                "name": name or "—",
                "user": username or "—",
                "status": info.get("status") or "—",
                "cpu": round(float(info.get("cpu_percent") or 0.0), 1),
                "mem_percent": round(float(info.get("memory_percent") or 0.0), 1),
                "rss": get_size_str(rss),
                "started": time.strftime("%d/%m %H:%M", time.localtime(info.get("create_time") or time.time())),
                "cmd": cmdline[:500] if cmdline else name,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue

    if sort == "mem":
        rows.sort(key=lambda r: (r.get("mem_percent") or 0, r.get("cpu") or 0), reverse=True)
    elif sort == "pid":
        rows.sort(key=lambda r: int(r.get("pid") or 0))
    elif sort == "name":
        rows.sort(key=lambda r: str(r.get("name") or "").lower())
    else:
        rows.sort(key=lambda r: (r.get("cpu") or 0, r.get("mem_percent") or 0), reverse=True)

    return {
        "ok": True,
        "generated_at": now_label(),
        "total": len(rows),
        "shown": min(len(rows), limit),
        "processes": rows[:limit],
    }


def read_journal_logs(source: str = "system", unit: str = "", lines: int = SYSTEM_LOG_LINES) -> Dict[str, Any]:
    try:
        lines = max(20, min(1000, int(lines)))
    except Exception:
        lines = SYSTEM_LOG_LINES

    source = (source or "system").strip().lower()
    unit = (unit or "").strip()
    cmd = [journalctl_bin(), "-n", str(lines), "--no-pager", "--output=short-iso"]

    if source == "kernel":
        cmd.insert(1, "-k")
    elif source == "unit" and unit:
        if not valid_service_name(unit):
            return {"ok": False, "message": "Nom de service refusé.", "output": ""}
        cmd[1:1] = ["-u", unit]
    elif source == "boot":
        cmd[1:1] = ["-b"]

    rc, out = run_cmd(cmd, timeout=15)
    return {
        "ok": rc == 0,
        "message": "Logs chargés." if rc == 0 else "Lecture journalctl impossible.",
        "source": source,
        "unit": unit,
        "generated_at": now_label(),
        "output": clean_output(out, limit=30000),
    }

# --------------------------------------------------
# ROUTES SYSTEME : moniteur + services systemd
# --------------------------------------------------
def _system_sys_info():
    try:
        with open("/sys/class/dmi/id/board_vendor", "r", encoding="utf-8", errors="replace") as f:
            v = f.read().strip()
        with open("/sys/class/dmi/id/board_name", "r", encoding="utf-8", errors="replace") as f:
            m = f.read().strip()
        mobo = f"{v} {m}"
    except Exception:
        mobo = "Inconnu"

    return {
        "node": platform.node(),
        "kernel": platform.release(),
        "mobo": mobo,
        "boot": time.strftime("%d/%m %H:%M", time.localtime(psutil.boot_time())),
    }



# --------------------------------------------------
# GESTIONNAIRE DE FICHIERS CONF INTÉGRÉ
# Gestionnaire de conf fusionné directement dans system.py.
# --------------------------------------------------
CONF_MANAGER_ROOT_BLACKLIST = {'proc', 'sys', 'dev', 'boot'}


def conf_manager_new_parser():
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    return parser


def conf_manager_split_list(value: str) -> List[str]:
    raw = str(value or "")
    for sep in ["\n", ";", "|"]:
        raw = raw.replace(sep, ",")
    return [item.strip() for item in raw.split(",") if item.strip()]


def conf_manager_numbered_values_from_loaded_conf(section_name: str, prefix: str) -> List[str]:
    """Lit vraiment les sections historiques du gestionnaire de conf dans system.conf.

    Exemple attendu, exactement comme l'ancien settings_ini.conf :
      [WATCH_DIRS]
      DIR1=../conf
      

      [INDIVIDUAL_FILES]
      FILE1=../conf/special.conf

    On ne dépend pas du chargeur global CONF ici, car system.conf est surtout un
    fichier clé=valeur et les sections du gestionnaire doivent rester lisibles
    séparément.
    """
    path = loaded_config
    if not path or not os.path.exists(path):
        return []

    wanted_section = str(section_name or "").strip().upper()
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$", re.IGNORECASE)
    in_section = False
    values = []

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or line.startswith(";"):
                    continue

                if line.startswith("[") and line.endswith("]"):
                    in_section = line[1:-1].strip().upper() == wanted_section
                    continue

                if not in_section or "=" not in line:
                    continue

                key, value = line.split("=", 1)
                match = pattern.match(key.strip())
                if not match:
                    continue

                clean_value = value.strip().strip('"').strip("'")
                if clean_value:
                    values.append((int(match.group(1)), clean_value))
    except Exception as exc:
        print(f"❌ Erreur lecture section [{section_name}] dans {path}: {exc}")
        return []

    return [value for _index, value in sorted(values, key=lambda item: item[0])]


def conf_manager_numbered_values(prefix: str, section_name: str = "") -> List[str]:
    """Retourne DIR1, DIR2, DIR3... ou FILE1, FILE2... dans l'ordre numérique.

    Priorité : vraie section [WATCH_DIRS] / [INDIVIDUAL_FILES] dans system.conf.
    Fallback : clés déjà chargées dans CONF, au cas où tu utilises encore un format plat.
    """
    if section_name:
        section_values = conf_manager_numbered_values_from_loaded_conf(section_name, prefix)
        if section_values:
            return section_values

    values = []
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$", re.IGNORECASE)

    for key, value in CONF.items():
        match = pattern.match(str(key).strip())
        if not match:
            continue
        clean_value = str(value or "").strip()
        if clean_value:
            values.append((int(match.group(1)), clean_value))

    return [value for _index, value in sorted(values, key=lambda item: item[0])]


def conf_manager_resolve_path(path: str) -> str:
    raw = os.path.expanduser(os.path.expandvars(str(path or "").strip().strip('"').strip("'")))
    if not raw:
        return ""
    if os.path.isabs(raw):
        return os.path.realpath(raw)

    # IMPORTANT : on garde le comportement de l'ancien settings_ini.py.
    # Avant, DIR1=../conf et  étaient testés tels quels
    # depuis le dossier de lancement du Flask. Donc ici on privilégie d'abord
    # le chemin relatif brut / cwd, puis le dossier du module, puis seulement
    # le dossier du system.conf chargé. Ça évite de casser DIR1, DIR2, DIR3...
    bases = [
        os.getcwd(),
        os.path.dirname(os.path.abspath(__file__)),
    ]
    if loaded_config_dir:
        bases.append(loaded_config_dir)

    candidates = []
    for base in bases:
        if not base:
            continue
        candidate = os.path.realpath(os.path.join(base, raw))
        if candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    return candidates[0] if candidates else os.path.realpath(raw)


def conf_manager_allowed_extensions() -> Tuple[str, ...]:
    exts = conf_manager_split_list(get_conf_str("CONF_MANAGER_ALLOWED_EXTENSIONS", ".ini,.conf,.cfg,.txt"))
    clean = []
    for ext in exts:
        ext = ext.lower().strip()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        clean.append(ext)
    return tuple(clean) or ('.ini', '.conf', '.cfg', '.txt')


def conf_manager_get_icon(filename: str) -> str:
    n = filename.lower()
    if 'ffmpeg' in n:
        return "🎬"
    if 'audio' in n:
        return "🎧"
    if 'network' in n:
        return "📡"
    if 'key' in n or 'ssh' in n:
        return "🔑"
    return "⚙️"


def conf_manager_get_sources() -> Dict[str, Any]:
    """Infos visibles dans l'onglet pour vérifier immédiatement DIR1/DIR2/DIR3."""
    watch_dirs = conf_manager_numbered_values("DIR", "WATCH_DIRS")
    if not watch_dirs:
        watch_dirs = conf_manager_split_list(get_conf_str("CONF_MANAGER_WATCH_DIRS", "../conf"))

    individual_files = conf_manager_numbered_values("FILE", "INDIVIDUAL_FILES")
    if not individual_files:
        individual_files = conf_manager_split_list(get_conf_str("CONF_MANAGER_INDIVIDUAL_FILES", ""))

    return {
        "config_file": loaded_config or "",
        "watch_dirs": [
            {
                "index": idx,
                "raw": raw,
                "resolved": conf_manager_resolve_path(raw),
                "exists": os.path.isdir(conf_manager_resolve_path(raw)),
            }
            for idx, raw in enumerate(watch_dirs, start=1)
        ],
        "individual_files": [
            {
                "index": idx,
                "raw": raw,
                "resolved": conf_manager_resolve_path(raw),
                "exists": os.path.isfile(conf_manager_resolve_path(raw)),
            }
            for idx, raw in enumerate(individual_files, start=1)
        ],
        "allowed_extensions": ", ".join(conf_manager_allowed_extensions()),
    }


def conf_manager_get_files_grouped() -> Dict[str, List[Dict[str, str]]]:
    grouped: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    allowed_ext = conf_manager_allowed_extensions()

    def add(path: str, name: str, folder: str) -> None:
        grouped[folder].append({
            'name': name,
            'path': path,
            'folder': folder,
            'icon': conf_manager_get_icon(name),
        })

    # Nouveau fonctionnement : tout est dans system.conf.
    # Priorité au format historique : DIR1, DIR2, DIR3... / FILE1, FILE2...
    # Fallback : listes CONF_MANAGER_WATCH_DIRS / CONF_MANAGER_INDIVIDUAL_FILES séparées par virgules.
    watch_dirs = conf_manager_numbered_values("DIR", "WATCH_DIRS")
    if not watch_dirs:
        watch_dirs = conf_manager_split_list(get_conf_str("CONF_MANAGER_WATCH_DIRS", "../conf"))

    individual_files = conf_manager_numbered_values("FILE", "INDIVIDUAL_FILES")
    if not individual_files:
        individual_files = conf_manager_split_list(get_conf_str("CONF_MANAGER_INDIVIDUAL_FILES", ""))

    for directory_raw in watch_dirs:
        directory = conf_manager_resolve_path(directory_raw)
        if not os.path.isdir(directory):
            continue
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    if entry.is_file() and entry.name.lower().endswith(allowed_ext):
                        add(os.path.realpath(entry.path), entry.name, directory)
        except Exception:
            pass

    for file_raw in individual_files:
        path = conf_manager_resolve_path(file_raw)
        if os.path.isfile(path) and os.path.basename(path).lower().endswith(allowed_ext):
            add(path, os.path.basename(path), os.path.dirname(path))

    for folder in grouped:
        grouped[folder].sort(key=lambda item: item['name'].lower())

    return dict(sorted(grouped.items()))



def conf_manager_clean_source_value(value: str) -> str:
    """Nettoie une valeur DIRn/FILEn sans la résoudre.

    Le system.conf doit garder les chemins tels que l'utilisateur les veut :
    ../conf pour le dossier NAS par défaut, /etc/ssh si l'utilisateur ajoute
    un dossier système, etc. On refuse seulement les lignes cassées.
    """
    value = str(value or "").replace("\r", " ").replace("\n", " ").strip().strip('"').strip("'")
    return value


def conf_manager_unique_sources(values: List[str], *, keep_default_conf: bool = False) -> List[str]:
    out: List[str] = []
    seen = set()

    if keep_default_conf:
        out.append("../conf")
        seen.add("../conf")

    for raw in values or []:
        value = conf_manager_clean_source_value(raw)
        if not value:
            continue
        key = value.rstrip("/") or value
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def conf_manager_system_conf_path() -> str:
    """Retourne le system.conf officiel à modifier pour les options du gestionnaire."""
    return loaded_config or nas_conf_file("system.conf")


def conf_manager_sources_block(watch_dirs: List[str], individual_files: List[str]) -> List[str]:
    lines: List[str] = [
        "",
        "# ============================================================",
        "# Gestionnaire de configurations intégré",
        "# DIRn = dossiers parcourus ; FILE1 reste vide pour compatibilité.",
        "# ============================================================",
        "[WATCH_DIRS]",
    ]
    for idx, value in enumerate(watch_dirs, start=1):
        lines.append(f"DIR{idx}={value}")

    lines.extend(["", "[INDIVIDUAL_FILES]"])
    if individual_files:
        for idx, value in enumerate(individual_files, start=1):
            lines.append(f"FILE{idx}={value}")
    else:
        lines.append("FILE1=")
    lines.append("")
    return lines


def conf_manager_replace_sources_in_system_conf(watch_dirs: List[str], individual_files: List[str]) -> str:
    """Réécrit uniquement [WATCH_DIRS] et [INDIVIDUAL_FILES] dans system.conf."""
    path = conf_manager_system_conf_path()
    ensure_system_conf_file(path)

    watch_dirs = conf_manager_unique_sources(watch_dirs, keep_default_conf=True)
    individual_files = conf_manager_unique_sources(individual_files, keep_default_conf=False)

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        lines = []

    remove_sections = {"WATCH_DIRS", "INDIVIDUAL_FILES"}
    filtered: List[str] = []
    removed_insert_at = None
    skip = False

    for idx, line in enumerate(lines):
        stripped = line.strip()
        is_section = stripped.startswith("[") and stripped.endswith("]")
        if is_section:
            section = stripped[1:-1].strip().upper()
            if section in remove_sections:
                if removed_insert_at is None:
                    removed_insert_at = len(filtered)
                skip = True
                continue
            skip = False
        if skip:
            continue
        filtered.append(line)

    block = conf_manager_sources_block(watch_dirs, individual_files)

    # Si les sections existaient, on remet le bloc au même endroit. Sinon on le
    # place avant la personnalisation/menu, pour garder system.conf lisible.
    insert_at = removed_insert_at
    if insert_at is None:
        insert_at = len(filtered)
        for i, line in enumerate(filtered):
            if line.strip() == "# BEGIN_SYSTEM_PERSONALIZATION_MENU":
                # remonte avant le gros commentaire de personnalisation si possible
                insert_at = max(0, i - 4)
                break

    new_lines = filtered[:insert_at]
    while new_lines and new_lines[-1].strip() == "":
        new_lines.pop()
    new_lines.extend(block)
    tail = filtered[insert_at:]
    while tail and tail[0].strip() == "":
        tail.pop(0)
    new_lines.extend(tail)

    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(new_lines).rstrip() + "\n")

    return path

def conf_manager_build_allowed_paths() -> set:
    return {item['path'] for group in conf_manager_get_files_grouped().values() for item in group}


def conf_manager_is_allowed_file(path: str) -> bool:
    target = os.path.realpath(str(path or '').strip())
    return target in conf_manager_build_allowed_paths()


def conf_manager_read_file_smart(filepath: str) -> Tuple[Dict[str, Dict[str, str]], str]:
    data: Dict[str, Dict[str, str]] = {}

    try:
        parser = conf_manager_new_parser()
        with open(filepath, 'r', encoding='utf-8') as handle:
            parser.read_file(handle)

        if parser.defaults() or parser.sections():
            if parser.defaults():
                data['DEFAULT'] = dict(parser.defaults())
            for section in parser.sections():
                data[section] = dict(parser.items(section, raw=True))
            return data, 'INI'
    except Exception:
        pass

    flat_data: Dict[str, str] = {}
    try:
        with open(filepath, 'r', encoding='utf-8') as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith('#') or line.startswith(';'):
                    continue
                if '=' in line:
                    key, value = line.split('=', 1)
                    flat_data[key.strip()] = value.strip()
        if flat_data:
            return {'GENERAL': flat_data}, 'FLAT'
    except Exception as exc:
        print(f"Erreur lecture conf : {exc}")

    return {}, 'UNKNOWN'


def conf_manager_save_flat_file(filepath: str, form_data: Any) -> None:
    lines = []
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as handle:
            lines = handle.readlines()

    original_keys = form_data.getlist('original_keys[]')
    keys = form_data.getlist('keys[]')
    values = form_data.getlist('values[]')

    if not (len(original_keys) == len(keys) == len(values)):
        raise ValueError("Les données du formulaire sont incohérentes.")

    updates_by_original = {}
    new_entries = []

    for original_key, new_key, new_value in zip(original_keys, keys, values):
        original_key = original_key.strip()
        new_key = new_key.strip()
        new_value = new_value.strip()

        if not new_key:
            continue

        if original_key:
            updates_by_original[original_key] = (new_key, new_value)
        else:
            new_entries.append((new_key, new_value))

    final_lines = []
    consumed_originals = set()

    for raw_line in lines:
        stripped = raw_line.strip()

        if '=' not in stripped or stripped.startswith('#') or stripped.startswith(';'):
            final_lines.append(raw_line)
            continue

        current_key = stripped.split('=', 1)[0].strip()

        if current_key in updates_by_original and current_key not in consumed_originals:
            new_key, new_value = updates_by_original[current_key]
            final_lines.append(f"{new_key}={new_value}\n")
            consumed_originals.add(current_key)
        else:
            continue

    for original_key, (new_key, new_value) in updates_by_original.items():
        if original_key not in consumed_originals:
            final_lines.append(f"{new_key}={new_value}\n")

    for new_key, new_value in new_entries:
        final_lines.append(f"{new_key}={new_value}\n")

    with open(filepath, 'w', encoding='utf-8') as handle:
        handle.writelines(final_lines)


def conf_manager_save_ini_file(filepath: str, form_data: Any) -> None:
    sections = form_data.getlist('sections[]')
    keys = form_data.getlist('keys[]')
    values = form_data.getlist('values[]')
    removed_sections = {item.strip() for item in form_data.getlist('removed_sections[]') if item.strip()}
    new_section_names = [item.strip() for item in form_data.getlist('new_section_name[]') if item.strip()]

    if not (len(sections) == len(keys) == len(values)):
        raise ValueError("Les données du formulaire sont incohérentes.")

    parser = conf_manager_new_parser()
    ordered_sections: List[str] = []
    section_map: Dict[str, List[Tuple[str, str]]] = defaultdict(list)

    for section, key, value in zip(sections, keys, values):
        section = section.strip() or 'GENERAL'
        key = key.strip()
        value = value.strip()

        if section in removed_sections or not key:
            continue

        if section not in ordered_sections:
            ordered_sections.append(section)
        section_map[section].append((key, value))

    for section_name in new_section_names:
        if section_name not in removed_sections and section_name not in ordered_sections:
            ordered_sections.append(section_name)
            section_map.setdefault(section_name, [])

    for section_name in ordered_sections:
        if section_name == 'DEFAULT':
            for key, value in section_map.get('DEFAULT', []):
                parser['DEFAULT'][key] = value
            continue

        if not parser.has_section(section_name):
            parser.add_section(section_name)
        for key, value in section_map.get(section_name, []):
            parser.set(section_name, key, value)

    with open(filepath, 'w', encoding='utf-8') as handle:
        parser.write(handle)


def conf_manager_save_file_smart(filepath: str, form_data: Any, original_mode: str) -> Tuple[bool, str]:
    try:
        if original_mode == 'INI':
            conf_manager_save_ini_file(filepath, form_data)
            return True, "Sauvegarde INI réussie."

        conf_manager_save_flat_file(filepath, form_data)
        return True, "Sauvegarde fichier plat réussie."
    except Exception as exc:
        print(f"❌ Erreur critique sauvegarde conf : {exc}")
        return False, str(exc)


# --------------------------------------------------
# PERSONNALISATION ACCUEIL : logique intégrée au module Système
# --------------------------------------------------
