def _is_app_log_path(value: str) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    raw_expanded = os.path.expanduser(os.path.expandvars(raw))
    if raw_expanded.startswith("../logs") or raw_expanded.startswith("./logs") or raw_expanded.startswith("logs/"):
        return True
    normalized = os.path.abspath(raw_expanded if os.path.isabs(raw_expanded) else os.path.join(NAS_CONF_DIR, raw_expanded))
    project_logs = os.path.abspath(os.path.join(NAS_ROOT_DIR, "logs"))
    module_logs = os.path.abspath(os.path.join(_NAS_MODULE_DIR, "logs"))
    return normalized == project_logs or normalized.startswith(project_logs + os.sep) or normalized == module_logs or normalized.startswith(module_logs + os.sep)


def _force_standard_log_paths() -> None:
    if _is_app_log_path(CONF.get("SERVICE_LOG", "")):
        CONF["SERVICE_LOG"] = "/var/log/mdns/mdns.log"
    if _is_app_log_path(CONF.get("LAN_LOG_FILE", "")):
        CONF["LAN_LOG_FILE"] = "/var/log/lan/lan.log"
    if str(CONF.get("SERVICE_SCRIPT", "")).strip() in {"../scripts/mdns_host.sh", "./scripts/mdns_host.sh"}:
        CONF["SERVICE_SCRIPT"] = ""


_force_standard_log_paths()
# -----------------------------------------------------

last_net = {"time": 0, "bytes_sent": 0, "bytes_recv": 0}
public_ip_cache = {"ip": "Chargement...", "time": 0}
remote_gpu_cache = {"time": 0, "stats": None, "raw_smi": None}
intel_gpu_cache = {"time": 0, "stats": None, "text": "", "raw": ""}

def get_conf_int(key, default):
    try:
        return int(str(CONF.get(key, default)).strip())
    except Exception:
        return int(default)

def get_conf_float(key, default):
    try:
        return float(str(CONF.get(key, default)).strip())
    except Exception:
        return float(default)

def get_conf_str(key, default=""):
    try:
        return str(CONF.get(key, default)).strip()
    except Exception:
        return str(default)

def get_size_str(bytes_val):
    try:
        val = float(bytes_val)
    except Exception:
        return "-"
    for unit in ['o', 'Ko', 'Mo', 'Go', 'To']:
        if val < 1024:
            return f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} Po"

def get_public_ip():
    global public_ip_cache
    if time.time() - public_ip_cache["time"] > get_conf_int("PUBLIC_IP_CACHE_SECONDS", 300):
        try:
            public_ip_cache["ip"] = urllib.request.urlopen('https://api.ipify.org', timeout=2).read().decode('utf8')
            public_ip_cache["time"] = time.time()
        except Exception:
            pass
    return public_ip_cache["ip"]

def ssh_exec_gpu(command):
    if paramiko is None:
        return 1, "", "Module Python paramiko introuvable : impossible de lire la NVIDIA distante en SSH"

    ssh = None
    try:
        key_path = get_conf_str("SSH_GPU_KEY_PATH", "/data/key/yoan")
        if not os.path.exists(key_path):
            candidates = get_conf_str("_SSH_GPU_KEY_PATH_CANDIDATES", "")
            extra = f" | candidats testés : {candidates}" if candidates else ""
            return 1, "", (
                f"Clé SSH introuvable : {key_path} | "
                f"system.conf chargé : {loaded_config or 'aucun, valeurs par défaut'}"
                f"{extra}"
            )

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=get_conf_str("SSH_GPU_HOST", "192.168.1.4"),
            port=get_conf_int("SSH_GPU_PORT", 22),
            username=get_conf_str("SSH_GPU_USER", "yoan"),
            key_filename=key_path,
            look_for_keys=False,
            allow_agent=False,
            timeout=get_conf_int("SSH_GPU_CONNECT_TIMEOUT", 5),
        )
        stdin, stdout, stderr = ssh.exec_command(command, timeout=get_conf_int("SSH_GPU_COMMAND_TIMEOUT", 8))
        out = stdout.read().decode('utf-8', errors='replace')
        err = stderr.read().decode('utf-8', errors='replace')
        rc = stdout.channel.recv_exit_status()
        return rc, out, err
    except Exception as e:
        return 1, "", str(e)
    finally:
        if ssh is not None:
            try:
                ssh.close()
            except Exception:
                pass

def get_remote_nvidia_stats():
    global remote_gpu_cache

    now = time.time()
    cache_seconds = get_conf_int("SSH_GPU_CACHE_SECONDS", 2)

    if remote_gpu_cache["stats"] is not None and (now - remote_gpu_cache["time"] < cache_seconds):
        return remote_gpu_cache["stats"]

    remote_nvidia_smi = get_conf_str("REMOTE_NVIDIA_SMI", "/usr/bin/nvidia-smi")
    cmd = f"{remote_nvidia_smi} --query-gpu=name,utilization.gpu,utilization.memory,temperature.gpu,power.draw,fan.speed --format=csv,noheader,nounits"
    rc, out, err = ssh_exec_gpu(cmd)

    if rc != 0:
        raise RuntimeError(err.strip() or f"Commande SSH/NVIDIA en erreur ({rc})")

    stats = []
    for l in out.strip().split('\n'):
        if not l.strip():
            continue
        p = [x.strip() for x in l.split(',')]
        if len(p) >= 6:
            stats.append({
                "type": "nvidia",
                "name": p[0],
                "load": p[1],
                "mem": p[2],
                "temp": p[3],
                "power": p[4],
                "fan": p[5]
            })

    remote_gpu_cache["stats"] = stats
    remote_gpu_cache["time"] = now
    return stats

def get_remote_nvidia_smi_raw():
    global remote_gpu_cache

    now = time.time()
    cache_seconds = get_conf_int("SSH_GPU_CACHE_SECONDS", 2)

    if remote_gpu_cache["raw_smi"] is not None and (now - remote_gpu_cache["time"] < cache_seconds):
        return remote_gpu_cache["raw_smi"]

    remote_nvidia_smi = get_conf_str("REMOTE_NVIDIA_SMI", "/usr/bin/nvidia-smi")
    rc, out, err = ssh_exec_gpu(remote_nvidia_smi)
    if rc != 0:
        raise RuntimeError(err.strip() or f"Commande SSH/NVIDIA en erreur ({rc})")

    remote_gpu_cache["raw_smi"] = out
    remote_gpu_cache["time"] = now
    return out



# --------------------------------------------------
# NVIDIA locale
# --------------------------------------------------
def get_local_nvidia_stats():
    """Lit les NVIDIA présentes sur l'hôte local, sans SSH.

    La NVIDIA distante garde le type historique "nvidia" pour ne pas casser
    les pages existantes. La locale reçoit un type séparé "nvidia_local" pour
    que la page d'accueil puisse afficher deux panneaux distincts.
    """
    nvidia_smi = shutil.which("nvidia-smi") or "/usr/bin/nvidia-smi"
    if not os.path.exists(nvidia_smi):
        return []

    cmd = [
        nvidia_smi,
        "--query-gpu=name,utilization.gpu,utilization.memory,temperature.gpu,power.draw,fan.speed",
        "--format=csv,noheader,nounits",
    ]
    rc, out = run_cmd(cmd, timeout=4)
    if rc != 0:
        return []

    stats = []
    for line in (out or "").strip().splitlines():
        if not line.strip():
            continue
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 6:
            continue
        stats.append({
            "type": "nvidia_local",
            "source": "local",
            "label": "NVIDIA GPU (local)",
            "name": parts[0],
            "load": parts[1],
            "mem": parts[2],
            "temp": parts[3],
            "power": parts[4],
            "fan": parts[5],
        })
    return stats


# --------------------------------------------------
# RESEAU DIRECT HOTE / MATERIEL
# --------------------------------------------------
def get_conf_list(key, default=""):
    raw = get_conf_str(key, default)
    return [x.strip() for x in raw.split(',') if x.strip()]

def _run_cmd(cmd, timeout=2):
    try:
        return subprocess.check_output(
            cmd,
            shell=True,
            encoding='utf-8',
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        ).strip()
    except Exception:
        return ""

def _read_file(path, default="-"):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            value = f.read().strip()
        return value if value else default
    except Exception:
        return default

def get_default_route_info():
    info = {"iface": "-", "gateway": "-"}

    out = _run_cmd("ip -j route show default")
    if out:
        try:
            routes = json.loads(out)
            if routes:
                route = routes[0]
                info["iface"] = route.get("dev") or "-"
                info["gateway"] = route.get("gateway") or "-"
                return info
        except Exception:
            pass

    out = _run_cmd("ip route show default | head -n 1")
    if out:
        parts = out.split()
        if "dev" in parts:
            try:
                info["iface"] = parts[parts.index("dev") + 1]
            except Exception:
                pass
        if "via" in parts:
            try:
                info["gateway"] = parts[parts.index("via") + 1]
            except Exception:
                pass

    return info

def get_iface_ipv4(iface):
    if not iface or iface == "-":
        return "-"

    out = _run_cmd(f"ip -j -4 addr show dev {iface}")
    if out:
        try:
            data = json.loads(out)
            for item in data:
                for addr in item.get("addr_info", []):
                    if addr.get("family") == "inet" and addr.get("local"):
                        return addr.get("local")
        except Exception:
            pass

    out = _run_cmd(f"ip -4 addr show dev {iface} | awk '/inet / {{print $2}}' | cut -d/ -f1 | head -n 1")
    return out or "-"

def get_iface_state(iface):
    if not iface or iface == "-":
        return "unknown"
    state = _read_file(f"/sys/class/net/{iface}/operstate", "unknown").lower()
    return state if state else "unknown"

def get_iface_speed(iface):
    if not iface or iface == "-":
        return "-"
    raw = _read_file(f"/sys/class/net/{iface}/speed", "-")
    try:
        speed = int(raw)
        if speed > 0:
            return f"{speed} Mb/s"
    except Exception:
        pass
    return "-"

def list_netdev_counters():
    path = '/host/proc/net/dev' if os.path.exists('/host/proc/net/dev') else '/proc/net/dev'
    counters = {}
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        for line in lines[2:]:
            if ':' not in line:
                continue
            iface, data = line.split(':', 1)
            iface = iface.strip()
            parts = data.split()
            if len(parts) >= 16:
                counters[iface] = {"rx": int(parts[0]), "tx": int(parts[8])}
    except Exception:
        pass
    return counters

def select_network_interfaces(route_info, counters):
    configured = get_conf_list("NET_INTERFACES", "")
    if configured:
        return [iface for iface in configured if iface in counters]

    default_iface = route_info.get("iface") or "-"
    if default_iface != "-" and default_iface in counters:
        return [default_iface]

    excluded = tuple(get_conf_list("NET_EXCLUDE_PREFIXES", "lo,veth,docker,virbr,br-,vnet,tun,tap"))
    selected = []
    for iface in counters:
        if any(iface.startswith(prefix) for prefix in excluded):
            continue
        selected.append(iface)
    return selected

def get_direct_network_stats():
    global last_net

    route_info = get_default_route_info()
    counters = list_netdev_counters()
    selected_ifaces = select_network_interfaces(route_info, counters)

    rx = sum(counters.get(iface, {}).get("rx", 0) for iface in selected_ifaces)
    tx = sum(counters.get(iface, {}).get("tx", 0) for iface in selected_ifaces)

    now = time.time()
    net_speed = {"up": "0 o/s", "down": "0 o/s"}
    delta = now - last_net.get('time', 0)
    if delta > 0 and last_net.get('time', 0) > 0:
        net_speed["down"] = f"{get_size_str((rx - last_net.get('bytes_recv', 0)) / delta)}/s"
        net_speed["up"] = f"{get_size_str((tx - last_net.get('bytes_sent', 0)) / delta)}/s"

    last_net = {"time": now, "bytes_recv": rx, "bytes_sent": tx}

    iface = selected_ifaces[0] if selected_ifaces else (route_info.get("iface") or "-")
    iface_state = get_iface_state(iface)

    direct = {
        "iface": iface,
        "watched": ", ".join(selected_ifaces) if selected_ifaces else "-",
        "gateway": route_info.get("gateway") or "-",
        "local_ip": get_iface_ipv4(iface),
        "state": iface_state,
        "status": "UP" if iface_state in ["up", "unknown"] and iface != "-" else "DOWN",
        "speed": get_iface_speed(iface),
    }

    ips = {
        "local": direct["local_ip"],
        "public": get_public_ip(),
        "gateway": direct["gateway"],
        "server": direct["local_ip"],
    }

    return net_speed, ips, direct

# --------------------------------------------------
# INTEL iGPU
# --------------------------------------------------
def _to_text(val):
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)

def _to_float(val, default=0.0):
    try:
        return float(val)
    except Exception:
        try:
            return float(str(val).strip())
        except Exception:
            return default

def _find_intel_render_node():
    for p in ["/dev/dri/renderD128", "/dev/dri/renderD129"]:
        if os.path.exists(p):
            return p
    return "/dev/dri/renderD128"

def _find_intel_monitor_node():
    for p in ["/dev/dri/card0", "/dev/dri/card1", "/dev/dri/renderD128", "/dev/dri/renderD129"]:
        if os.path.exists(p):
            return p
    return "/dev/dri/card0"

def _extract_json_objects(stream_text):
    objs = []
    depth = 0
    start = None
    in_string = False
    escaped = False

    for i, ch in enumerate(stream_text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue

        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    objs.append(stream_text[start:i+1])
                    start = None

    return objs

def _read_intel_power_watts():
    candidates = glob.glob("/sys/class/drm/card*/device/hwmon/hwmon*/power1_input")
    for path in candidates:
        try:
            with open(path, "r") as f:
                raw = f.read().strip()
            return round(int(raw) / 1000000, 1)
        except Exception:
            pass
    return 0.0

def _run_intel_gpu_top_json():
    monitor_node = _find_intel_monitor_node()
    cmd = ["intel_gpu_top", "-J", "-d", f"drm:{monitor_node}"]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError("intel_gpu_top introuvable dans le conteneur")
    except Exception as e:
        raise RuntimeError(f"Impossible de lancer intel_gpu_top : {e}")

    out = ""
    try:
        out, _ = proc.communicate(timeout=1.8)
    except subprocess.TimeoutExpired:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            out, _ = proc.communicate(timeout=0.7)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            out, _ = proc.communicate()

    out = _to_text(out).strip()
    if not out:
        raise RuntimeError("Aucune sortie renvoyée par intel_gpu_top")
    return out

def _parse_all_intel_snapshots(raw_text):
    snapshots = []
    last_err = None

    for block in _extract_json_objects(raw_text):
        try:
            snapshots.append(json.loads(block))
        except Exception as e:
            last_err = e

    if snapshots:
        return snapshots

    raise RuntimeError(f"Impossible de parser le JSON intel_gpu_top : {last_err}")

def _collect_engine_rows(snapshot):
    rows = []

    def visit(node, prefix=""):
        if isinstance(node, dict):
            if "busy" in node:
                name = prefix.strip("/") or "engine"
                rows.append((name, _to_float(node.get("busy"), 0.0)))
                return

            for key, value in node.items():
                next_prefix = f"{prefix}/{key}" if prefix else str(key)
                visit(value, next_prefix)

        elif isinstance(node, list):
            for idx, value in enumerate(node):
                next_prefix = f"{prefix}[{idx}]"
                visit(value, next_prefix)

    engines = snapshot.get("engines")
    if engines is not None:
        visit(engines, "engines")
    else:
        visit(snapshot, "")

    cleaned = []
    seen = set()
    for name, busy in rows:
        short = name
        short = short.replace("engines/", "")
        short = short.replace("engine/", "")
        if short not in seen:
            cleaned.append((short, busy))
            seen.add(short)

    return cleaned

def _score_snapshot(snapshot):
    rows = _collect_engine_rows(snapshot)
    if not rows:
        return -1.0

    vals = [max(0.0, _to_float(busy, 0.0)) for _name, busy in rows]
    return sum(vals) + max(vals)

def _select_best_snapshot(snapshots):
    if not snapshots:
        raise RuntimeError("Aucun snapshot Intel disponible")

    # On évite de prendre aveuglément le dernier snapshot :
    # à l'arrêt de intel_gpu_top, le dernier échantillon peut être revenu à zéro.
    best = max(snapshots, key=_score_snapshot)
    if _score_snapshot(best) > 0:
        return best

    return snapshots[-1]

def _compute_intel_stats_from_snapshot(snapshot):
    rows = _collect_engine_rows(snapshot)

    render_vals = []
    video_vals = []

    for name, busy in rows:
        lname = name.lower()

        if any(x in lname for x in ["render", "3d", "compute", "ccs", "rcs"]):
            render_vals.append(busy)

        if any(x in lname for x in [
            "video", "videoenhance", "enhance", "decode", "encode",
            "vdbox", "vdbox", "vebox", "vcs", "vecs"
        ]):
            video_vals.append(busy)

    load_3d = round(max(render_vals), 1) if render_vals else 0.0
    load_video = round(max(video_vals), 1) if video_vals else 0.0

    power = 0.0
    power_block = snapshot.get("power", {})
    if isinstance(power_block, dict):
        for key in ["GPU", "GT", "Package"]:
            if key in power_block:
                power = round(_to_float(power_block.get(key), 0.0), 1)
                if power > 0:
                    break

    if power <= 0:
        power = _read_intel_power_watts()

    if load_3d <= 0:
        try:
            with open("/sys/class/drm/card0/device/gpu_busy_percent", "r") as f:
                load_3d = round(_to_float(f.read().strip(), 0.0), 1)
        except Exception:
            pass

    return {
        "type": "intel",
        "name": "Intel iGPU",
        "load": f"{load_3d:.1f}",
        "mem": f"{load_video:.1f}",
        "temp": "-",
        "power": f"{power:.1f}",
        "fan": "-",
        "engine_rows": rows,
    }

def _format_intel_text(stats, raw_text=None):
    lines = []
    lines.append("INTEL GPU TOP - instantané retenu")
    lines.append(f"Monitor node : {_find_intel_monitor_node()}")
    lines.append(f"VAAPI node   : {_find_intel_render_node()}")
    lines.append("")

    try:
        lines.append(f"3D    : {float(stats['load']):.1f}%")
    except Exception:
        lines.append(f"3D    : {stats['load']}%")

    try:
        lines.append(f"Vidéo : {float(stats['mem']):.1f}%")
    except Exception:
        lines.append(f"Vidéo : {stats['mem']}%")

    try:
        lines.append(f"Power : {float(stats['power']):.1f} W")
    except Exception:
        lines.append(f"Power : {stats['power']} W")

    lines.append("")
    lines.append(f"{'MOTEUR':<30} {'BUSY %':>8}")
    lines.append("-" * 42)

    rows = stats.get("engine_rows", [])
    if rows:
        for name, busy in rows:
            lines.append(f"{name[:30]:<30} {busy:>7.1f}")
    else:
        lines.append("Aucun moteur détecté.")

    if raw_text and not rows:
        lines.append("")
        lines.append("Sortie brute :")
        lines.append(raw_text[:2500])

    return "\n".join(lines)

def get_intel_stats_and_text():
    global intel_gpu_cache

    now = time.time()
    cache_seconds = get_conf_float("INTEL_GPU_CACHE_SECONDS", 1)

    if (
        intel_gpu_cache["stats"] is not None
        and (now - intel_gpu_cache["time"] < cache_seconds)
    ):
        return intel_gpu_cache["stats"], intel_gpu_cache["text"]

    raw_text = _run_intel_gpu_top_json()
    snapshots = _parse_all_intel_snapshots(raw_text)
    snapshot = _select_best_snapshot(snapshots)
    stats = _compute_intel_stats_from_snapshot(snapshot)
    text = _format_intel_text(stats, raw_text=raw_text)

    intel_gpu_cache["time"] = now
    intel_gpu_cache["stats"] = stats
    intel_gpu_cache["text"] = text
    intel_gpu_cache["raw"] = raw_text

    return stats, text


def collect_mobile_hardware_stats():
    """Sous-ensemble matériel compact destiné au cliché des applications natives."""
    temperatures = []
    try:
        for chip, sensors in (psutil.sensors_temperatures() or {}).items():
            for index, sensor in enumerate(sensors or [], start=1):
                current = getattr(sensor, "current", None)
                if current is None:
                    continue
                temperatures.append({
                    "id": f"{chip}:{index}",
                    "chip": str(chip or ""),
                    "label": str(getattr(sensor, "label", "") or chip or f"Température {index}"),
                    "current": round(float(current), 1),
                    "high": getattr(sensor, "high", None),
                    "critical": getattr(sensor, "critical", None),
                })
                if len(temperatures) >= 32:
                    break
            if len(temperatures) >= 32:
                break
    except Exception:
        temperatures = []

    gpus = []
    try:
        gpus.extend(get_local_nvidia_stats() or [])
    except Exception:
        pass

    if get_conf_str("SSH_GPU_HOST", ""):
        try:
            for gpu in get_remote_nvidia_stats() or []:
                item = dict(gpu)
                item.setdefault("source", "ssh")
                item.setdefault("label", "NVIDIA GPU (SSH)")
                gpus.append(item)
        except Exception:
            pass

    if os.path.exists("/dev/dri/renderD128") or os.path.exists("/dev/dri/card0"):
        try:
            intel, _text = get_intel_stats_and_text()
            gpus.append({
                "type": "intel",
                "source": "local",
                "label": "Intel GPU",
                "name": intel.get("name") or "Intel iGPU",
                "load": intel.get("load", "0"),
                "mem": intel.get("mem", "0"),
                "temp": intel.get("temp", "-"),
                "power": intel.get("power", "-"),
                "fan": intel.get("fan", "-"),
            })
        except Exception:
            pass

    return {
        "temperatures": temperatures,
        "gpus": gpus,
    }



# --------------------------------------------------
# GESTIONNAIRE DE SERVICES SYSTEMD
# --------------------------------------------------
