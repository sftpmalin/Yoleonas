def system_normalize_subtab(subtab: str) -> str:
    """Sous-onglets internes de l'onglet Système host."""
    value = str(subtab or "main").strip().lower().replace("-", "_")
    if value in {"install", "installation", "installer"}:
        return "install"
    if value in {"updates", "update", "maj", "mise_a_jour"}:
        return "updates"
    if value in {"troubleshooting", "depannage", "dépannage", "reset", "defaults", "defaut", "défaut"}:
        return "troubleshooting"
    if value in {"logs", "log", "sorties", "output"}:
        return "logs"
    return "main"

def build_system_template_context(active_system_tab: str = "monitor", conf_manager_mode: str = "list", **extra_context: Any):
    lan_snapshot = lan_get_snapshot()
    try:
        mdns_context = mdns_build_template_context()
    except Exception as exc:
        mdns_context = mdns_empty_template_context(str(exc))
    context = dict(
        sys_info=_system_sys_info(),
        refresh_seconds=max(3, REFRESH_SECONDS),
        process_refresh_seconds=max(2, PROCESS_REFRESH_SECONDS),
        system_conf=loaded_config or "",
        default_log_unit=SYSTEM_LOG_DEFAULT_UNIT,
        mdns_conf=mdns_context.get("conf", {}),
        mdns_entries=mdns_context.get("entries", []),
        mdns_errors=mdns_context.get("errors", []),
        mdns_summary=mdns_context.get("summary", {}),
        mdns_log_tail=mdns_context.get("log_tail", ""),
        mdns_config_path=mdns_context.get("config_path", ""),
        conf=lan_load_config(),
        snapshot=lan_snapshot,
        interfaces=lan_snapshot.get("interfaces", []),
        plans=lan_load_plans(),
        defaults=lan_detect_defaults(lan_snapshot),
        log_tail=lan_read_log_tail(120),
        active_system_tab=active_system_tab,
        active_system_subtab=system_normalize_subtab(request.args.get("subtab", "main")),
        active_mdns_subtab=mdns_normalize_subtab(request.args.get("subtab", "dashboard")),
        active_lan_subtab=lan_normalize_subtab(request.args.get("subtab", "overview")),
        conf_manager_mode=conf_manager_mode,
        conf_manager_sources=conf_manager_get_sources(),
        conf_grouped_configs=conf_manager_get_files_grouped(),
        conf_edit_config={},
        conf_file_mode="",
        conf_filepath="",
        conf_filename="",
        path_rescue=get_conf_str("path_rescue", ""),
    )
    context.update(extra_context)

    # Alias conserves pour le contexte commun des templates Systeme.
    # Attention : la personnalisation utilise aussi une variable nommée
    # "config" (titre_tab, titre_logo, nav_icons). Le gestionnaire de conf
    # utilise "config" pour le contenu du fichier ouvert. On évite donc
    # d'écraser la personnalisation avec conf_edit_config.
    context["mode"] = context.get("conf_manager_mode", "list")
    context["grouped_configs"] = context.get("conf_grouped_configs", {})

    if context.get("active_system_tab") == "personalization":
        context["config"] = context.get("personalization_config") or context.get("config") or personalization_get_config()
        context.setdefault("menu_items", system_menu_load_items())
        context.setdefault("home_config", home_config_load())
        context.setdefault("home_config_path", home_config_get_path())
        context.setdefault("home_config_labels", HOME_CONFIG_LABELS)
        context.setdefault("home_ssh_config", home_ssh_config_load())
    else:
        context["config"] = context.get("conf_edit_config", {})

    context["file_mode"] = context.get("conf_file_mode", "")
    context["filepath"] = context.get("conf_filepath", "")
    context["filename"] = context.get("conf_filename", "")

    return context


SYSTEM_TAB_TEMPLATE_MAP = {
    "monitor": "system_info.html",
    "info": "system_info.html",
    "services": "system_services.html",
    "processes": "system_processus.html",
    "processus": "system_processus.html",
    "mdns": "system_mdns.html",
    "lan": "system_lan.html",
    "nftables": "system_nftables.html",
    "firewall": "system_nftables.html",
    "parefeu": "system_nftables.html",
    "logs": "system_logs.html",
    "confmgr": "system_gestionnaire_conf.html",
    "conf": "system_gestionnaire_conf.html",
    "gestionnaire_conf": "system_gestionnaire_conf.html",
    "admin": "system_system.html",
    "personalization": "system_personnalisation.html",
    "personnalisation": "system_personnalisation.html",
}

SYSTEM_TAB_CANONICAL = {
    "info": "monitor",
    "processus": "processes",
    "conf": "confmgr",
    "gestionnaire-conf": "confmgr",
    "gestionnaire_conf": "confmgr",
    "firewall": "nftables",
    "pare-feu": "nftables",
    "parefeu": "nftables",
}


def _normalize_system_tab(tab_name: str) -> str:
    requested = (tab_name or "monitor").strip().lower()
    requested = SYSTEM_TAB_CANONICAL.get(requested, requested)
    allowed_tabs = {
        "monitor",
        "services",
        "processes",
        "logs",
        "lan",
        "nftables",
        "mdns",
        "confmgr",
        "admin",
        "personalization",
    }
    if requested in {"personnalisation", "personalisation"}:
        requested = "personalization"
    if requested not in allowed_tabs:
        requested = "monitor"
    return requested


def _system_template_for_tab(tab_name: str) -> str:
    return SYSTEM_TAB_TEMPLATE_MAP.get(_normalize_system_tab(tab_name), "system_info.html")


def render_system_template(
    active_system_tab: str = "monitor",
    conf_manager_mode: str = "list",
    template_name: Optional[str] = None,
    **extra_context: Any,
):
    context = build_system_template_context(active_system_tab=active_system_tab, conf_manager_mode=conf_manager_mode, **extra_context)
    return render_template(template_name or _system_template_for_tab(active_system_tab), **context)


def render_system_tab_template(
    tab_name: str,
    conf_manager_mode: str = "list",
    template_name: Optional[str] = None,
    **extra_context: Any,
):
    requested = _normalize_system_tab(tab_name)
    return render_system_template(
        active_system_tab=requested,
        conf_manager_mode=conf_manager_mode,
        template_name=template_name,
        **extra_context,
    )


# ==========================================================
# Pare-feu Linux simple : nftables
# ==========================================================
FIREWALL_CONF_FILE = nas_conf_file("firewall.conf")
FIREWALL_LEGACY_INI_FILE = nas_conf_file("firewall.ini")
# Logs applicatifs Yoleo : chemin Linux standard. Ne plus écrire dans /yoleo/logs.
FIREWALL_LEGACY_LOG_FILE = nas_root_path("logs", "system_firewall.log")
FIREWALL_LOG_FILE = os.path.abspath(os.path.expanduser(os.path.expandvars(
    os.environ.get("YOLEO_FIREWALL_LOG", "/var/log/yoleo/system/firewall.log")
)))
FIREWALL_NFT_DIR = "/etc/nftables.d"
FIREWALL_NFT_FILE = os.path.join(FIREWALL_NFT_DIR, "yoleo.conf")
FIREWALL_NFT_MAIN = "/etc/nftables.conf"
FIREWALL_TABLE_NAME = "yoleo_filter"
FIREWALL_SERVICE = "nftables.service"
FIREWALL_COMMAND_TIMEOUT = 20


def firewall_migrate_legacy_ini() -> None:
    """Renomme l'ancien ../conf/firewall.ini en ../conf/firewall.conf si besoin."""
    if FIREWALL_CONF_FILE == FIREWALL_LEGACY_INI_FILE:
        return
    if os.path.exists(FIREWALL_CONF_FILE) or not os.path.exists(FIREWALL_LEGACY_INI_FILE):
        return
    try:
        os.makedirs(os.path.dirname(FIREWALL_CONF_FILE), exist_ok=True)
        os.replace(FIREWALL_LEGACY_INI_FILE, FIREWALL_CONF_FILE)
    except Exception as exc:
        firewall_log(f"Migration firewall.ini -> firewall.conf impossible : {exc}")


_FIREWALL_LEGACY_LOG_MIGRATED = False


def firewall_migrate_legacy_log_once() -> None:
    """Déplace l'ancien log /yoleo/logs vers /var/log/yoleo sans recréer /yoleo/logs."""
    global _FIREWALL_LEGACY_LOG_MIGRATED
    if _FIREWALL_LEGACY_LOG_MIGRATED:
        return
    _FIREWALL_LEGACY_LOG_MIGRATED = True
    try:
        legacy = os.path.abspath(os.path.expanduser(os.path.expandvars(str(FIREWALL_LEGACY_LOG_FILE))))
        current = os.path.abspath(os.path.expanduser(os.path.expandvars(str(FIREWALL_LOG_FILE))))
        if legacy == current or not os.path.exists(legacy):
            return
        os.makedirs(os.path.dirname(current), exist_ok=True)
        if not os.path.exists(current):
            os.replace(legacy, current)
            return
        with open(legacy, "r", encoding="utf-8", errors="replace") as src, open(current, "a", encoding="utf-8") as dst:
            dst.write("\n# --- Ancien log firewall migré depuis %s ---\n" % legacy)
            dst.write(src.read())
        os.remove(legacy)
    except Exception:
        pass


def firewall_log(message: str) -> None:
    try:
        firewall_migrate_legacy_log_once()
        os.makedirs(os.path.dirname(FIREWALL_LOG_FILE), exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(FIREWALL_LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


def firewall_run(cmd: List[str], timeout: int = FIREWALL_COMMAND_TIMEOUT) -> Tuple[int, str]:
    try:
        completed = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
        out = ((completed.stdout or "") + (completed.stderr or "")).strip()
        firewall_log("$ " + " ".join(shlex.quote(str(part)) for part in cmd))
        if out:
            firewall_log(out)
        return completed.returncode, out
    except FileNotFoundError:
        msg = f"Commande introuvable : {cmd[0]}"
        firewall_log(msg)
        return 127, msg
    except subprocess.TimeoutExpired:
        msg = f"Timeout commande : {' '.join(cmd)}"
        firewall_log(msg)
        return 124, msg
    except Exception as exc:
        msg = str(exc)
        firewall_log(msg)
        return 1, msg


def firewall_read_kv_file(path: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                data[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:
        pass
    return data


def firewall_safe_int(value: Any, default: int = 0, minimum: int = 0, maximum: int = 65535) -> int:
    try:
        number = int(str(value).strip())
    except Exception:
        return default
    if number < minimum:
        return minimum
    if maximum and number > maximum:
        return maximum
    return number


def firewall_add_port_value(values: set, value: Any) -> None:
    port = firewall_safe_int(value, 0, 1, 65535)
    if port:
        values.add(port)


def firewall_current_request_ports() -> List[int]:
    ports = set()
    try:
        host = request.host or ""
        if ":" in host and not host.endswith("]"):
            firewall_add_port_value(ports, host.rsplit(":", 1)[1])
        firewall_add_port_value(ports, request.environ.get("SERVER_PORT", ""))
    except Exception:
        pass
    return sorted(ports)


def firewall_detect_yoleo_ports() -> List[int]:
    ports = set(firewall_current_request_ports())

    env_values = firewall_read_kv_file(nas_conf_file(".env"))
    if not env_values:
        env_values = firewall_read_kv_file(nas_conf_file("flask_system.env"))
    firewall_add_port_value(ports, env_values.get("PORT"))

    try:
        script = os.path.join(_NAS_MODULE_DIR, "system.sh")
        text = open(script, "r", encoding="utf-8", errors="ignore").read()
        match = re.search(r'PORT="\$\{PORT:-([0-9]+)\}"', text)
        if match:
            firewall_add_port_value(ports, match.group(1))
    except Exception:
        pass

    if not ports:
        ports.add(12345)
    return sorted(ports)


def firewall_detect_terminal_ports() -> List[int]:
    ports = set()
    conf = firewall_read_kv_file(nas_conf_file("terminal.conf"))
    firewall_add_port_value(ports, conf.get("TERMINAL_PORT") or "7681")
    return sorted(ports)


def firewall_detect_minidlna_ports() -> List[int]:
    ports = set()
    for name in ("minidnla.conf", "minidlna.conf", "services.conf"):
        conf = firewall_read_kv_file(nas_conf_file(name))
        firewall_add_port_value(ports, conf.get("PORT") or conf.get("MINIDLNA_PORT"))
    if not ports:
        ports.add(8200)
    return sorted(ports)


def firewall_detect_registry_ports() -> List[int]:
    ports = set()
    for name in ("builds.conf", "build.conf", "registry.conf"):
        conf = firewall_read_kv_file(nas_conf_file(name))
        firewall_add_port_value(ports, conf.get("PORT") or conf.get("REGISTRY_PORT"))
    if not ports:
        ports.add(7777)
    return sorted(ports)


def firewall_make_rule(start: int, end: int, proto: str, label: str, auto: bool = False) -> Dict[str, Any]:
    start = firewall_safe_int(start, 0, 1, 65535)
    end = firewall_safe_int(end, start, 1, 65535)
    if end < start:
        start, end = end, start
    proto = (proto or "tcp").strip().lower()
    if proto not in {"tcp", "udp", "both"}:
        proto = "tcp"
    return {
        "id": uuid.uuid4().hex[:10],
        "start": start,
        "end": end,
        "proto": proto,
        "label": (label or "Port autorisé").strip(),
        "auto": bool(auto),
    }


def firewall_default_rules() -> List[Dict[str, Any]]:
    rules: List[Dict[str, Any]] = []

    def add(start: int, end: Optional[int] = None, proto: str = "tcp", label: str = "", auto: bool = True) -> None:
        rule = firewall_make_rule(start, end if end is not None else start, proto, label, auto)
        key = (rule["start"], rule["end"], rule["proto"], rule["label"])
        if key not in {(r["start"], r["end"], r["proto"], r["label"]) for r in rules}:
            rules.append(rule)

    add(22, proto="tcp", label="SSH / SFTP")
    add(80, proto="tcp", label="HTTP")
    add(443, proto="tcp", label="HTTPS")

    for port in firewall_detect_yoleo_ports():
        add(port, proto="tcp", label="Interface Yoleo / Flask")

    for port in firewall_detect_terminal_ports():
        add(port, proto="tcp", label="Terminal ttyd")

    add(7780, 7819, proto="tcp", label="Docker ttyd logs/exec")
    add(5900, 5999, proto="tcp", label="VM video VNC/SPICE")
    add(6080, 6179, proto="tcp", label="VM console noVNC / websockify")
    add(7820, 7859, proto="tcp", label="VM serial ttyd / virsh console")

    add(137, proto="udp", label="Samba NetBIOS name")
    add(138, proto="udp", label="Samba NetBIOS datagram")
    add(139, proto="tcp", label="Samba NetBIOS session")
    add(445, proto="tcp", label="Samba SMB")
    add(3702, proto="udp", label="WSDD Windows discovery")
    add(3702, proto="tcp", label="WSDD Windows metadata")
    add(5357, proto="tcp", label="WSDD Windows metadata")
    add(5355, proto="udp", label="LLMNR Windows name discovery")
    add(5355, proto="tcp", label="LLMNR Windows name discovery")

    add(5353, proto="udp", label="mDNS / Avahi")

    add(111, proto="both", label="rpcbind / NFS")
    add(2049, proto="both", label="NFS")

    for port in firewall_detect_minidlna_ports():
        add(port, proto="tcp", label="MiniDLNA")
    add(1900, proto="udp", label="SSDP / DLNA")

    add(21, proto="tcp", label="FTP / ProFTPD")
    add(30000, 30100, proto="tcp", label="FTP passif ProFTPD")
    add(9090, proto="tcp", label="Cockpit")
    add(10000, proto="tcp", label="Webmin")

    for port in firewall_detect_registry_ports():
        add(port, proto="tcp", label="Registry Docker interne")

    return rules


def firewall_ensure_safety_rules(data: Dict[str, Any]) -> bool:
    """Garde-fou anti-coupure : SSH + port HTTP courant restent autorisés."""
    rules = data.setdefault("rules", [])
    existing = {(int(rule.get("start", 0)), int(rule.get("end", 0)), str(rule.get("proto") or "tcp")) for rule in rules}
    changed = False

    def ensure(port: int, proto: str, label: str) -> None:
        nonlocal changed
        rule = firewall_make_rule(port, port, proto, label, True)
        key = (rule["start"], rule["end"], rule["proto"])
        if key not in existing:
            rules.append(rule)
            existing.add(key)
            changed = True

    ensure(22, "tcp", "SSH / SFTP")
    ensure(3702, "udp", "WSDD Windows discovery")
    ensure(5357, "tcp", "WSDD Windows metadata")
    ensure(5355, "udp", "LLMNR Windows name discovery")
    for port in firewall_detect_yoleo_ports():
        ensure(port, "tcp", "Interface Yoleo / Flask")
    return changed


def firewall_load_config(create: bool = True) -> Dict[str, Any]:
    firewall_migrate_legacy_ini()
    if not os.path.exists(FIREWALL_CONF_FILE) and create:
        data = {"enabled": False, "rules": firewall_default_rules()}
        firewall_save_config(data)
        return data

    parser = configparser.ConfigParser()
    parser.optionxform = str
    try:
        parser.read(FIREWALL_CONF_FILE, encoding="utf-8")
    except Exception:
        pass

    enabled = parser.getboolean("GENERAL", "enabled", fallback=False)
    rules: List[Dict[str, Any]] = []
    if parser.has_section("RULES"):
        for key in sorted(parser["RULES"].keys(), key=lambda value: firewall_safe_int(value, 0, 0, 9999)):
            raw = parser["RULES"].get(key, "")
            parts = raw.split("|", 4)
            if len(parts) < 4:
                continue
            start = firewall_safe_int(parts[0], 0, 1, 65535)
            end = firewall_safe_int(parts[1], start, 1, 65535)
            proto = parts[2].strip().lower()
            label = parts[3].strip() or "Port autorisé"
            auto = len(parts) >= 5 and parts[4].strip().lower() in {"1", "true", "yes", "on", "auto"}
            if start:
                rule = firewall_make_rule(start, end, proto, label, auto)
                rule["id"] = key
                rules.append(rule)
    if not rules and create:
        rules = firewall_default_rules()
        enabled = False
        firewall_save_config({"enabled": enabled, "rules": rules})
    return {"enabled": enabled, "rules": rules}


def firewall_save_config(data: Dict[str, Any]) -> None:
    firewall_migrate_legacy_ini()
    os.makedirs(os.path.dirname(FIREWALL_CONF_FILE), exist_ok=True)
    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser["GENERAL"] = {
        "enabled": "1" if data.get("enabled") else "0",
        "policy_in": "drop",
        "policy_out": "accept",
    }
    parser["RULES"] = {}
    for idx, rule in enumerate(data.get("rules") or [], 1):
        rid = str(rule.get("id") or uuid.uuid4().hex[:10])
        value = "|".join([
            str(firewall_safe_int(rule.get("start"), 0, 1, 65535)),
            str(firewall_safe_int(rule.get("end"), firewall_safe_int(rule.get("start"), 0), 1, 65535)),
            str(rule.get("proto") or "tcp"),
            str(rule.get("label") or "Port autorisé").replace("|", "/"),
            "1" if rule.get("auto") else "0",
        ])
        parser["RULES"][rid or str(idx)] = value
    with open(FIREWALL_CONF_FILE, "w", encoding="utf-8") as handle:
        parser.write(handle)
    firewall_log(f"Configuration enregistrée : {FIREWALL_CONF_FILE}")


def firewall_nft_port_expr(rule: Dict[str, Any]) -> str:
    start = firewall_safe_int(rule.get("start"), 0, 1, 65535)
    end = firewall_safe_int(rule.get("end"), start, 1, 65535)
    return str(start) if start == end else f"{start}-{end}"


def firewall_render_nft_rules(rules: List[Dict[str, Any]]) -> str:
    lines = [
        "# Fichier généré par Yoleo - Système > nftables",
        "# Ne pas modifier à la main : utilise ../conf/firewall.conf ou l'interface.",
        "",
        f"table inet {FIREWALL_TABLE_NAME} {{",
        "  chain input {",
        "    type filter hook input priority 0; policy drop;",
        "",
        "    iif lo accept",
        "    ct state established,related accept",
        "    meta l4proto icmp accept",
        "    meta l4proto ipv6-icmp accept",
        "",
    ]
    for rule in rules:
        expr = firewall_nft_port_expr(rule)
        proto = str(rule.get("proto") or "tcp").lower()
        label = str(rule.get("label") or "Port autorisé").replace("\n", " ").replace("\r", " ")[:80]
        if proto in {"tcp", "both"}:
            lines.append(f"    tcp dport {expr} accept comment \"{label}\"")
        if proto in {"udp", "both"}:
            lines.append(f"    udp dport {expr} accept comment \"{label}\"")
    lines += [
        "  }",
        "",
        "  chain forward {",
        "    type filter hook forward priority 0; policy accept;",
        "  }",
        "",
        "  chain output {",
        "    type filter hook output priority 0; policy accept;",
        "  }",
        "}",
        "",
    ]
    return "\n".join(lines)


def firewall_write_nft_files(rules: List[Dict[str, Any]]) -> Tuple[bool, str]:
    if os.geteuid() != 0:
        return False, "Action refusée : il faut être root pour écrire /etc/nftables.d et piloter nftables."
    try:
        os.makedirs(FIREWALL_NFT_DIR, exist_ok=True)
        with open(FIREWALL_NFT_FILE, "w", encoding="utf-8") as handle:
            handle.write(firewall_render_nft_rules(rules))

        include_line = f'include "{FIREWALL_NFT_FILE}"'
        if not os.path.exists(FIREWALL_NFT_MAIN):
            with open(FIREWALL_NFT_MAIN, "w", encoding="utf-8") as handle:
                handle.write("#!/usr/sbin/nft -f\nflush ruleset\n" + include_line + "\n")
        else:
            text = open(FIREWALL_NFT_MAIN, "r", encoding="utf-8", errors="ignore").read()
            if FIREWALL_NFT_FILE not in text and "/etc/nftables.d/*.conf" not in text:
                with open(FIREWALL_NFT_MAIN, "a", encoding="utf-8") as handle:
                    if text and not text.endswith("\n"):
                        handle.write("\n")
                    handle.write("\n# Yoleo firewall\n" + include_line + "\n")
        rc, out = firewall_run(["nft", "-c", "-f", FIREWALL_NFT_MAIN])
        if rc != 0:
            return False, out or "Validation nftables échouée."
        return True, f"Règles écrites dans {FIREWALL_NFT_FILE}."
    except Exception as exc:
        return False, str(exc)


def firewall_apply_rules(start_service: bool = False) -> Tuple[bool, str]:
    data = firewall_load_config(create=True)
    if firewall_ensure_safety_rules(data):
        firewall_save_config(data)
    ok, msg = firewall_write_nft_files(data.get("rules") or [])
    if not ok:
        return False, msg
    outputs = [msg]
    if start_service:
        enable_cmd = ["systemctl", "enable", FIREWALL_SERVICE]
        rc, out = firewall_run(enable_cmd, timeout=30)
        outputs.append(out or " ".join(enable_cmd))
        if rc != 0:
            return False, "\n".join(outputs)
    cmd = ["systemctl", "restart", FIREWALL_SERVICE]
    rc, out = firewall_run(cmd, timeout=30)
    outputs.append(out or " ".join(cmd))
    message = "\n".join(outputs)
    if rc != 0:
        return False, message
    data["enabled"] = True
    firewall_save_config(data)
    return True, message


def firewall_stop_service() -> Tuple[bool, str]:
    if os.geteuid() != 0:
        return False, "Action refusée : il faut être root pour arrêter nftables."
    rc, out = firewall_run(["systemctl", "disable", "--now", FIREWALL_SERVICE], timeout=30)
    firewall_run(["nft", "delete", "table", "inet", FIREWALL_TABLE_NAME], timeout=10)
    data = firewall_load_config(create=True)
    data["enabled"] = False
    firewall_save_config(data)
    return rc == 0, out or "systemctl disable --now nftables"


def firewall_systemd_state() -> Dict[str, str]:
    def one(args: List[str]) -> str:
        rc, out = firewall_run(args, timeout=8)
        return (out or "").strip() if rc == 0 else (out or "inconnu").strip()
    return {
        "active": one(["systemctl", "is-active", FIREWALL_SERVICE]),
        "enabled": one(["systemctl", "is-enabled", FIREWALL_SERVICE]),
    }


def firewall_status_payload() -> Dict[str, Any]:
    data = firewall_load_config(create=True)
    nft_present = shutil.which("nft") is not None
    service_state = firewall_systemd_state() if shutil.which("systemctl") else {"active": "inconnu", "enabled": "inconnu"}
    applied_rules = ""
    if nft_present:
        rc, out = firewall_run(["nft", "list", "table", "inet", FIREWALL_TABLE_NAME], timeout=10)
        applied_rules = out if rc == 0 else ""
    return {
        "ok": True,
        "installed": nft_present,
        "service": FIREWALL_SERVICE,
        "active": service_state.get("active", "inconnu"),
        "enabled": service_state.get("enabled", "inconnu"),
        "conf_file": FIREWALL_CONF_FILE,
        "nft_file": FIREWALL_NFT_FILE,
        "log_file": FIREWALL_LOG_FILE,
        "rules": data.get("rules") or [],
        "policy_in": "drop",
        "policy_out": "accept",
        "current_ports": firewall_detect_yoleo_ports(),
        "terminal_ports": firewall_detect_terminal_ports(),
        "applied_rules": applied_rules,
    }


@system_bp.route("/system/nftables")
@system_bp.route("/system/firewall")
@system_bp.route("/system/pare-feu")
def system_nftables_route():
    firewall_load_config(create=True)
    return render_system_tab_template("nftables", template_name="system_nftables.html")


@system_bp.route("/system/api/nftables/status")
def system_nftables_status_api():
    return jsonify(firewall_status_payload())


@system_bp.route("/system/api/nftables/action", methods=["POST"])
def system_nftables_action_api():
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    action = str(payload.get("action") or "").strip().lower()
    data = firewall_load_config(create=True)

    if action in {"start", "enable", "on"}:
        ok, message = firewall_apply_rules(start_service=True)
    elif action in {"stop", "disable", "off"}:
        ok, message = firewall_stop_service()
    elif action in {"default", "defaults", "classic", "classique"}:
        data = {"enabled": data.get("enabled", False), "rules": firewall_default_rules()}
        firewall_save_config(data)
        if firewall_systemd_state().get("active") == "active":
            ok, message = firewall_apply_rules(start_service=False)
        else:
            ok, message = True, "Configuration classique restaurée dans ../conf/firewall.conf. Démarre nftables pour l'appliquer."
    elif action == "add":
        start = firewall_safe_int(payload.get("start"), 0, 1, 65535)
        end = firewall_safe_int(payload.get("end") or start, start, 1, 65535)
        if not start:
            return jsonify({"ok": False, "message": "Port de début invalide."}), 400
        rule = firewall_make_rule(start, end, str(payload.get("proto") or "both"), str(payload.get("label") or "Port ajouté"), False)
        data.setdefault("rules", []).append(rule)
        firewall_save_config(data)
        if firewall_systemd_state().get("active") == "active":
            ok, message = firewall_apply_rules(start_service=False)
        else:
            ok, message = True, "Port ajouté dans ../conf/firewall.conf. Démarre nftables pour l'appliquer."
    elif action == "delete":
        rid = str(payload.get("id") or "").strip()
        before = len(data.get("rules") or [])
        data["rules"] = [rule for rule in (data.get("rules") or []) if str(rule.get("id")) != rid]
        if len(data["rules"]) == before:
            return jsonify({"ok": False, "message": "Règle introuvable."}), 404
        firewall_save_config(data)
        if firewall_systemd_state().get("active") == "active":
            ok, message = firewall_apply_rules(start_service=False)
        else:
            ok, message = True, "Règle supprimée dans ../conf/firewall.conf. Démarre nftables pour l'appliquer."
    else:
        return jsonify({"ok": False, "message": "Action inconnue."}), 400

    status = firewall_status_payload()
    status.update({"ok": bool(ok), "message": message})
    return jsonify(status), (200 if ok else 500)


@system_bp.route("/system/info")
def system_info_route():
    return render_system_tab_template("monitor")


@system_bp.route("/system/services")
def system_services_route():
    return render_system_tab_template("services")


@system_bp.route("/system/processus")
def system_processus_route():
    return render_system_tab_template("processes")


def _render_system_mdns_section(section: str = "dashboard"):
    section = mdns_normalize_subtab(section)
    templates = {
        "dashboard": "system_mdns.html",
        "config": "system_mdns_config.html",
        "logs": "system_mdns_logs.html",
    }
    return render_system_tab_template(
        "mdns",
        template_name=templates.get(section, "system_mdns.html"),
        active_mdns_subtab=section,
    )


@system_bp.route("/system/mdns")
def system_mdns_route():
    requested_subtab = request.args.get("subtab", "").strip()
    if requested_subtab:
        section = mdns_normalize_subtab(requested_subtab)
        if section != "dashboard":
            if section == "config":
                return redirect(url_for("system_bp.system_mdns_config_route"))
            if section == "logs":
                return redirect(url_for("system_bp.system_mdns_logs_route"))
            if section == "info":
                return redirect(url_for("system_bp.system_mdns_route"))
    return _render_system_mdns_section("dashboard")


@system_bp.route("/system/mdns/names")
@system_bp.route("/system/mdns/config")
def system_mdns_config_route():
    return _render_system_mdns_section("config")


@system_bp.route("/system/mdns/logs")
def system_mdns_logs_route():
    return _render_system_mdns_section("logs")


@system_bp.route("/system/mdns/info")
def system_mdns_info_route():
    # Ancienne sous-route conservée seulement en redirection : l'onglet Info est intégré à /system/mdns.
    return redirect(url_for("system_bp.system_mdns_route"))


def _render_system_lan_section(section: str = "overview"):
    section = lan_normalize_subtab(section)
    templates = {
        "overview": "system_lan.html",
        "apply": "system_lan_apply.html",
    }
    return render_system_tab_template(
        "lan",
        template_name=templates.get(section, "system_lan.html"),
        active_lan_subtab=section,
    )


@system_bp.route("/system/lan")
def system_lan_route():
    requested_subtab = request.args.get("subtab", "").strip()
    if requested_subtab:
        section = lan_normalize_subtab(requested_subtab)
        if section == "apply":
            return redirect(url_for("system_bp.system_lan_apply_route"))
        if section == "plans":
            return redirect(url_for("system_bp.system_lan_route"))
    return _render_system_lan_section("overview")


@system_bp.route("/system/lan/plans")
def system_lan_plans_route():
    # Ancienne page séparée : les plans sont maintenant intégrés sous /system/lan.
    return redirect(url_for("system_bp.system_lan_route"))


@system_bp.route("/system/lan/apply")
def system_lan_apply_route():
    return _render_system_lan_section("apply")


@system_bp.route("/system/lan/info")
def system_lan_info_route():
    # Ancienne sous-route conservée en compatibilité : les infos LAN sont intégrées à /system/lan.
    return redirect(url_for("system_bp.system_lan_route"))


@system_bp.route("/system/logs")
def system_logs_page_route():
    return render_system_tab_template("logs")


@system_bp.route("/system/gestionnaire-conf")
def system_gestionnaire_conf_route():
    return render_system_tab_template("confmgr", conf_manager_mode="list", template_name="system_gestionnaire_conf.html")


@system_bp.route("/system/gestionnaire_conf")
def system_gestionnaire_conf_legacy_route():
    # Ancienne route avec underscore : on force la route canonique pour que
    # le menu crante reste actif et que le bandeau interne ne change pas d'etat.
    return redirect(url_for("system_bp.system_gestionnaire_conf_route"))


@system_bp.route("/system/gestionnaire-conf/options")
def system_gestionnaire_conf_options_route():
    return render_system_tab_template("confmgr", conf_manager_mode="options", template_name="system_gestionnaire_conf_options.html")


@system_bp.route("/system/gestionnaire_conf/options")
@system_bp.route("/system/conf/options")
def system_gestionnaire_conf_options_legacy_route():
    # Compatibilite ancienne route : toujours revenir sur la route canonique.
    return redirect(url_for("system_bp.system_gestionnaire_conf_options_route"))


@system_bp.route("/system/install")
def system_install_route():
    return render_system_tab_template("admin", template_name="system_install.html", active_system_subtab="install")


@system_bp.route("/system/update")
@system_bp.route("/system/updates")
def system_update_route():
    return render_system_tab_template("admin", template_name="system_updates.html", active_system_subtab="updates")


@system_bp.route("/system/troubleshooting")
def system_troubleshooting_route():
    return render_system_tab_template("admin", template_name="system_troubleshooting.html", active_system_subtab="troubleshooting")


@system_bp.route("/system/install/logs")
@system_bp.route("/system/actions/logs")
def system_install_logs_route():
    return redirect(url_for("system_bp.system_logs_page_route"))

@system_bp.route("/system/tab/<tab_name>")
def system_tab_route(tab_name: str):
    return render_system_tab_template(tab_name)


@system_bp.route("/system/conf")
def system_conf_route():
    # Route historique : on renvoie vers la vraie page pour que le menu cranté
    # reste cohérent et que l'ancienne barre horizontale ne réapparaisse pas.
    return redirect(url_for("system_bp.system_gestionnaire_conf_route"))



@system_bp.route("/system/gestionnaire-conf/options", methods=["POST"])
@system_bp.route("/system/gestionnaire_conf/options", methods=["POST"])
@system_bp.route("/system/conf/options", methods=["POST"])
def system_conf_options_save_route():
    watch_dirs = request.form.getlist("watch_dirs[]")
    # UI volontairement simplifiée : le gestionnaire expose seulement l'ajout de dossiers.
    # La section [INDIVIDUAL_FILES] reste écrite avec FILE1= vide pour compatibilité
    # avec l'ancien format, mais elle n'est plus alimentée depuis l'interface.
    individual_files: List[str] = []
    try:
        conf_manager_replace_sources_in_system_conf(watch_dirs, individual_files)
        flash("✅ Options enregistrées.", "success")
    except Exception as exc:
        flash(f"❌ Impossible d'enregistrer les options du gestionnaire : {exc}", "danger")
    # Ne pas utiliser url_for ici : selon l'ordre des routes Flask peut ressortir
    # l'ancienne variante avec underscore. On force l'URL canonique.
    return redirect("/system/gestionnaire-conf/options")


def _render_conf_edit_page(target: str):
    data, mode = conf_manager_read_file_smart(target)
    return render_system_tab_template(
        "confmgr",
        conf_manager_mode="edit",
        template_name="system_gestionnaire_conf_edit.html",
        conf_edit_config=data,
        conf_file_mode=mode,
        conf_filepath=target,
        conf_filename=os.path.basename(target),
    )


def _handle_conf_edit_request(target: str):
    if not target or not conf_manager_is_allowed_file(target):
        flash("❌ Fichier non autorisé.", "danger")
        return redirect("/system/gestionnaire-conf")

    if request.method == 'POST':
        original_mode = request.form.get('original_mode', 'UNKNOWN').strip() or 'UNKNOWN'
        ok, message = conf_manager_save_file_smart(target, request.form, original_mode)
        flash(("✅ " if ok else "❌ ") + message, "success" if ok else "danger")
        return redirect(url_for('system_bp.system_conf_edit_route', file=target))

    return _render_conf_edit_page(target)


@system_bp.route("/system/gestionnaire-conf/edit", methods=['GET', 'POST'])
def system_conf_edit_route():
    target = os.path.realpath(request.args.get('file', '').strip())
    return _handle_conf_edit_request(target)


@system_bp.route("/system/conf/edit", methods=['GET', 'POST'])
def system_conf_edit_legacy_route():
    # Ancienne route : en GET on bascule vers la route canonique. En POST on
    # sauvegarde puis on redirige aussi vers la route canonique pour ne pas perdre
    # le menu crante apres un enregistrement depuis un ancien onglet.
    target = os.path.realpath(request.args.get('file', '').strip())
    if request.method == 'GET':
        if target:
            return redirect(url_for('system_bp.system_conf_edit_route', file=target))
        return redirect("/system/gestionnaire-conf")
    return _handle_conf_edit_request(target)


@system_bp.route("/system/conf/browse", methods=['POST'])
def system_conf_browse_api():
    path = (request.form.get('path', '') or '/').strip() or '/'
    path = os.path.realpath(path)

    if os.path.isfile(path):
        path = os.path.dirname(path)

    if not os.path.isdir(path):
        return jsonify({'error': 'Dossier introuvable'}), 404

    try:
        folders = []
        files = []
        for name in os.listdir(path):
            if path == '/' and name in CONF_MANAGER_ROOT_BLACKLIST:
                continue
            full_path = os.path.join(path, name)
            if os.path.isdir(full_path):
                folders.append({'name': name, 'path': full_path})
            elif os.path.isfile(full_path):
                files.append({'name': name, 'path': full_path})

        folders.sort(key=lambda item: item['name'].lower())
        files.sort(key=lambda item: item['name'].lower())

        parent_path = '/' if path == '/' else os.path.dirname(path.rstrip('/')) or '/'
        return jsonify({
            'folders': folders,
            'files': files,
            'current_path': path,
            'parent_path': parent_path,
        })
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500

@system_bp.route("/system")
def system_page():
    """Entrée du module Système.

    L'ancien système arrivait sur un grand HTML avec des sous-tabs JavaScript.
    La refonte garde les anciennes API, mais envoie vers de vraies sous-routes.
    """
    requested_tab = request.args.get("tab", "info").strip().lower() or "info"
    requested_subtab = request.args.get("subtab", "").strip().lower()

    if requested_tab in {"monitor", "info", "host"}:
        return redirect(url_for("system_bp.system_info_route"))
    if requested_tab in {"services", "systemctl"}:
        return redirect(url_for("system_bp.system_services_route"))
    if requested_tab in {"processes", "processus"}:
        return redirect(url_for("system_bp.system_processus_route"))
    if requested_tab == "logs":
        return redirect(url_for("system_bp.system_logs_page_route"))
    if requested_tab == "lan":
        return redirect(url_for("system_bp.system_lan_route", subtab=requested_subtab or None))
    if requested_tab == "mdns":
        return redirect(url_for("system_bp.system_mdns_route", subtab=requested_subtab or None))
    if requested_tab in {"nftables", "firewall", "pare-feu", "parefeu"}:
        return redirect(url_for("system_bp.system_nftables_route"))
    if requested_tab in {"confmgr", "conf", "gestionnaire-conf", "gestionnaire_conf", "settings"}:
        return redirect(url_for("system_bp.system_gestionnaire_conf_route"))
    if requested_tab in {"personalization", "personnalisation"}:
        return redirect(url_for("system_bp.system_personalization_page", subtab=requested_subtab or None))
    if requested_tab == "system":
        sub = system_normalize_subtab(requested_subtab or "")
        if sub == "install":
            return redirect(url_for("system_bp.system_install_route"))
        if sub == "updates":
            return redirect(url_for("system_bp.system_update_route"))
        if sub == "troubleshooting":
            return redirect(url_for("system_bp.system_troubleshooting_route"))
        if sub == "logs":
            return redirect(url_for("system_bp.system_logs_page_route"))
        return redirect(url_for("system_bp.system_info_route"))

    return redirect(url_for("system_bp.system_info_route"))

# Alias pratiques pendant la transition : l'ancien menu peut pointer encore dessus.
@system_bp.route("/monitor")
@system_bp.route("/systemctl")
def system_page_alias():
    return system_page()





# --------------------------------------------------
# SYSTEME : INSTALLATION / MISE A JOUR INTEGREES
# --------------------------------------------------
SYSTEM_ADMIN_TIMEOUT = get_conf_int("SYSTEM_ADMIN_COMMAND_TIMEOUT", 1800)
SYSTEM_ADMIN_MAX_OUTPUT = get_conf_int("SYSTEM_ADMIN_MAX_OUTPUT_CHARS", 16000)
