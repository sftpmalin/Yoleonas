#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# PART 003 - Console noVNC, websockify et proxy WebSocket



def clean_base_url(value: str) -> str:
    return (value or "").strip().rstrip("/")


def first_console_graphics(vm: Dict[str, object]) -> Optional[Dict[str, str]]:
    graphics = vm.get("graphics", []) or []
    if not isinstance(graphics, list):
        return None

    def has_port(gfx: Dict[str, str]) -> bool:
        ws = str(gfx.get("websocket", "") or "").strip()
        port = str(gfx.get("port", "") or "").strip()
        return bool((ws and ws != "-1") or (port and port != "-1"))

    for gfx in graphics:
        if isinstance(gfx, dict) and str(gfx.get("type", "")).lower() == "vnc" and has_port(gfx):
            return gfx
    for gfx in graphics:
        if isinstance(gfx, dict) and has_port(gfx):
            return gfx
    return None


def console_host_from_graphics(conf: Dict[str, str], gfx: Dict[str, str]) -> str:
    configured = str(conf.get("VNC_HOST", "") or "").strip()
    if configured:
        return configured
    listen = str(gfx.get("listen", "") or "").strip()
    if listen and listen not in {"0.0.0.0", "::", "::0", "127.0.0.1", "localhost"}:
        return listen
    base = clean_base_url(str(conf.get("UNRAID_WEB_URL", "") or ""))
    if "://" in base:
        # garde hostname:port, par exemple 192.168.1.2:12345
        return base.split("://", 1)[1].split("/", 1)[0]
    return request.host


def request_public_host() -> str:
    # DerriÃ¨re Nginx Proxy Manager, X-Forwarded-Host garde le domaine public.
    return (request.headers.get("X-Forwarded-Host") or request.host or "").strip()


def split_host_port_for_novnc(value: str) -> Tuple[str, str]:
    """SÃ©pare host et port pour noVNC.

    Important : noVNC veut host=IP_OU_DNS et port=PORT sÃ©parÃ©s.
    Si on lui envoie host=192.168.1.26:5000 avec port vide, selon la version
    il tente une URL websocket invalide ou sans le bon port.
    """
    raw = (value or "").strip()
    if not raw:
        return "", ""
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    raw = raw.split("/", 1)[0]

    # IPv6 : [::1]:5000
    if raw.startswith("[") and "]" in raw:
        end = raw.index("]")
        host = raw[1:end]
        rest = raw[end + 1:]
        port = rest[1:] if rest.startswith(":") and rest[1:].isdigit() else ""
        return host, port

    # IPv4 / DNS avec port : 192.168.1.26:5000
    if raw.count(":") == 1:
        host, port = raw.rsplit(":", 1)
        if port.isdigit():
            return host, port

    # IPv6 sans crochets ou nom simple sans port.
    return raw, ""


def request_novnc_host_port() -> Tuple[str, str]:
    public = request_public_host()
    host, port = split_host_port_for_novnc(public)
    if host:
        return host, port
    return split_host_port_for_novnc(request.host or "")


def apply_url_template(template: str, values: Dict[str, object]) -> str:
    out = template or "{base}/plugins/dynamix.vm.manager/vnc.html?v={ts}&resize=scale&autoconnect=true&host={host:raw}&port=&path=/vm/wsproxy/{wsport}/"
    for key, raw in values.items():
        plain = str(raw or "")
        # {xxx:raw} = non encodÃ©, utile pour host=192.168.1.2:12345 et base=http://...
        out = out.replace("{" + key + ":raw}", plain)
        # Les valeurs noVNC supportent trÃ¨s bien les caractÃ¨res bruts ici ; garder non encodÃ©
        # Ã©vite de casser base=http://... et path=/vm/wsproxy/5700/.
        out = out.replace("{" + key + "}", plain)
    return out


def console_ports_from_vm(vm: Dict[str, object]) -> Tuple[Optional[Dict[str, str]], str, str, str]:
    gfx = first_console_graphics(vm)
    if not gfx:
        return None, "", "", "Aucune console VNC/noVNC dÃ©tectÃ©e dans le XML de cette VM."
    vnc_port = str(gfx.get("port", "") or "").strip()
    ws_port = str(gfx.get("websocket", "") or "").strip() or vnc_port
    if not ws_port or ws_port == "-1":
        return gfx, vnc_port, "", "Port websocket noVNC introuvable dans le XML libvirt."
    return gfx, vnc_port, ws_port, ""


def vm_looks_running_for_console(conf: Dict[str, str], vm: Dict[str, object]) -> bool:
    """DÃ©termine l'Ã©tat rÃ©el pour la console vidÃ©o.

    La route technique /vm/novnc/open relit la VM juste avant d'ouvrir noVNC. Sur certains
    systÃ¨mes, domstate peut revenir localisÃ© ou incomplet pendant quelques
    secondes. On utilise donc plusieurs signaux libvirt au lieu de bloquer sur
    un seul champ state_class.
    """
    if str(vm.get("state_class", "") or "").lower() == "running":
        return True

    state_text = str(vm.get("state", "") or "")
    if state_class(state_text) == "running":
        return True

    name = str(vm.get("name", "") or "").strip()
    if name:
        rc, out = virsh(conf, "list", "--state-running", "--name", timeout=10)
        if rc == 0:
            running_names = {line.strip() for line in out.splitlines() if line.strip()}
            if name in running_names:
                return True

        rc, out = virsh(conf, "domstate", name, timeout=10)
        if rc == 0 and state_class(out.strip()) == "running":
            return True

        rc, out = virsh(conf, "dominfo", name, timeout=10)
        if rc == 0:
            info = parse_key_values(out)
            if state_class(info.get("State", "")) == "running":
                return True
            # Une VM avec un Id numÃ©rique est en pratique active cÃ´tÃ© libvirt.
            vm_id = str(info.get("Id", "") or "").strip()
            if vm_id and vm_id != "-":
                return True

    return False




def socket_port_open(host: str, port: int, timeout: float = 0.35) -> bool:
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def websockify_port_for_vnc(conf: Dict[str, str], vnc_port: str) -> int:
    base = conf_int(conf, "WEBSOCKIFY_BASE_PORT", 6080)
    try:
        vnc = int(str(vnc_port).strip())
    except Exception:
        vnc = 5900
    # Convention simple et lisible : 5900 -> 6080, 5901 -> 6081, etc.
    return base + max(0, vnc - 5900)


def websockify_browser_host(conf: Dict[str, str], web_host: str) -> str:
    configured = str(conf.get("WEBSOCKIFY_BROWSER_HOST", "") or "").strip()
    return configured or web_host


def websockify_log_file(conf: Dict[str, str]) -> str:
    return resolve_module_path(str(conf.get("WEBSOCKIFY_LOG_FILE", "") or DEFAULT_CONFIG["WEBSOCKIFY_LOG_FILE"]), DEFAULT_CONFIG["WEBSOCKIFY_LOG_FILE"])


def ensure_websockify(conf: Dict[str, str], vnc_port: str) -> Tuple[int, str]:
    """Lance websockify si nÃ©cessaire.

    Le proxy Python Flask/Gunicorn n'est pas fiable pour noVNC dans tous les
    modes WSGI. websockify, lui, est l'outil officiel fait pour transformer un
    VNC TCP brut en WebSocket noVNC.
    """
    try:
        vnc = int(str(vnc_port).strip())
    except Exception:
        return 0, f"Port VNC invalide : {vnc_port}"
    if vnc < 1 or vnc > 65535:
        return 0, f"Port VNC invalide : {vnc_port}"

    ws_port = websockify_port_for_vnc(conf, str(vnc))
    if ws_port < 1 or ws_port > 65535:
        return 0, f"Port websockify invalide : {ws_port}"

    # Si quelque chose Ã©coute dÃ©jÃ , on ne relance pas. Dans le cas normal,
    # c'est le websockify dÃ©jÃ  dÃ©marrÃ© pour cette VM.
    if socket_port_open("127.0.0.1", ws_port):
        console_log(conf, f"websockify dÃ©jÃ  actif ws={ws_port} -> vnc={vnc}")
        return ws_port, ""

    websockify_bin = str(conf.get("WEBSOCKIFY_BIN", "websockify") or "websockify").strip()
    if not os.path.isabs(websockify_bin):
        websockify_bin = shutil.which(websockify_bin) or websockify_bin
    if not os.path.exists(websockify_bin) and os.sep in websockify_bin:
        return 0, f"websockify introuvable : {websockify_bin}. Installe : apt install -y websockify novnc"
    if shutil.which(websockify_bin) is None and not os.path.isabs(websockify_bin):
        return 0, "websockify introuvable. Installe : apt install -y websockify novnc"

    bind_host = str(conf.get("WEBSOCKIFY_BIND_HOST", "0.0.0.0") or "0.0.0.0").strip()
    target = f"127.0.0.1:{vnc}"
    listen = f"{bind_host}:{ws_port}"
    cmd = [websockify_bin, "--verbose"]
    idle = conf_int(conf, "WEBSOCKIFY_IDLE_TIMEOUT", 0)
    if idle > 0:
        cmd.extend(["--idle-timeout", str(idle)])
    cmd.extend([listen, target])

    log_path = websockify_log_file(conf)
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        log_handle = open(log_path, "a", encoding="utf-8")
        log_handle.write(time.strftime("%Y-%m-%d %H:%M:%S") + " START " + " ".join(cmd) + "\n")
        log_handle.flush()
        subprocess.Popen(
            cmd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except Exception as exc:
        return 0, f"Impossible de lancer websockify : {exc}"

    # Petite attente : websockify doit ouvrir le port quasi immÃ©diatement.
    for _ in range(20):
        if socket_port_open("127.0.0.1", ws_port, timeout=0.15):
            console_log(conf, f"websockify dÃ©marrÃ© ws={ws_port} -> vnc={vnc}")
            return ws_port, ""
        time.sleep(0.1)

    return 0, f"websockify lancÃ© mais le port {ws_port} ne rÃ©pond pas encore. Voir {log_path}"

def build_console_url_server(conf: Dict[str, str], vm: Dict[str, object]) -> Tuple[str, str]:
    gfx, vnc_port, ws_port, error = console_ports_from_vm(vm)
    if error:
        return "", error

    if not vm_looks_running_for_console(conf, vm):
        # Si libvirt donne dÃ©jÃ  un port console rÃ©el, on laisse quand mÃªme tenter
        # noVNC : c'est plus utile que de bloquer Ã  tort sur un Ã©tat mal traduit.
        if (ws_port and ws_port != "-1") or (vnc_port and vnc_port != "-1"):
            pass
        else:
            state = str(vm.get("state", "") or vm.get("state_class", "") or "unknown")
            return "", f"La console vidÃ©o est disponible seulement quand la VM est dÃ©marrÃ©e. Ã‰tat dÃ©tectÃ© : {state}"

    if error:
        return "", error

    mode = str(conf.get("CONSOLE_MODE", "flask_proxy") or "flask_proxy").strip().lower()

    # Mode recommandÃ© host Debian : websockify transforme le VNC TCP brut
    # en WebSocket noVNC. Flask sert seulement les fichiers noVNC.
    if mode in {"websockify", "websockify_proxy", "external_websockify"}:
        web_host, _flask_web_port = request_novnc_host_port()
        proxy_port, proxy_error = ensure_websockify(conf, vnc_port or ws_port)
        if proxy_error:
            return "", proxy_error
        browser_host = websockify_browser_host(conf, web_host)
        values = {
            "base": "",
            "host": browser_host,
            "web_port": proxy_port,
            "port": proxy_port,
            "wsport": proxy_port,
            "vnc_port": vnc_port,
            "name": vm.get("name", ""),
            "ts": int(time.time()),
        }
        local_template = "/vm/novnc/vnc.html?v={ts}&resize=scale&autoconnect=true&host={host:raw}&port={web_port}&path=/"
        console_log(conf, f"Console URL websockify VM={vm.get('name','')} novnc={browser_host}:{proxy_port} vnc=127.0.0.1:{vnc_port}")
        return apply_url_template(local_template, values), ""

    # Ancien mode : le navigateur reste sur System Manager, puis le websocket
    # passe par /vm/wsproxy/<port>/. GardÃ© en secours, mais moins fiable avec Gunicorn.
    if mode in {"flask", "flask_proxy", "proxy", "local"}:
        web_host, web_port = request_novnc_host_port()
        values = {
            "base": "",
            # noVNC veut host et port sÃ©parÃ©s. Ne PAS mettre 192.168.x.x:5000
            # dans host avec port vide, sinon certaines versions noVNC Ã©chouent.
            "host": web_host,
            "web_port": web_port,
            # CompatibilitÃ© : {port} continue de dÃ©signer le port VNC/wsproxy.
            "port": ws_port,
            "wsport": ws_port,
            "vnc_port": vnc_port,
            "name": vm.get("name", ""),
            "ts": int(time.time()),
        }
        # path sans slash initial : noVNC ajoute dÃ©jÃ  le / entre host:port et path.
        local_template = "/vm/novnc/vnc.html?v={ts}&resize=scale&autoconnect=true&host={host:raw}&port={web_port}&path=vm/wsproxy/{wsport}/"
        console_log(conf, f"Console URL locale VM={vm.get('name','')} web={web_host}:{web_port} vnc={vnc_port} wsproxy={ws_port}")
        return apply_url_template(local_template, values), ""

    # Ancien mode : redirection vers une interface noVNC externe.
    base = clean_base_url(str(conf.get("UNRAID_WEB_URL", "") or ""))
    if not base:
        return "", "CONSOLE_MODE inconnu ou redirection externe non configurÃ©e. Utilise CONSOLE_MODE=websockify ou flask_proxy."

    values = {
        "base": base,
        "host": console_host_from_graphics(conf, gfx or {}),
        # CompatibilitÃ© : {port} dÃ©signe le port websocket/noVNC pour /wsproxy/.
        "port": ws_port,
        "wsport": ws_port,
        "vnc_port": vnc_port,
        "name": vm.get("name", ""),
        "ts": int(time.time()),
    }
    return apply_url_template(str(conf.get("NOVNC_URL_TEMPLATE", "") or ""), values), ""


def console_error_page(title: str, message: str, status: int = 400) -> Response:
    html = f"""<!doctype html>
<html lang=\"fr\"><head><meta charset=\"utf-8\"><title>{title}</title>
<style>
body{{margin:0;background:#0e1218;color:#e9eef5;font-family:Arial,sans-serif;padding:28px;}}
.box{{max-width:900px;margin:auto;background:#151c27;border:1px solid #333;border-radius:16px;padding:20px;}}
h1{{margin-top:0;color:#fff;}}pre{{white-space:pre-wrap;background:#05080d;border:1px solid #26364b;border-radius:12px;padding:12px;}}
</style></head><body><div class=\"box\"><h1>{title}</h1><pre>{message}</pre></div></body></html>"""
    return Response(html, status=status, mimetype="text/html")


def console_redirect_for_name(name: str):
    conf = get_config()
    try:
        name = clean_vm_name(name)
        names, err = list_vm_names(conf)
        if err:
            return console_error_page("Erreur virsh", err, 500)
        if name not in names:
            return console_error_page("VM introuvable", f"VM introuvable : {name}", 404)
        vm = collect_one_vm(conf, name)
        url, error = build_console_url_server(conf, vm)
        if error:
            return console_error_page("Console vidÃ©o indisponible", error, 400)
        # La pop-up ouvre /vm/novnc/open, puis Flask calcule la console exacte.
        # En CONSOLE_MODE=flask_proxy, cette redirection reste sur le mÃªme domaine Flask.
        return redirect(url, code=302)
    except Exception as exc:
        return console_error_page("Erreur console", str(exc), 500)


@vm_bp.route("/vm/novnc/open")
def novnc_open_query():
    name = str(request.args.get("name", "") or "").strip()
    if not name:
        return console_error_page("VM manquante", "Aucun nom de VM fourni.", 400)
    return console_redirect_for_name(name)


@vm_bp.route("/vm/novnc/open/<path:name>")
def novnc_open_path(name: str):
    return console_redirect_for_name(name)


def plugin_root_dir(conf: Dict[str, str]) -> str:
    """Dossier noVNC local standard Linux Ã  servir."""
    configured = str(conf.get("NOVNC_LOCAL_PLUGIN_DIR", "") or "").strip()
    candidates = [
        configured,
        "/usr/share/novnc",
        "/usr/local/share/novnc",
    ]

    for path in candidates:
        if path and os.path.isdir(path):
            return os.path.realpath(path)

    # Retourne le chemin configurÃ© pour afficher une erreur utile.
    return os.path.realpath(configured or "/usr/share/novnc")


def send_novnc_asset(asset: str = "vnc.html"):
    conf = get_config()
    root_dir = plugin_root_dir(conf)
    if not os.path.isdir(root_dir):
        return console_error_page(
            "noVNC introuvable",
            "Dossier absent : " + root_dir + "\n\nSur Debian pure host, installe noVNC sur lâ€™hÃ´te : apt install -y novnc websockify. Ensuite rÃ¨gle NOVNC_LOCAL_PLUGIN_DIR=/usr/share/novnc dans vm.conf.",
            404,
        )

    asset = (asset or "vnc.html").lstrip("/")
    real_path = os.path.realpath(os.path.join(root_dir, asset))
    if real_path != root_dir and not real_path.startswith(root_dir + os.sep):
        abort(403)
    if not os.path.isfile(real_path):
        abort(404)
    return send_from_directory(root_dir, asset)


# Route host propre : on ne dÃ©pend plus de /plugins/dynamix...
@vm_bp.route("/vm/novnc/")
@vm_bp.route("/vm/novnc/<path:asset>")
def local_novnc_asset(asset: str = "vnc.html"):
    return send_novnc_asset(asset)


# Alias compatibles avec les anciens liens noVNC.
@vm_bp.route("/plugins/dynamix.vm.manager/")
@vm_bp.route("/plugins/dynamix.vm.manager/<path:asset>")
@vm_bp.route("/vm/plugins/dynamix.vm.manager/")
@vm_bp.route("/vm/plugins/dynamix.vm.manager/<path:asset>")
def local_legacy_vm_plugin_asset(asset: str = "vnc.html"):
    return send_novnc_asset(asset)


def host_without_web_port(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0]
    if value.startswith("[") and "]" in value:
        return value[1:value.index("]")]
    # Cas normal IPv4 / nom DNS avec port web : 192.168.1.2:12345 -> 192.168.1.2
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0]
    return value


def ws_target_url(conf: Dict[str, str], port: int) -> str:
    template = str(conf.get("CONSOLE_WS_TARGET_TEMPLATE", "ws://127.0.0.1:{wsport}/") or "ws://127.0.0.1:{wsport}/")
    values = {
        "port": port,
        "wsport": port,
        "legacy_host": console_host_from_graphics(conf, {}),
        "legacy_ip": host_without_web_port(str(conf.get("UNRAID_WEB_URL", "") or conf.get("VNC_HOST", "") or "")),
        "ts": int(time.time()),
    }
    return apply_url_template(template, values)


def ws_target_candidates(conf: Dict[str, str], port: int) -> List[str]:
    candidates: List[str] = []

    def add(url: str):
        url = (url or "").strip()
        if url and url not in candidates:
            candidates.append(url)

    add(ws_target_url(conf, port))

    if conf_bool(conf, "CONSOLE_WS_FALLBACKS", "1"):
        add(f"ws://127.0.0.1:{port}/")
        add(f"ws://localhost:{port}/")
        host = host_without_web_port(str(conf.get("UNRAID_WEB_URL", "") or conf.get("VNC_HOST", "") or ""))
        if host and host not in {"127.0.0.1", "localhost"}:
            add(f"ws://{host}:{port}/")

    return candidates


def tcp_target_candidates(conf: Dict[str, str], port: int) -> List[Tuple[str, int]]:
    """Cibles VNC TCP brut.

    Sur Debian/libvirt classique, le port VNC 5900 est un port TCP RFB brut,
    pas un websocket. noVNC parle WebSocket cÃ´tÃ© navigateur, donc Flask doit
    faire le pont WebSocket navigateur -> TCP VNC brut.
    """
    candidates: List[Tuple[str, int]] = []

    def add(host: str):
        host = (host or "").strip()
        if not host:
            return
        item = (host, int(port))
        if item not in candidates:
            candidates.append(item)

    add("127.0.0.1")
    add("localhost")

    # Si Flask tourne dans Docker, 127.0.0.1 peut Ãªtre le container.
    # On tente aussi l'IP configurÃ©e dans VNC_HOST/UNRAID_WEB_URL.
    host = host_without_web_port(str(conf.get("VNC_HOST", "") or conf.get("UNRAID_WEB_URL", "") or ""))
    if host and host not in {"127.0.0.1", "localhost"}:
        add(host)

    # IP de la requÃªte actuelle si utile.
    try:
        add(request.host.split(":", 1)[0])
    except Exception:
        pass

    return candidates


def connect_backend_tcp(conf: Dict[str, str], port: int, timeout: int = 8):
    import socket
    tried: List[str] = []
    for host, tcp_port in tcp_target_candidates(conf, port):
        try:
            sock = socket.create_connection((host, tcp_port), timeout=timeout)
            sock.settimeout(1.0)
            return sock, f"{host}:{tcp_port}", tried
        except Exception as exc:
            tried.append(f"{host}:{tcp_port} -> {exc}")
    raise RuntimeError("Impossible de joindre le VNC TCP brut. " + " ; ".join(tried[-8:]))



def websocket_proxy_error(message: str, status: int = 500) -> Response:
    # Pour un accÃ¨s HTTP classique Ã  /vm/wsproxy/5700/, Ã§a donne un diagnostic lisible.
    return console_error_page("Proxy websocket VM", message, status)


def console_log(conf: Dict[str, str], message: str) -> None:
    path = resolve_module_path(str(conf.get("CONSOLE_LOG_FILE", "") or DEFAULT_CONFIG["CONSOLE_LOG_FILE"]), DEFAULT_CONFIG["CONSOLE_LOG_FILE"])
    if not path:
        return
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + message.rstrip() + "\n")
    except Exception:
        pass


def connect_backend_ws(targets: List[str], timeout: int = 8, preferred_subprotocol: str = ""):
    import websocket as ws_client_lib
    protocol_sets = []
    preferred_subprotocol = (preferred_subprotocol or "").strip()
    if preferred_subprotocol:
        protocol_sets.append([preferred_subprotocol])
    protocol_sets.extend([["binary"], ["base64"], None])
    tried = []
    for target in targets:
        for protocols in protocol_sets:
            label = ",".join(protocols) if protocols else "sans-protocole"
            try:
                kwargs = {"timeout": timeout, "enable_multithread": True}
                if protocols:
                    kwargs["subprotocols"] = protocols
                ws = ws_client_lib.create_connection(target, **kwargs)
                return ws, target, label, tried
            except Exception as exc:
                tried.append(f"{target} [{label}] -> {exc}")
    raise RuntimeError("Impossible de joindre le websocket QEMU/noVNC. " + " ; ".join(tried[-8:]))


@vm_bp.route("/vm/novnc/debug")
def novnc_debug():
    """Diagnostic simple : montre le port websocket dÃ©tectÃ© et teste les cibles depuis Python."""
    conf = get_config()
    name = str(request.args.get("name", "") or "").strip()
    try:
        name = clean_vm_name(name)
        vm = collect_one_vm(conf, name)
        gfx, vnc_port, ws_port, error = console_ports_from_vm(vm)
        if error:
            return jsonify({"ok": False, "name": name, "error": error, "graphics": vm.get("graphics", [])}), 400

        results = []
        try:
            import websocket as ws_client_lib
            for target in ws_target_candidates(conf, int(ws_port)):
                t0 = time.time()
                try:
                    ok_detail = []
                    for protocols in (["binary"], ["base64"], None):
                        try:
                            kwargs = {"timeout": 4}
                            if protocols:
                                kwargs["subprotocols"] = protocols
                            ws = ws_client_lib.create_connection(target, **kwargs)
                            ws.close()
                            ok_detail.append(",".join(protocols) if protocols else "sans-protocole")
                            break
                        except Exception as proto_exc:
                            ok_detail.append((",".join(protocols) if protocols else "sans-protocole") + " KO: " + str(proto_exc))
                    if ok_detail and not ok_detail[-1].startswith("binary KO") and not ok_detail[-1].startswith("base64 KO") and not ok_detail[-1].startswith("sans-protocole KO"):
                        results.append({"target": target, "ok": True, "protocol": ok_detail[-1], "ms": int((time.time() - t0) * 1000)})
                    else:
                        results.append({"target": target, "ok": False, "error": " | ".join(ok_detail), "ms": int((time.time() - t0) * 1000)})
                except Exception as exc:
                    results.append({"target": target, "ok": False, "error": str(exc), "ms": int((time.time() - t0) * 1000)})
        except Exception as exc:
            results.append({"target": "websocket-client import", "ok": False, "error": str(exc)})

        return jsonify({
            "ok": True,
            "name": name,
            "vnc_port": vnc_port,
            "ws_port": ws_port,
            "graphics": gfx,
            "console_url": build_console_url_server(conf, vm)[0],
            "targets": results,
            "public_host": request_public_host(),
            "novnc_host_port": request_novnc_host_port(),
            "expected_path": f"vm/wsproxy/{ws_port}/",
            "websockify_port": websockify_port_for_vnc(conf, vnc_port or ws_port),
            "websockify_log": websockify_log_file(conf),
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@vm_bp.route("/wsproxy/<int:port>/")
@vm_bp.route("/wsproxy/<int:port>")
@vm_bp.route("/vm/wsproxy/<int:port>/")
@vm_bp.route("/vm/wsproxy/<int:port>")
def vm_wsproxy(port: int):
    """Proxy WebSocket noVNC.

    Deux cas :
      1) backend WebSocket dÃ©jÃ  prÃªt : ws://127.0.0.1:PORT/
      2) Debian/libvirt classique : PORT est un VNC TCP brut.
         Dans ce cas Flask fait le pont WebSocket navigateur -> TCP VNC.
    """
    if port < 1 or port > 65535:
        return websocket_proxy_error(f"Port invalide : {port}", 400)

    if "upgrade" not in (request.headers.get("Connection", "") or "").lower() and (request.headers.get("Upgrade", "") or "").lower() != "websocket":
        return websocket_proxy_error(
            "Cette route attend une connexion WebSocket noVNC.\n"
            "Si tu vois cette page dans le navigateur, c'est que noVNC n'a pas ouvert le websocket correctement.",
            400,
        )

    try:
        from simple_websocket import Server
        import websocket as ws_client_lib
    except Exception as exc:
        return websocket_proxy_error(
            "Modules Python manquants pour le proxy websocket.\n\n"
            "Installe dans l'environnement Flask : pip install simple-websocket websocket-client\n\n"
            f"Erreur import : {exc}",
            500,
        )

    import socket

    conf = get_config()
    client_ws = None
    backend_ws = None
    backend_tcp = None
    stop = threading.Event()

    try:
        client_ws = Server.accept(request.environ, subprotocols=["binary", "base64"])
        preferred_proto = getattr(client_ws, "subprotocol", "") or "binary"

        # Essai 1 : backend websocket.
        try:
            targets = ws_target_candidates(conf, port)
            console_log(conf, f"WS client connectÃ©, port={port}, essai backend websocket targets={targets}")
            backend_ws, backend_target, backend_proto, tried_errors = connect_backend_ws(targets, timeout=3, preferred_subprotocol=preferred_proto)
            console_log(conf, f"WS backend websocket OK target={backend_target}, protocol={backend_proto}")

            def client_to_backend_ws():
                while not stop.is_set():
                    try:
                        data = client_ws.receive()
                        if data is None:
                            break
                        if isinstance(data, (bytes, bytearray)):
                            backend_ws.send_binary(bytes(data))
                        else:
                            backend_ws.send(str(data))
                    except Exception:
                        break
                stop.set()
                try:
                    backend_ws.close()
                except Exception:
                    pass

            def backend_ws_to_client():
                while not stop.is_set():
                    try:
                        opcode, data = backend_ws.recv_data()
                        if opcode == ws_client_lib.ABNF.OPCODE_CLOSE:
                            break
                        if opcode == ws_client_lib.ABNF.OPCODE_BINARY:
                            client_ws.send(data)
                        elif opcode == ws_client_lib.ABNF.OPCODE_TEXT:
                            if isinstance(data, bytes):
                                data = data.decode("utf-8", "replace")
                            client_ws.send(data)
                    except Exception:
                        break
                stop.set()
                try:
                    client_ws.close()
                except Exception:
                    pass

            t1 = threading.Thread(target=client_to_backend_ws, daemon=True)
            t2 = threading.Thread(target=backend_ws_to_client, daemon=True)
            t1.start()
            t2.start()
            while not stop.is_set():
                time.sleep(0.1)
            return Response("", status=101)

        except Exception as ws_exc:
            console_log(conf, f"Backend websocket KO, fallback TCP brut VNC: {ws_exc}")

        # Essai 2 : backend TCP VNC brut.
        backend_tcp, backend_label, tried = connect_backend_tcp(conf, port, timeout=5)
        console_log(conf, f"Backend TCP VNC OK target={backend_label}")

        def client_to_tcp():
            while not stop.is_set():
                try:
                    data = client_ws.receive()
                    if data is None:
                        break
                    if isinstance(data, str):
                        data = data.encode("latin1", "ignore")
                    backend_tcp.sendall(bytes(data))
                except Exception:
                    break
            stop.set()
            try:
                backend_tcp.close()
            except Exception:
                pass

        def tcp_to_client():
            while not stop.is_set():
                try:
                    data = backend_tcp.recv(65536)
                    if not data:
                        break
                    client_ws.send(data)
                except socket.timeout:
                    continue
                except Exception:
                    break
            stop.set()
            try:
                client_ws.close()
            except Exception:
                pass

        t1 = threading.Thread(target=client_to_tcp, daemon=True)
        t2 = threading.Thread(target=tcp_to_client, daemon=True)
        t1.start()
        t2.start()

        while not stop.is_set():
            time.sleep(0.1)

        return Response("", status=101)

    except Exception as exc:
        console_log(conf, f"WS proxy erreur port={port}: {exc}")
        return websocket_proxy_error(str(exc), 500)

    finally:
        stop.set()
        try:
            if backend_ws:
                backend_ws.close()
        except Exception:
            pass
        try:
            if backend_tcp:
                backend_tcp.close()
        except Exception:
            pass
        try:
            if client_ws:
                client_ws.close()
        except Exception:
            pass
