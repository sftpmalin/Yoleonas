SAFE_RAID_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{1,31}$")


def raid_job_dir(conf: Dict[str, str]) -> str:
    return disk_conf_resolve_path(conf.get("RAID_JOB_DIR", "/var/lib/yoleo/disk/raid_jobs"))


def raid_log_dir(conf: Dict[str, str]) -> str:
    return disk_conf_resolve_path(conf.get("RAID_LOG_DIR", "/var/log/yoleo/disk/raid"))


def raid_status_path(conf: Dict[str, str], job_id: str) -> str:
    return os.path.join(raid_job_dir(conf), f"{job_id}.json")


def raid_log_path(conf: Dict[str, str], job_id: str) -> str:
    return os.path.join(raid_log_dir(conf), f"{job_id}.log")


def raid_session_name(job_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(job_id or "raid")).strip("-") or "raid"
    return f"disk-{safe}"[:80]


def raid_mode_options(engine: str, count: int) -> List[Dict[str, str]]:
    engine = str(engine or "").strip().lower()
    n = max(0, int(count or 0))
    out: List[Dict[str, str]] = []
    if engine == "mdadm":
        if n >= 2:
            out.extend([
                {"value": "raid1", "label": "RAID 1 / miroir", "hint": "Copie les données sur les disques sélectionnés."},
                {"value": "raid0", "label": "RAID 0 / stripe", "hint": "Additionne les performances/capacités, sans redondance."},
                {"value": "linear", "label": "Linear / concaténation", "hint": "Remplit les disques les uns après les autres, sans redondance."},
            ])
        if n >= 3:
            out.append({"value": "raid5", "label": "RAID 5", "hint": "Parité simple, minimum 3 disques."})
        if n >= 4:
            out.extend([
                {"value": "raid6", "label": "RAID 6", "hint": "Double parité, minimum 4 disques."},
                {"value": "raid10", "label": "RAID 10", "hint": "Miroirs + stripe, minimum 4 disques."},
            ])
    # ZFS/BTRFS sont volontairement désactivés dans cette première version stable.
    # Leur détection et leur cycle de vie sont différents de mdadm et doivent être
    # repris dans une passe dédiée pour ne pas casser la page RAID fonctionnelle.
    return out


def raid_allowed_modes(engine: str, count: int) -> set[str]:
    return {item["value"] for item in raid_mode_options(engine, count)}


def raid_clean_name(raw: str) -> Tuple[bool, str, str]:
    name = str(raw or "").strip()
    if not name:
        return False, name, "Nom du volume obligatoire."
    if not SAFE_RAID_NAME_RE.match(name):
        return False, name, "Nom refusé : commence par une lettre, puis lettres/chiffres/point/tiret/underscore."
    forbidden = {"mirror", "raidz", "raidz1", "raidz2", "raidz3", "spare", "log", "cache", "none"}
    if name.lower() in forbidden:
        return False, name, "Nom réservé refusé."
    return True, name, ""


def disk_has_any_signature(conf: Dict[str, str], device: str) -> bool:
    # Important : cette vérification doit être VIVANTE pour la page RAID.
    # collect_disks() utilise volontairement des caches froids pour ne pas réveiller
    # les disques dans l'onglet général ; après un wipe terminal, ces caches peuvent
    # encore indiquer btrfs/linux_raid_member alors que le disque est réellement vide.
    meta = parse_blkid_export(device)
    if meta:
        keys = {k for k, v in meta.items() if str(v or "").strip()}
        if keys & {"type", "uuid", "label", "pttype", "ptuuid", "partuuid"}:
            return True

    # wipefs -n voit parfois des signatures que blkid ne remonte pas encore.
    # La page RAID est une page d'action destructive/maintenance, donc ici on
    # accepte une lecture explicite du périphérique au lieu de se fier au cache.
    wipefs_bin = shutil.which("wipefs")
    if wipefs_bin:
        rc, out = run_cmd([wipefs_bin, "-n", device], timeout=6)
        if rc == 0:
            lines = []
            for line in (out or "").splitlines():
                clean = line.strip()
                if not clean:
                    continue
                low = clean.lower()
                if low.startswith("device") or low.startswith("offset"):
                    continue
                lines.append(clean)
            if lines:
                return True
    return False


def raid_empty_disk_candidates() -> List[Dict[str, Any]]:
    conf = get_config()
    data = augment_maintenance_data(collect_disks())
    out: List[Dict[str, Any]] = []
    sys_block_path = str(conf.get("SYS_BLOCK_PATH") or "/sys/block")

    for disk in data.get("disks", []) or []:
        device = str(disk.get("device") or "")
        if not device or not SAFE_DISK_RE.match(device):
            continue
        if disk.get("maintenance_protected") or disk.get("is_system_disk"):
            continue
        if disk.get("mountpoint") or disk.get("mounts"):
            continue

        disk_name = block_name_from_path(device)
        if not disk_name:
            continue

        # Ne pas se fier aux champs fstype/blkid_fstype de collect_disks() ici :
        # ils peuvent venir du cache JSON/udev et rester faux juste après un wipe.
        # Pour RAID on veut uniquement la vérité actuelle : partitions sysfs +
        # signatures vivantes blkid/wipefs.
        live_parts = list_partitions(sys_block_path, disk_name)
        if live_parts:
            continue
        if disk_has_any_signature(conf, device):
            continue
        if disk_maintenance_sessions_for_target(device):
            continue

        out.append({
            "device": device,
            "name": disk.get("name") or device,
            "display_device": disk.get("display_device") or device,
            "model": disk.get("model") or "—",
            "serial": disk.get("serial") or "",
            "size": disk.get("size") or "—",
            "transport": disk.get("transport") or "",
            "power_state": disk.get("power_state") or "",
            "health_label": disk.get("health_label") or "—",
        })
    return out


def fs_value_python(item: Dict[str, Any]) -> str:
    return str(item.get("blkid_fstype") or item.get("fstype") or item.get("fs_type") or item.get("filesystem") or "").strip()


def raid_running_devices(conf: Dict[str, str]) -> set[str]:
    devices: set[str] = set()
    for job in raid_jobs(conf):
        if job.get("status") == "running" and tmux_session_exists(str(job.get("session") or "")):
            for dev in job.get("devices") or []:
                devices.add(str(dev))
    return devices


def validate_raid_devices(devices: List[str]) -> Tuple[bool, List[str], str]:
    conf = get_config()
    cleaned: List[str] = []
    for dev in devices:
        dev = str(dev or "").strip()
        if dev and dev not in cleaned:
            cleaned.append(dev)
    if len(cleaned) < 2:
        return False, cleaned, "Sélectionne au moins deux disques. Pour un seul disque, utilise Maintenance."
    candidates = {row["device"] for row in raid_empty_disk_candidates()}
    busy = raid_running_devices(conf)
    for dev in cleaned:
        ok, target, error = require_safe_target(conf, dev, allow_disk=True, allow_part=False)
        if not ok:
            return False, cleaned, error
        if target not in candidates:
            return False, cleaned, f"{target} refusé : disque non vide, monté, partitionné, protégé ou occupé."
        if target in busy:
            return False, cleaned, f"{target} déjà utilisé par une création RAID en cours."
    return True, cleaned, ""


def preferred_md_path(name: str) -> str:
    target = f"/dev/{name}"
    md_dir = "/dev/md"
    try:
        if os.path.isdir(md_dir):
            real_target = os.path.realpath(target)
            for entry in sorted(os.listdir(md_dir)):
                path = os.path.join(md_dir, entry)
                if os.path.realpath(path) == real_target and entry not in {name}:
                    return path
    except Exception:
        pass
    return target


def list_raid_volumes() -> List[Dict[str, Any]]:
    conf = get_config()
    volumes: List[Dict[str, Any]] = []
    try:
        names = sorted(n for n in os.listdir("/sys/class/block") if re.fullmatch(r"md\d+", n))
    except Exception:
        names = []
    for name in names:
        # On garde toujours le périphérique canonique /dev/mdX pour les actions.
        # Les alias créés par mdadm dans /dev/md/ peuvent contenir le hostname
        # et des caractères comme ':' (ex: /dev/md/debian-nas:yoleo), ce qui est
        # correct côté Linux mais volontairement refusé par require_safe_target().
        # L'alias reste affichable, mais il ne doit pas être envoyé aux actions
        # destructives comme la suppression.
        device = f"/dev/{name}"
        alias_device = preferred_md_path(name)
        if alias_device == device or not os.path.exists(alias_device):
            alias_device = ""
        meta = parse_blkid_export(device)
        mounts = mount_paths_for_target(conf, device)
        volumes.append({
            "engine": "mdadm",
            "name": name,
            "device": device,
            "alias_device": alias_device,
            "size": human_bytes(sysfs_block_size_bytes(name)),
            "fstype": meta.get("type", ""),
            "label": meta.get("label", ""),
            "uuid": meta.get("uuid", ""),
            "mountpoint": mounts[0] if mounts else "",
            "mounts": mounts,
        })
    if shutil.which("zpool"):
        rc, out = run_cmd(["zpool", "list", "-H", "-o", "name,size,health"], timeout=8)
        if rc == 0:
            for line in (out or "").splitlines():
                parts = line.split("\t")
                if len(parts) >= 3:
                    volumes.append({
                        "engine": "zfs",
                        "name": parts[0],
                        "device": "zpool:" + parts[0],
                        "size": parts[1],
                        "fstype": "zfs",
                        "label": parts[2],
                        "uuid": "",
                        "mountpoint": "",
                        "mounts": [],
                    })
    return volumes


def raid_jobs(conf: Dict[str, str]) -> List[Dict[str, Any]]:
    base = raid_job_dir(conf)
    jobs: List[Dict[str, Any]] = []
    try:
        files = sorted([os.path.join(base, f) for f in os.listdir(base) if f.endswith(".json")], key=os.path.getmtime, reverse=True)
    except Exception:
        files = []
    for path in files[:20]:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                job = json.load(handle)
            if not isinstance(job, dict):
                continue
        except Exception:
            continue
        session = str(job.get("session") or "")
        running = bool(session and tmux_session_exists(session))
        if job.get("status") == "running" and not running:
            # Le script écrit normalement success/error. Si le navigateur tombe pile entre
            # la fin de tmux et l'écriture du statut, on garde un état prudent.
            job["status"] = "finished"
            job["message"] = job.get("message") or "Création terminée. Vérifie le log si besoin."
        job["active"] = running
        jobs.append(job)
    return jobs


def shell_join(cmd: List[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in cmd)


def make_raid_command(engine: str, mode: str, name: str, devices: List[str], mountpoint: str) -> Tuple[bool, str, str]:
    engine = str(engine or "").strip().lower()
    mode = str(mode or "").strip().lower()
    if engine == "mdadm":
        binary = shutil.which("mdadm") or "mdadm"
        level_map = {"linear": "linear", "raid0": "0", "raid1": "1", "raid5": "5", "raid6": "6", "raid10": "10"}
        level = level_map.get(mode)
        if not level:
            return False, "", "Mode mdadm refusé."
        md_path = f"/dev/md/{name}"
        cmd = [binary, "--create", md_path, "--run", "--force", "--metadata=1.2", f"--name={name}", f"--level={level}", f"--raid-devices={len(devices)}", *devices]
        # mdadm crée un bloc logique mais ne le formate pas. Si les disques ont
        # déjà servi, une ancienne signature XFS/ext4 peut réapparaître sur le
        # nouveau /dev/mdX. On efface seulement ces signatures filesystem sur
        # le volume logique fraîchement créé.
        command = " && ".join([
            "mkdir -p /dev/md",
            shell_join(cmd),
            "(command -v udevadm >/dev/null 2>&1 && udevadm settle || true)",
            "(command -v wipefs >/dev/null 2>&1 && wipefs -a " + shlex.quote(md_path) + " || true)",
        ])
        return True, command, f"Création mdadm {mode} : {md_path}"

    if engine == "zfs":
        if not shutil.which("zpool"):
            return False, "", "zpool introuvable. Installe ZFS avant de créer un pool ZFS."
        ok, mountpoint, error = safe_real_mount_path(get_config(), mountpoint or f"/mnt/{name}")
        if not ok:
            return False, "", error
        vdev: List[str] = []
        if mode != "stripe":
            vdev.append(mode)
        cmd = ["zpool", "create", "-f", "-m", mountpoint, name, *vdev, *devices]
        # ZFS a besoin du module kernel chargé. On crée aussi le point de montage
        # avant la commande pour éviter les échecs bêtes côté chemin, même si zpool
        # sait généralement gérer le montage lui-même.
        command = " && ".join([
            "mkdir -p " + shlex.quote(mountpoint),
            "(command -v modprobe >/dev/null 2>&1 && modprobe zfs || true)",
            shell_join(cmd),
        ])
        return True, command, f"Création zpool {mode} : {name}"

    if engine == "btrfs":
        binary = shutil.which("mkfs.btrfs") or "mkfs.btrfs"
        ok, mountpoint, error = safe_real_mount_path(get_config(), mountpoint or f"/mnt/{name}")
        if not ok:
            return False, "", error
        data_mode = mode
        meta_mode = mode
        mkfs_cmd = [binary, "-f", "-L", name, "-d", data_mode, "-m", meta_mode, *devices]
        mount_cmd = ["mount", devices[0], mountpoint]
        command = shell_join(mkfs_cmd) + " && mkdir -p " + shlex.quote(mountpoint) + " && (btrfs device scan || true) && " + shell_join(mount_cmd)
        return True, command, f"Création BTRFS multi-device {mode} : {name}"

    return False, "", "Moteur RAID refusé."



def raid_systemd_unit_name(session: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(session or "raid")).strip(".-_") or "raid"
    return f"yoleo-disk-{safe}"[:120]


def systemd_run_available() -> bool:
    return shutil.which("systemd-run") is not None


def launch_raid_tmux_worker(session: str, script: str, description: str) -> Tuple[bool, str, str]:
    """Lance une session tmux de Disk via systemd-run quand c'est possible.

    Le point important est le même que pour Build/Maintenance : Flask/Gunicorn
    ne doit pas être le parent durable du travail destructif. Avec systemd-run,
    le job reste vivant même si le service Flask est redémarré pendant la création.
    """
    if not shutil.which("tmux"):
        return False, "", "tmux introuvable. Installe tmux avant de lancer cette opération."

    session = str(session or "").strip()
    if not session:
        return False, "", "Nom de session tmux vide."
    if tmux_session_exists(session):
        return True, "", f"Session tmux déjà active : {session}"

    systemd_error = ""
    if systemd_run_available():
        unit = raid_systemd_unit_name(session)
        try:
            run_cmd(["systemctl", "reset-failed", f"{unit}.service"], timeout=5)
        except Exception:
            pass
        wrapper = "\n".join([
            "set -u",
            f"SESSION={shlex.quote(session)}",
            "if tmux has-session -t \"$SESSION\" 2>/dev/null; then",
            "  exit 0",
            "fi",
            "tmux new-session -d -s \"$SESSION\" /bin/bash -lc " + shlex.quote(script),
            "rc=$?",
            "if [ $rc -ne 0 ]; then",
            "  exit $rc",
            "fi",
            "while tmux has-session -t \"$SESSION\" 2>/dev/null; do",
            "  sleep 2",
            "done",
        ])
        rc, out = run_cmd([
            "systemd-run",
            "--unit", unit,
            "--description", description or f"Yoleo Disk tmux {session}",
            "--collect",
            "--property=Type=simple",
            "--property=Restart=no",
            "/bin/bash", "-lc", wrapper,
        ], timeout=15)
        if rc == 0:
            for _ in range(20):
                if tmux_session_exists(session):
                    return True, f"{unit}.service", f"Opération lancée dans tmux autonome : {session} ({unit}.service)"
                time.sleep(0.15)
            return True, f"{unit}.service", f"Opération lancée via systemd-run : {unit}.service"
        systemd_error = out.strip() or f"systemd-run impossible pour {unit}.service"

    # Fallback : moins robuste, mais permet de ne pas bloquer une machine sans systemd-run.
    rc, out = run_cmd(["tmux", "new-session", "-d", "-s", session, "/bin/bash", "-lc", script], timeout=10)
    if rc != 0:
        return False, "", out or systemd_error or f"Création session tmux impossible : {session}"
    msg = f"Opération lancée dans tmux direct : {session}"
    if systemd_error:
        msg += f" (⚠️ {systemd_error}; non isolé du cgroup Flask)"
    return True, "", msg


def raid_tool_status() -> Dict[str, Any]:
    mdadm_ok = bool(shutil.which("mdadm"))
    zfs_ok = bool(shutil.which("zpool") and shutil.which("zfs"))
    btrfs_ok = bool(shutil.which("mkfs.btrfs") or shutil.which("btrfs"))
    xfs_ok = bool(shutil.which("mkfs.xfs"))
    tmux_ok = bool(shutil.which("tmux"))
    apt_ok = bool(shutil.which("apt-get"))
    tools = {
        "mdadm": mdadm_ok,
        "zfs": zfs_ok,
        "btrfs": btrfs_ok,
        "xfsprogs": xfs_ok,
        "tmux": tmux_ok,
        "systemd_run": systemd_run_available(),
        "apt_get": apt_ok,
    }
    # Pour l'instant, le module RAID stable est limité à mdadm.
    # ZFS/BTRFS restent visibles comme outils présents/absents, mais ne bloquent
    # plus le statut global ni le bouton principal.
    required = {
        "mdadm": mdadm_ok,
        "xfsprogs": xfs_ok,
        "tmux": tmux_ok,
    }
    missing = [name for name, ok in required.items() if not ok]
    return {
        "tools": tools,
        "missing": missing,
        "all_installed": not missing,
        "message": "Services RAID mdadm/XFS/tmux installés." if not missing else "Services manquants : " + ", ".join(missing),
        "experimental_disabled": ["zfs", "btrfs"],
    }

def write_raid_status(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or "/tmp", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def start_raid_job(engine: str, mode: str, name: str, devices: List[str], mountpoint: str) -> Tuple[bool, Dict[str, Any], str]:
    conf = get_config()
    if not shutil.which("tmux"):
        return False, {}, "tmux introuvable. Installe tmux avant de lancer une création RAID."
    ok_cmd, command_shell, command_label = make_raid_command(engine, mode, name, devices, mountpoint)
    if not ok_cmd:
        return False, {}, command_label

    stamp = time.strftime("%Y%m%d-%H%M%S")
    job_id = f"raid-{stamp}-{os.getpid()}"
    session = raid_session_name(job_id)
    status_file = raid_status_path(conf, job_id)
    log_file = raid_log_path(conf, job_id)
    os.makedirs(raid_job_dir(conf), exist_ok=True)
    os.makedirs(raid_log_dir(conf), exist_ok=True)

    base_payload = {
        "id": job_id,
        "type": "create",
        "session": session,
        "engine": engine,
        "mode": mode,
        "name": name,
        "devices": devices,
        "mountpoint": mountpoint,
        "log_file": log_file,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    running_payload = dict(base_payload, status="running", message=command_label + " en cours...")
    success_payload = dict(base_payload, status="success", message="Création terminée. Tu peux maintenant formater et monter le volume dans Maintenance.", finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    error_payload = dict(base_payload, status="error", message="Erreur pendant la création RAID. Consulte le log.", finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))

    script = f"""
set -uo pipefail
mkdir -p {shlex.quote(os.path.dirname(status_file))} {shlex.quote(os.path.dirname(log_file))}
exec > >(tee -a {shlex.quote(log_file)}) 2>&1
printf '%s\n' {shlex.quote(json.dumps(running_payload, ensure_ascii=False))} > {shlex.quote(status_file)}
clear
echo "============================================================"
echo " Création RAID / ZFS / BTRFS Yoleo"
echo " Session tmux : {session}"
echo " Moteur       : {engine}"
echo " Mode         : {mode}"
echo " Nom          : {name}"
echo " Disques      : {' '.join(devices)}"
echo " Démarrage    : $(date '+%F %T')"
echo "============================================================"
echo
echo "Commande : {command_shell}"
echo
{command_shell}
rc=$?
echo
if command -v udevadm >/dev/null 2>&1; then
  echo ">>> udevadm settle"
  udevadm settle || true
fi
if command -v partprobe >/dev/null 2>&1; then
  echo ">>> partprobe"
  partprobe || true
fi
echo
echo "============================================================"
echo " Fin création : $(date '+%F %T')"
echo " Code retour  : $rc"
echo "============================================================"
if [ "$rc" -eq 0 ]; then
  printf '%s\n' {shlex.quote(json.dumps(success_payload, ensure_ascii=False))} > {shlex.quote(status_file)}
else
  printf '%s\n' {shlex.quote(json.dumps(error_payload, ensure_ascii=False))} > {shlex.quote(status_file)}
fi
sleep 20
exit "$rc"
""".strip()

    write_raid_status(status_file, running_payload)
    ok_launch, systemd_unit, launch_msg = launch_raid_tmux_worker(session, script, f"Yoleo RAID {engine} {name}")
    if systemd_unit:
        running_payload["systemd_unit"] = systemd_unit
        success_payload["systemd_unit"] = systemd_unit
        error_payload["systemd_unit"] = systemd_unit
        write_raid_status(status_file, running_payload)
    if not ok_launch:
        error_payload["message"] = launch_msg or "Création session tmux impossible."
        write_raid_status(status_file, error_payload)
        return False, error_payload, error_payload["message"]
    running_payload["message"] = launch_msg or running_payload["message"]
    write_raid_status(status_file, running_payload)
    return True, running_payload, running_payload["message"]



def start_raid_install_job() -> Tuple[bool, Dict[str, Any], str]:
    conf = get_config()
    status = raid_tool_status()
    if status.get("all_installed"):
        return False, {}, "Services RAID/ZFS/BTRFS déjà installés."
    if not status.get("tools", {}).get("apt_get"):
        return False, {}, "apt-get introuvable : installation automatique impossible."

    stamp = time.strftime("%Y%m%d-%H%M%S")
    job_id = f"raid-install-{stamp}-{os.getpid()}"
    session = raid_session_name(job_id)
    status_file = raid_status_path(conf, job_id)
    log_file = raid_log_path(conf, job_id)
    os.makedirs(raid_job_dir(conf), exist_ok=True)
    os.makedirs(raid_log_dir(conf), exist_ok=True)

    base_payload = {
        "id": job_id,
        "type": "install",
        "session": session,
        "engine": "packages",
        "mode": "install",
        "name": "services RAID mdadm",
        "devices": [],
        "mountpoint": "",
        "log_file": log_file,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    running_payload = dict(base_payload, status="running", message="Installation des services RAID/ZFS/BTRFS en cours...")
    success_payload = dict(base_payload, status="success", message="Services RAID mdadm/XFS installés.", finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    error_payload = dict(base_payload, status="error", message="Erreur pendant l'installation des services RAID mdadm/XFS. Consulte le log.", finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))

    packages = "mdadm xfsprogs tmux"
    script = f"""
set -uo pipefail
mkdir -p {shlex.quote(os.path.dirname(status_file))} {shlex.quote(os.path.dirname(log_file))}
exec > >(tee -a {shlex.quote(log_file)}) 2>&1
printf '%s\n' {shlex.quote(json.dumps(running_payload, ensure_ascii=False))} > {shlex.quote(status_file)}
clear
echo "============================================================"
echo " Installation services RAID mdadm Yoleo"
echo " Session tmux : {session}"
echo " Démarrage    : $(date '+%F %T')"
echo "============================================================"
echo
echo "Paquets : {packages}"
echo
export DEBIAN_FRONTEND=noninteractive
apt-get update
rc_update=$?
if [ "$rc_update" -eq 0 ]; then
  apt-get install -y {packages}
  rc=$?
else
  rc=$rc_update
fi
echo
echo "============================================================"
echo " Fin installation : $(date '+%F %T')"
echo " Code retour      : $rc"
echo "============================================================"
if [ "$rc" -eq 0 ]; then
  printf '%s\n' {shlex.quote(json.dumps(success_payload, ensure_ascii=False))} > {shlex.quote(status_file)}
else
  printf '%s\n' {shlex.quote(json.dumps(error_payload, ensure_ascii=False))} > {shlex.quote(status_file)}
fi
sleep 60
exit "$rc"
""".strip()

    write_raid_status(status_file, running_payload)
    ok_launch, systemd_unit, launch_msg = launch_raid_tmux_worker(session, script, "Yoleo Disk install RAID mdadm services")
    if systemd_unit:
        running_payload["systemd_unit"] = systemd_unit
        success_payload["systemd_unit"] = systemd_unit
        error_payload["systemd_unit"] = systemd_unit
        write_raid_status(status_file, running_payload)
    if not ok_launch:
        error_payload["message"] = launch_msg or "Création session tmux impossible."
        write_raid_status(status_file, error_payload)
        return False, error_payload, error_payload["message"]
    running_payload["message"] = launch_msg or running_payload["message"]
    write_raid_status(status_file, running_payload)
    return True, running_payload, running_payload["message"]


def raid_volume_by_request(engine: str, device: str, name: str) -> Dict[str, Any]:
    engine = str(engine or "").strip().lower()
    device = str(device or "").strip()
    name = str(name or "").strip()
    for volume in list_raid_volumes():
        vol_engine = str(volume.get("engine") or "").strip().lower()
        vol_device = str(volume.get("device") or "").strip()
        vol_name = str(volume.get("name") or "").strip()
        if engine and vol_engine != engine:
            continue
        if device and (vol_device == device or os.path.realpath(vol_device) == os.path.realpath(device)):
            return volume
        if name and vol_name == name:
            return volume
    return {}


def mdadm_member_devices(target: str) -> List[str]:
    mdadm_bin = shutil.which("mdadm") or "mdadm"
    members: List[str] = []
    rc, out = run_cmd([mdadm_bin, "--detail", target], timeout=15)
    if rc == 0:
        for found in re.findall(r"/dev/(?:sd[a-z]+\d*|hd[a-z]+\d*|vd[a-z]+\d*|xvd[a-z]+\d*|nvme\d+n\d+(?:p\d+)?|mmcblk\d+(?:p\d+)?)\b", out or ""):
            dev = found if found.startswith("/dev/") else "/dev/" + found
            if dev not in members and SAFE_BLOCK_PATH_RE.match(dev):
                members.append(dev)
    if members:
        return members

    # Secours sans mdadm --detail : /proc/mdstat expose souvent les membres sous forme sdb[0] sdc[1].
    block = block_name_from_path(target)
    try:
        
        with open("/proc/mdstat", "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except Exception:
        text = ""
    capture = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(block + " :"):
            capture = True
            for token in line.split():
                base = token.split("[", 1)[0]
                dev = "/dev/" + base
                if SAFE_BLOCK_PATH_RE.match(dev) and dev != target and dev not in members:
                    members.append(dev)
        elif capture and line.startswith("[>"):
            continue
        elif capture and line:
            capture = False
    return members


def delete_mdadm_volume(device: str, name: str) -> Tuple[bool, str, str]:
    conf = get_config()
    volume = raid_volume_by_request("mdadm", device, name)
    if not volume:
        return False, "Volume mdadm introuvable.", ""
    target = str(volume.get("device") or device or "").strip()

    # Sécurité + compatibilité mdadm : on force la cible réelle /dev/mdX avant
    # validation. Les chemins /dev/md/<hostname>:<nom> sont des alias valides,
    # mais ils ne respectent pas la whitelist stricte utilisée par les actions
    # destructives.
    block = block_name_from_path(target)
    vol_name = str(volume.get("name") or name or "").strip()
    if not block and re.fullmatch(r"md\d+", vol_name or ""):
        block = vol_name
    if re.fullmatch(r"md\d+", block or ""):
        target = f"/dev/{block}"

    ok, target, error = require_safe_target(conf, target, allow_disk=True, allow_part=False)
    if not ok:
        return False, error, ""
    block = block_name_from_path(target)
    if not re.fullmatch(r"md\d+", block or ""):
        return False, "Suppression refusée : seul un volume /dev/mdX est accepté ici.", ""
    mounts = mount_paths_for_target(conf, target)
    if mounts:
        return False, "Volume monté : démonte-le d'abord dans Maintenance avant suppression.", ""
    mdadm_bin = shutil.which("mdadm")
    if not mdadm_bin:
        return False, "mdadm introuvable. Installe les outils depuis la rubrique Installation avant suppression.", ""

    members = mdadm_member_devices(target)
    safe_members: List[str] = []
    for member in members:
        ok_member, member_target, member_error = require_safe_target(conf, member, allow_disk=True, allow_part=True)
        if not ok_member:
            return False, f"Membre RAID refusé ({member}) : {member_error}", ""
        if mount_paths_for_target(conf, member_target):
            return False, f"Membre RAID monté ({member_target}) : suppression refusée.", ""
        if member_target not in safe_members:
            safe_members.append(member_target)

    output_parts: List[str] = []
    rc, out = run_cmd([mdadm_bin, "--stop", target], timeout=30)
    output_parts.append(out or "")
    if rc != 0:
        return False, "Arrêt mdadm impossible : " + ((out or "").strip() or f"code {rc}"), "\n".join(output_parts)

    run_cmd([mdadm_bin, "--remove", target], timeout=10)

    zeroed: List[str] = []
    for member in safe_members:
        rc_zero, out_zero = run_cmd([mdadm_bin, "--zero-superblock", "--force", member], timeout=30)
        output_parts.append(out_zero or "")
        if rc_zero == 0:
            zeroed.append(member)

    refresh_udev_best_effort(target)
    clear_disk_runtime_caches()
    extra = f" Signatures effacées : {', '.join(zeroed)}." if zeroed else " Membres non détectés ou signature déjà absente."
    return True, f"Volume mdadm {target} supprimé.{extra}", "\n".join(x for x in output_parts if x)


def delete_zfs_volume(name: str) -> Tuple[bool, str, str]:
    name = str(name or "").strip()
    if not SAFE_RAID_NAME_RE.match(name):
        return False, "Nom ZFS refusé.", ""
    volume = raid_volume_by_request("zfs", "", name)
    if not volume:
        return False, "Pool ZFS introuvable.", ""
    zpool_bin = shutil.which("zpool")
    if not zpool_bin:
        return False, "zpool introuvable. Installe les outils depuis la rubrique Installation avant suppression.", ""
    rc, out = run_cmd([zpool_bin, "destroy", name], timeout=60)
    if rc != 0:
        return False, "Suppression ZFS impossible : " + ((out or "").strip() or f"code {rc}"), out or ""
    clear_disk_runtime_caches()
    return True, f"Pool ZFS {name} supprimé.", out or ""


def delete_raid_volume(engine: str, device: str, name: str) -> Tuple[bool, str, str]:
    engine = str(engine or "").strip().lower()
    if engine == "mdadm":
        return delete_mdadm_volume(device, name)
    if engine == "zfs":
        return delete_zfs_volume(name)
    return False, "Suppression disponible uniquement pour mdadm et ZFS dans cette page.", ""


@disk_bp.route("/disk/api/raid")
def disk_raid_api():
    conf = get_config()
    return jsonify({
        "ok": True,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "available_disks": raid_empty_disk_candidates(),
        "volumes": list_raid_volumes(),
        "jobs": raid_jobs(conf),
        "service_status": raid_tool_status(),
        "modes": {
            "mdadm": {str(n): raid_mode_options("mdadm", n) for n in range(2, 13)},
            "zfs": {str(n): raid_mode_options("zfs", n) for n in range(2, 13)},
            "btrfs": {str(n): raid_mode_options("btrfs", n) for n in range(2, 13)},
        },
    })


@disk_bp.route("/disk/api/action/raid/delete", methods=["POST"])
def disk_raid_delete_api():
    payload = request.get_json(silent=True) or {}
    engine = str(payload.get("engine") or "").strip().lower()
    device = str(payload.get("device") or "").strip()
    name = str(payload.get("name") or "").strip()
    ok, msg, output = delete_raid_volume(engine, device, name)
    return json_response(ok, msg, output=output)


@disk_bp.route("/disk/api/action/raid/create", methods=["POST"])
def disk_raid_create_api():
    payload = request.get_json(silent=True) or {}
    engine = str(payload.get("engine") or "mdadm").strip().lower()
    mode = str(payload.get("mode") or "").strip().lower()
    devices_raw = payload.get("devices") or []
    if not isinstance(devices_raw, list):
        return json_response(False, "Liste de disques invalide.")
    ok, devices, error = validate_raid_devices([str(x) for x in devices_raw])
    if not ok:
        return json_response(False, error)
    if engine != "mdadm":
        return json_response(False, "ZFS/BTRFS sont temporairement désactivés dans /disk/raid. On stabilise d'abord mdadm, puis on reprendra ZFS/BTRFS dans une passe dédiée.")
    allowed = raid_allowed_modes(engine, len(devices))
    if mode not in allowed:
        return json_response(False, f"Mode {mode or 'vide'} non disponible avec {len(devices)} disque(s) pour {engine}.")
    ok_name, name, name_error = raid_clean_name(str(payload.get("name") or "yoleo_raid"))
    if not ok_name:
        return json_response(False, name_error)
    mountpoint = str(payload.get("mountpoint") or f"/mnt/{name}").strip()
    if engine in {"zfs", "btrfs"}:
        ok_mount, mountpoint, mount_error = safe_real_mount_path(get_config(), mountpoint)
        if not ok_mount:
            return json_response(False, mount_error)

    ok_job, job, msg = start_raid_job(engine, mode, name, devices, mountpoint)
    if not ok_job:
        return json_response(False, msg, job=job)
    return jsonify({"ok": True, "message": msg, "job": job})



@disk_bp.route("/disk/api/action/raid/install-services", methods=["POST"])
def disk_raid_install_services_api():
    ok_job, job, msg = start_raid_install_job()
    if not ok_job:
        return json_response(False, msg, job=job)
    return jsonify({"ok": True, "message": msg, "job": job})


@disk_bp.route("/disk/api/action/raid/tmux/view", methods=["POST"])
def disk_raid_tmux_view_api():
    payload = request.get_json(silent=True) or {}
    session = str(payload.get("session") or "").strip()
    job_id = str(payload.get("job_id") or "").strip()
    conf = get_config()
    allowed_jobs = raid_jobs(conf)
    if job_id and not session:
        for job in allowed_jobs:
            if str(job.get("id") or "") == job_id:
                session = str(job.get("session") or "")
                break
    allowed = {str(job.get("session") or "") for job in allowed_jobs if job.get("session")}
    if not session or session not in allowed:
        return json_response(False, "Session RAID inconnue ou expirée.")
    if not tmux_session_exists(session):
        log_file = next((str(job.get("log_file") or "") for job in allowed_jobs if str(job.get("session") or "") == session), "")
        msg = "La session tmux est terminée."
        if log_file:
            msg += " Log fichier : " + log_file
        return json_response(False, msg, session=session)
    ok_ttyd, url, port, ttyd_msg = start_ttyd_for_tmux_session(session)
    if not ok_ttyd:
        return json_response(False, ttyd_msg, session=session)
    return jsonify({
        "ok": True,
        "message": ttyd_msg,
        "session": session,
        "terminal_url": url,
        "terminal_port": port,
        "mode": "raid",
    })

