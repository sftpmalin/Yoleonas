# ==========================================================
# FFmpeg over IP / FFmpeg Server v4 - service NAS optionnel
# ==========================================================
# Philosophie : le serveur ne remplace pas FFmpeg et ne partage pas les fichiers.
# Il traduit seulement les chemins client -> chemins locaux NAS, puis lance le vrai FFmpeg.

import socket
import platform
import re
import shlex
from datetime import datetime

FFOI_CONFIG_FILE = nas_conf_file("ffmpeg-over-ip.conf")
FFOI_JSON_FILE_DEFAULT = "../conf/ffmpeg-over-ip.server.json"
FFOI_SERVER_BIN_DEFAULT = "../bin/ffmpeg-over-ip-server"
FFOI_CLIENT_BIN_DEFAULT = "../bin/ffmpeg-over-ip-client.exe"
FFOI_SERVICE_NAME_DEFAULT = "ffmpeg-over-ip.service"
FFOI_LOG_FILE_DEFAULT = "/var/log/yoleo/ffmpeg-over-ip.log"
FFOI_FIREWALL_LABEL = "FFmpeg over IP"
FFOI_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,126}\.service$")

FFOI_DEFAULT_CONFIG: Dict[str, str] = {
    "ENABLED": "0",
    "ADDRESS": "0.0.0.0",
    "PORT": "3333",
    "CLIENT_HOST": "",
    "AUTH_SECRET": "",
    "FFMPEG_PATH": "/usr/bin/ffmpeg",
    "SERVER_BIN": FFOI_SERVER_BIN_DEFAULT,
    "CLIENT_BIN": FFOI_CLIENT_BIN_DEFAULT,
    "SERVER_JSON": FFOI_JSON_FILE_DEFAULT,
    "SERVER_ARGS": "-config {config}",
    "SERVICE_NAME": FFOI_SERVICE_NAME_DEFAULT,
    "LOG_FILE": FFOI_LOG_FILE_DEFAULT,
    "OUTPUT_DIR": "",
    "SHARE_MODE": "samba",
    "SAMBA_SHARE": "",
    "NFS_MOUNT": "",
    "CLIENT_PATH": "",
    "SERVER_PATH": "",
}

FFOI_CONFIG_ORDER = [
    "ENABLED", "ADDRESS", "PORT", "CLIENT_HOST", "AUTH_SECRET", "FFMPEG_PATH",
    "SERVER_BIN", "CLIENT_BIN", "SERVER_JSON", "SERVER_ARGS", "SERVICE_NAME", "LOG_FILE",
    "OUTPUT_DIR", "SHARE_MODE", "SAMBA_SHARE", "NFS_MOUNT", "CLIENT_PATH", "SERVER_PATH",
]


def ffoi_redirect(subtab: str = "main") -> str:
    """Redirection canonique FFmpeg : garde /services/ffmpeg/... après une action.

    Ne pas repasser par l'ancien /services?tab=ffmpeg&subtab=...
    sinon le menu cranté ne matche plus la route propre et l'ancienne
    navigation horizontale peut réapparaître.
    """
    subtab = str(subtab or "main").strip().lower()
    if subtab not in {"main", "share", "info"}:
        subtab = "main"
    return redirect(url_for("services_bp.services_section", service="ffmpeg", subtab=subtab))


def ffoi_safe_int(value: Any, default: int = 3333, minimum: int = 1, maximum: int = 65535) -> int:
    try:
        port = int(str(value or "").strip())
    except Exception:
        port = default
    return max(minimum, min(maximum, port))


def ffoi_one_line(value: Any) -> str:
    return str(value or "").replace("\x00", "").replace("\r", " ").replace("\n", " ").strip()


def ffoi_strip_quotes(value: str) -> str:
    value = ffoi_one_line(value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def ffoi_normalize_service_name(value: str) -> str:
    name = ffoi_one_line(value or FFOI_SERVICE_NAME_DEFAULT)
    if not name:
        name = FFOI_SERVICE_NAME_DEFAULT
    if not name.endswith(".service"):
        name += ".service"
    if not FFOI_SERVICE_NAME_RE.fullmatch(name):
        return FFOI_SERVICE_NAME_DEFAULT
    return name


def ffoi_resolve_path(value: str, base_dir: Optional[str] = None) -> str:
    raw = ffoi_strip_quotes(value)
    if not raw:
        return ""
    raw = os.path.expanduser(os.path.expandvars(raw))
    if os.path.isabs(raw):
        return os.path.abspath(raw)
    return os.path.abspath(os.path.join(base_dir or NAS_CONF_DIR, raw))


def ffoi_display_path(path: str) -> str:
    """Garde les chemins ../conf/../bin lisibles dans les formulaires."""
    raw = ffoi_one_line(path)
    if raw:
        return raw
    return ""


def ffoi_normalize_unc(value: str) -> str:
    value = ffoi_one_line(value)
    if not value:
        return ""
    # On garde volontairement les antislashs Windows. On s'assure juste d'une fin de racine.
    if value.startswith("\\\\"):
        value = value.replace("/", "\\")
        if not value.endswith("\\"):
            value += "\\"
        return value
    # Chemin Linux/client manuel : on ne force pas le format, mais on garde un slash final si racine.
    if value.startswith("/") and not value.endswith("/"):
        value += "/"
    return value


def ffoi_normalize_server_root(value: str) -> str:
    value = ffoi_one_line(value).replace("\\", "/")
    if not value:
        return ""
    if not value.startswith("/"):
        return value
    value = os.path.normpath(value)
    if value != "/" and not value.endswith("/"):
        value += "/"
    return value


def ffoi_normalize_server_args(value: str) -> str:
    """Normalise la syntaxe de lancement du binaire serveur v4.

    Le binaire ffmpeg-over-ip-server n'accepte pas le JSON comme argument direct.
    Il attend explicitement : -config /chemin/ffmpeg-over-ip.server.json

    Migration douce : les anciennes valeurs {config} ou {json} générées par le
    premier jet sont automatiquement corrigées sans demander à l'utilisateur de
    repasser dans les réglages.
    """
    args = ffoi_one_line(value)
    if not args or args in {"{config}", "{json}"}:
        return "-config {config}"
    return args


def ffoi_sanitize_config(conf: Dict[str, str]) -> Dict[str, str]:
    clean = dict(FFOI_DEFAULT_CONFIG)
    for key in FFOI_CONFIG_ORDER:
        if key in conf:
            clean[key] = ffoi_one_line(conf.get(key))
    clean["PORT"] = str(ffoi_safe_int(clean.get("PORT"), 3333))
    clean["SERVER_ARGS"] = ffoi_normalize_server_args(clean.get("SERVER_ARGS"))
    clean["SERVICE_NAME"] = ffoi_normalize_service_name(clean.get("SERVICE_NAME"))
    clean["SHARE_MODE"] = (clean.get("SHARE_MODE") or "samba").strip().lower()
    if clean["SHARE_MODE"] not in {"samba", "manual"}:
        clean["SHARE_MODE"] = "samba"
    clean["OUTPUT_DIR"] = ffoi_normalize_server_root(clean.get("OUTPUT_DIR") or "")
    clean["CLIENT_PATH"] = ffoi_normalize_unc(clean.get("CLIENT_PATH") or "")
    clean["SERVER_PATH"] = ffoi_normalize_server_root(clean.get("SERVER_PATH") or "")
    return clean


def ffoi_read_kv_file(path: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not os.path.exists(path):
        return data
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().upper()
            if key:
                data[key] = ffoi_one_line(ffoi_strip_quotes(value))
    return data


def ffoi_write_kv_file(path: str, conf: Dict[str, str]) -> None:
    conf = ffoi_sanitize_config(conf)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    lines = [
        "# ============================================================",
        "# ffmpeg-over-ip.conf - Configuration Yoleo FFmpeg over IP",
        "# Le JSON consommé par le binaire est généré dans SERVER_JSON.",
        "# NFS/manuel : le chemin client est saisi manuellement.",
        "# ============================================================",
        "",
    ]
    for key in FFOI_CONFIG_ORDER:
        lines.append(f"{key}={ffoi_one_line(conf.get(key, FFOI_DEFAULT_CONFIG.get(key, '')))}")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines).rstrip() + "\n")
    try:
        os.chmod(path, 0o640)
    except OSError:
        pass


def ffoi_read_config(create: bool = True) -> Dict[str, str]:
    conf = dict(FFOI_DEFAULT_CONFIG)
    conf.update(ffoi_read_kv_file(FFOI_CONFIG_FILE))
    if not conf.get("AUTH_SECRET"):
        conf["AUTH_SECRET"] = uuid.uuid4().hex
    conf = ffoi_sanitize_config(conf)
    if create and not os.path.exists(FFOI_CONFIG_FILE):
        ffoi_write_kv_file(FFOI_CONFIG_FILE, conf)
        ffoi_write_server_json(conf)
    return conf


def ffoi_collect_settings_from_form(conf: Dict[str, str]) -> Dict[str, str]:
    new = dict(conf)
    for key in ("ADDRESS", "CLIENT_HOST", "AUTH_SECRET", "FFMPEG_PATH", "SERVER_BIN", "CLIENT_BIN", "SERVER_JSON", "SERVER_ARGS", "SERVICE_NAME", "LOG_FILE", "OUTPUT_DIR"):
        if key in request.form:
            new[key] = ffoi_one_line(request.form.get(key))
    new["PORT"] = str(ffoi_safe_int(request.form.get("PORT"), ffoi_safe_int(conf.get("PORT"))))
    if not new.get("AUTH_SECRET"):
        new["AUTH_SECRET"] = uuid.uuid4().hex
    return ffoi_sanitize_config(new)


def ffoi_collect_share_from_form(conf: Dict[str, str]) -> Dict[str, str]:
    new = dict(conf)
    mode = ffoi_one_line(request.form.get("SHARE_MODE") or conf.get("SHARE_MODE") or "samba").lower()
    if mode not in {"samba", "manual"}:
        mode = "samba"
    new["SHARE_MODE"] = mode
    new["SAMBA_SHARE"] = ffoi_one_line(request.form.get("SAMBA_SHARE"))
    new["NFS_MOUNT"] = ffoi_one_line(request.form.get("NFS_MOUNT"))
    new["CLIENT_PATH"] = ffoi_normalize_unc(request.form.get("CLIENT_PATH") or "")
    new["SERVER_PATH"] = ffoi_normalize_server_root(request.form.get("SERVER_PATH") or "")

    if mode == "samba" and new["SAMBA_SHARE"]:
        for share in ffoi_detect_samba_shares():
            if share.get("name") == new["SAMBA_SHARE"]:
                new["CLIENT_PATH"] = ffoi_normalize_unc(str(share.get("client_path") or ""))
                new["SERVER_PATH"] = ffoi_normalize_server_root(str(share.get("server_path") or ""))
                new["NFS_MOUNT"] = ""
                break

    # Mode manuel/NFS : le chemin client reste saisi à la main,
    # mais le chemin serveur peut être pris automatiquement depuis un montage NFS détecté.
    if mode == "manual" and new["NFS_MOUNT"]:
        for mount in ffoi_detect_nfs_mounts():
            if mount.get("target") == new["NFS_MOUNT"]:
                new["SERVER_PATH"] = ffoi_normalize_server_root(str(mount.get("target") or ""))
                break
    return ffoi_sanitize_config(new)


def ffoi_architecture() -> str:
    try:
        rc, out = ffoi_run(["dpkg", "--print-architecture"], timeout=5, log=False)
        arch = out.strip().splitlines()[0].strip() if rc == 0 and out.strip() else ""
        if arch:
            return arch
    except Exception:
        pass
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "amd64"
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    return machine or "inconnu"


def ffoi_is_amd64() -> bool:
    return ffoi_architecture() in {"amd64", "x86_64"}


def ffoi_run(cmd: List[str], timeout: int = 30, log: bool = True) -> Tuple[int, str]:
    try:
        proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        out = proc.stdout or ""
        if log:
            ffoi_log("$ " + " ".join(shlex.quote(str(part)) for part in cmd) + "\n" + out)
        return proc.returncode, out
    except subprocess.TimeoutExpired as exc:
        msg = f"Timeout : {' '.join(cmd)}"
        if log:
            ffoi_log(msg)
        return 124, msg
    except Exception as exc:
        msg = f"Erreur commande : {exc}"
        if log:
            ffoi_log(msg)
        return 1, msg


def ffoi_log(message: str) -> None:
    try:
        conf = ffoi_read_config(create=False)
        log_file = ffoi_resolve_path(conf.get("LOG_FILE") or FFOI_LOG_FILE_DEFAULT, NAS_CONF_DIR)
    except Exception:
        log_file = FFOI_LOG_FILE_DEFAULT
    try:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        with open(log_file, "a", encoding="utf-8", errors="replace") as handle:
            handle.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message.rstrip()}\n")
    except Exception:
        pass


def ffoi_systemctl(service: str, action: str, timeout: int = 30) -> Tuple[int, str]:
    if not shutil.which("systemctl"):
        return 1, "systemctl introuvable."
    return ffoi_run(["systemctl", action, service], timeout=timeout)


def ffoi_systemctl_is(service: str, what: str) -> str:
    if not shutil.which("systemctl"):
        return "inconnu"
    rc, out = ffoi_run(["systemctl", f"is-{what}", service], timeout=8, log=False)
    return out.strip() or ("oui" if rc == 0 else "non")


def ffoi_server_json_path(conf: Dict[str, str]) -> str:
    return ffoi_resolve_path(conf.get("SERVER_JSON") or FFOI_JSON_FILE_DEFAULT, NAS_CONF_DIR)


def ffoi_server_bin_path(conf: Dict[str, str]) -> str:
    return ffoi_resolve_path(conf.get("SERVER_BIN") or FFOI_SERVER_BIN_DEFAULT, NAS_CONF_DIR)


def ffoi_client_bin_path(conf: Dict[str, str]) -> str:
    return ffoi_resolve_path(conf.get("CLIENT_BIN") or FFOI_CLIENT_BIN_DEFAULT, NAS_CONF_DIR)


def ffoi_guess_client_host(conf: Optional[Dict[str, str]] = None) -> str:
    """IP/nom à mettre dans la configuration du client Windows.

    ADDRESS reste l'adresse d'écoute du serveur. Si elle vaut 0.0.0.0,
    on ne l'écrit pas dans le client : on préfère CLIENT_HOST ou l'hôte HTTP courant.
    """
    conf = conf or {}
    explicit = str(conf.get("CLIENT_HOST") or "").strip()
    if explicit:
        return explicit.split(":", 1)[0]
    try:
        host = str((request.host or "").split(":", 1)[0]).strip()
        if host and host not in {"0.0.0.0", "127.0.0.1", "localhost"}:
            return host
    except Exception:
        pass
    address = str(conf.get("ADDRESS") or "").strip().split(":", 1)[0]
    if address and address not in {"0.0.0.0", "127.0.0.1", "localhost"}:
        return address
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.2)
        sock.connect(("8.8.8.8", 80))
        host = sock.getsockname()[0]
        sock.close()
        if host:
            return host
    except Exception:
        pass
    return socket.gethostname()


def ffoi_client_payload(conf: Dict[str, str]) -> Dict[str, str]:
    host = ffoi_guess_client_host(conf)
    port = ffoi_safe_int(conf.get("PORT"), 3333)
    return {
        "log": "stdout",
        "address": f"{host}:{port}",
        "authSecret": conf.get("AUTH_SECRET") or "",
    }


def ffoi_client_jsonc(conf: Dict[str, str]) -> str:
    return json.dumps(ffoi_client_payload(conf), ensure_ascii=False, indent=2) + "\n"


def ffoi_service_name(conf: Dict[str, str]) -> str:
    return ffoi_normalize_service_name(conf.get("SERVICE_NAME") or FFOI_SERVICE_NAME_DEFAULT)


def ffoi_service_unit_path(conf: Dict[str, str]) -> str:
    return f"/etc/systemd/system/{ffoi_service_name(conf)}"


def ffoi_rewrites(conf: Dict[str, str]) -> List[List[str]]:
    client = ffoi_normalize_unc(conf.get("CLIENT_PATH") or "")
    server = ffoi_normalize_server_root(conf.get("SERVER_PATH") or "")
    if client and server:
        return [[client, server]]
    return []


def ffoi_write_server_json(conf: Dict[str, str]) -> str:
    json_path = ffoi_server_json_path(conf)
    os.makedirs(os.path.dirname(json_path) or ".", exist_ok=True)
    port = ffoi_safe_int(conf.get("PORT"), 3333)
    payload = {
        "log": "stdout",
        "address": f"{conf.get('ADDRESS') or '0.0.0.0'}:{port}",
        "authSecret": conf.get("AUTH_SECRET") or uuid.uuid4().hex,
        "ffmpegPath": conf.get("FFMPEG_PATH") or "/usr/bin/ffmpeg",
        "rewrites": ffoi_rewrites(conf),
    }
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    try:
        os.chmod(json_path, 0o640)
    except OSError:
        pass
    return json_path


def ffoi_service_workdir(conf: Dict[str, str]) -> str:
    """Dossier de travail du service.

    FFmpeg over IP exécute le vrai ffmpeg côté Linux. Quand le client Windows
    donne un fichier de sortie relatif comme "sortie.mp4", c'est donc le
    WorkingDirectory systemd qui décide où le fichier est créé.

    Si OUTPUT_DIR est renseigné, il gagne toujours. Sinon, on utilise par défaut
    le dossier Linux du partage/rewrite sélectionné, par exemple :
      \\\\Samba126\\test\\  ->  /mnt/user/Yoan/
    Ainsi une sortie relative arrive directement dans le partage Samba choisi.
    """
    output_dir = ffoi_normalize_server_root(conf.get("OUTPUT_DIR") or "")
    if output_dir and output_dir.startswith("/") and os.path.isdir(output_dir):
        return output_dir.rstrip("/") or "/"
    candidate = ffoi_normalize_server_root(conf.get("SERVER_PATH") or "")
    if candidate and candidate.startswith("/") and os.path.isdir(candidate):
        return candidate.rstrip("/") or "/"
    return NAS_ROOT_DIR


def ffoi_output_dir_error(conf: Dict[str, str]) -> str:
    output_dir = ffoi_normalize_server_root(conf.get("OUTPUT_DIR") or "")
    if not output_dir:
        return ""
    if not output_dir.startswith("/"):
        return "Destination serveur invalide : choisis un chemin absolu Linux."
    if not os.path.isdir(output_dir):
        return f"Destination serveur introuvable : {output_dir}"
    return ""


def ffoi_render_unit(conf: Dict[str, str]) -> str:
    service = ffoi_service_name(conf)
    server_bin = ffoi_server_bin_path(conf)
    client_bin = ffoi_client_bin_path(conf)
    json_path = ffoi_server_json_path(conf)
    log_file = ffoi_resolve_path(conf.get("LOG_FILE") or FFOI_LOG_FILE_DEFAULT, NAS_CONF_DIR)
    workdir = ffoi_service_workdir(conf)
    args_template = ffoi_normalize_server_args(conf.get("SERVER_ARGS"))
    args = args_template.replace("{config}", shlex.quote(json_path)).replace("{json}", shlex.quote(json_path))
    exec_start = shlex.quote(server_bin) + (" " + args if args else "")
    return f"""[Unit]
Description=Yoleo FFmpeg over IP server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={workdir}
Environment=FFMPEG_OVER_IP_CONFIG={json_path}
ExecStart={exec_start}
Restart=on-failure
RestartSec=3
StandardOutput=append:{log_file}
StandardError=append:{log_file}

[Install]
WantedBy=multi-user.target
"""


def ffoi_install_service(conf: Dict[str, str], enable: bool = True) -> Tuple[bool, str]:
    if not ffoi_is_amd64():
        return False, "Architecture non compatible : FFmpeg over IP est volontairement limité à AMD64."
    output_error = ffoi_output_dir_error(conf)
    if output_error:
        return False, output_error
    unit_path = ffoi_service_unit_path(conf)
    if os.geteuid() != 0:
        return False, "Action refusée : il faut être root pour écrire le service systemd."
    server_bin = ffoi_server_bin_path(conf)
    if not os.path.exists(server_bin):
        return False, f"Binaire serveur introuvable : {server_bin}"
    try:
        os.chmod(server_bin, os.stat(server_bin).st_mode | 0o111)
    except Exception:
        pass
    ffoi_write_server_json(conf)
    os.makedirs(os.path.dirname(ffoi_resolve_path(conf.get("LOG_FILE") or FFOI_LOG_FILE_DEFAULT, NAS_CONF_DIR)), exist_ok=True)
    with open(unit_path, "w", encoding="utf-8") as handle:
        handle.write(ffoi_render_unit(conf))
    rc, out = ffoi_run(["systemctl", "daemon-reload"], timeout=20)
    if rc != 0:
        return False, out or "daemon-reload échoué."
    if enable:
        rc, out = ffoi_run(["systemctl", "enable", ffoi_service_name(conf)], timeout=20)
        if rc != 0:
            return False, out or "systemctl enable échoué."
    return True, f"Service installé : {unit_path}"


def ffoi_remove_service(conf: Dict[str, str]) -> Tuple[bool, str]:
    """Supprime proprement le service systemd FFmpeg over IP.

    On ne désinstalle pas le paquet ffmpeg ni les fichiers de configuration Yoleo :
    on retire uniquement l'unité systemd, comme un bouton "Supprimer service"
    doit le faire côté interface.
    """
    if os.geteuid() != 0:
        return False, "Action refusée : il faut être root pour supprimer le service systemd."

    service = ffoi_service_name(conf)
    unit_path = ffoi_service_unit_path(conf)
    messages: List[str] = []

    rc, out = ffoi_run(["systemctl", "disable", "--now", service], timeout=30)
    if out:
        messages.append(out.strip())

    try:
        if os.path.exists(unit_path):
            os.remove(unit_path)
            messages.append(f"Service supprimé : {unit_path}")
        else:
            messages.append(f"Service déjà absent : {unit_path}")
    except Exception as exc:
        return False, f"Impossible de supprimer le fichier service : {exc}"

    ffoi_run(["systemctl", "daemon-reload"], timeout=20)
    ffoi_run(["systemctl", "reset-failed", service], timeout=20)

    return True, "\n".join(m for m in messages if m) or "Service supprimé."


def ffoi_install_ffmpeg() -> Tuple[bool, str]:
    if os.geteuid() != 0:
        return False, "Action refusée : il faut être root pour installer FFmpeg."
    if shutil.which("apt-get"):
        env = dict(os.environ)
        env["DEBIAN_FRONTEND"] = "noninteractive"
        try:
            proc = subprocess.run(["apt-get", "install", "-y", "ffmpeg"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600, env=env)
            ffoi_log("$ apt-get install -y ffmpeg\n" + (proc.stdout or ""))
            return proc.returncode == 0, proc.stdout or "apt-get install -y ffmpeg"
        except subprocess.TimeoutExpired:
            return False, "Timeout pendant l'installation de FFmpeg."
    return False, "apt-get introuvable : installation automatique FFmpeg non disponible sur ce système."


def ffoi_apply_firewall_rule(conf: Dict[str, str]) -> Tuple[bool, str]:
    port = ffoi_safe_int(conf.get("PORT"), 3333)
    try:
        import system as system_module
        load_config = getattr(system_module, "firewall_load_config")
        save_config = getattr(system_module, "firewall_save_config")
        apply_rules = getattr(system_module, "firewall_apply_rules")
        make_rule = getattr(system_module, "firewall_make_rule")
        data = load_config(create=True)
        rules = data.setdefault("rules", [])
        kept = []
        for rule in rules:
            label = str(rule.get("label") or "")
            if label.strip().lower() == FFOI_FIREWALL_LABEL.lower():
                continue
            kept.append(rule)
        rule = make_rule(port, port, "tcp", FFOI_FIREWALL_LABEL, True)
        kept.append(rule)
        data["rules"] = kept
        save_config(data)
        ok, message = apply_rules(start_service=True)
        return bool(ok), message or f"Règle pare-feu FFmpeg over IP TCP/{port} appliquée."
    except Exception as exc:
        return False, f"Pare-feu non mis à jour automatiquement : {exc}"


def ffoi_firewall_status(conf: Dict[str, str]) -> Dict[str, Any]:
    port = ffoi_safe_int(conf.get("PORT"), 3333)
    status = {"port": port, "configured": False, "enabled": False, "message": "inconnu"}
    try:
        import system as system_module
        data = getattr(system_module, "firewall_load_config")(create=True)
        status["enabled"] = bool(data.get("enabled"))
        for rule in data.get("rules") or []:
            if int(rule.get("start") or 0) <= port <= int(rule.get("end") or 0) and str(rule.get("proto") or "tcp") in {"tcp", "both"}:
                if str(rule.get("label") or "").strip().lower() == FFOI_FIREWALL_LABEL.lower():
                    status["configured"] = True
                    status["message"] = "règle Yoleo présente"
                    return status
        status["message"] = "règle absente"
    except Exception as exc:
        status["message"] = str(exc)
    return status


def ffoi_detect_gpus() -> List[str]:
    gpus: List[str] = []
    if shutil.which("lspci"):
        rc, out = ffoi_run(["lspci"], timeout=8, log=False)
        if rc == 0:
            for line in out.splitlines():
                low = line.lower()
                if "vga compatible controller" in low or "3d controller" in low or "display controller" in low:
                    gpus.append(line.strip())
    if os.path.isdir("/dev/dri"):
        try:
            dri = ", ".join(sorted(os.listdir("/dev/dri")))
            if dri:
                gpus.append("/dev/dri : " + dri)
        except Exception:
            pass
    if shutil.which("nvidia-smi"):
        rc, out = ffoi_run(["nvidia-smi", "-L"], timeout=8, log=False)
        if rc == 0 and out.strip():
            gpus.extend([line.strip() for line in out.splitlines() if line.strip()])
    return gpus or ["Aucune carte graphique détectée par l'interface pour l'instant."]


def ffoi_port_listening(port: int) -> bool:
    if shutil.which("ss"):
        rc, out = ffoi_run(["ss", "-ltn"], timeout=8, log=False)
        return rc == 0 and (f":{port} " in out or f":{port}\n" in out)
    return False


def ffoi_status(conf: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    conf = conf or ffoi_read_config(create=True)
    service = ffoi_service_name(conf)
    server_bin = ffoi_server_bin_path(conf)
    client_bin = ffoi_client_bin_path(conf)
    json_path = ffoi_server_json_path(conf)
    ffmpeg_path = conf.get("FFMPEG_PATH") or "/usr/bin/ffmpeg"
    ffmpeg_exists = bool(shutil.which(ffmpeg_path) or os.path.exists(ffmpeg_path))
    service_path = ffoi_service_unit_path(conf)
    port = ffoi_safe_int(conf.get("PORT"), 3333)
    return {
        "arch": ffoi_architecture(),
        "arch_ok": ffoi_is_amd64(),
        "ffmpeg_exists": ffmpeg_exists,
        "ffmpeg_path": ffmpeg_path,
        "server_bin": server_bin,
        "server_bin_exists": os.path.exists(server_bin),
        "client_bin": client_bin,
        "client_bin_exists": os.path.exists(client_bin),
        "client_address": ffoi_client_payload(conf).get("address", ""),
        "json_path": json_path,
        "json_exists": os.path.exists(json_path),
        "config_file": FFOI_CONFIG_FILE,
        "service": service,
        "service_path": service_path,
        "service_installed": os.path.exists(service_path),
        "active": ffoi_systemctl_is(service, "active"),
        "enabled": ffoi_systemctl_is(service, "enabled"),
        "port": port,
        "listening": ffoi_port_listening(port),
        "firewall": ffoi_firewall_status(conf),
        "rewrites_count": len(ffoi_rewrites(conf)),
    }


def ffoi_detect_samba_shares() -> List[Dict[str, str]]:
    shares: List[Dict[str, str]] = []
    server_name = ""
    source_conf = nas_conf_file("samba.conf")
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read(source_conf, encoding="utf-8")
        if parser.has_section("global"):
            g = parser["global"]
            server_name = str(g.get("netbios_name") or g.get("wsdd_name") or "").strip()
        for section in parser.sections():
            if not section.startswith("share:"):
                continue
            name = section.split(":", 1)[1].strip()
            path = str(parser[section].get("path") or "").strip()
            if name and path:
                shares.append({
                    "name": name,
                    "server": server_name,
                    "client_path": f"\\\\{server_name or socket.gethostname()}\\{name}\\",
                    "server_path": ffoi_normalize_server_root(path),
                    "source": source_conf,
                })
    except Exception:
        pass

    # Fallback : smb.conf généré / existant, utile si la source Yoleo n'existe pas encore.
    smb_conf = "/etc/samba/smb.conf"
    if os.path.exists(smb_conf):
        parser2 = configparser.ConfigParser(interpolation=None, strict=False)
        parser2.optionxform = str
        try:
            parser2.read(smb_conf, encoding="utf-8")
            if not server_name and parser2.has_section("global"):
                g = parser2["global"]
                server_name = str(g.get("netbios name") or g.get("netbios_name") or "").strip()
            existing = {item["name"].lower() for item in shares}
            for section in parser2.sections():
                if section.lower() in {"global", "printers", "print$"}:
                    continue
                path = str(parser2[section].get("path") or "").strip()
                if not path or section.lower() in existing:
                    continue
                shares.append({
                    "name": section,
                    "server": server_name,
                    "client_path": f"\\\\{server_name or socket.gethostname()}\\{section}\\",
                    "server_path": ffoi_normalize_server_root(path),
                    "source": smb_conf,
                })
        except Exception:
            pass
    shares.sort(key=lambda item: item.get("name", "").lower())
    return shares


def ffoi_detect_nfs_mounts() -> List[Dict[str, str]]:
    """Détecte uniquement les montages NFS déjà montés côté NAS.

    Pour FFmpeg over IP, Yoleo ne peut pas deviner le chemin côté client NFS.
    Mais il peut proposer proprement les racines serveur déjà montées :
      serveur:/export/media -> /mnt/nfs/media
    L'utilisateur complète ensuite le chemin client à la main.
    """
    mounts: List[Dict[str, str]] = []
    seen = set()

    def add_mount(target: str, source: str, fstype: str) -> None:
        target = str(target or "").strip()
        source = str(source or "").strip()
        fstype = str(fstype or "").strip()
        if not target or not source or not fstype.lower().startswith("nfs"):
            return
        key = (target, source, fstype)
        if key in seen:
            return
        seen.add(key)
        mounts.append({
            "target": target,
            "source": source,
            "fstype": fstype,
            "label": f"{fstype} — {target} ← {source}",
        })

    if shutil.which("findmnt"):
        rc, out = ffoi_run(["findmnt", "-rn", "-t", "nfs,nfs4", "-o", "TARGET,SOURCE,FSTYPE"], timeout=8, log=False)
        if rc == 0:
            for line in out.splitlines():
                parts = line.split(None, 2)
                if len(parts) >= 3:
                    add_mount(parts[0], parts[1], parts[2])

    # Fallback simple si findmnt n'est pas disponible ou ne retourne rien.
    if not mounts:
        try:
            with open("/proc/self/mounts", "r", encoding="utf-8", errors="replace") as handle:
                for raw in handle:
                    parts = raw.split()
                    if len(parts) >= 3 and parts[2].lower().startswith("nfs"):
                        add_mount(parts[1].replace("\\040", " "), parts[0], parts[2])
        except Exception:
            pass

    mounts.sort(key=lambda item: item.get("target", ""))
    return mounts


def ffoi_detect_remote_mounts() -> List[Dict[str, str]]:
    mounts: List[Dict[str, str]] = []
    if shutil.which("findmnt"):
        rc, out = ffoi_run(["findmnt", "-rn", "-t", "cifs,smb3", "-o", "TARGET,SOURCE,FSTYPE"], timeout=8, log=False)
        if rc == 0:
            for line in out.splitlines():
                parts = line.split(None, 2)
                if len(parts) >= 3:
                    mounts.append({"target": parts[0], "source": parts[1], "fstype": parts[2]})
    return mounts


def ffoi_read_log_lines(conf: Dict[str, str], max_lines: int = 300) -> str:
    log_file = ffoi_resolve_path(conf.get("LOG_FILE") or FFOI_LOG_FILE_DEFAULT, NAS_CONF_DIR)
    chunks: List[str] = []
    if os.path.exists(log_file):
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()[-max_lines:]
            chunks.append("\n".join(lines))
        except Exception as exc:
            chunks.append(f"Lecture log fichier impossible : {exc}")
    service = ffoi_service_name(conf)
    if shutil.which("journalctl"):
        rc, out = ffoi_run(["journalctl", "-u", service, "-n", str(max_lines), "--no-pager"], timeout=12, log=False)
        if rc == 0 and out.strip():
            chunks.append("\n--- journalctl ---\n" + out.strip())
    return "\n".join(chunk for chunk in chunks if chunk.strip()) or "Aucun log FFmpeg over IP pour l'instant."


def ffoi_apply_config(conf: Dict[str, str], restart_if_active: bool = True) -> Tuple[bool, str]:
    """Enregistrer + appliquer = vraie application NAS.

    Cette action ne se limite pas à écrire les fichiers : elle régénère le JSON,
    remet la règle pare-feu, installe/met à jour l'unit systemd, active le boot,
    puis redémarre le service pour que les nouveaux réglages soient réellement pris.
    FFmpeg lui-même n'est pas installé ici : cela reste une action séparée.
    """
    output_error = ffoi_output_dir_error(conf)
    if output_error:
        return False, output_error
    ffoi_write_kv_file(FFOI_CONFIG_FILE, conf)
    json_path = ffoi_write_server_json(conf)
    ok_fw, msg_fw = ffoi_apply_firewall_rule(conf)
    service = ffoi_service_name(conf)
    messages = [
        f"Configuration écrite : {FFOI_CONFIG_FILE}",
        f"JSON serveur écrit : {json_path}",
        ("Pare-feu OK : " if ok_fw else "Pare-feu : ") + msg_fw,
    ]

    ok_svc, msg_svc = ffoi_install_service(conf, enable=True)
    messages.append(("Service OK : " if ok_svc else "Service : ") + msg_svc)
    if not ok_svc:
        return False, "\n".join(messages)

    if restart_if_active:
        rc, out = ffoi_systemctl(service, "restart", timeout=30)
        messages.append(("Redémarrage OK" if rc == 0 else "Redémarrage échoué") + (": " + out.strip() if out.strip() else ""))
        return rc == 0 and ok_fw and ok_svc, "\n".join(messages)

    return ok_fw and ok_svc, "\n".join(messages)


def ffoi_ensure_menu_entry() -> None:
    """Ne crée jamais d'entrée dans le menu latéral.

    La sidebar est administrée uniquement par conf/menu, initialisé depuis
    default_menu_sidebar si l'utilisateur le veut. Un module peut démarrer sans
    s'enregistrer tout seul dans le menu : pas de fallback, pas de réparation,
    pas d'ajout automatique type ``035_FFmpeg over IP.conf``.
    """
    return


# Ne pas appeler ffoi_ensure_menu_entry() au démarrage : source unique du menu.


def _render_ffmpeg():
    conf = ffoi_read_config(create=True)
    active_subtab = services_requested_subtab({"main", "share", "info"}, "main")
    samba_shares = ffoi_detect_samba_shares()
    nfs_mounts = ffoi_detect_nfs_mounts()
    remote_mounts = ffoi_detect_remote_mounts()

    # Valeurs dédiées au mode NFS/manuel. On ne doit jamais recopier
    # automatiquement un ancien chemin Samba dans le champ client NFS.
    manual_client_value = str(conf.get("CLIENT_PATH") or "").strip()
    if conf.get("SHARE_MODE") != "manual" or manual_client_value.startswith("\\"):
        manual_client_value = "/"

    manual_server_value = str(conf.get("SERVER_PATH") or "").strip() if conf.get("SHARE_MODE") == "manual" else ""
    if conf.get("SHARE_MODE") == "manual" and conf.get("NFS_MOUNT"):
        for mount in nfs_mounts:
            if mount.get("target") == conf.get("NFS_MOUNT"):
                manual_server_value = ffoi_normalize_server_root(str(mount.get("target") or ""))
                break

    ctx = {
        "conf": conf,
        "status": ffoi_status(conf),
        "active_subtab": active_subtab,
        "service_active": "ffmpeg",
        "samba_shares": samba_shares,
        "nfs_mounts": nfs_mounts,
        "remote_mounts": remote_mounts,
        "manual_client_value": manual_client_value,
        "manual_server_value": manual_server_value,
        "gpus": ffoi_detect_gpus(),
        "server_json_preview": json.dumps({
            "log": "stdout",
            "address": f"{conf.get('ADDRESS') or '0.0.0.0'}:{ffoi_safe_int(conf.get('PORT'), 3333)}",
            "authSecret": "********" if conf.get("AUTH_SECRET") else "",
            "ffmpegPath": conf.get("FFMPEG_PATH") or "/usr/bin/ffmpeg",
            "rewrites": ffoi_rewrites(conf),
        }, ensure_ascii=False, indent=2),
        "unit_preview": ffoi_render_unit(conf),
        "client_json_preview": ffoi_client_jsonc(conf),
        "service_workdir": ffoi_service_workdir(conf),
    }
    if active_subtab == "share":
        return render_template("services_ffmpeg_share.html", **ctx)
    if active_subtab == "info":
        return render_template("services_ffmpeg_info.html", **ctx)
    return render_template("services_ffmpeg_main.html", **ctx)


@services_bp.route("/services/ffmpeg/client/download", methods=["GET"])
def ffoi_download_client():
    conf = ffoi_read_config(create=True)
    client_bin = ffoi_client_bin_path(conf)
    if not os.path.exists(client_bin):
        flash(f"❌ Client Windows introuvable : {client_bin}", "error")
        return ffoi_redirect("main")
    try:
        import io
        import zipfile
        from flask import send_file
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(client_bin, arcname="ffmpeg-over-ip-client.exe")
            zf.writestr("ffmpeg-over-ip.client.jsonc", ffoi_client_jsonc(conf))
        buffer.seek(0)
        filename = f"ffmpeg-over-ip-client-{ffoi_safe_int(conf.get('PORT'), 3333)}.zip"
        return send_file(buffer, mimetype="application/zip", as_attachment=True, download_name=filename)
    except Exception as exc:
        flash(f"❌ Génération du zip client impossible : {exc}", "error")
        return ffoi_redirect("main")


@services_bp.route("/services/ffmpeg/settings/save", methods=["POST"])
def ffoi_settings_save():
    conf = ffoi_collect_settings_from_form(ffoi_read_config(create=True))
    output_error = ffoi_output_dir_error(conf)
    if output_error:
        flash("❌ " + output_error, "error")
        return ffoi_redirect("main")
    ffoi_write_kv_file(FFOI_CONFIG_FILE, conf)
    ffoi_write_server_json(conf)
    if request.form.get("apply") == "1":
        ok, msg = ffoi_apply_config(conf, restart_if_active=True)
        flash(("✅ " if ok else "❌ ") + msg, "success" if ok else "error")
    else:
        flash("✅ Réglages FFmpeg over IP enregistrés.", "success")
    return ffoi_redirect("main")


@services_bp.route("/services/ffmpeg/share/save", methods=["POST"])
def ffoi_share_save():
    conf = ffoi_collect_share_from_form(ffoi_read_config(create=True))
    ffoi_write_kv_file(FFOI_CONFIG_FILE, conf)
    ffoi_write_server_json(conf)
    if request.form.get("apply") == "1":
        ok, msg = ffoi_apply_config(conf, restart_if_active=True)
        flash(("✅ " if ok else "❌ ") + msg, "success" if ok else "error")
    else:
        flash("✅ Partage/rewrite FFmpeg over IP enregistré.", "success")
    return ffoi_redirect("share")


@services_bp.route("/services/ffmpeg/action", methods=["POST"])
def ffoi_action():
    conf = ffoi_read_config(create=True)
    action = str(request.form.get("action") or "").strip().lower()
    target = str(request.form.get("return_to") or "main").strip().lower()
    ok = False
    msg = "Action inconnue."
    service = ffoi_service_name(conf)
    if action == "install_ffmpeg":
        ok, msg = ffoi_install_ffmpeg()
    elif action == "install_service":
        ffoi_write_kv_file(FFOI_CONFIG_FILE, conf)
        ffoi_write_server_json(conf)
        ok, msg = ffoi_install_service(conf, enable=True)
    elif action == "remove_service":
        ok, msg = ffoi_remove_service(conf)
    elif action == "apply":
        ok, msg = ffoi_apply_config(conf, restart_if_active=True)
    elif action == "start":
        ok_fw, msg_fw = ffoi_apply_firewall_rule(conf)
        # On régénère toujours l'unit avant démarrage : cela corrige automatiquement
        # les anciens services créés avec l'ExecStart invalide sans -config.
        enable_now = True if not os.path.exists(ffoi_service_unit_path(conf)) else (ffoi_systemctl_is(service, "enabled") == "enabled")
        ok_inst, msg_inst = ffoi_install_service(conf, enable=enable_now)
        if ok_inst:
            rc, out = ffoi_systemctl(service, "start", timeout=30)
            ok = rc == 0 and ok_fw
            msg = msg_inst + "\n" + msg_fw + "\n" + (out or "Service démarré.")
        else:
            ok = False
            msg = msg_inst
    elif action == "stop":
        rc, out = ffoi_systemctl(service, "stop", timeout=30)
        ok = rc == 0
        msg = out or "Service arrêté."
    elif action == "restart":
        ok_fw, msg_fw = ffoi_apply_firewall_rule(conf)
        enable_now = ffoi_systemctl_is(service, "enabled") == "enabled"
        ok_inst, msg_inst = ffoi_install_service(conf, enable=enable_now)
        if ok_inst:
            rc, out = ffoi_systemctl(service, "restart", timeout=30)
            ok = rc == 0 and ok_fw
            msg = msg_inst + "\n" + msg_fw + "\n" + (out or "Service redémarré.")
        else:
            ok = False
            msg = msg_inst
    elif action == "enable":
        rc, out = ffoi_systemctl(service, "enable", timeout=30)
        ok = rc == 0
        msg = out or "Service activé au démarrage."
    elif action == "disable":
        rc, out = ffoi_systemctl(service, "disable", timeout=30)
        ok = rc == 0
        msg = out or "Service désactivé au démarrage."
    flash(("✅ " if ok else "❌ ") + msg, "success" if ok else "error")
    return ffoi_redirect(target if target in {"main", "share", "info"} else "main")


@services_bp.route("/services/ffmpeg/share/test", methods=["POST"])
def ffoi_share_test():
    conf = ffoi_collect_share_from_form(ffoi_read_config(create=True))
    sample = str(request.form.get("sample") or "").strip().lstrip("/\\")
    server_root = ffoi_normalize_server_root(conf.get("SERVER_PATH") or "")
    client_root = ffoi_normalize_unc(conf.get("CLIENT_PATH") or "")
    server_test = os.path.join(server_root, sample.replace("\\", "/")) if sample else server_root
    ok = bool(server_root and os.path.exists(server_test))
    return jsonify({
        "ok": ok,
        "client_root": client_root,
        "server_root": server_root,
        "sample": sample,
        "server_test": server_test,
        "message": "Correspondance OK : chemin serveur trouvé." if ok else "Chemin serveur introuvable. Vérifie la racine ou le fichier test.",
    })
