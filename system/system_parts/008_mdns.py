BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def abs_from_base(path: str) -> str:
    path = strip_quotes(path or "").strip()
    if not path:
        return ""
    path = os.path.expanduser(os.path.expandvars(path))
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.abspath(os.path.join(BASE_DIR, path)))


DEFAULT_CONFIG_CANDIDATES = [
    os.environ.get("MDNS_MODULE_CONFIG", "").strip(),
    os.path.join(BASE_DIR, "system.conf"),
    os.path.join(BASE_DIR, "system.conf"),
    os.path.join(BASE_DIR, "conf", "system.conf"),
    os.path.join(BASE_DIR, "conf", "system.conf"),
    os.path.join(BASE_DIR, "..", "conf", "system.conf"),
    os.path.join(BASE_DIR, "..", "conf", "system.conf"),
    nas_conf_file("system.conf"),
    nas_conf_file("system.conf"),
]

DEFAULT_CONFIG = {
    # Le module Flask lance maintenant directement la logique en Python.
    "EXEC_MODE": "local-python",
    "TITLE": "mDNS local",
    "SUBTITLE": "Gestion des noms .local sans terminal",

    # Le shell reste seulement un secours si EXEC_MODE=local-sh.
    "SERVICE_SCRIPT": "",

    # Chemins hôte du module mDNS.
    "SERVICE_CONF": nas_conf_file("mdns.conf"),
    "SERVICE_LOG": "/var/log/mdns/mdns.log",
    "SERVICE_RUN_DIR": "/var/run/mdns",
    "RUNTIME_HOSTS": "/var/run/mdns/mdns.hosts",
    "PID_FILE": "/var/run/mdns/mdns-publish.pids",
    "MDNS_LITE_PID": "/var/run/mdns/mdns-lite.pid",
    "AVAHI_HOSTS": "/etc/avahi/hosts",
    "AVAHI_DAEMON_CONF": "/etc/avahi/avahi-daemon.conf",

    # Service systemd optionnel, uniquement pour diagnostic.
    "SYSTEMD_SERVICE": "mdns-labo.service",

    # Outils.
    "BASH_BIN": "/bin/bash",
    "PING_BIN": "ping",
    "SS_BIN": "ss",
    "PGREP_BIN": "pgrep",
    "PKILL_BIN": "pkill",
    "PS_BIN": "ps",
    "SYSTEMCTL_BIN": "systemctl",
    "SERVICE_BIN": "service",
    "AVAHI_DAEMON_BIN": "avahi-daemon",
    "AVAHI_PUBLISH_BIN": "avahi-publish-address",
    "IP_BIN": "ip",

    "ACTION_TIMEOUT": "90",
    "LOG_TAIL_LINES": "180",
    "DEFAULT_IP": "192.168.1.2",
    "MDNS_INTERFACE": "auto",
    "MDNS_USE_IPV6": "no",
}

HOST_RE = re.compile(r"^[A-Za-z0-9-]+\.local$")
VALID_ACTIONS = {"start", "stop", "restart", "status"}


def strip_quotes(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def read_config_file(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path or not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key:
                out[key] = strip_quotes(value)
    return out


def get_config_path() -> str:
    """
    mDNS est fusionné dans le module Système :
    on lit maintenant le même system.conf que system.py.
    Plus besoin de mdns_ui.conf.
    """
    if loaded_config:
        return loaded_config
    env_conf = os.environ.get("SYSTEM_CONF", "").strip()
    if env_conf:
        return abs_from_base(env_conf)
    return abs_from_base(nas_conf_file("system.conf"))


def get_config() -> Dict[str, str]:
    conf = DEFAULT_CONFIG.copy()
    config_path = get_config_path()
    conf.update(read_config_file(config_path))
    # system.py a déjà chargé system.conf dans CONF ; on le reprend aussi,
    # pour garder les mêmes valeurs si le chemin vient de SYSTEM_CONF.
    try:
        conf.update({str(k): str(v) for k, v in CONF.items()})
    except Exception:
        pass
    conf["_CONFIG_PATH"] = config_path

    path_keys = (
        "SERVICE_SCRIPT",
        "SERVICE_CONF",
        "SERVICE_LOG",
        "SERVICE_RUN_DIR",
        "RUNTIME_HOSTS",
        "PID_FILE",
        "MDNS_LITE_PID",
        "AVAHI_HOSTS",
        "AVAHI_DAEMON_CONF",
    )
    for key in path_keys:
        conf[key] = abs_from_base(conf.get(key, DEFAULT_CONFIG[key]))

    if _is_app_log_path(conf.get("SERVICE_LOG", "")):
        conf["SERVICE_LOG"] = "/var/log/mdns/mdns.log"
    if str(conf.get("SERVICE_SCRIPT", "")).strip().endswith("/scripts/mdns_host.sh"):
        conf["SERVICE_SCRIPT"] = ""

    # Si SERVICE_CONF pointe ailleurs qu'au mdns.conf central, on crée aussi ce
    # fichier cible, vide, sans écraser une configuration existante.
    try:
        ensure_mdns_conf_file(conf["SERVICE_CONF"])
    except Exception as exc:
        print(f"⚠️ Impossible de créer mdns.conf : {exc}")

    for key in (
        "BASH_BIN",
        "PING_BIN",
        "SS_BIN",
        "PGREP_BIN",
        "PKILL_BIN",
        "PS_BIN",
        "SYSTEMCTL_BIN",
        "SERVICE_BIN",
        "AVAHI_DAEMON_BIN",
        "AVAHI_PUBLISH_BIN",
        "IP_BIN",
    ):
        conf[key] = strip_quotes(conf.get(key, DEFAULT_CONFIG[key])).strip() or DEFAULT_CONFIG[key]

    conf["EXEC_MODE"] = strip_quotes(conf.get("EXEC_MODE", DEFAULT_CONFIG["EXEC_MODE"])).strip() or "local-python"
    conf["SYSTEMD_SERVICE"] = strip_quotes(conf.get("SYSTEMD_SERVICE", DEFAULT_CONFIG["SYSTEMD_SERVICE"])).strip()
    conf["ACTION_TIMEOUT"] = str(conf.get("ACTION_TIMEOUT", DEFAULT_CONFIG["ACTION_TIMEOUT"])).strip() or "90"
    conf["LOG_TAIL_LINES"] = str(conf.get("LOG_TAIL_LINES", DEFAULT_CONFIG["LOG_TAIL_LINES"])).strip() or "180"
    conf["DEFAULT_IP"] = str(conf.get("DEFAULT_IP", DEFAULT_CONFIG["DEFAULT_IP"])).strip() or "192.168.1.2"
    conf["MDNS_INTERFACE"] = strip_quotes(conf.get("MDNS_INTERFACE", DEFAULT_CONFIG["MDNS_INTERFACE"])).strip() or "auto"
    conf["MDNS_USE_IPV6"] = strip_quotes(conf.get("MDNS_USE_IPV6", DEFAULT_CONFIG["MDNS_USE_IPV6"])).strip().lower() or "no"
    return conf


def local_read_text(path: str) -> str:
    try:
        if not path or not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except Exception:
        return ""


def local_write_text(path: str, content: str) -> Tuple[bool, str]:
    try:
        parent = os.path.dirname(path.rstrip("/")) or "."
        os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
        return True, ""
    except Exception as exc:
        return False, str(exc)


def run_capture(cmd: List[str], timeout: int = 8) -> Tuple[int, str]:
    try:
        completed = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return completed.returncode, completed.stdout or ""
    except FileNotFoundError as exc:
        return 127, str(exc)
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return 124, (output + "\nTIMEOUT").strip()
    except Exception as exc:
        return 1, str(exc)


def command_exists(name: str) -> bool:
    return bool(shutil.which(name))


def _mdns_iface_name(value: str) -> str:
    return str(value or "").strip().split("@", 1)[0]


def _mdns_iface_is_virtual_or_private(name: str) -> bool:
    name = _mdns_iface_name(name)
    low = name.lower()
    if not low:
        return True
    if low in {"lo", "docker0", "mv-host", "ollama_lan"}:
        return True
    return low.startswith((
        "br-",
        "docker",
        "veth",
        "virbr",
        "tap",
        "tun",
        "wg",
        "tailscale",
        "zt",
    ))


def _mdns_ipv4_interfaces(conf: Dict[str, str]) -> List[Dict[str, str]]:
    ip_bin = conf.get("IP_BIN", "ip")
    if not command_exists(ip_bin):
        return []
    rc, output = run_capture([ip_bin, "-o", "-4", "addr", "show", "scope", "global"], timeout=5)
    if rc != 0:
        return []

    items: List[Dict[str, str]] = []
    seen = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        iface = _mdns_iface_name(parts[1])
        ip = parts[3].split("/", 1)[0]
        if not iface or iface in seen:
            continue
        seen.add(iface)
        items.append({"name": iface, "ip": ip})
    return items


def _mdns_default_route_interface(conf: Dict[str, str]) -> str:
    ip_bin = conf.get("IP_BIN", "ip")
    if not command_exists(ip_bin):
        return ""
    rc, output = run_capture([ip_bin, "-4", "route", "show", "default"], timeout=5)
    if rc != 0:
        return ""
    for line in output.splitlines():
        parts = line.split()
        if "dev" in parts:
            index = parts.index("dev")
            if index + 1 < len(parts):
                return _mdns_iface_name(parts[index + 1])
    return ""


def select_mdns_interface(conf: Dict[str, str]) -> Tuple[str, str]:
    configured = str(conf.get("MDNS_INTERFACE", "auto") or "auto").strip()
    interfaces = _mdns_ipv4_interfaces(conf)
    names = {item["name"] for item in interfaces}

    if configured.lower() not in {"", "auto", "default"}:
        if configured in names:
            return configured, "config system.conf"
        return configured, "config system.conf, interface non vue maintenant"

    if "br0" in names:
        return "br0", "bridge LAN detecte"

    default_iface = _mdns_default_route_interface(conf)
    if default_iface and default_iface in names and not _mdns_iface_is_virtual_or_private(default_iface):
        return default_iface, "route par defaut"

    for item in interfaces:
        name = item["name"]
        if not _mdns_iface_is_virtual_or_private(name):
            return name, "premiere interface LAN IPv4"

    return "", "aucune interface LAN IPv4 exploitable"


def local_ipv4_addresses(conf: Dict[str, str]) -> set:
    return {item["ip"] for item in _mdns_ipv4_interfaces(conf) if item.get("ip")}


def split_entries_for_avahi(conf: Dict[str, str], entries: List[Dict[str, str]]) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    local_ips = local_ipv4_addresses(conf)
    static_entries: List[Dict[str, str]] = []
    alias_entries: List[Dict[str, str]] = []
    for item in entries:
        if item.get("ip") in local_ips:
            alias_entries.append(item)
        else:
            static_entries.append(item)
    return static_entries, alias_entries


def format_avahi_hosts(entries: List[Dict[str, str]]) -> str:
    if not entries:
        return "# Aucun nom statique Avahi pour le moment.\n"

    grouped: Dict[str, List[str]] = {}
    for item in entries:
        ip = item["ip"].strip()
        name = item["name"].strip()
        if not ip or not name:
            continue
        grouped.setdefault(ip, [])
        if name not in grouped[ip]:
            grouped[ip].append(name)

    lines = [f"{ip} {' '.join(names)}" for ip, names in grouped.items() if names]
    return "\n".join(lines).rstrip() + "\n"


def _set_ini_section_value(text: str, section: str, key: str, value: str) -> Tuple[str, bool]:
    lines = (text or "").splitlines()
    section_label = f"[{section}]"
    desired = f"{key}={value}"
    section_index = -1
    section_end = len(lines)
    found_key = -1

    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section_index >= 0:
                section_end = index
                break
            if stripped.lower() == section_label.lower():
                section_index = index

    if section_index < 0:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([section_label, desired])
        return "\n".join(lines).rstrip() + "\n", True

    key_prefix = f"{key.lower()}="
    for index in range(section_index + 1, section_end):
        stripped = lines[index].strip()
        uncommented = stripped.lstrip("#;").strip()
        if uncommented.lower().startswith(key_prefix):
            found_key = index
            break

    if found_key >= 0:
        if lines[found_key].strip() == desired:
            return text if text.endswith("\n") else text + "\n", False
        lines[found_key] = desired
        return "\n".join(lines).rstrip() + "\n", True

    lines.insert(section_index + 1, desired)
    return "\n".join(lines).rstrip() + "\n", True


def ensure_avahi_daemon_config(conf: Dict[str, str]) -> str:
    """Force Avahi sur l'interface LAN, sinon le hostname peut partir sur mv-host."""
    iface, source = select_mdns_interface(conf)
    if not iface:
        return f"Avahi interface mDNS non modifiee : {source}"

    path = conf.get("AVAHI_DAEMON_CONF", "/etc/avahi/avahi-daemon.conf")
    text = local_read_text(path)
    if not text:
        text = "# Fichier Avahi prepare par le module mDNS Flask.\n[server]\n"

    original = text if text.endswith("\n") else text + "\n"
    updated, changed_iface = _set_ini_section_value(original, "server", "allow-interfaces", iface)
    updated, changed_ipv4 = _set_ini_section_value(updated, "server", "use-ipv4", "yes")

    ipv6_enabled = str(conf.get("MDNS_USE_IPV6", "no") or "no").strip().lower() in {"1", "yes", "true", "on"}
    updated, changed_ipv6 = _set_ini_section_value(updated, "server", "use-ipv6", "yes" if ipv6_enabled else "no")
    updated, changed_aaaa = _set_ini_section_value(updated, "publish", "publish-aaaa-on-ipv4", "yes" if ipv6_enabled else "no")
    updated, changed_a_on_v6 = _set_ini_section_value(updated, "publish", "publish-a-on-ipv6", "yes" if ipv6_enabled else "no")
    changed = changed_iface or changed_ipv4 or changed_ipv6 or changed_aaaa or changed_a_on_v6 or updated != original

    if changed and os.path.isfile(path):
        backup = f"{path}.bak.mdns.{time.strftime('%Y%m%d_%H%M%S')}"
        try:
            shutil.copy2(path, backup)
            log_line(conf, f"Backup Avahi daemon conf: {backup}")
        except OSError as exc:
            log_line(conf, f"Backup Avahi daemon conf impossible: {exc}")

    if changed:
        ok, message = local_write_text(path, updated)
        if not ok:
            raise RuntimeError(f"Impossible d'ecrire {path}: {message}")
        log_line(conf, f"OK: Avahi daemon limited to interface {iface}, ipv6={'yes' if ipv6_enabled else 'no'}")

    state = "modifiee" if changed else "deja correcte"
    return f"Avahi interface mDNS {state} : {iface} ({source}), IPv6={'oui' if ipv6_enabled else 'non'}"


def log_line(conf: Dict[str, str], message: str) -> None:
    try:
        log_path = conf.get("SERVICE_LOG", "")
        if not log_path:
            return
        parent = os.path.dirname(log_path.rstrip("/")) or "."
        os.makedirs(parent, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} | {message}\n")
    except Exception:
        pass


def validate_entry(ip: str, name: str) -> Tuple[bool, str]:
    ip = (ip or "").strip()
    name = (name or "").strip()
    if not ip or not name:
        return False, "IP et nom obligatoires."
    try:
        parsed_ip = ipaddress.ip_address(ip)
        if parsed_ip.version != 4:
            return False, "Seules les IPv4 sont acceptées."
    except ValueError:
        return False, "IP invalide."
    if not HOST_RE.fullmatch(name):
        return False, "Nom invalide : attendu nom.local avec lettres/chiffres/tirets."
    return True, ""


def parse_entries_text(text: str) -> Tuple[List[Dict[str, str]], List[str]]:
    entries: List[Dict[str, str]] = []
    errors: List[str] = []
    seen_names = set()

    for index, raw_line in enumerate((text or "").splitlines(), start=1):
        clean = raw_line.split("#", 1)[0].strip()
        if not clean:
            continue
        if "=" not in clean:
            errors.append(f"Ligne {index}: séparateur '=' manquant : {raw_line}")
            continue
        ip, name = clean.split("=", 1)
        ip = ip.strip()
        name = name.strip()
        ok, message = validate_entry(ip, name)
        if not ok:
            errors.append(f"Ligne {index}: {message} ({raw_line})")
            continue
        if name.lower() in seen_names:
            errors.append(f"Ligne {index}: nom en double : {name}")
            continue
        seen_names.add(name.lower())
        entries.append({"ip": ip, "name": name})
    return entries, errors


def create_default_conf(conf: Dict[str, str]) -> None:
    """Crée mdns.conf si absent, mais sans aucune entrée publiée par défaut."""
    path = conf["SERVICE_CONF"]
    if os.path.exists(path):
        log_line(conf, f"OK: existing config kept: {path}")
        return
    try:
        ensure_mdns_conf_file(path)
        log_line(conf, f"OK: empty config created: {path}")
    except Exception as exc:
        raise RuntimeError(f"Impossible de créer {path}: {exc}")


def read_entries(conf: Dict[str, str]) -> Tuple[List[Dict[str, str]], List[str]]:
    path = conf["SERVICE_CONF"]
    if not os.path.exists(path):
        try:
            ensure_mdns_conf_file(path)
            log_line(conf, f"OK: empty config created before read: {path}")
        except Exception as exc:
            return [], [f"Impossible de créer le fichier mDNS : {path} ({exc})"]
    return parse_entries_text(local_read_text(path))


def format_entries(entries: List[Dict[str, str]]) -> str:
    lines = [
        "# mDNS local names",
        "# Format normal: IP=name.local",
        "# Exemple: 192.168.1.2=system.local",
        "",
    ]
    for item in entries:
        lines.append(f"{item['ip']}={item['name']}")
    return "\n".join(lines).rstrip() + "\n"


def normalize_rows(rows) -> Tuple[List[Dict[str, str]], List[str]]:
    entries: List[Dict[str, str]] = []
    errors: List[str] = []
    seen_names = set()

    if not isinstance(rows, list):
        return [], ["Payload invalide : rows doit être une liste."]

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            errors.append(f"Ligne {index}: format invalide.")
            continue
        ip = str(row.get("ip", "")).strip()
        name = str(row.get("name", "")).strip()
        if not ip and not name:
            continue
        ok, message = validate_entry(ip, name)
        if not ok:
            errors.append(f"Ligne {index}: {message}")
            continue
        if name.lower() in seen_names:
            errors.append(f"Ligne {index}: nom en double : {name}")
            continue
        seen_names.add(name.lower())
        entries.append({"ip": ip, "name": name})

    if not entries:
        errors.append("Aucune entrée valide à enregistrer.")
    return entries, errors


def tail_file(path: str, lines: int = 180) -> str:
    try:
        lines = max(20, min(int(lines), 1000))
    except Exception:
        lines = 180
    try:
        if not path or not os.path.exists(path):
            return ""
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            block = 4096
            data = b""
            while size > 0 and data.count(b"\n") <= lines:
                read_size = min(block, size)
                size -= read_size
                handle.seek(size)
                data = handle.read(read_size) + data
            return b"\n".join(data.splitlines()[-lines:]).decode("utf-8", errors="replace")
    except Exception as exc:
        return f"Erreur lecture log : {exc}"


def process_lines(pattern: str, conf: Dict[str, str], full_cmd: bool = False) -> List[str]:
    # pgrep sans -f ne cherche que dans le nom court du processus.
    # Sur Linux ce nom est limité/tronqué à 15 caractères, donc
    # "avahi-publish-address" peut devenir invisible si on ne cherche
    # pas dans la ligne de commande complète.
    args = [conf["PGREP_BIN"], "-af" if full_cmd else "-a", pattern]
    rc, output = run_capture(args, timeout=5)
    lines = [line for line in output.splitlines() if line.strip()] if rc == 0 else []

    if full_cmd and not lines:
        # Secours si pgrep se comporte différemment selon les distributions.
        rc_ps, ps_out = run_capture([conf["PS_BIN"], "-eo", "pid=,args="], timeout=5)
        if rc_ps == 0:
            lines = [line.strip() for line in ps_out.splitlines() if pattern in line]

    if full_cmd:
        # Évite les faux positifs éventuels du pgrep/ps lancé pour la recherche.
        lines = [line for line in lines if "pgrep" not in line and "ps -eo" not in line]

    return lines


def pid_alive(pid: str) -> bool:
    pid = (pid or "").strip()
    if not pid.isdigit():
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def read_pid_lines(path: str) -> List[str]:
    text = local_read_text(path).strip()
    if not text:
        return []
    return [line.strip() for line in text.splitlines() if line.strip().isdigit()]


def read_pid(path: str) -> str:
    lines = read_pid_lines(path)
    return lines[0] if lines else ""


def ps_line_for_pid(conf: Dict[str, str], pid: str) -> str:
    if not pid_alive(pid):
        return ""
    rc, output = run_capture([conf["PS_BIN"], "-p", str(pid), "-o", "pid=,args="], timeout=5)
    if rc == 0 and output.strip():
        return output.strip().splitlines()[-1].strip()
    return f"{pid} <processus actif>"


def active_pid_file_publishers(conf: Dict[str, str]) -> List[str]:
    lines: List[str] = []
    for pid in read_pid_lines(conf["PID_FILE"]):
        detail = ps_line_for_pid(conf, pid)
        if detail:
            lines.append(detail)
    return lines


def configured_publishers(conf: Dict[str, str], entries: List[Dict[str, str]]) -> List[str]:
    # Secours si le fichier PID a été perdu mais que les publications existent encore.
    all_publishers = process_lines("avahi-publish-address", conf, full_cmd=True)
    if not entries:
        return all_publishers

    matched: List[str] = []
    for line in all_publishers:
        for item in entries:
            name = item["name"]
            ip = item["ip"]
            if name in line and ip in line:
                matched.append(line)
                break
    return matched


def publisher_entries_state(publishers: List[str], entries: List[Dict[str, str]]) -> Dict[str, object]:
    matched: List[str] = []
    missing: List[str] = []
    for item in entries:
        name = item["name"]
        ip = item["ip"]
        label = f"{ip} {name}"
        if any(name in line and ip in line for line in publishers):
            matched.append(label)
        else:
            missing.append(label)
    return {
        "ok": len(missing) == 0,
        "matched": matched,
        "missing": missing,
        "matched_count": len(matched),
        "missing_count": len(missing),
    }


def udp_5353_lines(conf: Dict[str, str]) -> List[str]:
    rc, output = run_capture([conf["SS_BIN"], "-lunp"], timeout=5)
    if rc != 0:
        return []
    return [line for line in output.splitlines() if ":5353" in line]


def parse_hosts_file_lines(text: str) -> List[Dict[str, str]]:
    """Lit un fichier au format /etc/avahi/hosts : IP nom.local [alias.local...]."""
    entries: List[Dict[str, str]] = []
    for raw_line in (text or "").splitlines():
        clean = raw_line.split("#", 1)[0].strip()
        if not clean:
            continue
        parts = clean.split()
        if len(parts) < 2:
            continue
        ip = parts[0].strip()
        for name in parts[1:]:
            name = name.strip()
            if ip and name:
                entries.append({"ip": ip, "name": name})
    return entries


def avahi_hosts_state(conf: Dict[str, str], entries: List[Dict[str, str]]) -> Dict[str, object]:
    """Vérifie si les entrées du mdns.conf sont réellement présentes dans /etc/avahi/hosts."""
    path = conf.get("AVAHI_HOSTS", "")
    text = local_read_text(path)
    parsed = parse_hosts_file_lines(text)
    actual = {(item["ip"].strip(), item["name"].strip().lower()) for item in parsed}
    expected = [(item["ip"].strip(), item["name"].strip().lower()) for item in entries]

    matched = []
    missing = []
    for ip, name in expected:
        label = f"{ip} {name}"
        if (ip, name) in actual:
            matched.append(label)
        else:
            missing.append(label)

    display_lines = []
    for item in parsed:
        display_lines.append(f"{item['ip']} {item['name']}")

    return {
        "exists": bool(path and os.path.exists(path)),
        "count": len(parsed),
        "matched_count": len(matched),
        "missing_count": len(missing),
        "ok": bool(expected) and len(missing) == 0,
        "lines": display_lines,
        "matched": matched,
        "missing": missing,
    }


def clear_avahi_hosts(conf: Dict[str, str]) -> str:
    """Arrête les noms personnalisés sans couper avahi-daemon lui-même."""
    output: List[str] = []
    hosts_path = conf.get("AVAHI_HOSTS", "")

    if hosts_path:
        os.makedirs(os.path.dirname(hosts_path) or ".", exist_ok=True)
        if os.path.isfile(hosts_path):
            backup = f"{hosts_path}.bak.stop.{time.strftime('%Y%m%d_%H%M%S')}"
            try:
                shutil.copy2(hosts_path, backup)
                output.append(f"Backup Avahi hosts : {backup}")
            except OSError as exc:
                output.append(f"Backup Avahi impossible : {exc}")

        ok, message = local_write_text(
            hosts_path,
            "# Fichier vidé par le module mDNS Flask.\n"
            "# Les noms personnalisés IP=nom.local sont dans le fichier mdns.conf.\n",
        )
        if ok:
            output.append(f"OK: {hosts_path} vidé")
            log_line(conf, f"OK: {hosts_path} emptied")
        else:
            output.append(f"Impossible de vider {hosts_path}: {message}")

    for key in ("RUNTIME_HOSTS", "PID_FILE", "MDNS_LITE_PID"):
        path = conf.get(key, "")
        if path:
            try:
                os.remove(path)
                output.append(f"Supprimé : {path}")
            except FileNotFoundError:
                pass
            except OSError as exc:
                output.append(f"Impossible de supprimer {path}: {exc}")

    restart_output = restart_avahi_daemon(conf).strip()
    if restart_output:
        output.append(restart_output)
    return "\n".join(part for part in output if part).strip()


def stop_systemd_service(conf: Dict[str, str]) -> str:
    service_name = conf.get("SYSTEMD_SERVICE", "").strip()
    if not service_name or not command_exists(conf.get("SYSTEMCTL_BIN", "systemctl")):
        return ""
    state = systemd_state(conf)
    if state.get("active") != "active":
        return ""
    rc, output = run_capture([conf["SYSTEMCTL_BIN"], "stop", service_name], timeout=30)
    if rc == 0:
        return f"Systemd stoppé : {service_name}"
    return (output or f"Impossible de stopper systemd : {service_name}").strip()


def start_systemd_service_if_enabled(conf: Dict[str, str]) -> str:
    service_name = conf.get("SYSTEMD_SERVICE", "").strip()
    if not service_name or not command_exists(conf.get("SYSTEMCTL_BIN", "systemctl")):
        return ""
    state = systemd_state(conf)
    if state.get("enabled") != "enabled" or state.get("active") == "active":
        return ""
    rc, output = run_capture([conf["SYSTEMCTL_BIN"], "start", service_name], timeout=60)
    if rc == 0:
        return f"Systemd démarré : {service_name}"
    return (output or f"Impossible de démarrer systemd : {service_name}").strip()


def _enable_now_unit(conf: Dict[str, str], service_name: str) -> str:
    service_name = (service_name or "").strip()
    systemctl = conf.get("SYSTEMCTL_BIN", "systemctl")
    if not service_name or not command_exists(systemctl):
        return ""

    state = systemd_unit_state(conf, service_name)
    if state.get("active") == "active" and state.get("enabled") == "enabled":
        return f"Systemd déjà actif et activé au boot : {service_name}"
    if state.get("load") == "not-found":
        return f"Unité systemd introuvable ignorée : {service_name}"

    log_line(conf, f"Enable/start systemd service: systemctl enable --now {service_name}")
    rc, output = run_capture([systemctl, "enable", "--now", service_name], timeout=60)
    if rc == 0:
        return f"Systemd activé au boot et démarré : {service_name}"

    # Fallback : certaines unités peuvent refuser enable --now mais accepter les deux étapes.
    parts = []
    rc_enable, out_enable = run_capture([systemctl, "enable", service_name], timeout=30)
    if rc_enable == 0:
        parts.append(f"Systemd activé au boot : {service_name}")
    elif out_enable.strip():
        parts.append(out_enable.strip())

    rc_start, out_start = run_capture([systemctl, "start", service_name], timeout=60)
    if rc_start == 0:
        parts.append(f"Systemd démarré : {service_name}")
    elif out_start.strip():
        parts.append(out_start.strip())

    if rc_enable == 0 or rc_start == 0:
        return "\n".join(parts).strip()
    return (output or "\n".join(parts) or f"Impossible d'activer/démarrer systemd : {service_name}").strip()


def enable_and_start_systemd_service(conf: Dict[str, str]) -> str:
    """Quand l'UI clique sur Démarrer, le mDNS devient persistant.

    Le vrai service qui publie /etc/avahi/hosts est avahi-daemon.service.
    L'ancienne clé SYSTEMD_SERVICE reste un diagnostic optionnel, mais elle ne
    doit pas masquer l'état réel d'Avahi ni empêcher son activation au boot.
    Stopper reste temporaire : la désactivation se fait depuis Services systemd.
    """
    parts: List[str] = []

    avahi_out = _enable_now_unit(conf, "avahi-daemon.service")
    if avahi_out:
        parts.append(avahi_out)

    configured = conf.get("SYSTEMD_SERVICE", "").strip()
    if configured and configured != "avahi-daemon.service":
        configured_state = systemd_unit_state(conf, configured)
        # Ne force pas l'ancien service mdns-labo s'il n'est pas déjà utilisé :
        # l'état affiché et la persistance passent maintenant par Avahi.
        if configured_state.get("active") == "active" or configured_state.get("enabled") == "enabled":
            configured_out = _enable_now_unit(conf, configured)
            if configured_out:
                parts.append(configured_out)

    return "\n".join(part for part in parts if part).strip()


def systemd_unit_state(conf: Dict[str, str], service_name: str) -> Dict[str, str]:
    """Lit l'état systemd d'une unité précise, sans inventer un état silencieux.

    mDNS publie maintenant via Avahi. Le service réellement important côté
    systemd est donc souvent avahi-daemon.service, même si SYSTEMD_SERVICE garde
    une ancienne valeur optionnelle comme mdns-labo.service.
    """
    service_name = (service_name or "").strip()
    systemctl = conf.get("SYSTEMCTL_BIN", "systemctl")
    if not service_name or not command_exists(systemctl):
        return {"name": service_name, "active": "unknown", "enabled": "unknown", "load": "unknown"}

    rc_show, output = run_capture([
        systemctl,
        "show",
        service_name,
        "--property=LoadState,ActiveState,UnitFileState",
        "--no-page",
    ], timeout=5)
    props: Dict[str, str] = {}
    if rc_show == 0 and output.strip():
        for raw in output.splitlines():
            if "=" in raw:
                key, value = raw.split("=", 1)
                props[key.strip()] = value.strip()

    load = props.get("LoadState") or "unknown"
    active = props.get("ActiveState") or "unknown"
    enabled = props.get("UnitFileState") or "unknown"

    if active in {"activating", "deactivating"}:
        active = active
    elif active != "active":
        active = "inactive" if load not in {"not-found", "masked", "unknown"} else "unknown"

    if enabled in {"enabled", "enabled-runtime", "linked", "linked-runtime"}:
        enabled = "enabled"
    elif enabled in {"disabled", "static", "indirect", "generated", "masked"}:
        # static/généré n'est pas activable comme une unité classique, mais ce n'est
        # pas une erreur : on l'affiche comme non activé au boot pour rester lisible.
        enabled = "disabled" if enabled != "masked" else "masked"
    elif load == "not-found":
        enabled = "unknown"

    return {"name": service_name, "active": active, "enabled": enabled, "load": load}


def systemd_state(conf: Dict[str, str]) -> Dict[str, str]:
    """État de l'unité optionnelle configurée dans system.conf.

    Cette fonction reste volontairement limitée à SYSTEMD_SERVICE pour ne pas
    casser les actions stop/start optionnelles existantes. L'affichage mDNS, lui,
    utilise mdns_display_systemd_state() pour montrer Avahi quand c'est le vrai
    service actif.
    """
    return systemd_unit_state(conf, conf.get("SYSTEMD_SERVICE", ""))


def mdns_display_systemd_state(conf: Dict[str, str]) -> Dict[str, str]:
    """État systemd affiché dans la tuile mDNS.

    On privilégie avahi-daemon.service, car c'est lui qui publie réellement
    /etc/avahi/hosts. L'ancienne clé SYSTEMD_SERVICE reste disponible comme
    diagnostic, mais elle ne doit plus faire afficher un faux "inactive/disabled"
    quand Avahi tourne et est activé au boot.
    """
    avahi_state = systemd_unit_state(conf, "avahi-daemon.service")
    configured_state = systemd_state(conf)

    if avahi_state.get("active") == "active" or avahi_state.get("enabled") == "enabled":
        return avahi_state
    if configured_state.get("active") == "active" or configured_state.get("enabled") == "enabled":
        return configured_state
    if avahi_state.get("load") not in {"not-found", "unknown"}:
        return avahi_state
    return configured_state


def service_summary(conf: Dict[str, str], entries_count: int = 0, errors_count: int = 0) -> Dict[str, object]:
    entries, _ = read_entries(conf) if os.path.exists(conf["SERVICE_CONF"]) else ([], [])
    static_entries, alias_entries = split_entries_for_avahi(conf, entries)
    avahi = process_lines("avahi-daemon", conf)
    pid_publishers = active_pid_file_publishers(conf)
    matched_publishers = configured_publishers(conf, alias_entries)
    publishers = pid_publishers or matched_publishers
    alias_state = publisher_entries_state(publishers, alias_entries)

    lite_pid = read_pid(conf["MDNS_LITE_PID"])
    lite_active = bool(lite_pid and pid_alive(lite_pid))
    udp = udp_5353_lines(conf)
    configured_systemd = systemd_state(conf)
    avahi_systemd = systemd_unit_state(conf, "avahi-daemon.service")
    systemd = mdns_display_systemd_state(conf)
    static_state = avahi_hosts_state(conf, static_entries)
    selected_interface, selected_interface_source = select_mdns_interface(conf)

    avahi_active = bool(avahi)
    udp_active = bool(udp)
    systemd_active = (
        configured_systemd.get("active") == "active"
        or avahi_systemd.get("active") == "active"
    )
    static_hosts_ok = (not static_entries) or bool(static_state.get("ok"))
    alias_publishers_ok = (not alias_entries) or bool(alias_state.get("ok"))
    config_ok = entries_count > 0 and errors_count == 0
    static_publication_active = bool(
        config_ok
        and static_entries
        and avahi_active
        and udp_active
        and static_hosts_ok
    )
    alias_publication_active = bool(
        config_ok
        and alias_entries
        and alias_publishers_ok
    )

    # Correction importante : les entrées sont appliquées dans /etc/avahi/hosts.
    # Dans ce mode, les avahi-publish-address peuvent mourir normalement, car Avahi
    # publie déjà les noms depuis son fichier hosts. L'état ne doit donc plus dépendre
    # uniquement des PID avahi-publish-address.
    service_active = bool(
        lite_active
        or static_publication_active
        or alias_publication_active
    )

    alias_note = ""
    if alias_entries and not alias_publishers_ok:
        alias_note = ", alias locaux a verifier"

    if static_publication_active and systemd_active:
        mode = f"Avahi hosts actif + systemd actif{alias_note}"
    elif static_publication_active:
        mode = f"Avahi hosts actif{alias_note}"
    elif alias_publication_active and systemd_active:
        mode = "alias locaux actifs + systemd actif"
    elif alias_publication_active:
        mode = "alias locaux actifs"
    elif config_ok and systemd_active and avahi_active:
        mode = "systemd actif, configuration a appliquer"
    elif systemd_active and avahi_active:
        mode = "systemd actif, publication à vérifier"
    elif pid_publishers:
        mode = "avahi-publish-address (PID module)"
    elif matched_publishers:
        mode = "avahi-publish-address (détecté)"
    elif lite_active:
        mode = "mdns-lite"
    elif avahi:
        mode = "arrêté (avahi-daemon prêt)"
    else:
        mode = "arrêté"

    publisher_pids = [line.split(None, 1)[0] for line in pid_publishers if line.split(None, 1)[0].isdigit()]

    return {
        "service_active": service_active,
        "mode": mode,
        "entries_count": entries_count,
        "errors_count": errors_count,
        "avahi_count": len(avahi),
        "publisher_count": len(publishers),
        "publisher_pid_count": len(pid_publishers),
        "publisher_detected_count": len(matched_publishers),
        "publisher_pids": publisher_pids,
        "active_publisher_lines": publishers,
        "pid_display": ", ".join(publisher_pids) if publisher_pids else "",
        "mdns_lite_pid": lite_pid,
        "mdns_lite_active": lite_active,
        "udp_5353_count": len(udp),
        "avahi_lines": avahi,
        "publisher_lines": publishers,
        "udp_5353_lines": udp,
        "static_hosts_ok": static_hosts_ok,
        "static_publication_active": static_publication_active,
        "static_hosts_count": int(static_state.get("count", 0) or 0),
        "static_hosts_matched_count": int(static_state.get("matched_count", 0) or 0),
        "static_hosts_missing_count": int(static_state.get("missing_count", 0) or 0),
        "static_host_lines": static_state.get("lines", []),
        "static_hosts_missing": static_state.get("missing", []),
        "static_hosts_expected_count": len(static_entries),
        "alias_entries_count": len(alias_entries),
        "alias_publishers_ok": alias_publishers_ok,
        "alias_publication_active": alias_publication_active,
        "alias_publishers_matched_count": int(alias_state.get("matched_count", 0) or 0),
        "alias_publishers_missing_count": int(alias_state.get("missing_count", 0) or 0),
        "alias_publishers_missing": alias_state.get("missing", []),
        "selected_interface": selected_interface,
        "selected_interface_source": selected_interface_source,
        "systemd": systemd,
        "systemd_configured": configured_systemd,
        "systemd_avahi": avahi_systemd,
        "script_exists": os.path.isfile(conf["SERVICE_SCRIPT"]),
        "conf_exists": os.path.isfile(conf["SERVICE_CONF"]),
        "log_exists": os.path.isfile(conf["SERVICE_LOG"]),
        "config_ok": config_ok,
        "can_start": (not service_active) and config_ok,
        "can_stop": bool(service_active or publishers or lite_active or int(static_state.get("count", 0) or 0) > 0),
        "can_restart": service_active and config_ok,
    }

def write_runtime_hosts(conf: Dict[str, str], entries: List[Dict[str, str]]) -> None:
    lines = [f"{item['ip']} {item['name']}" for item in entries]
    ok, message = local_write_text(conf["RUNTIME_HOSTS"], "\n".join(lines).rstrip() + "\n")
    if not ok:
        raise RuntimeError(message)
    log_line(conf, f"OK: runtime hosts generated: {conf['RUNTIME_HOSTS']}")


def kill_pid(conf: Dict[str, str], pid: str, wait_seconds: float = 1.5) -> None:
    if not pid_alive(pid):
        return
    try:
        log_line(conf, f"Stopping avahi-publish-address PID {pid}")
        os.kill(int(pid), signal.SIGTERM)
    except OSError:
        return

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.1)

    try:
        os.kill(int(pid), signal.SIGKILL)
    except OSError:
        pass


def stop_publishers(conf: Dict[str, str], entries: List[Dict[str, str]] = None) -> str:
    output: List[str] = []
    log_line(conf, "Stop previous avahi-publish-address processes started by this module")

    pid_file = conf["PID_FILE"]
    pids = read_pid_lines(pid_file)
    if pids:
        for pid in pids:
            if pid_alive(pid):
                output.append(f"Stop PID {pid}")
                kill_pid(conf, pid)
            else:
                output.append(f"PID mort ignoré : {pid}")
        try:
            os.remove(pid_file)
        except FileNotFoundError:
            pass
        except OSError as exc:
            output.append(f"Impossible de supprimer {pid_file}: {exc}")
    else:
        output.append("Aucun PID enregistré par le module.")

    if entries is None:
        entries, _ = read_entries(conf)

    # Sécurité : nettoie les publications du mdns.conf même si le fichier PID a disparu.
    if entries and command_exists(conf.get("PKILL_BIN", "pkill")):
        for item in entries:
            pattern = rf"avahi-publish-address.*[[:space:]]{re.escape(item['name'])}[[:space:]]+{re.escape(item['ip'])}"
            rc, pkill_out = run_capture([conf["PKILL_BIN"], "-f", pattern], timeout=5)
            if rc == 0:
                output.append(f"Nettoyage publication détectée : {item['name']} -> {item['ip']}")
            elif pkill_out.strip():
                output.append(pkill_out.strip())

    return "\n".join(output).strip()


def ensure_avahi_available(conf: Dict[str, str]) -> None:
    if not command_exists(conf.get("AVAHI_DAEMON_BIN", "avahi-daemon")):
        raise RuntimeError("avahi-daemon introuvable. Installe : apt install avahi-daemon libnss-mdns")

    if process_lines("avahi-daemon", conf):
        return

    log_line(conf, "Avahi daemon not active yet, trying to start it")
    if command_exists(conf.get("SYSTEMCTL_BIN", "systemctl")):
        run_capture([conf["SYSTEMCTL_BIN"], "enable", "--now", "avahi-daemon"], timeout=30)
    elif command_exists(conf.get("SERVICE_BIN", "service")):
        run_capture([conf["SERVICE_BIN"], "avahi-daemon", "start"], timeout=30)

    time.sleep(1)
    if not process_lines("avahi-daemon", conf):
        raise RuntimeError("avahi-daemon n'est pas actif et n'a pas pu être démarré.")

def restart_avahi_daemon(conf: Dict[str, str]) -> str:
    if command_exists(conf.get("SYSTEMCTL_BIN", "systemctl")):
        log_line(conf, "Restart avahi: systemctl restart avahi-daemon")
        rc, out = run_capture([conf["SYSTEMCTL_BIN"], "restart", "avahi-daemon"], timeout=30)
        return out
    if command_exists(conf.get("SERVICE_BIN", "service")):
        log_line(conf, "Restart avahi: service avahi-daemon restart")
        rc, out = run_capture([conf["SERVICE_BIN"], "avahi-daemon", "restart"], timeout=30)
        return out

    log_line(conf, "No systemctl/service found, try HUP avahi-daemon")
    run_capture([conf["PKILL_BIN"], "-HUP", "avahi-daemon"], timeout=10)
    return ""


def start_no_reverse_publishers(conf: Dict[str, str], entries: List[Dict[str, str]]) -> str:
    if not entries:
        return ""
    publish_bin = conf.get("AVAHI_PUBLISH_BIN", "avahi-publish-address")
    if not command_exists(publish_bin):
        raise RuntimeError("avahi-publish-address introuvable pour les alias locaux.")

    os.makedirs(os.path.dirname(conf["PID_FILE"]) or ".", exist_ok=True)
    output: List[str] = []
    pids: List[str] = []
    for item in entries:
        cmd = [publish_bin, "-R", item["name"], item["ip"]]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            time.sleep(0.2)
            if proc.poll() is None:
                pids.append(str(proc.pid))
                output.append(f"Alias local publie : {item['name']} -> {item['ip']} (PID {proc.pid}, no-reverse)")
                log_line(conf, output[-1])
            else:
                output.append(f"Alias local refuse : {item['name']} -> {item['ip']} (code {proc.returncode})")
        except Exception as exc:
            output.append(f"Alias local impossible : {item['name']} -> {item['ip']} ({exc})")

    if pids:
        ok, message = local_write_text(conf["PID_FILE"], "\n".join(pids).rstrip() + "\n")
        if not ok:
            raise RuntimeError(message)
    return "\n".join(output).strip()


def publish_with_avahi(conf: Dict[str, str], entries: List[Dict[str, str]]) -> str:
    output: List[str] = []
    ensure_avahi_available(conf)
    avahi_conf_output = ensure_avahi_daemon_config(conf).strip()
    if avahi_conf_output:
        output.append(avahi_conf_output)
    static_entries, alias_entries = split_entries_for_avahi(conf, entries)

    os.makedirs(os.path.dirname(conf["AVAHI_HOSTS"]) or ".", exist_ok=True)
    if os.path.isfile(conf["AVAHI_HOSTS"]):
        backup = f"{conf['AVAHI_HOSTS']}.bak.{time.strftime('%Y%m%d_%H%M%S')}"
        try:
            shutil.copy2(conf["AVAHI_HOSTS"], backup)
            output.append(f"Backup Avahi hosts : {backup}")
        except OSError as exc:
            output.append(f"Backup Avahi impossible : {exc}")

    ok, message = local_write_text(conf["AVAHI_HOSTS"], format_avahi_hosts(static_entries))
    if not ok:
        raise RuntimeError(message)
    output.append(f"OK: {conf['AVAHI_HOSTS']} ecrit avec {len(static_entries)} entree(s) statique(s)")
    log_line(conf, output[-1])

    # Plus de avahi-publish-address en double : /etc/avahi/hosts suffit.
    # Les anciens PID éventuels sont supprimés pour éviter un faux état "mort".
    try:
        os.remove(conf["PID_FILE"])
    except FileNotFoundError:
        pass
    except OSError as exc:
        output.append(f"Impossible de supprimer {conf['PID_FILE']}: {exc}")

    avahi_out = restart_avahi_daemon(conf).strip()
    if avahi_out:
        output.append(avahi_out)

    alias_output = start_no_reverse_publishers(conf, alias_entries).strip()
    if alias_output:
        output.append(alias_output)

    time.sleep(1)
    summary = service_summary(conf, len(entries), 0)
    if not summary.get("avahi_count"):
        raise RuntimeError("Avahi a été redémarré mais aucun avahi-daemon actif n'est visible.")
    if not summary.get("udp_5353_count"):
        raise RuntimeError("Avahi tourne peut-être, mais aucun listener UDP 5353 n'est visible.")
    if not summary.get("static_hosts_ok"):
        missing = ", ".join(summary.get("static_hosts_missing", [])) or "entrées inconnues"
        raise RuntimeError(f"/etc/avahi/hosts ne contient pas toutes les entrées attendues : {missing}")

    if not summary.get("alias_publishers_ok"):
        missing = ", ".join(summary.get("alias_publishers_missing", [])) or "alias locaux inconnus"
        raise RuntimeError(f"Les alias locaux ne sont pas tous publies : {missing}")

    output.append("Mode publication : /etc/avahi/hosts + alias locaux no-reverse")
    output.append(f"Entrées statiques Avahi actives : {summary.get('static_hosts_matched_count', 0)}")
    output.append(f"Alias locaux actifs : {summary.get('alias_publishers_matched_count', 0)}")
    output.append("DONE: Avahi setup finished")
    return "\n".join(output).strip()

def local_start(conf: Dict[str, str]) -> Tuple[int, str]:
    try:
        log_line(conf, "------------------------------------------------------------")
        log_line(conf, "Start local mDNS via Flask Python host module")

        create_default_conf(conf)
        entries, errors = read_entries(conf)
        if errors:
            return 400, "\n".join(errors)
        if not entries:
            return 400, "Config vide : aucune entrée mDNS à publier."

        current = service_summary(conf, len(entries), len(errors))
        if current.get("service_active") and current.get("static_hosts_ok"):
            return 0, (
                "mDNS déjà actif : aucun deuxième démarrage lancé.\n"
                f"Mode détecté : {current.get('mode', 'inconnu')}\n"
                "Utilise Redémarrer si tu veux vraiment réappliquer la configuration."
            )

        stop_output = stop_publishers(conf, entries)
        write_runtime_hosts(conf, entries)
        publish_output = publish_with_avahi(conf, entries)
        systemd_output = enable_and_start_systemd_service(conf)

        parts = [part for part in [stop_output, publish_output, systemd_output] if part]
        return 0, "\n".join(parts).strip()
    except PermissionError as exc:
        return 13, f"Permission refusée : {exc}. Lance Flask en hôte/root si tu veux écrire dans /etc/avahi."
    except Exception as exc:
        log_line(conf, f"ERROR start: {exc}")
        return 1, str(exc)


def local_stop(conf: Dict[str, str]) -> Tuple[int, str]:
    try:
        log_line(conf, "------------------------------------------------------------")
        log_line(conf, "Stop local mDNS via Flask Python host module")
        entries, _ = read_entries(conf)
        parts = []
        publishers_output = stop_publishers(conf, entries)
        if publishers_output:
            parts.append(publishers_output)
        static_output = clear_avahi_hosts(conf)
        if static_output:
            parts.append(static_output)
        systemd_output = stop_systemd_service(conf)
        if systemd_output:
            parts.append(systemd_output)
        return 0, "\n".join(parts).strip() or "mDNS arrêté."
    except Exception as exc:
        log_line(conf, f"ERROR stop: {exc}")
        return 1, str(exc)

def local_restart(conf: Dict[str, str]) -> Tuple[int, str]:
    rc_stop, out_stop = local_stop(conf)
    rc_start, out_start = local_start(conf)
    output = "\n\n".join(part for part in [out_stop, out_start] if part).strip()
    return rc_start if rc_start != 0 else rc_stop, output


def local_status(conf: Dict[str, str]) -> Tuple[int, str]:
    entries, errors = read_entries(conf)
    summary = service_summary(conf, len(entries), len(errors))
    systemd = summary.get("systemd", {}) or {}
    lines = [
        "mDNS status",
        f"Config used      : {conf['SERVICE_CONF']}",
        f"Runtime hosts    : {conf['RUNTIME_HOSTS']}",
        f"Avahi hosts      : {conf['AVAHI_HOSTS']}",
        f"Log file         : {conf['SERVICE_LOG']}",
        f"PID file         : {conf['PID_FILE']}",
        f"Mode             : {summary['mode']}",
        f"Service actif    : {'oui' if summary['service_active'] else 'non'}",
        f"Entrées          : {summary['entries_count']}",
        f"Erreurs conf     : {summary['errors_count']}",
        f"Avahi daemon     : {summary['avahi_count']} processus",
        f"Avahi hosts      : {summary.get('static_hosts_matched_count', 0)}/{summary['entries_count']} entrée(s) attendue(s)",
        f"Publishers       : {summary['publisher_count']} processus",
        f"UDP 5353         : {summary['udp_5353_count']} listener(s)",
        f"Systemd          : {systemd.get('active', 'unknown')} / {systemd.get('enabled', 'unknown')}",
    ]
    if summary.get("static_host_lines"):
        lines.append("")
        lines.append("Avahi hosts actifs :")
        lines.extend(str(x) for x in summary["static_host_lines"])
    if summary.get("static_hosts_missing"):
        lines.append("")
        lines.append("Entrées manquantes dans /etc/avahi/hosts :")
        lines.extend(str(x) for x in summary["static_hosts_missing"])
    if summary.get("active_publisher_lines"):
        lines.append("")
        lines.append("Publishers actifs :")
        lines.extend(str(x) for x in summary["active_publisher_lines"])
    if errors:
        lines.append("")
        lines.append("Erreurs :")
        lines.extend(errors)
    return 0, "\n".join(lines)

def run_service_action_shell(conf: Dict[str, str], action: str) -> Tuple[int, str]:
    script = conf["SERVICE_SCRIPT"]
    if not os.path.isfile(script):
        return 404, f"Script introuvable : {script}"
    timeout = int(conf.get("ACTION_TIMEOUT", "90") or "90")
    bash_bin = conf.get("BASH_BIN", "/bin/bash") or "/bin/bash"
    return run_capture([bash_bin, script, action], timeout=timeout)


def run_service_action(conf: Dict[str, str], action: str) -> Tuple[int, str]:
    action = (action or "").strip().lower()
    if action not in VALID_ACTIONS:
        return 400, "Action inconnue."

    if conf.get("EXEC_MODE", "local-python").lower() == "local-sh":
        return run_service_action_shell(conf, action)

    if action == "start":
        return local_start(conf)
    if action == "stop":
        return local_stop(conf)
    if action == "restart":
        return local_restart(conf)
    if action == "status":
        return local_status(conf)
    return 400, "Action inconnue."


def test_local_name(conf: Dict[str, str], name: str) -> Tuple[int, str]:
    name = (name or "").strip()
    if not HOST_RE.fullmatch(name):
        return 400, "Nom invalide : attendu nom.local."
    ping_bin = conf.get("PING_BIN", "ping") or "ping"
    return run_capture([ping_bin, "-c", "1", "-W", "2", name], timeout=8)


@system_bp.route("/mdns")
def mdns_show():
    return render_system_template(active_system_tab="mdns", conf_manager_mode="list")


def mdns_build_template_context() -> Dict[str, Any]:
    conf = get_config()
    entries, errors = read_entries(conf)
    summary = service_summary(conf, len(entries), len(errors))
    log_tail = tail_file(conf["SERVICE_LOG"], int(conf.get("LOG_TAIL_LINES", "180") or "180"))
    return {
        "conf": conf,
        "entries": entries,
        "errors": errors,
        "summary": summary,
        "log_tail": log_tail,
        "config_path": get_config_path(),
    }


def mdns_empty_template_context(error_message: str = "") -> Dict[str, Any]:
    conf = get_config()
    summary = {
        "service_active": False,
        "mode": "erreur",
        "entries_count": 0,
        "avahi_count": 0,
        "static_hosts_matched_count": 0,
        "udp_5353_count": 0,
        "avahi_lines": [],
        "static_host_lines": [],
        "publisher_lines": [],
        "udp_5353_lines": [],
        "systemd": {"active": "unknown", "enabled": "unknown"},
        "config_ok": False,
    }
    return {
        "conf": conf,
        "entries": [],
        "errors": [error_message] if error_message else [],
        "summary": summary,
        "log_tail": error_message or "",
        "config_path": get_config_path(),
    }


@system_bp.route("/system/api/mdns/save", methods=["POST"])
@system_bp.route("/mdns/save", methods=["POST"])
def mdns_save_entries():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    rows, errors = normalize_rows(payload.get("rows", []))
    if errors:
        return jsonify({"ok": False, "message": "\n".join(errors), "errors": errors}), 400

    ok, message = local_write_text(conf["SERVICE_CONF"], format_entries(rows))
    if not ok:
        return jsonify({"ok": False, "message": message}), 500

    entries, parse_errors = read_entries(conf)
    return jsonify({
        "ok": True,
        "message": f"Configuration enregistrée : {len(entries)} entrée(s).",
        "entries": entries,
        "errors": parse_errors,
        "summary": service_summary(conf, len(entries), len(parse_errors)),
    })


@system_bp.route("/system/api/mdns/action", methods=["POST"])
@system_bp.route("/mdns/action", methods=["POST"])
def mdns_action():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    action_name = str(payload.get("action", "")).strip().lower()

    log_line(conf, "------------------------------------------------------------")
    log_line(conf, f"UI action requested: {action_name or 'unknown'}")

    rc, output = run_service_action(conf, action_name)

    if rc == 0:
        log_line(conf, f"UI action finished: {action_name} OK")
    else:
        log_line(conf, f"UI action finished: {action_name} ERROR {rc}: {output}")

    entries, errors = read_entries(conf)
    status = service_summary(conf, len(entries), len(errors))
    log_tail = tail_file(conf["SERVICE_LOG"], int(conf.get("LOG_TAIL_LINES", "180") or "180"))
    display_output = (output or "").strip()
    if log_tail:
        display_output = (display_output + "\n\n--- Dernières lignes du log ---\n" + log_tail).strip()

    return jsonify({
        "ok": rc == 0,
        "returncode": rc,
        "action": action_name,
        "output": output,
        "display_output": display_output,
        "summary": status,
        "log_tail": log_tail,
    }), (200 if rc == 0 else 409 if rc == 409 else 500)


@system_bp.route("/system/api/mdns/status_json")
@system_bp.route("/mdns/status_json")
def mdns_status_json():
    conf = get_config()
    entries, errors = read_entries(conf)
    return jsonify({
        "ok": True,
        "summary": service_summary(conf, len(entries), len(errors)),
        "entries": entries,
        "errors": errors,
        "log_tail": tail_file(conf["SERVICE_LOG"], int(conf.get("LOG_TAIL_LINES", "180") or "180")),
    })


@system_bp.route("/system/api/mdns/log_json")
@system_bp.route("/mdns/log_json")
def mdns_log_json():
    conf = get_config()
    return jsonify({"ok": True, "log_tail": tail_file(conf["SERVICE_LOG"], int(conf.get("LOG_TAIL_LINES", "180") or "180"))})


@system_bp.route("/system/api/mdns/test", methods=["POST"])
@system_bp.route("/mdns/test", methods=["POST"])
def mdns_test_name():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    rc, output = test_local_name(conf, name)
    return jsonify({"ok": rc == 0, "returncode": rc, "name": name, "output": output}), (200 if rc == 0 else 500)
