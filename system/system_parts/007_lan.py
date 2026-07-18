from flask import Blueprint, jsonify, render_template, request

lan_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
lan_CONFIG_CANDIDATES = [
    os.environ.get("LAN_CONFIG_PATH", "").strip(),
    nas_conf_file("lan.conf"),
    os.path.join(lan_BASE_DIR, "conf", "lan.conf"),
    os.path.join(lan_BASE_DIR, "lan.conf"),
]

lan_DEFAULT_CONFIG: Dict[str, str] = {
    "MODULE_TITLE": "Réseau Linux",
    "PLAN_FILE": "../conf/lan_plans.json",
    "BACKUP_DIR": "../conf/lan_backups",
    "LOG_FILE": "/var/log/lan/lan.log",
    "IP_BIN": "ip",
    "DHCLIENT_BIN": "dhclient",
    "SYSTEMCTL_BIN": "systemctl",
    "RESOLV_CONF": "/etc/resolv.conf",
    "DEFAULT_BRIDGE_NAME": "br0",
    "DEFAULT_IPV4_MODE": "copy",
    "DEFAULT_PERSIST_BACKEND": "interfaces",
    "INTERFACES_OUTPUT_FILE": "/etc/network/interfaces.d/zz-flask-lan.conf",
    "NETWORKD_OUTPUT_DIR": "/etc/systemd/network",
    "ALLOW_RUNTIME_APPLY": "1",
    "ALLOW_PERSISTENT_WRITE": "0",
    "ALLOW_INTERFACE_UPDOWN": "1",
    "ALLOW_VIRTUAL_DELETE": "1",
    "ROLLBACK_SECONDS": "90",
    "COMMAND_TIMEOUT": "25",
    "APPLY_TIMEOUT": "90",
    "PING_TARGET": "",
    "SHOW_LOOPBACK": "0",
    "SHOW_DOCKER_INTERFACES": "1",
    "SAFE_NAME_RE": r"^[A-Za-z0-9_.:-]+$",
}

lan_CONFIG_FILE = nas_conf_file("lan.conf")


def lan__strip_quotes(value: str) -> str:
    value = str(value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def lan__read_kv_file(path: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not path or not os.path.exists(path):
        return data
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key:
                    data[key] = lan__strip_quotes(value)
    except OSError:
        pass
    return data


def lan__find_config_path() -> str:
    for candidate in lan_CONFIG_CANDIDATES:
        if candidate and os.path.exists(candidate):
            return candidate
    return nas_conf_file("lan.conf")


def lan__truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "oui", "y"}


def lan__int_conf(conf: Dict[str, str], key: str, default: int) -> int:
    try:
        return int(str(conf.get(key, default)).strip())
    except Exception:
        return default


def lan__resolve_path(conf: Dict[str, str], key: str) -> str:
    value = lan__strip_quotes(conf.get(key, lan_DEFAULT_CONFIG.get(key, ""))).strip()
    if not value:
        return value
    if os.path.isabs(value):
        return value
    config_dir = os.path.dirname(os.path.abspath(conf.get("_config_path", lan_CONFIG_FILE)))
    return os.path.abspath(os.path.join(config_dir, value))


def lan_load_config() -> Dict[str, str]:
    global lan_CONFIG_FILE
    lan_CONFIG_FILE = lan__find_config_path()
    conf = lan_DEFAULT_CONFIG.copy()
    conf.update(lan__read_kv_file(lan_CONFIG_FILE))
    conf["_config_path"] = lan_CONFIG_FILE
    for key in ("PLAN_FILE", "BACKUP_DIR", "LOG_FILE"):
        conf[key] = lan__resolve_path(conf, key)
    if _is_app_log_path(conf.get("LOG_FILE", "")):
        conf["LOG_FILE"] = "/var/log/lan/lan.log"
    return conf


def lan_ensure_parent_dir(path: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)


def lan_append_log(message: str) -> None:
    conf = lan_load_config()
    log_file = conf.get("LOG_FILE", "")
    if not log_file:
        return
    lan_ensure_parent_dir(log_file)
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(log_file, "a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message.rstrip()}\n")
    except OSError:
        pass


def lan_read_log_tail(lines: int = 300) -> str:
    conf = lan_load_config()
    path = conf.get("LOG_FILE", "")
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            data = handle.readlines()
        return "".join(data[-max(1, min(lines, 2000)):])
    except OSError as exc:
        return f"Erreur lecture log: {exc}"


def lan_run_cmd(argv: List[str], timeout: Optional[int] = None) -> Tuple[bool, str, str, int]:
    conf = lan_load_config()
    try:
        proc = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=timeout or lan__int_conf(conf, "COMMAND_TIMEOUT", 25),
        )
        return proc.returncode == 0, proc.stdout or "", proc.stderr or "", proc.returncode
    except FileNotFoundError as exc:
        return False, "", str(exc), 127
    except subprocess.TimeoutExpired as exc:
        return False, exc.stdout or "", exc.stderr or f"Timeout après {timeout}s", 124
    except Exception as exc:
        return False, "", str(exc), 1


def lan_read_json_cmd(argv: List[str], default: Any) -> Any:
    ok, out, err, rc = lan_run_cmd(argv)
    if not ok or not out.strip():
        return default
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        lan_append_log(f"JSON invalide pour {shlex.join(argv)}: {err}")
        return default


def lan_read_sysfs(iface: str, name: str, default: str = "") -> str:
    safe = os.path.basename(iface)
    path = os.path.join("/sys/class/net", safe, name)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return default


def lan_sysfs_link_name(path: str) -> str:
    try:
        if os.path.islink(path):
            return os.path.basename(os.readlink(path))
    except OSError:
        pass
    return ""


def lan_iface_is_virtual(iface: str) -> bool:
    path = os.path.realpath(os.path.join("/sys/class/net", iface))
    return "/devices/virtual/" in path


def lan_iface_master(iface: str) -> str:
    return lan_sysfs_link_name(os.path.join("/sys/class/net", iface, "master"))


def lan_iface_kind_from_link(link: Dict[str, Any]) -> str:
    info = link.get("linkinfo") or {}
    kind = info.get("info_kind") or ""
    if kind:
        return str(kind)
    name = str(link.get("ifname") or "")
    if os.path.exists(os.path.join("/sys/class/net", name, "bridge")):
        return "bridge"
    if name == "lo":
        return "loopback"
    if lan_iface_is_virtual(name):
        return "virtual"
    return "ethernet"


def lan_parse_dns(conf: Dict[str, str]) -> List[str]:
    path = conf.get("RESOLV_CONF", "/etc/resolv.conf")
    out: List[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if line.startswith("nameserver"):
                    parts = line.split()
                    if len(parts) >= 2:
                        out.append(parts[1])
    except OSError:
        pass
    return out


def lan_iface_addresses(link: Dict[str, Any], family: Optional[str] = None) -> List[str]:
    out: List[str] = []
    for item in link.get("addr_info") or []:
        fam = item.get("family")
        if family and fam != family:
            continue
        local = item.get("local")
        prefix = item.get("prefixlen")
        if local is not None and prefix is not None:
            out.append(f"{local}/{prefix}")
    return out


def lan_route_rows_by_iface(routes: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for route in routes:
        dev = route.get("dev")
        if dev:
            out.setdefault(str(dev), []).append(route)
    return out


def lan_default_route(routes: List[Dict[str, Any]]) -> Dict[str, Any]:
    for route in routes:
        if route.get("dst") == "default" or "dst" not in route:
            if route.get("gateway") or route.get("dev"):
                return route
    return {}


def lan_get_snapshot() -> Dict[str, Any]:
    conf = lan_load_config()
    ip_bin = conf.get("IP_BIN", "ip")
    links = lan_read_json_cmd([ip_bin, "-j", "-d", "addr", "show"], [])
    routes = lan_read_json_cmd([ip_bin, "-j", "route", "show"], [])
    route_map = lan_route_rows_by_iface(routes)
    default = lan_default_route(routes)
    rows: List[Dict[str, Any]] = []

    show_loopback = lan__truthy(conf.get("SHOW_LOOPBACK", "0"))
    show_docker = lan__truthy(conf.get("SHOW_DOCKER_INTERFACES", "1"))

    for link in links:
        name = str(link.get("ifname") or "")
        if not name:
            continue
        kind = lan_iface_kind_from_link(link)
        if name == "lo" and not show_loopback:
            continue
        if not show_docker and (name.startswith("docker") or name.startswith("veth") or name.startswith("br-")):
            continue
        mac = lan_read_sysfs(name, "address", link.get("address", "") or "")
        master = lan_iface_master(name)
        operstate = lan_read_sysfs(name, "operstate", str(link.get("operstate", "")))
        speed = lan_read_sysfs(name, "speed", "")
        carrier = lan_read_sysfs(name, "carrier", "")
        row_routes = route_map.get(name, [])
        rows.append({
            "name": name,
            "kind": kind,
            "state": str(link.get("operstate") or operstate or "unknown").lower(),
            "operstate": operstate,
            "flags": link.get("flags") or [],
            "mtu": link.get("mtu") or lan_read_sysfs(name, "mtu", ""),
            "mac": mac,
            "master": master,
            "is_virtual": lan_iface_is_virtual(name),
            "carrier": carrier,
            "speed": speed,
            "ipv4": lan_iface_addresses(link, "inet"),
            "ipv6": lan_iface_addresses(link, "inet6"),
            "routes": row_routes,
            "default_route": bool(default.get("dev") == name),
            "gateway": default.get("gateway") if default.get("dev") == name else "",
            "raw": link,
        })

    rows.sort(key=lambda r: (0 if r["default_route"] else 1, 0 if not r["is_virtual"] else 1, r["name"]))
    return {
        "interfaces": rows,
        "routes": routes,
        "default_route": default,
        "dns": lan_parse_dns(conf),
        "config": {
            "path": conf.get("_config_path", lan_CONFIG_FILE),
            "plan_file": conf.get("PLAN_FILE"),
            "log_file": conf.get("LOG_FILE"),
            "backup_dir": conf.get("BACKUP_DIR"),
        },
        "now": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def lan_detect_defaults(snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    snap = snapshot or lan_get_snapshot()
    conf = lan_load_config()
    default = snap.get("default_route") or {}
    parent = str(default.get("dev") or "")
    gateway = str(default.get("gateway") or "")
    parent_row = next((r for r in snap.get("interfaces", []) if r.get("name") == parent), {})
    ipv4 = ""
    subnet = ""
    netmask = ""
    if parent_row.get("ipv4"):
        ipv4 = parent_row["ipv4"][0]
        try:
            net = ipaddress.ip_interface(ipv4).network
            subnet = str(net)
            netmask = str(net.netmask)
        except Exception:
            pass
    return {
        "parent": parent,
        "parent_mac": parent_row.get("mac", ""),
        "parent_ipv4": ipv4,
        "subnet": subnet,
        "netmask": netmask,
        "gateway": gateway,
        "dns": ",".join(snap.get("dns") or []),
        "bridge_name": conf.get("DEFAULT_BRIDGE_NAME", "br0"),
        "ipv4_mode": conf.get("DEFAULT_IPV4_MODE", "copy"),
    }


def lan_normalize_plan(plan: Dict[str, Any], existing_id: Optional[str] = None) -> Dict[str, Any]:
    defaults = lan_detect_defaults()
    plan_type = str(plan.get("type") or "bridge").strip().lower()
    if plan_type not in {"bridge", "direct", "vlan"}:
        plan_type = "bridge"
    name = str(plan.get("name") or "").strip()
    if not name:
        name = defaults["bridge_name"] if plan_type == "bridge" else defaults.get("parent", "")
    parent = str(plan.get("parent") or "").strip()
    if not parent and plan_type in {"bridge", "vlan"}:
        parent = defaults.get("parent", "")
    ipv4_mode = str(plan.get("ipv4_mode") or defaults.get("ipv4_mode") or "copy").strip().lower()
    if ipv4_mode not in {"copy", "dhcp", "static", "none"}:
        ipv4_mode = "copy"
    return {
        "id": str(plan.get("id") or existing_id or uuid.uuid4().hex[:12]),
        "enabled": bool(plan.get("enabled", True)),
        "name": name,
        "type": plan_type,
        "parent": parent,
        "vlan_id": str(plan.get("vlan_id") or "").strip(),
        "use_parent_mac": bool(plan.get("use_parent_mac", True)),
        "ipv4_mode": ipv4_mode,
        "address": str(plan.get("address") or "").strip(),
        "gateway": str(plan.get("gateway") or "").strip(),
        "dns": str(plan.get("dns") or "").strip(),
        "mtu": str(plan.get("mtu") or "").strip(),
        "autostart": bool(plan.get("autostart", True)),
        "comment": str(plan.get("comment") or "").strip(),
        "updated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def lan_load_plans() -> List[Dict[str, Any]]:
    conf = lan_load_config()
    path = conf.get("PLAN_FILE", "")
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            data = json.load(handle)
        raw = data.get("plans", data if isinstance(data, list) else [])
        if not isinstance(raw, list):
            return []
        return [lan_normalize_plan(item if isinstance(item, dict) else {}) for item in raw]
    except Exception as exc:
        lan_append_log(f"Erreur lecture plans: {exc}")
        return []


def lan_save_plans(plans: List[Dict[str, Any]]) -> None:
    conf = lan_load_config()
    path = conf.get("PLAN_FILE", "")
    lan_ensure_parent_dir(path)
    data = {
        "version": 1,
        "updated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "plans": [lan_normalize_plan(p) for p in plans],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)


def lan_safe_name(value: str) -> bool:
    conf = lan_load_config()
    pattern = conf.get("SAFE_NAME_RE", lan_DEFAULT_CONFIG["SAFE_NAME_RE"])
    return bool(re.match(pattern, value or ""))


def lan_validate_plan(plan: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    name = str(plan.get("name") or "").strip()
    parent = str(plan.get("parent") or "").strip()
    plan_type = plan.get("type")
    if not name:
        errors.append("Nom réseau/interface manquant.")
    elif not lan_safe_name(name):
        errors.append(f"Nom invalide: {name}")
    if plan_type in {"bridge", "vlan"}:
        if not parent:
            errors.append("Parent physique manquant.")
        elif not lan_safe_name(parent):
            errors.append(f"Parent invalide: {parent}")
    if plan_type == "vlan":
        try:
            vlan_id = int(str(plan.get("vlan_id") or ""))
            if vlan_id < 1 or vlan_id > 4094:
                raise ValueError
        except Exception:
            errors.append("VLAN ID invalide, attendu 1-4094.")
    if plan.get("ipv4_mode") == "static":
        try:
            ipaddress.ip_interface(str(plan.get("address") or ""))
        except Exception:
            errors.append("Adresse statique invalide, attendu format 192.168.1.10/24.")
        gateway = str(plan.get("gateway") or "").strip()
        if gateway:
            try:
                ipaddress.ip_address(gateway)
            except Exception:
                errors.append("Passerelle invalide.")
    mtu = str(plan.get("mtu") or "").strip()
    if mtu:
        try:
            mtu_int = int(mtu)
            if mtu_int < 576 or mtu_int > 9216:
                errors.append("MTU hors plage raisonnable, attendu 576-9216.")
        except Exception:
            errors.append("MTU invalide.")
    return errors


def lan_find_iface(snapshot: Dict[str, Any], name: str) -> Dict[str, Any]:
    return next((r for r in snapshot.get("interfaces", []) if r.get("name") == name), {})


def lan_command(argv: List[str], label: str = "", ignore_error: bool = False) -> Dict[str, Any]:
    return {"argv": [str(x) for x in argv], "label": label or shlex.join([str(x) for x in argv]), "ignore_error": ignore_error}


def lan_plan_commands(plan: Dict[str, Any], snapshot: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    conf = lan_load_config()
    snap = snapshot or lan_get_snapshot()
    ip_bin = conf.get("IP_BIN", "ip")
    dhclient = conf.get("DHCLIENT_BIN", "dhclient")
    cmds: List[Dict[str, Any]] = []
    warnings: List[str] = []
    plan = lan_normalize_plan(plan)
    errors = lan_validate_plan(plan)
    if errors:
        return [], errors
    if not plan.get("enabled", True):
        return [], ["Plan désactivé : aucune commande générée."]

    name = plan["name"]
    parent = plan.get("parent", "")
    ptype = plan.get("type", "bridge")
    mode = plan.get("ipv4_mode", "copy")
    parent_row = lan_find_iface(snap, parent)

    if ptype in {"bridge", "vlan"} and not parent_row:
        warnings.append(f"Parent {parent} introuvable dans l'état courant.")

    if ptype == "bridge":
        cmds.append(lan_command([ip_bin, "link", "add", "name", name, "type", "bridge"], f"Créer le bridge {name} si absent", True))
        cmds.append(lan_command([ip_bin, "link", "set", "dev", name, "down"], f"Descendre {name} pour préparer la MAC", True))
        if plan.get("use_parent_mac"):
            mac = parent_row.get("mac", "") if parent_row else ""
            if mac:
                cmds.append(lan_command([ip_bin, "link", "set", "dev", name, "address", mac], f"Reprendre la MAC physique {mac} sur {name}", True))
            else:
                warnings.append("MAC parent introuvable : la commande MAC ne sera pas générée.")
        if plan.get("mtu"):
            cmds.append(lan_command([ip_bin, "link", "set", "dev", parent, "mtu", str(plan["mtu"])], f"MTU {plan['mtu']} sur {parent}", True))
            cmds.append(lan_command([ip_bin, "link", "set", "dev", name, "mtu", str(plan["mtu"])], f"MTU {plan['mtu']} sur {name}", True))
        cmds.append(lan_command([ip_bin, "link", "set", "dev", parent, "down"], f"Descendre temporairement {parent}"))
        cmds.append(lan_command([ip_bin, "addr", "flush", "dev", parent], f"Retirer les IP de {parent}", True))
        cmds.append(lan_command([ip_bin, "link", "set", "dev", parent, "master", name], f"Brancher {parent} dans {name}"))
        cmds.append(lan_command([ip_bin, "link", "set", "dev", parent, "up"], f"Remonter {parent}"))
        cmds.append(lan_command([ip_bin, "link", "set", "dev", name, "up"], f"Remonter {name}"))
        cmds.extend(lan_ipv4_commands(plan, parent_row, snap, target=name, old_dev=parent))

    elif ptype == "direct":
        target = name
        if plan.get("mtu"):
            cmds.append(lan_command([ip_bin, "link", "set", "dev", target, "mtu", str(plan["mtu"])], f"MTU {plan['mtu']} sur {target}", True))
        cmds.append(lan_command([ip_bin, "link", "set", "dev", target, "up"], f"Activer {target}", True))
        cmds.extend(lan_ipv4_commands(plan, lan_find_iface(snap, target), snap, target=target, old_dev=target))

    elif ptype == "vlan":
        vlan_id = str(plan.get("vlan_id"))
        cmds.append(lan_command([ip_bin, "link", "add", "link", parent, "name", name, "type", "vlan", "id", vlan_id], f"Créer VLAN {name} id {vlan_id}", True))
        if plan.get("mtu"):
            cmds.append(lan_command([ip_bin, "link", "set", "dev", name, "mtu", str(plan["mtu"])], f"MTU {plan['mtu']} sur {name}", True))
        cmds.append(lan_command([ip_bin, "link", "set", "dev", name, "up"], f"Activer {name}"))
        cmds.extend(lan_ipv4_commands(plan, {}, snap, target=name, old_dev=name))

    return cmds, warnings


def lan_ipv4_commands(plan: Dict[str, Any], source_row: Dict[str, Any], snapshot: Dict[str, Any], target: str, old_dev: str) -> List[Dict[str, Any]]:
    conf = lan_load_config()
    ip_bin = conf.get("IP_BIN", "ip")
    dhclient = conf.get("DHCLIENT_BIN", "dhclient")
    cmds: List[Dict[str, Any]] = []
    mode = plan.get("ipv4_mode", "copy")
    if mode == "none":
        return cmds
    if mode == "dhcp":
        cmds.append(lan_command([dhclient, "-r", old_dev], f"Libérer DHCP sur {old_dev}", True))
        cmds.append(lan_command([dhclient, target], f"Demander DHCP sur {target}", True))
        return cmds

    default = snapshot.get("default_route") or {}
    gateway = plan.get("gateway") or (default.get("gateway") if default.get("dev") == old_dev else "") or ""

    if mode == "copy":
        addrs = source_row.get("ipv4") or []
        if not addrs:
            return cmds
        cmds.append(lan_command([ip_bin, "addr", "flush", "dev", target], f"Nettoyer les IP de {target}", True))
        for addr in addrs:
            cmds.append(lan_command([ip_bin, "addr", "add", addr, "dev", target], f"Copier {addr} vers {target}", True))
    elif mode == "static":
        addr = str(plan.get("address") or "").strip()
        cmds.append(lan_command([ip_bin, "addr", "flush", "dev", target], f"Nettoyer les IP de {target}", True))
        cmds.append(lan_command([ip_bin, "addr", "add", addr, "dev", target], f"Ajouter {addr} sur {target}"))

    if gateway:
        cmds.append(lan_command([ip_bin, "route", "replace", "default", "via", gateway, "dev", target], f"Route par défaut via {gateway} sur {target}", True))
    return cmds


def lan_persistent_text(plans: List[Dict[str, Any]], backend: str = "interfaces") -> str:
    backend = (backend or "interfaces").strip().lower()
    lines: List[str] = [
        "# Fichier généré par le module Flask System / LAN",
        f"# Généré le {_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "# Vérifie avant reboot si ton OS utilise bien ce backend réseau.",
        "",
    ]
    if backend == "interfaces":
        for raw in plans:
            plan = lan_normalize_plan(raw)
            if not plan.get("enabled", True) or not plan.get("autostart", True):
                continue
            if plan["type"] == "bridge":
                mode = plan.get("ipv4_mode", "copy")
                inet = "dhcp" if mode in {"copy", "dhcp"} else ("manual" if mode == "none" else "static")
                lines.append(f"auto {plan['name']}")
                lines.append(f"iface {plan['name']} inet {inet}")
                if inet == "static":
                    if plan.get("address"):
                        lines.append(f"    address {plan['address']}")
                    if plan.get("gateway"):
                        lines.append(f"    gateway {plan['gateway']}")
                    if plan.get("dns"):
                        lines.append(f"    dns-nameservers {' '.join(lan_split_csv(plan['dns']))}")
                lines.append(f"    bridge_ports {plan['parent']}")
                lines.append("    bridge_stp off")
                lines.append("    bridge_fd 0")
                if plan.get("use_parent_mac"):
                    lines.append("    # MAC conservée côté runtime avec ip link set dev <bridge> address <mac parent>")
                if plan.get("mtu"):
                    lines.append(f"    mtu {plan['mtu']}")
                lines.append("")
            elif plan["type"] == "direct":
                mode = plan.get("ipv4_mode", "dhcp")
                inet = "dhcp" if mode in {"copy", "dhcp"} else ("manual" if mode == "none" else "static")
                lines.append(f"auto {plan['name']}")
                lines.append(f"iface {plan['name']} inet {inet}")
                if inet == "static":
                    if plan.get("address"):
                        lines.append(f"    address {plan['address']}")
                    if plan.get("gateway"):
                        lines.append(f"    gateway {plan['gateway']}")
                if plan.get("mtu"):
                    lines.append(f"    mtu {plan['mtu']}")
                lines.append("")
            elif plan["type"] == "vlan":
                mode = plan.get("ipv4_mode", "dhcp")
                inet = "dhcp" if mode in {"copy", "dhcp"} else ("manual" if mode == "none" else "static")
                lines.append(f"auto {plan['name']}")
                lines.append(f"iface {plan['name']} inet {inet}")
                lines.append(f"    vlan-raw-device {plan['parent']}")
                if inet == "static":
                    if plan.get("address"):
                        lines.append(f"    address {plan['address']}")
                    if plan.get("gateway"):
                        lines.append(f"    gateway {plan['gateway']}")
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    return "# Backend persistant non géré par ce module pour le moment.\n"


def lan_split_csv(value: str) -> List[str]:
    out = []
    for part in re.split(r"[,\s]+", str(value or "")):
        part = part.strip()
        if part:
            out.append(part)
    return out


def lan_selected_plans_from_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    plans = lan_load_plans()
    ids = payload.get("plan_ids")
    if isinstance(ids, list) and ids:
        wanted = {str(x) for x in ids}
        plans = [p for p in plans if p.get("id") in wanted]
    return plans


def lan_make_backup(snapshot: Dict[str, Any], plans: List[Dict[str, Any]], commands: List[Dict[str, Any]]) -> Dict[str, str]:
    conf = lan_load_config()
    backup_dir = conf.get("BACKUP_DIR", "")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.join(backup_dir, stamp)
    files = {
        "snapshot": base + "_snapshot.json",
        "plans": base + "_plans.json",
        "commands": base + "_commands.sh",
        "rollback": base + "_rollback.sh",
        "marker": base + "_rollback.pending",
    }
    with open(files["snapshot"], "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, indent=2, ensure_ascii=False)
    with open(files["plans"], "w", encoding="utf-8") as handle:
        json.dump(plans, handle, indent=2, ensure_ascii=False)
    with open(files["commands"], "w", encoding="utf-8") as handle:
        handle.write("#!/usr/bin/env bash\nset -u\n")
        for cmd in commands:
            line = shlex.join(cmd["argv"])
            if cmd.get("ignore_error"):
                line += " || true"
            handle.write(line + "\n")
    os.chmod(files["commands"], 0o700)
    with open(files["marker"], "w", encoding="utf-8") as handle:
        handle.write("rollback pending\n")
    with open(files["rollback"], "w", encoding="utf-8") as handle:
        handle.write(lan_generate_rollback_script(snapshot, plans, files["marker"]))
    os.chmod(files["rollback"], 0o700)
    return files


def lan_generate_rollback_script(snapshot: Dict[str, Any], plans: List[Dict[str, Any]], marker: str) -> str:
    conf = lan_load_config()
    ip_bin = shlex.quote(conf.get("IP_BIN", "ip"))
    lines = [
        "#!/usr/bin/env bash",
        "set +e",
        f"LOG={shlex.quote(conf.get('LOG_FILE', '/var/log/lan/lan.log'))}",
        "echo \"[$(date '+%F %T')] Rollback LAN automatique demandé\" >> \"$LOG\"",
    ]
    default = snapshot.get("default_route") or {}
    for raw in plans:
        plan = lan_normalize_plan(raw)
        if plan.get("type") != "bridge":
            continue
        bridge = shlex.quote(plan.get("name", ""))
        parent = shlex.quote(plan.get("parent", ""))
        parent_row = lan_find_iface(snapshot, plan.get("parent", ""))
        lines.extend([
            f"{ip_bin} link set dev {bridge} down >> \"$LOG\" 2>&1 || true",
            f"{ip_bin} link set dev {parent} nomaster >> \"$LOG\" 2>&1 || true",
            f"{ip_bin} addr flush dev {bridge} >> \"$LOG\" 2>&1 || true",
            f"{ip_bin} addr flush dev {parent} >> \"$LOG\" 2>&1 || true",
        ])
        for addr in parent_row.get("ipv4") or []:
            lines.append(f"{ip_bin} addr add {shlex.quote(addr)} dev {parent} >> \"$LOG\" 2>&1 || true")
        lines.append(f"{ip_bin} link set dev {parent} up >> \"$LOG\" 2>&1 || true")
        if default.get("dev") == plan.get("parent") and default.get("gateway"):
            lines.append(f"{ip_bin} route replace default via {shlex.quote(str(default['gateway']))} dev {parent} >> \"$LOG\" 2>&1 || true")
        lines.append(f"{ip_bin} link delete {bridge} type bridge >> \"$LOG\" 2>&1 || true")
    lines.extend([
        f"rm -f {shlex.quote(marker)}",
        "echo \"[$(date '+%F %T')] Rollback LAN terminé\" >> \"$LOG\"",
        "exit 0",
        "",
    ])
    return "\n".join(lines)


def lan_schedule_rollback(rollback_script: str, marker: str, seconds: int) -> bool:
    if seconds <= 0:
        return False
    conf = lan_load_config()
    log_file = conf.get("LOG_FILE", "/var/log/lan/lan.log")
    shell = (
        f"sleep {int(seconds)}; "
        f"if [ -f {shlex.quote(marker)} ]; then "
        f"bash {shlex.quote(rollback_script)} >> {shlex.quote(log_file)} 2>&1; "
        "fi"
    )
    try:
        subprocess.Popen(["nohup", "bash", "-c", shell], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        lan_append_log(f"Rollback automatique armé dans {seconds}s : {rollback_script}")
        return True
    except Exception as exc:
        lan_append_log(f"Impossible d'armer le rollback automatique: {exc}")
        return False


def lan_execute_commands(commands: List[Dict[str, Any]], timeout: int) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for cmd in commands:
        argv = cmd.get("argv") or []
        label = cmd.get("label") or shlex.join(argv)
        lan_append_log(f"CMD: {shlex.join(argv)}")
        ok, out, err, rc = lan_run_cmd(argv, timeout=timeout)
        ignored = bool(cmd.get("ignore_error")) and not ok
        status = ok or ignored
        lan_append_log(f"RC={rc} OK={ok} IGNORE={ignored} {label}\n{out}{err}")
        results.append({
            "label": label,
            "command": shlex.join(argv),
            "ok": status,
            "raw_ok": ok,
            "ignored": ignored,
            "rc": rc,
            "stdout": out,
            "stderr": err,
        })
        if not status:
            break
    return results


def lan_write_persistent_config(text: str) -> str:
    conf = lan_load_config()
    path = conf.get("INTERFACES_OUTPUT_FILE", lan_DEFAULT_CONFIG["INTERFACES_OUTPUT_FILE"])
    lan_ensure_parent_dir(path)
    backup = ""
    if os.path.exists(path):
        backup = path + ".bak_" + _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as src, open(backup, "w", encoding="utf-8") as dst:
                dst.write(src.read())
        except OSError:
            backup = ""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    lan_append_log(f"Config persistante écrite: {path} backup={backup}")
    return path


def lan_ping_check() -> Dict[str, Any]:
    conf = lan_load_config()
    target = str(conf.get("PING_TARGET", "")).strip()
    if not target:
        return {"enabled": False}
    ok, out, err, rc = lan_run_cmd(["ping", "-c", "1", "-W", "2", target], timeout=5)
    return {"enabled": True, "target": target, "ok": ok, "stdout": out, "stderr": err, "rc": rc}


@system_bp.route("/lan")
def lan_lan_index():
    # Ancienne route de transition : le LAN est maintenant intégré dans le module Système.
    return system_page()
@system_bp.route("/system/api/lan/status", methods=["GET"])
@system_bp.route("/api/lan/status", methods=["GET"])
def lan_api_lan_status():
    snapshot = lan_get_snapshot()
    return jsonify({"ok": True, "snapshot": snapshot, "defaults": lan_detect_defaults(snapshot), "plans": lan_load_plans()})


@system_bp.route("/system/api/lan/log", methods=["GET"])
@system_bp.route("/api/lan/log", methods=["GET"])
def lan_api_lan_log():
    try:
        lines = int(request.args.get("lines", "300"))
    except Exception:
        lines = 300
    return jsonify({"ok": True, "log": lan_read_log_tail(lines)})


@system_bp.route("/system/api/lan/plans/save", methods=["POST"])
@system_bp.route("/api/lan/plans/save", methods=["POST"])
def lan_api_lan_plans_save():
    payload = request.get_json(silent=True) or {}
    raw_plans = payload.get("plans")
    if not isinstance(raw_plans, list):
        return jsonify({"ok": False, "error": "Payload invalide: plans attendu."}), 400
    normalized = [lan_normalize_plan(p if isinstance(p, dict) else {}) for p in raw_plans]
    all_errors: List[str] = []
    seen = set()
    for plan in normalized:
        if plan["id"] in seen:
            all_errors.append(f"ID plan en double: {plan['id']}")
        seen.add(plan["id"])
        all_errors.extend(lan_validate_plan(plan))
    if all_errors:
        return jsonify({"ok": False, "errors": all_errors}), 400
    lan_save_plans(normalized)
    lan_append_log(f"Plans LAN sauvegardés: {len(normalized)}")
    return jsonify({"ok": True, "plans": lan_load_plans()})


@system_bp.route("/system/api/lan/preview", methods=["POST"])
@system_bp.route("/api/lan/preview", methods=["POST"])
def lan_api_lan_preview():
    payload = request.get_json(silent=True) or {}
    plans = lan_selected_plans_from_payload(payload)
    snapshot = lan_get_snapshot()
    commands: List[Dict[str, Any]] = []
    warnings: List[str] = []
    errors: List[str] = []
    for plan in plans:
        cmds, warns = lan_plan_commands(plan, snapshot)
        if any("invalide" in w.lower() or "manquant" in w.lower() for w in warns):
            errors.extend(warns)
        else:
            warnings.extend(warns)
        commands.extend(cmds)
    text = lan_persistent_text(plans, payload.get("persist_backend") or lan_load_config().get("DEFAULT_PERSIST_BACKEND", "interfaces"))
    return jsonify({
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "commands": commands,
        "shell": "\n".join((shlex.join(c["argv"]) + (" || true" if c.get("ignore_error") else "")) for c in commands),
        "persistent_text": text,
    })


@system_bp.route("/system/api/lan/apply", methods=["POST"])
@system_bp.route("/api/lan/apply", methods=["POST"])
def lan_api_lan_apply():
    conf = lan_load_config()
    if not lan__truthy(conf.get("ALLOW_RUNTIME_APPLY", "1")):
        return jsonify({"ok": False, "error": "ALLOW_RUNTIME_APPLY=0 dans system.conf."}), 403
    payload = request.get_json(silent=True) or {}
    confirm = str(payload.get("confirm") or "").strip().upper()
    if confirm not in {"APPLIQUER", "APPLY", "OUI"}:
        return jsonify({"ok": False, "error": "Confirmation manquante. Tape APPLIQUER."}), 400
    plans = lan_selected_plans_from_payload(payload)
    snapshot = lan_get_snapshot()
    commands: List[Dict[str, Any]] = []
    warnings: List[str] = []
    errors: List[str] = []
    for plan in plans:
        cmds, warns = lan_plan_commands(plan, snapshot)
        plan_errors = [w for w in warns if "invalide" in w.lower() or "manquant" in w.lower()]
        if plan_errors:
            errors.extend(plan_errors)
        else:
            warnings.extend(warns)
            commands.extend(cmds)
    if errors:
        return jsonify({"ok": False, "errors": errors, "warnings": warnings}), 400
    if not commands:
        return jsonify({"ok": False, "error": "Aucune commande à appliquer."}), 400

    backup = lan_make_backup(snapshot, plans, commands)
    rollback_seconds = lan__int_conf(conf, "ROLLBACK_SECONDS", 90)
    rollback_armed = lan_schedule_rollback(backup["rollback"], backup["marker"], rollback_seconds)
    lan_append_log(f"APPLICATION LAN demandée: {len(commands)} commande(s), plans={len(plans)}")
    results = lan_execute_commands(commands, timeout=lan__int_conf(conf, "APPLY_TIMEOUT", 90))
    ok = all(r.get("ok") for r in results)
    persistent_written = ""
    if ok and lan__truthy(payload.get("write_persistent", False)):
        if not lan__truthy(conf.get("ALLOW_PERSISTENT_WRITE", "0")):
            warnings.append("Conf persistante non écrite: ALLOW_PERSISTENT_WRITE=0 dans system.conf.")
        else:
            persistent_written = lan_write_persistent_config(lan_persistent_text(plans, payload.get("persist_backend") or conf.get("DEFAULT_PERSIST_BACKEND", "interfaces")))
    check = lan_ping_check()
    return jsonify({
        "ok": ok,
        "warnings": warnings,
        "results": results,
        "backup": backup,
        "rollback_armed": rollback_armed,
        "rollback_seconds": rollback_seconds,
        "persistent_written": persistent_written,
        "ping_check": check,
        "message": "Appliqué. Si tout fonctionne, annule le rollback automatique." if ok and rollback_armed else "Application terminée.",
    })


@system_bp.route("/system/api/lan/rollback/cancel", methods=["POST"])
@system_bp.route("/api/lan/rollback/cancel", methods=["POST"])
def lan_api_lan_rollback_cancel():
    payload = request.get_json(silent=True) or {}
    marker = str(payload.get("marker") or "").strip()
    if not marker:
        # Annule tous les rollback pending récents du dossier backup.
        conf = lan_load_config()
        backup_dir = conf.get("BACKUP_DIR", "")
        removed = 0
        if backup_dir and os.path.isdir(backup_dir):
            for name in os.listdir(backup_dir):
                if name.endswith("_rollback.pending"):
                    try:
                        os.remove(os.path.join(backup_dir, name))
                        removed += 1
                    except OSError:
                        pass
        lan_append_log(f"Rollback LAN annulé: {removed} marker(s) supprimés")
        return jsonify({"ok": True, "removed": removed})
    if not os.path.exists(marker):
        return jsonify({"ok": True, "removed": 0, "message": "Marker déjà absent."})
    try:
        os.remove(marker)
        lan_append_log(f"Rollback LAN annulé: {marker}")
        return jsonify({"ok": True, "removed": 1})
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@system_bp.route("/system/api/lan/interface/action", methods=["POST"])
@system_bp.route("/api/lan/interface/action", methods=["POST"])
def lan_api_lan_interface_action():
    conf = lan_load_config()
    payload = request.get_json(silent=True) or {}
    iface = str(payload.get("iface") or "").strip()
    action = str(payload.get("action") or "").strip().lower()
    if not iface or not lan_safe_name(iface):
        return jsonify({"ok": False, "error": "Interface invalide."}), 400
    ip_bin = conf.get("IP_BIN", "ip")
    if action in {"up", "down"}:
        if not lan__truthy(conf.get("ALLOW_INTERFACE_UPDOWN", "1")):
            return jsonify({"ok": False, "error": "ALLOW_INTERFACE_UPDOWN=0."}), 403
        ok, out, err, rc = lan_run_cmd([ip_bin, "link", "set", "dev", iface, action], timeout=lan__int_conf(conf, "COMMAND_TIMEOUT", 25))
        lan_append_log(f"Interface {iface} {action}: rc={rc} {out}{err}")
        return jsonify({"ok": ok, "stdout": out, "stderr": err, "rc": rc})
    if action == "delete":
        if not lan__truthy(conf.get("ALLOW_VIRTUAL_DELETE", "1")):
            return jsonify({"ok": False, "error": "ALLOW_VIRTUAL_DELETE=0."}), 403
        if not lan_iface_is_virtual(iface):
            return jsonify({"ok": False, "error": "Suppression refusée: interface non virtuelle."}), 400
        ok, out, err, rc = lan_run_cmd([ip_bin, "link", "delete", iface], timeout=lan__int_conf(conf, "COMMAND_TIMEOUT", 25))
        lan_append_log(f"Interface {iface} delete: rc={rc} {out}{err}")
        return jsonify({"ok": ok, "stdout": out, "stderr": err, "rc": rc})
    return jsonify({"ok": False, "error": "Action inconnue."}), 400

# ---------------------------------------------------------------------------
# Intégration LAN dans system.py :
# Le module LAN lit maintenant system.conf. Les clés LAN_* sont les clés propres
# du nouveau module Système, puis elles sont remappées en interne vers les noms
# historiques attendus par les fonctions LAN.
# ---------------------------------------------------------------------------
LAN_CONFIG_ALIASES = {
    "LAN_MODULE_TITLE": "MODULE_TITLE",
    "LAN_PLAN_FILE": "PLAN_FILE",
    "LAN_BACKUP_DIR": "BACKUP_DIR",
    "LAN_LOG_FILE": "LOG_FILE",
    "LAN_DHCLIENT_BIN": "DHCLIENT_BIN",
    "LAN_DEFAULT_BRIDGE_NAME": "DEFAULT_BRIDGE_NAME",
    "LAN_DEFAULT_IPV4_MODE": "DEFAULT_IPV4_MODE",
    "LAN_DEFAULT_PERSIST_BACKEND": "DEFAULT_PERSIST_BACKEND",
    "LAN_ALLOW_RUNTIME_APPLY": "ALLOW_RUNTIME_APPLY",
    "LAN_ALLOW_PERSISTENT_WRITE": "ALLOW_PERSISTENT_WRITE",
    "LAN_INTERFACES_OUTPUT_FILE": "INTERFACES_OUTPUT_FILE",
    "LAN_NETWORKD_OUTPUT_DIR": "NETWORKD_OUTPUT_DIR",
    "LAN_ALLOW_INTERFACE_UPDOWN": "ALLOW_INTERFACE_UPDOWN",
    "LAN_ALLOW_VIRTUAL_DELETE": "ALLOW_VIRTUAL_DELETE",
    "LAN_ROLLBACK_SECONDS": "ROLLBACK_SECONDS",
    "LAN_COMMAND_TIMEOUT": "COMMAND_TIMEOUT",
    "LAN_APPLY_TIMEOUT": "APPLY_TIMEOUT",
    "LAN_PING_TARGET": "PING_TARGET",
    "LAN_SHOW_LOOPBACK": "SHOW_LOOPBACK",
    "LAN_SHOW_DOCKER_INTERFACES": "SHOW_DOCKER_INTERFACES",
    "LAN_SAFE_NAME_RE": "SAFE_NAME_RE",
}

def lan__find_config_path() -> str:
    # Priorité au system.conf déjà chargé par le module Système.
    if loaded_config and os.path.exists(loaded_config):
        return loaded_config
    for candidate in _build_config_candidates():
        if candidate and os.path.exists(candidate):
            return candidate
    return nas_conf_file("system.conf")

def lan_load_config() -> Dict[str, str]:
    global lan_CONFIG_FILE
    lan_CONFIG_FILE = lan__find_config_path()

    conf = lan_DEFAULT_CONFIG.copy()
    raw = lan__read_kv_file(lan_CONFIG_FILE)

    for key, value in raw.items():
        mapped_key = LAN_CONFIG_ALIASES.get(key, key)

        # Clés partagées non préfixées.
        if key in {"IP_BIN", "SYSTEMCTL_BIN", "RESOLV_CONF"}:
            mapped_key = key

        if mapped_key in conf:
            conf[mapped_key] = value

    conf["_config_path"] = lan_CONFIG_FILE

    # Valeurs affichées dans l'onglet LAN > Info : on montre ce qui est déclaré
    # dans system.conf, sans transformer ../conf en /dockers/conf.
    display_paths = {
        "PLAN_FILE": str(conf.get("PLAN_FILE", "")).strip(),
        "BACKUP_DIR": str(conf.get("BACKUP_DIR", "")).strip(),
        "LOG_FILE": str(conf.get("LOG_FILE", "")).strip(),
    }

    # Migration douce : si un ancien conf contient encore ../logs ou /dockers/logs,
    # on force le runtime et l'affichage sur le chemin Linux standard.
    if _is_app_log_path(display_paths.get("LOG_FILE", "")):
        conf["LOG_FILE"] = "/var/log/lan/lan.log"
        display_paths["LOG_FILE"] = "/var/log/lan/lan.log"

    # Chemins runtime absolus, utilisés par le moteur LAN.
    for key in ("PLAN_FILE", "BACKUP_DIR", "LOG_FILE"):
        conf[key] = lan__resolve_path(conf, key)

    # Chemins affichés, non modifiables, fidèles au conf initial.
    conf["PLAN_FILE_DISPLAY"] = display_paths["PLAN_FILE"]
    conf["BACKUP_DIR_DISPLAY"] = display_paths["BACKUP_DIR"]
    conf["LOG_FILE_DISPLAY"] = display_paths["LOG_FILE"]

    return conf

# --------------------------------------------------
# MDNS INTEGRE AU MODULE SYSTEME
# Ancien module mdns.py fusionné ici : aucune dépendance à mdns.py/html/conf.
# --------------------------------------------------
# Dossier réel où se trouve CE fichier Python.
# Tous les chemins relatifs de system.conf sont résolus depuis ce dossier,
# comme un script shell qui ferait SCRIPT_DIR="$(dirname "$0")".
