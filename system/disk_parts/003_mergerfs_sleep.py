def mergerfs_default_options(conf: Dict[str, str]) -> str:
    return str(conf.get("MERGERFS_DEFAULT_OPTIONS") or "defaults,use_ino,cache.files=partial,category.create=mfs,allow_other").strip()


def module_relative_path(path: str) -> str:
    """Résout les anciens chemins relatifs par rapport au fichier Python du module.

    Les nouveaux profils MargeFS utilisent /var/lib/yoleo/disk.
    Cette fonction reste seulement pour compatibilité si un vieux disk.conf
    contient encore un chemin relatif explicite.
    """
    path = str(path or "").strip()
    if not path:
        path = "/var/lib/yoleo/disk/mergerfs_profiles.json"
    if os.path.isabs(path):
        return path
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(base_dir, path))


def mergerfs_state_file(conf: Dict[str, str]) -> str:
    return module_relative_path(str(conf.get("MERGERFS_STATE_FILE") or "/var/lib/yoleo/disk/mergerfs_profiles.json"))


def read_mergerfs_state(conf: Dict[str, str]) -> Dict[str, Any]:
    path = mergerfs_state_file(conf)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            data.setdefault("profiles", {})
            if isinstance(data.get("profiles"), dict):
                return data
    except Exception:
        pass
    return {"profiles": {}, "updated_at": ""}


def write_mergerfs_state(conf: Dict[str, str], data: Dict[str, Any]) -> None:
    path = mergerfs_state_file(conf)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def update_mergerfs_profile(conf: Dict[str, str], target: str, **updates: Any) -> None:
    target = str(target or "").strip()
    if not target:
        return
    data = read_mergerfs_state(conf)
    profiles = data.setdefault("profiles", {})
    if not isinstance(profiles, dict):
        profiles = {}
        data["profiles"] = profiles
    profile = profiles.get(target)
    if not isinstance(profile, dict):
        profile = {"target": target, "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    profile.update(updates)
    profile["target"] = target
    profile["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    profiles[target] = profile
    write_mergerfs_state(conf, data)


def remove_mergerfs_profile(conf: Dict[str, str], target: str) -> None:
    target = str(target or "").strip()
    data = read_mergerfs_state(conf)
    profiles = data.get("profiles")
    if isinstance(profiles, dict) and target in profiles:
        profiles.pop(target, None)
        write_mergerfs_state(conf, data)


def normalize_mergerfs_fstab_line(line: str) -> Tuple[str, bool]:
    stripped = (line or "").strip()
    if stripped.startswith(DISABLED_MERGERFS_PREFIX):
        return stripped[len(DISABLED_MERGERFS_PREFIX):].strip(), True
    return line, False


def read_fstab_lines(conf: Dict[str, str]) -> Tuple[bool, List[str], str]:
    path = fstab_path(conf)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return True, handle.read().splitlines(), ""
    except Exception as exc:
        return False, [], f"Lecture fstab impossible : {exc}"


def is_mergerfs_fstab_line(line: str) -> bool:
    logical_line, _disabled = normalize_mergerfs_fstab_line(line)
    stripped = logical_line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    parts = stripped.split()
    if len(parts) < 4:
        return False
    spec, target, fstype, options = parts[:4]
    blob = " ".join([spec, target, fstype, options]).lower()
    return "mergerfs" in blob or fstype.lower() in {"fuse.mergerfs", "mergerfs"}


def parse_mergerfs_fstab(conf: Dict[str, str]) -> List[Dict[str, Any]]:
    ok, lines, _error = read_fstab_lines(conf)
    if not ok:
        return []
    rows: List[Dict[str, Any]] = []
    for idx, line in enumerate(lines):
        logical_line, disabled = normalize_mergerfs_fstab_line(line)
        if not is_mergerfs_fstab_line(line):
            continue
        parts = logical_line.strip().split()
        if len(parts) < 4:
            continue
        source, target, fstype, options = parts[:4]
        branches = [p for p in source.split(":") if p]
        rows.append({
            "id": f"fstab-{idx}",
            "line_index": idx,
            "source": source,
            "sources": branches,
            "target": target,
            "fstype": fstype,
            "options": options,
            "raw": line,
            "disabled": bool(disabled),
            "from_fstab": True,
        })
    return rows


def current_mergerfs_live(conf: Dict[str, str]) -> List[Dict[str, Any]]:
    mountinfo = read_mountinfo(conf)
    live: List[Dict[str, Any]] = []
    for item in mergerfs_detected_mounts(mountinfo):
        live.append({
            "mount": item.get("mount") or "",
            "target": item.get("mount") or "",
            "source": item.get("source") or "",
            "sources": [p for p in str(item.get("source") or "").split(":") if p],
            "fstype": item.get("fstype") or "",
            "options": item.get("options") or "",
            "command": item.get("command") or "",
            "mounted": True,
            "from_live": True,
        })
    return live


def mergerfs_targets_live(conf: Dict[str, str]) -> set[str]:
    return {str(item.get("target") or item.get("mount") or "") for item in current_mergerfs_live(conf)}


def selectable_mergerfs_sources(conf: Dict[str, str]) -> List[Dict[str, str]]:
    data = collect_disks()
    rows: List[Dict[str, str]] = []
    seen: set[str] = set()

    def add_path(path: str, label: str, device: str = "") -> None:
        path = str(path or "").strip()
        if not path or path in seen:
            return
        # On évite de proposer les cibles mergerfs comme branches.
        base = os.path.basename(path.rstrip("/"))
        if path in {"/", "/boot", "/mnt/user", "/mnt/user0"} or base in {"user", "user0"}:
            return
        if not os.path.isdir(path):
            return
        seen.add(path)
        rows.append({"path": path, "label": label or path, "device": device})

    for disk in data.get("disks", []) or []:
        if disk.get("is_system_disk"):
            continue
        dev = str(disk.get("device") or "")
        mountpoint = str(disk.get("mountpoint") or "")
        if mountpoint:
            add_path(mountpoint, f"{dev} → {mountpoint}", dev)
        for m in disk.get("mounts") or []:
            add_path(str(m.get("path") or ""), f"{dev} → {m.get('path') or ''}", dev)
    return sorted(rows, key=lambda x: x["path"])


def write_mergerfs_fstab_entry(conf: Dict[str, str], sources: List[str], target: str, options: str) -> Tuple[bool, str]:
    ok, lines, error = read_fstab_lines(conf)
    if not ok:
        return False, error
    source_text = ":".join(sources)
    options = options or mergerfs_default_options(conf)
    new_line = f"{source_text}\t{target}\tfuse.mergerfs\t{options}\t0\t0"
    kept: List[str] = []
    for line in lines:
        logical_line, _disabled = normalize_mergerfs_fstab_line(line)
        stripped = logical_line.strip()
        if stripped and not stripped.startswith("#") and is_mergerfs_fstab_line(line):
            parts = stripped.split()
            if len(parts) >= 2 and parts[1] == target:
                continue
        kept.append(line)
    kept.append(new_line)
    try:
        fstab_backup_and_write(conf, kept)
    except Exception as exc:
        return False, f"Écriture fstab impossible : {exc}"
    if shutil.which("systemctl"):
        run_cmd(["systemctl", "daemon-reload"], timeout=12)
    return True, "Ligne mergerfs ajoutée/mise à jour dans fstab."


def validate_mergerfs_payload(conf: Dict[str, str], payload: Dict[str, Any]) -> Tuple[bool, List[str], str, str, str]:
    raw_sources = payload.get("sources") or []
    if isinstance(raw_sources, str):
        raw_sources = [raw_sources]
    sources: List[str] = []
    for item in raw_sources:
        p = os.path.normpath(str(item or "").strip())
        if not p or p in sources:
            continue
        path_ok, safe_path, path_error = safe_real_mount_path(conf, p)
        if not path_ok:
            return False, [], "", "", path_error
        if not os.path.isdir(safe_path):
            return False, [], "", "", f"Branche absente ou non dossier : {safe_path}"
        sources.append(safe_path)
    if len(sources) < 1:
        return False, [], "", "", "Ajoute au moins un disque/chemin source."

    path_ok, target, path_error = safe_real_mount_path(conf, str(payload.get("target") or ""))
    if not path_ok:
        return False, [], "", "", path_error
    if target in sources:
        return False, [], "", "", "La destination ne peut pas être aussi une source."
    options = str(payload.get("options") or mergerfs_default_options(conf)).strip()
    return True, sources, target, options, ""


def remove_mergerfs_fstab_target(conf: Dict[str, str], target: str) -> Tuple[bool, str]:
    ok, lines, error = read_fstab_lines(conf)
    if not ok:
        return False, error
    kept: List[str] = []
    removed = 0
    for line in lines:
        logical_line, _disabled = normalize_mergerfs_fstab_line(line)
        stripped = logical_line.strip()
        if stripped and not stripped.startswith("#") and is_mergerfs_fstab_line(line):
            parts = stripped.split()
            if len(parts) >= 2 and parts[1] == target:
                removed += 1
                continue
        kept.append(line)
    if not removed:
        return False, "Aucune ligne mergerfs fstab trouvée pour cette destination."
    try:
        fstab_backup_and_write(conf, kept)
    except Exception as exc:
        return False, f"Écriture fstab impossible : {exc}"
    if shutil.which("systemctl"):
        run_cmd(["systemctl", "daemon-reload"], timeout=12)
    return True, f"{removed} ligne(s) mergerfs supprimée(s) du fstab."


def set_mergerfs_fstab_disabled(conf: Dict[str, str], target: str, disabled: bool) -> Tuple[bool, str]:
    ok, lines, error = read_fstab_lines(conf)
    if not ok:
        return False, error
    changed = 0
    out_lines: List[str] = []
    for line in lines:
        logical_line, was_disabled = normalize_mergerfs_fstab_line(line)
        stripped = logical_line.strip()
        if stripped and not stripped.startswith("#") and is_mergerfs_fstab_line(line):
            parts = stripped.split()
            if len(parts) >= 2 and parts[1] == target:
                changed += 1
                clean = stripped
                if disabled:
                    out_lines.append(DISABLED_MERGERFS_PREFIX + clean)
                else:
                    out_lines.append(clean)
                continue
        out_lines.append(line)
    if not changed:
        return False, "Aucune ligne mergerfs fstab trouvée pour cette destination."
    try:
        fstab_backup_and_write(conf, out_lines)
    except Exception as exc:
        return False, f"Écriture fstab impossible : {exc}"
    if shutil.which("systemctl"):
        run_cmd(["systemctl", "daemon-reload"], timeout=12)
    return True, "Ligne MargeFS désactivée." if disabled else "Ligne MargeFS réactivée."


def mount_mergerfs_target(target: str) -> Tuple[bool, str]:
    rc, out = run_cmd(["mount", target], timeout=30)
    return rc == 0, out


def unmount_mergerfs_target(target: str) -> Tuple[bool, str]:
    rc, out = run_cmd(["umount", target], timeout=30)
    return rc == 0, out


def systemd_show(service: str) -> Dict[str, str]:
    """Lecture directe de systemd : c'est la vérité Linux/PID, pas un cache Flask."""
    data: Dict[str, str] = {}
    if not shutil.which("systemctl"):
        return data
    fields = [
        "Id",
        "Names",
        "LoadState",
        "ActiveState",
        "SubState",
        "UnitFileState",
        "MainPID",
        "ExecMainPID",
        "FragmentPath",
        "DropInPaths",
        "Result",
        "ExecStart",
    ]
    rc, out = run_cmd(["systemctl", "show", service, "--no-pager", *[f"-p{x}" for x in fields]], timeout=8)
    if rc != 0 and not out:
        return data
    for raw in (out or "").splitlines():
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def service_status_text(service: str) -> str:
    info = systemd_show(service)
    if not info:
        return "systemctl absent ou service introuvable"
    active = info.get("ActiveState") or "unknown"
    sub = info.get("SubState") or ""
    enabled = info.get("UnitFileState") or "unknown"
    pid = info.get("MainPID") or info.get("ExecMainPID") or "0"
    pid_text = f"pid {pid}" if pid and pid != "0" else "pid —"
    return f"{active}{('/' + sub) if sub else ''} / {enabled} / {pid_text}"


def sleep_service_name(conf: Dict[str, str]) -> str:
    # Service officiel runtime : hd-idle.
    return str(conf.get("DISK_SLEEP_SERVICE") or "hd-idle.service").strip()


def legacy_sleep_service_name(conf: Dict[str, str]) -> str:
    return str(conf.get("DISK_SLEEP_LEGACY_SERVICE") or "flask-disk-spindown.service").strip()


def old_sleep_service_name(conf: Dict[str, str]) -> str:
    return str(conf.get("DISK_SLEEP_OLD_SERVICE") or "hdd-veille.service").strip()


def sleep_conf_path(conf: Dict[str, str]) -> str:
    return str(conf.get("DISK_SLEEP_CONF") or "/etc/default/hd-idle").strip()


def hdparm_standby_value(minutes: int) -> int:
    # Conversion hdparm historique conservée pour compatibilité d'affichage.
    # 1..240 = multiples de 5 secondes,
    # puis 241..251 = paliers de 30 min jusqu'à 330 min.
    minutes = max(0, int(minutes))
    if minutes <= 0:
        return 0
    if minutes <= 20:
        return minutes * 12
    if minutes <= 330:
        units = (minutes + 29) // 30
        return 240 + units
    return 251


def parse_shell_defaults(path: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key:
                    continue
                data[key] = strip_quotes(value.strip())
    except Exception:
        pass
    return data


def parse_hd_idle_options(opts: str) -> Tuple[int, List[str]]:
    """Extrait les secondes/minutes et les disques depuis les options hd-idle."""
    try:
        tokens = shlex.split(str(opts or ""))
    except Exception:
        tokens = str(opts or "").split()
    devices: List[str] = []
    seconds = 0
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "-a" and i + 1 < len(tokens):
            dev = tokens[i + 1]
            if SAFE_DISK_RE.match(dev) and dev not in devices:
                devices.append(dev)
            i += 2
            continue
        if token == "-i" and i + 1 < len(tokens):
            try:
                value = int(tokens[i + 1])
                if value > 0:
                    seconds = value
            except Exception:
                pass
            i += 2
            continue
        i += 1
    minutes = int(round(seconds / 60)) if seconds > 0 else 0
    return minutes, devices


def hd_idle_runtime_info(conf: Dict[str, str]) -> Dict[str, Any]:
    service = sleep_service_name(conf)
    info = systemd_show(service)
    exec_start = info.get("ExecStart") or ""
    minutes, devices = parse_hd_idle_options(exec_start)

    # Complète avec /etc/default/hd-idle si ExecStart ne contient pas les options.
    raw_conf = parse_shell_defaults(sleep_conf_path(conf))
    opts = raw_conf.get("HD_IDLE_OPTS") or raw_conf.get("HD_IDLE_OPTIONS") or raw_conf.get("OPTIONS") or ""
    conf_minutes, conf_devices = parse_hd_idle_options(opts)
    if not minutes and conf_minutes:
        minutes = conf_minutes
    if not devices and conf_devices:
        devices = conf_devices

    pid = info.get("MainPID") or info.get("ExecMainPID") or "0"
    return {
        "service": service,
        "systemd": info,
        "pid": pid if pid and pid != "0" else "",
        "exec_start": exec_start,
        "options": opts,
        "minutes": minutes or 30,
        "devices": devices,
        "config_raw": raw_conf,
        "config_file": sleep_conf_path(conf),
    }


def read_sleep_config(conf: Dict[str, str]) -> Dict[str, Any]:
    # Source de vérité runtime : systemd + hd-idle.service.
    # L'UI lit le vrai PID Linux et la vraie configuration /etc/default/hd-idle.
    runtime = hd_idle_runtime_info(conf)

    # Compatibilité : si l'ancien /etc/default/hdd-veille existe encore,
    # on le lit seulement en fallback d'affichage, pas comme vérité runtime.
    old_defaults = parse_shell_defaults("/etc/default/hdd-veille")
    old_minutes = 0
    old_value = 0
    try:
        old_minutes = int(old_defaults.get("HDD_STANDBY_MINUTES") or 0)
    except Exception:
        old_minutes = 0
    try:
        old_value = int(old_defaults.get("HDD_HDPARM_S") or 0)
    except Exception:
        old_value = 0

    devices = runtime.get("devices") or [str(d.get("device") or "") for d in available_sleep_disks(conf) if d.get("device")]
    minutes = int(runtime.get("minutes") or old_minutes or 30)
    return {
        "backend": "hd-idle",
        "global": True,
        "devices": devices,
        "minutes": minutes,
        "seconds": minutes * 60,
        "hdparm_value": old_value or hdparm_standby_value(minutes),
        "pid": runtime.get("pid") or "",
        "exec_start": runtime.get("exec_start") or "",
        "options": runtime.get("options") or "",
        "systemd": runtime.get("systemd") or {},
        "updated_at": "",
        "defaults_file": runtime.get("config_file") or sleep_conf_path(conf),
        "old_defaults_file": "/etc/default/hdd-veille" if old_defaults else "",
    }


def disable_legacy_sleep_service(conf: Dict[str, str]) -> str:
    """Nettoie les anciens services pour éviter deux démons de veille concurrents."""
    if not shutil.which("systemctl"):
        return ""
    current = sleep_service_name(conf)
    candidates = [
        legacy_sleep_service_name(conf),
        old_sleep_service_name(conf),
    ]
    outputs: List[str] = []
    seen: set[str] = set()
    for service in candidates:
        service = str(service or "").strip()
        if not service or service == current or service in seen:
            continue
        seen.add(service)
        exists_rc, _exists_out = run_cmd(["systemctl", "list-unit-files", service, "--no-legend"], timeout=8)
        state_rc, _state_out = run_cmd(["systemctl", "status", service, "--no-pager"], timeout=8)
        if exists_rc != 0 and state_rc != 0:
            continue
        rc, out = run_cmd(["systemctl", "disable", "--now", service], timeout=20)
        if rc == 0:
            outputs.append(f"Ancien service désactivé : {service}\n{out or ''}".strip())
        else:
            outputs.append(f"Ancien service non désactivé : {service}\n{out or ''}".strip())
    return "\n\n".join(x for x in outputs if x)

def available_sleep_disks(conf: Dict[str, str]) -> List[Dict[str, Any]]:
    data = collect_disks()
    rows: List[Dict[str, Any]] = []
    for disk in data.get("disks", []) or []:
        if not disk.get("is_hdd"):
            continue
        rows.append({
            "device": disk.get("device"),
            "name": disk.get("display_device") or disk.get("device"),
            "model": disk.get("model") or "",
            "size": disk.get("size") or "",
            "mountpoint": disk.get("mountpoint") or "",
            "protected": bool(disk.get("is_system_disk")),
            "power_state": disk.get("power_state") or "",
        })
    return rows


def build_hd_idle_options(devices: List[str], minutes: int) -> str:
    """Construit exactement la logique hdd.sh : -i 0 puis -a /dev/sdX -i secondes.

    minutes=10 => 600 secondes.
    Aucun chemin inventé, aucune valeur 30 min forcée côté UI.
    """
    seconds = max(0, min(330, int(minutes))) * 60
    parts = ["-i", "0"]
    for dev in devices:
        dev = str(dev or "").strip()
        if SAFE_DISK_RE.match(dev):
            parts.extend(["-a", dev, "-i", str(seconds)])
    return " ".join(shlex.quote(part) for part in parts)


def sleep_target_devices(conf: Dict[str, str]) -> List[str]:
    """Liste les HDD rotatifs ciblés par la veille, comme hdd.sh.

    On ne dépend pas de lsblk ici : collect_disks() utilise déjà sysfs/mountinfo
    avec la logique anti-réveil du module Disk.
    """
    devices: List[str] = []
    for row in available_sleep_disks(conf):
        dev = str(row.get("device") or "").strip()
        if SAFE_DISK_RE.match(dev) and dev not in devices:
            devices.append(dev)
    return devices


def ensure_hd_idle_installed() -> Tuple[bool, str]:
    """Vérifie hd-idle, avec installation apt si possible comme hdd.sh."""
    if shutil.which("hd-idle") or os.path.exists("/usr/sbin/hd-idle"):
        return True, "hd-idle déjà présent."
    if not shutil.which("apt-get"):
        return False, "hd-idle introuvable et apt-get absent. Installe le paquet hd-idle."
    outputs: List[str] = ["hd-idle introuvable : installation du paquet hd-idle..."]
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    for cmd in (["apt-get", "update"], ["apt-get", "install", "-y", "hd-idle"]):
        try:
            completed = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                env=env,
                check=False,
            )
            outputs.append(f"$ {' '.join(cmd)}\n{completed.stdout or ''}".strip())
            if completed.returncode != 0:
                return False, "\n\n".join(outputs)
        except Exception as exc:
            outputs.append(f"$ {' '.join(cmd)}\n{exc}")
            return False, "\n\n".join(outputs)
    return bool(shutil.which("hd-idle") or os.path.exists("/usr/sbin/hd-idle")), "\n\n".join(outputs)


def write_hd_idle_defaults(conf: Dict[str, str], devices: List[str], minutes: int) -> str:
    """Écrit /etc/default/hd-idle + compat /etc/default/hdd-veille.

    C'est la partie qui était cassée : la fonction ne générait plus le fichier,
    donc l'UI retombait sur 30 min à la relecture.
    """
    minutes = max(0, min(330, int(minutes)))
    seconds = minutes * 60
    devices = devices or sleep_target_devices(conf)
    opts = build_hd_idle_options(devices, minutes)

    hd_idle_path = sleep_conf_path(conf)
    os.makedirs(os.path.dirname(hd_idle_path) or "/", exist_ok=True)

    if minutes <= 0:
        hd_idle_text = """# généré par Flask System / disk.py
# hd-idle désactivé par l'onglet Veille.
START_HD_IDLE=false
HD_IDLE_OPTS="-i 0"
"""
    else:
        hd_idle_text = f"""# généré par Flask System / disk.py
# logique identique à hdd.sh : hd-idle surveille l'inactivité réelle des HDD.
START_HD_IDLE=true
HD_IDLE_OPTS="{opts}"
"""

    with open(hd_idle_path, "w", encoding="utf-8") as handle:
        handle.write(hd_idle_text)
    try:
        os.chmod(hd_idle_path, 0o644)
    except OSError:
        pass

    # Compat de lecture/debug : hdd.sh écrivait aussi ce fichier.
    legacy_defaults = "/etc/default/hdd-veille"
    try:
        if minutes <= 0:
            legacy_text = """# généré par Flask System / disk.py
HDD_STANDBY_MODE=off
HDD_STANDBY_MINUTES=0
SCRIPT_PATH=disk.py
"""
        else:
            legacy_text = f"""# généré par Flask System / disk.py
HDD_STANDBY_MODE=hd-idle
HDD_STANDBY_MINUTES={minutes}
HDD_STANDBY_SECONDS={seconds}
HD_IDLE_OPTS="{opts}"
SCRIPT_PATH=disk.py
"""
        with open(legacy_defaults, "w", encoding="utf-8") as handle:
            handle.write(legacy_text)
        os.chmod(legacy_defaults, 0o644)
    except Exception:
        pass

    return f"{hd_idle_path} écrit.\nOptions hd-idle : {opts}\nHDD ciblés : {', '.join(devices) or 'aucun'}"


def apply_hdparm_timer(conf: Dict[str, str], minutes: int, devices: Optional[List[str]] = None) -> str:
    """Applique aussi hdparm -S comme garde-fou immédiat.

    hd-idle reste le runtime officiel, mais appliquer hdparm -S évite que l'état
    courant du disque reste incohérent juste après validation.
    """
    devices = devices or sleep_target_devices(conf)
    hdparm = which_or_config(conf, "HDPARM_BIN", "hdparm")
    value = hdparm_standby_value(minutes)
    outputs: List[str] = [f"Application hdparm -S {value} sur les HDD..."]
    for dev in devices:
        if not SAFE_DISK_RE.match(dev):
            continue
        rc, out = run_cmd([hdparm, "-S", str(value), dev], timeout=10)
        outputs.append(f"$ {hdparm} -S {value} {dev}\n{out or ''}".strip())
        if rc == 127:
            break
    return "\n\n".join(outputs)


def write_sleep_service(conf: Dict[str, str], devices: List[str], minutes: int) -> Tuple[bool, str]:
    """Applique la veille via hd-idle, sans retomber sur 30 min.

    C'est la logique Python équivalente à :
      bash hdd.sh -10 / -20 / -30 / -60
    """
    minutes = max(0, min(330, int(minutes)))
    service = sleep_service_name(conf)
    devices = devices or sleep_target_devices(conf)
    outputs: List[str] = []

    if minutes <= 0:
        try:
            outputs.append(write_hd_idle_defaults(conf, devices, 0))
        except Exception as exc:
            return False, f"Écriture config hd-idle impossible : {exc}"
        if shutil.which("systemctl"):
            for cmd in (["systemctl", "disable", "--now", service], ["systemctl", "reset-failed", service]):
                rc, out = run_cmd(list(cmd), timeout=25)
                outputs.append(f"$ {' '.join(cmd)}\n{out or ''}".strip())
                if rc != 0 and cmd[1] == "disable":
                    return False, "\n\n".join(outputs)
        outputs.append(apply_hdparm_timer(conf, 0, devices))
        clear_disk_runtime_caches()
        return True, "\n\n".join(outputs)

    ok_hd_idle, install_msg = ensure_hd_idle_installed()
    outputs.append(install_msg)
    if not ok_hd_idle:
        return False, "\n\n".join(outputs)

    if not devices:
        return False, "Aucun HDD rotatif détecté : hd-idle non configuré."

    cleanup = disable_legacy_sleep_service(conf)
    if cleanup:
        outputs.append(cleanup)

    try:
        outputs.append(write_hd_idle_defaults(conf, devices, minutes))
    except Exception as exc:
        return False, "\n\n".join(outputs + [f"Écriture config hd-idle impossible : {exc}"])

    if not shutil.which("systemctl"):
        outputs.append("systemctl introuvable : fichier écrit, mais service non redémarré.")
        clear_disk_runtime_caches()
        return False, "\n\n".join(outputs)

    for cmd in (
        ["systemctl", "daemon-reload"],
        ["systemctl", "reset-failed", service],
        ["systemctl", "enable", service],
        ["systemctl", "restart", service],
        ["systemctl", "reset-failed", service],
    ):
        rc, out = run_cmd(list(cmd), timeout=35)
        outputs.append(f"$ {' '.join(cmd)}\n{out or ''}".strip())
        if rc != 0 and cmd[1] in {"enable", "restart"}:
            return False, "\n\n".join(outputs)

    outputs.append(apply_hdparm_timer(conf, minutes, devices))
    clear_disk_runtime_caches()
    return True, "\n\n".join(outputs)


@disk_bp.route("/disk/api/mergerfs")
def disk_mergerfs_api():
    conf = get_config()
    fstab_rows = parse_mergerfs_fstab(conf)
    live_rows = current_mergerfs_live(conf)
    live_targets = {str(item.get("target") or "") for item in live_rows}
    state = read_mergerfs_state(conf)
    profiles = state.get("profiles") if isinstance(state, dict) else {}
    if not isinstance(profiles, dict):
        profiles = {}

    def decorate(row: Dict[str, Any]) -> Dict[str, Any]:
        target = str(row.get("target") or row.get("mount") or "")
        profile = profiles.get(target) if isinstance(profiles.get(target), dict) else {}
        row["profile"] = profile
        row["mounted"] = target in live_targets
        row["disabled"] = bool(row.get("disabled") or profile.get("disabled"))
        if row["disabled"]:
            row["status"] = "disabled"
            row["status_label"] = "désactivé"
        elif row["mounted"]:
            row["status"] = "mounted"
            row["status_label"] = "démarré"
        else:
            row["status"] = "stopped"
            row["status_label"] = "arrêté"
        return row

    for row in fstab_rows:
        decorate(row)
    # Ajoute les montages live non déclarés dans fstab pour que tu voies tout.
    declared = {str(item.get("target") or "") for item in fstab_rows}
    rows = list(fstab_rows)
    for item in live_rows:
        if str(item.get("target") or "") not in declared:
            item["from_fstab"] = False
            item["disabled"] = False
            rows.append(decorate(item))
    return jsonify({
        "ok": True,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "entries": rows,
        "live": live_rows,
        "sources": selectable_mergerfs_sources(conf),
        "default_options": mergerfs_default_options(conf),
        "fstab_file": fstab_path(conf),
        "state_file": mergerfs_state_file(conf),
    })


@disk_bp.route("/disk/api/action/mergerfs/add", methods=["POST"])
def disk_mergerfs_add_api():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    ok, sources, target, options, error = validate_mergerfs_payload(conf, payload)
    if not ok:
        return json_response(False, error)
    create_target = bool_from_payload(payload.get("create_target"))
    mount_now = bool_from_payload(payload.get("mount_now", True))
    if not os.path.exists(target):
        if not create_target:
            return json_response(False, "La destination n'existe pas. Coche 'Créer le dossier'.")
        try:
            os.makedirs(target, exist_ok=True)
        except Exception as exc:
            return json_response(False, f"Création destination impossible : {exc}")
    if not os.path.isdir(target):
        return json_response(False, "La destination existe mais n'est pas un dossier.")
    ok_write, msg = write_mergerfs_fstab_entry(conf, sources, target, options)
    if not ok_write:
        return json_response(False, msg)
    output = msg
    mounted_now = False
    if mount_now:
        ok_mount, mount_out = mount_mergerfs_target(target)
        output += "\n" + (mount_out or "")
        mounted_now = bool(ok_mount or "already mounted" in (mount_out or "").lower())
        if not mounted_now:
            return json_response(False, "Ligne fstab écrite, mais montage immédiat impossible.", output=output[-2500:])
    update_mergerfs_profile(conf, target, sources=sources, options=options, disabled=False, mounted=mounted_now, status="mounted" if mounted_now else "stopped")
    return json_response(True, "MargeFS ajouté et appliqué.", output=output[-2500:])


@disk_bp.route("/disk/api/action/mergerfs/edit", methods=["POST"])
def disk_mergerfs_edit_api():
    conf = get_config()
    payload = request.get_json(silent=True) or {}

    old_target_raw = str(payload.get("old_target") or payload.get("previous_target") or "").strip()
    path_ok, old_target, path_error = safe_real_mount_path(conf, old_target_raw)
    if not path_ok:
        return json_response(False, path_error)

    ok, sources, new_target, options, error = validate_mergerfs_payload(conf, payload)
    if not ok:
        return json_response(False, error)

    create_target = bool_from_payload(payload.get("create_target"))
    mount_now = bool_from_payload(payload.get("mount_now", True))

    existing_rows = parse_mergerfs_fstab(conf)
    old_rows = [r for r in existing_rows if str(r.get("target") or "") == old_target]
    if not old_rows:
        return json_response(False, "Aucune ligne MargeFS existante trouvée pour cette destination.")

    output_parts: List[str] = []
    was_disabled = any(bool(r.get("disabled")) for r in old_rows)
    was_mounted = old_target in mergerfs_targets_live(conf)

    if was_mounted:
        ok_umount, out = unmount_mergerfs_target(old_target)
        output_parts.append(f"$ umount {old_target}\n{out}".strip())
        if not ok_umount:
            return json_response(False, "Modification impossible : le montage actuel refuse de se démonter.", output="\n\n".join(output_parts)[-3000:])

    ok_rm, rm_msg = remove_mergerfs_fstab_target(conf, old_target)
    output_parts.append(rm_msg)
    if not ok_rm:
        return json_response(False, rm_msg, output="\n\n".join(output_parts)[-3000:])

    def rollback_old_line() -> None:
        old = old_rows[0]
        old_sources = old.get("sources") or []
        old_options = old.get("options") or mergerfs_default_options(conf)
        write_mergerfs_fstab_entry(conf, old_sources, old_target, old_options)
        if was_disabled:
            set_mergerfs_fstab_disabled(conf, old_target, True)

    if not os.path.exists(new_target):
        if not create_target:
            rollback_old_line()
            return json_response(False, "La nouvelle destination n'existe pas. Coche 'Créer le dossier'.", output="\n\n".join(output_parts)[-3000:])
        try:
            os.makedirs(new_target, exist_ok=True)
        except Exception as exc:
            rollback_old_line()
            return json_response(False, f"Création nouvelle destination impossible : {exc}", output="\n\n".join(output_parts)[-3000:])

    if not os.path.isdir(new_target):
        rollback_old_line()
        return json_response(False, "La nouvelle destination existe mais n'est pas un dossier.", output="\n\n".join(output_parts)[-3000:])

    ok_write, write_msg = write_mergerfs_fstab_entry(conf, sources, new_target, options)
    output_parts.append(write_msg)
    if not ok_write:
        rollback_old_line()
        return json_response(False, write_msg, output="\n\n".join(output_parts)[-3000:])

    mounted_now = False
    if mount_now:
        ok_mount, mount_out = mount_mergerfs_target(new_target)
        output_parts.append(f"$ mount {new_target}\n{mount_out}".strip())
        mounted_now = bool(ok_mount or "already mounted" in (mount_out or "").lower())
        if not mounted_now:
            update_mergerfs_profile(conf, new_target, sources=sources, options=options, disabled=False, mounted=False, status="arrêté")
            if old_target != new_target:
                remove_mergerfs_profile(conf, old_target)
            return json_response(False, "Ligne modifiée dans fstab, mais montage immédiat impossible.", output="\n\n".join(output_parts)[-3000:])
    elif was_disabled:
        # Si la ligne était désactivée et qu'on ne demande pas le montage immédiat,
        # on conserve l'intention de désactivation.
        ok_disable, disable_msg = set_mergerfs_fstab_disabled(conf, new_target, True)
        output_parts.append(disable_msg)
        if not ok_disable:
            return json_response(False, disable_msg, output="\n\n".join(output_parts)[-3000:])

    if old_target != new_target:
        remove_mergerfs_profile(conf, old_target)
    update_mergerfs_profile(
        conf,
        new_target,
        sources=sources,
        options=options,
        disabled=bool(was_disabled and not mount_now),
        mounted=mounted_now,
        status="démarré" if mounted_now else ("désactivé" if was_disabled and not mount_now else "arrêté"),
        previous_target=old_target if old_target != new_target else "",
    )

    return json_response(True, "MargeFS modifié et appliqué.", output="\n\n".join(output_parts)[-3000:])


@disk_bp.route("/disk/api/action/mergerfs/delete", methods=["POST"])
def disk_mergerfs_delete_api():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    target = str(payload.get("target") or "").strip()
    path_ok, target, path_error = safe_real_mount_path(conf, target)
    if not path_ok:
        return json_response(False, path_error)
    output = ""
    if target in mergerfs_targets_live(conf):
        ok_umount, out = unmount_mergerfs_target(target)
        output += out or ""
        if not ok_umount:
            return json_response(False, "Impossible de démonter cette destination mergerfs.", output=output[-2500:])
    ok_rm, msg = remove_mergerfs_fstab_target(conf, target)
    if not ok_rm:
        return json_response(False, msg, output=output[-2500:])
    remove_mergerfs_profile(conf, target)
    return json_response(True, "MargeFS supprimé du fstab." , output=(output + "\n" + msg)[-2500:])


@disk_bp.route("/disk/api/action/mergerfs/service", methods=["POST"])
def disk_mergerfs_service_api():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action") or "status").strip().lower()
    rows = parse_mergerfs_fstab(conf)

    requested_raw = payload.get("targets") if "targets" in payload else payload.get("target")
    requested_targets: List[str] = []
    if isinstance(requested_raw, list):
        raw_items = requested_raw
    elif requested_raw:
        raw_items = [requested_raw]
    else:
        raw_items = []
    for item in raw_items:
        path_ok, safe_target, path_error = safe_real_mount_path(conf, str(item or ""))
        if not path_ok:
            return json_response(False, path_error)
        if safe_target and safe_target not in requested_targets:
            requested_targets.append(safe_target)

    if requested_targets:
        rows = [r for r in rows if str(r.get("target") or "") in requested_targets]
        if not rows:
            return json_response(False, "Aucune ligne MargeFS déclarée pour cette sélection.")

    targets = [str(r.get("target") or "") for r in rows if r.get("target")]
    outputs: List[str] = []
    if shutil.which("systemctl"):
        run_cmd(["systemctl", "daemon-reload"], timeout=12)

    live = mergerfs_targets_live(conf)

    if action == "status":
        text_rows = []
        for row in rows:
            target = str(row.get("target") or "")
            if not target:
                continue
            if row.get("disabled"):
                label = "désactivé"
            elif target in live:
                label = "démarré"
            else:
                label = "arrêté"
            text_rows.append(f"{target}: {label}")
            update_mergerfs_profile(conf, target, sources=row.get("sources") or [], options=row.get("options") or "", disabled=bool(row.get("disabled")), mounted=target in live, status=label)
        text = "\n".join(text_rows) or "Aucune ligne mergerfs dans fstab."
        return json_response(True, "État MargeFS lu.", output=text)

    if action == "disable":
        for row in sorted(rows, key=lambda r: len(str(r.get("target") or "")), reverse=True):
            target = str(row.get("target") or "")
            if not target:
                continue
            if target in mergerfs_targets_live(conf):
                ok_umount, out = unmount_mergerfs_target(target)
                outputs.append(f"$ umount {target}\n{out}".strip())
                if not ok_umount:
                    return json_response(False, f"Désactivation impossible, démontage refusé : {target}", output="\n\n".join(outputs)[-3000:])
            ok_disable, msg = set_mergerfs_fstab_disabled(conf, target, True)
            outputs.append(msg)
            if not ok_disable:
                return json_response(False, msg, output="\n\n".join(outputs)[-3000:])
            update_mergerfs_profile(conf, target, sources=row.get("sources") or [], options=row.get("options") or "", disabled=True, mounted=False, status="désactivé")
        return json_response(True, "MargeFS désactivé.", output="\n\n".join(outputs)[-3000:])

    if action in {"stop", "restart"}:
        for row in sorted(rows, key=lambda r: len(str(r.get("target") or "")), reverse=True):
            target = str(row.get("target") or "")
            if not target:
                continue
            if target in mergerfs_targets_live(conf):
                ok_umount, out = unmount_mergerfs_target(target)
                outputs.append(f"$ umount {target}\n{out}".strip())
                if not ok_umount:
                    return json_response(False, f"Arrêt impossible : {target}", output="\n\n".join(outputs)[-3000:])
            update_mergerfs_profile(conf, target, sources=row.get("sources") or [], options=row.get("options") or "", disabled=bool(row.get("disabled")), mounted=False, status="arrêté")
        if action == "stop":
            return json_response(True, "MargeFS arrêté.", output="\n\n".join(outputs)[-3000:])

    if action in {"start", "restart"}:
        # Relit après l'arrêt éventuel, pour récupérer les lignes encore désactivées.
        rows_by_target = {str(r.get("target") or ""): r for r in parse_mergerfs_fstab(conf)}
        start_rows = [rows_by_target.get(t) for t in targets]
        start_rows = [r for r in start_rows if r]
        for row in start_rows:
            target = str(row.get("target") or "")
            if not target:
                continue
            if row.get("disabled"):
                ok_enable, msg = set_mergerfs_fstab_disabled(conf, target, False)
                outputs.append(msg)
                if not ok_enable:
                    return json_response(False, msg, output="\n\n".join(outputs)[-3000:])
            try:
                os.makedirs(target, exist_ok=True)
            except Exception as exc:
                return json_response(False, f"Création destination impossible {target}: {exc}")
            ok_mount, out = mount_mergerfs_target(target)
            outputs.append(f"$ mount {target}\n{out}".strip())
            mounted_ok = bool(ok_mount or "already mounted" in (out or "").lower())
            if not mounted_ok:
                return json_response(False, f"Démarrage impossible : {target}", output="\n\n".join(outputs)[-3000:])
            update_mergerfs_profile(conf, target, sources=row.get("sources") or [], options=row.get("options") or "", disabled=False, mounted=True, status="démarré")
        return json_response(True, "MargeFS démarré." if action == "start" else "MargeFS redémarré.", output="\n\n".join(outputs)[-3000:])

    return json_response(False, "Action MargeFS inconnue.")


# ---------------------------------------------------------------------------
# RAID / ZFS / BTRFS
# ---------------------------------------------------------------------------

