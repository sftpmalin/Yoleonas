import ipaddress
import json
import os
import platform
import shlex
import signal
import subprocess
import sys
import threading
import time
from typing import Dict, List, Tuple
import urllib.error
import urllib.parse
import urllib.request

from flask import Blueprint, Response, has_request_context, jsonify, redirect, render_template, request, session

terminal_bp = Blueprint("terminal_bp", __name__)

# V5 : terminal host direct + arrêt automatique après inactivité.
# Les chemins relatifs sont résolus depuis le dossier du module Flask
# (/dockers/system), pas depuis le dossier du terminal.conf.

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ==========================================================
# 📁 CONF CENTRALISÉE
# ==========================================================
# app.py pose NAS_CONF_DIR. Les modules le lisent sans importer app.py
# pour éviter les imports circulaires pendant le chargement des blueprints.
_NAS_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_NAS_DEFAULT_CONF_DIR = os.path.abspath(os.path.join(_NAS_MODULE_DIR, "..", "conf"))
NAS_CONF_DIR = os.path.abspath(os.path.expanduser(os.path.expandvars(os.environ.get("NAS_CONF_DIR", _NAS_DEFAULT_CONF_DIR))))
NAS_ROOT_DIR = os.path.abspath(os.path.join(NAS_CONF_DIR, ".."))

def nas_conf_file(name: str) -> str:
    return os.path.join(NAS_CONF_DIR, name)

def nas_root_path(*parts: str) -> str:
    return os.path.join(NAS_ROOT_DIR, *parts)

DEFAULT_CONFIG_CANDIDATES = [
    os.environ.get("TERMINAL_CONFIG_PATH", "").strip(),
    nas_conf_file("terminal.conf"),
    os.path.join(BASE_DIR, "..", "conf", "terminal.conf"),
    os.path.join(BASE_DIR, "conf", "terminal.conf"),
]

DEFAULT_CONFIG = {
    "TERMINAL_TITLE": "Terminal NAS",
    "TERMINAL_PORT": "7681",
    "TERMINAL_LISTEN": "127.0.0.1",
    "TERMINAL_PUBLIC_URL": "",
    "TERMINAL_PUBLIC_DYNAMIC_URL": "",
    "TERMINAL_TTYD_PROXY_MIN_PORT": "7700",
    "TERMINAL_TTYD_PROXY_MAX_PORT": "8999",
    "TERMINAL_ENABLE_URL_ARGS": "1",
    "TERMINAL_START_DIR": "/root",
    "TERMINAL_SHELL": "/bin/bash",
    "TERMINAL_LOGIN_REQUIRED": "0",
    "TERMINAL_LOGIN_USER": "admin",
    "TERMINAL_LOGIN_PASSWORD": "admin",
    "TERMINAL_WRITABLE": "1",
    "TERMINAL_MAX_CLIENTS": "8",
    "TERMINAL_RECONNECT": "1",
    "TERMINAL_DISABLE_LEAVE_ALERT": "1",
    "TERMINAL_AUTOSTART_ON_PAGE": "1",
    "TERMINAL_DISCONNECTION_MINUTES": "5",
    "TERMINAL_IDLE_CHECK_SECONDS": "30",
    "TERMINAL_TTYD_WS_IDLE_TIMEOUT_SECONDS": "0",
    "TERMINAL_LAST_SEEN_FILE": "/tmp/flask_system_ttyd.lastseen",
    "TERMINAL_BIN_DIR": "../bin",
    "TERMINAL_BIN_X86_64": "ttyd.x86_64",
    "TERMINAL_BIN_AARCH64": "ttyd.aarch64",
    "TERMINAL_PID_FILE": "/tmp/flask_system_ttyd.pid",
    "TERMINAL_LOG_FILE": "/var/log/flask-system/terminal.log",
    "TERMINAL_THEME_BACKGROUND": "#000000",
    "TERMINAL_THEME_FOREGROUND": "#00ff00",
    "TERMINAL_THEME_CURSOR": "#ffffff",
    "TERMINAL_EXTRA_ARGS": "",
}

WRITABLE_KEYS = {
    "TERMINAL_TITLE",
    "TERMINAL_PORT",
    "TERMINAL_START_DIR",
    "TERMINAL_SHELL",
    "TERMINAL_LOGIN_REQUIRED",
    "TERMINAL_LOGIN_USER",
    "TERMINAL_LOGIN_PASSWORD",
    "TERMINAL_WRITABLE",
    "TERMINAL_MAX_CLIENTS",
    "TERMINAL_RECONNECT",
    "TERMINAL_DISABLE_LEAVE_ALERT",
    "TERMINAL_AUTOSTART_ON_PAGE",
    "TERMINAL_DISCONNECTION_MINUTES",
    "TERMINAL_THEME_BACKGROUND",
    "TERMINAL_THEME_FOREGROUND",
    "TERMINAL_THEME_CURSOR",
}

IDLE_THREAD_STARTED = False
IDLE_THREAD_LOCK = threading.Lock()



def strip_quotes(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def bool_conf(conf: Dict[str, str], key: str, default: str = "0") -> bool:
    return str(conf.get(key, default)).strip().lower() in {"1", "true", "yes", "on", "oui"}


def int_conf(conf: Dict[str, str], key: str, default: int = 0, minimum: int = 0, maximum: int = 0) -> int:
    raw = str(conf.get(key, str(default))).strip()
    try:
        value = int(float(raw))
    except Exception:
        value = default
    if value < minimum:
        value = minimum
    if maximum and value > maximum:
        value = maximum
    return value


def resolve_path(path: str, base_file: str = "") -> str:
    path = strip_quotes(path)
    if not path:
        return path
    path = os.path.expanduser(os.path.expandvars(path))
    if os.path.isabs(path):
        return os.path.abspath(path)

    # Convention du NAS : les chemins relatifs des confs sont relatifs au dossier
    # du module Flask, pas au dossier où terminal.conf a été trouvé.
    # Exemple avec terminal.py dans /dockers/system :
    #   ../bin  -> /dockers/bin
    #   les logs restent en chemin Linux standard : /var/log/flask-system/terminal.log
    # Ça évite le bug /dockers/system/bin quand terminal.conf se retrouve dans
    # /dockers/system/conf au lieu de /dockers/conf.
    return os.path.abspath(os.path.join(BASE_DIR, path))


def get_config_path() -> str:
    for candidate in DEFAULT_CONFIG_CANDIDATES:
        if candidate and os.path.exists(candidate):
            return candidate
    # Chemin propre par défaut : <base>/conf/terminal.conf quand le module est dans <base>/system.
    return os.path.abspath(os.path.join(BASE_DIR, "..", "conf", "terminal.conf"))


CONFIG_FILE = get_config_path()


def read_config_file(path: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not path or not os.path.exists(path):
        return data
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key:
                data[key] = strip_quotes(value)
    return data


def get_config() -> Dict[str, str]:
    conf = DEFAULT_CONFIG.copy()
    file_conf = read_config_file(CONFIG_FILE)
    conf.update(file_conf)

    # Alias volontairement acceptés, parce que tu voulais pouvoir penser
    # simplement "DISCONNECTION=5" dans la conf. La clé propre affichée
    # dans l'interface reste TERMINAL_DISCONNECTION_MINUTES.
    for alias in ("DISCONNECTION", "DISCONNEXION", "DECONNEXION", "TERMINAL_DISCONNECTION"):
        if alias in file_conf and file_conf[alias].strip():
            conf["TERMINAL_DISCONNECTION_MINUTES"] = file_conf[alias].strip()
            break

    conf["TERMINAL_BIN_DIR_RESOLVED"] = resolve_path(conf.get("TERMINAL_BIN_DIR", "../bin"), CONFIG_FILE)
    conf["TERMINAL_LOG_FILE_RESOLVED"] = resolve_path(conf.get("TERMINAL_LOG_FILE", "/var/log/flask-system/terminal.log"), CONFIG_FILE)
    conf["TERMINAL_LAST_SEEN_FILE_RESOLVED"] = resolve_path(conf.get("TERMINAL_LAST_SEEN_FILE", "/tmp/flask_system_ttyd.lastseen"), CONFIG_FILE)
    conf["TERMINAL_START_DIR_RESOLVED"] = resolve_start_dir(conf.get("TERMINAL_START_DIR", "/root"))
    conf["TERMINAL_BIN_RESOLVED"] = find_ttyd_binary(conf)
    conf["TERMINAL_BIN_SEARCH_DIRS"] = ", ".join(binary_search_dirs(conf))
    conf["CONFIG_FILE"] = CONFIG_FILE
    return conf


def resolve_start_dir(start_dir: str) -> str:
    path = os.path.expanduser(os.path.expandvars(strip_quotes(start_dir) or "/root"))
    if os.path.isdir(path):
        return os.path.abspath(path)
    if os.path.isdir("/root"):
        return "/root"
    return "/"


def machine_arch() -> str:
    arch = platform.machine().lower()
    if arch in {"x86_64", "amd64"}:
        return "x86_64"
    if arch in {"aarch64", "arm64"}:
        return "aarch64"
    return arch


def unique_existing_order(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        item = os.path.abspath(os.path.expanduser(os.path.expandvars(strip_quotes(item)))) if item else ""
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def binary_search_dirs(conf: Dict[str, str]) -> List[str]:
    raw_bin_dir = conf.get("TERMINAL_BIN_DIR", "../bin")
    dirs = [
        resolve_path(raw_bin_dir, CONFIG_FILE),                 # /dockers/bin avec ../bin
        os.path.abspath(os.path.join(BASE_DIR, "..", "bin")),  # sécurité explicite
        os.path.abspath(os.path.join(BASE_DIR, "bin")),         # ancien mauvais chemin, en fallback
    ]
    if CONFIG_FILE:
        dirs.append(os.path.abspath(os.path.join(os.path.dirname(CONFIG_FILE), raw_bin_dir)))
    return unique_existing_order(dirs)


def find_ttyd_binary(conf: Dict[str, str]) -> str:
    arch = machine_arch()
    preferred = conf.get("TERMINAL_BIN_AARCH64" if arch == "aarch64" else "TERMINAL_BIN_X86_64", "")
    names = []
    if preferred:
        names.append(preferred)
    names.extend(["ttyd", "ttyd.x86_64", "ttyd.aarch64"])

    candidates: List[str] = []
    for bin_dir in binary_search_dirs(conf):
        for name in names:
            candidates.append(os.path.join(bin_dir, name))
    candidates.append(shutil_which("ttyd") or "")

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return os.path.abspath(candidate)

    # Message d'erreur propre : le premier chemin affiché sera le bon chemin NAS.
    first_dir = binary_search_dirs(conf)[0] if binary_search_dirs(conf) else os.path.join(BASE_DIR, "..", "bin")
    first_name = preferred or "ttyd.x86_64"
    return os.path.abspath(os.path.join(first_dir, first_name))


def shutil_which(name: str) -> str:
    try:
        import shutil
        return shutil.which(name) or ""
    except Exception:
        return ""


def pid_file(conf: Dict[str, str]) -> str:
    return conf.get("TERMINAL_PID_FILE", "/tmp/flask_system_ttyd.pid")


def read_pid(conf: Dict[str, str]) -> int:
    try:
        with open(pid_file(conf), "r", encoding="utf-8") as handle:
            return int(handle.read().strip())
    except Exception:
        return 0


def process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def last_seen_file(conf: Dict[str, str]) -> str:
    return conf.get("TERMINAL_LAST_SEEN_FILE_RESOLVED", conf.get("TERMINAL_LAST_SEEN_FILE", "/tmp/flask_system_ttyd.lastseen"))


def touch_presence(conf: Dict[str, str]) -> None:
    path = last_seen_file(conf)
    try:
        os.makedirs(os.path.dirname(path) or "/tmp", exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(str(time.time()))
    except Exception:
        pass


def read_last_seen(conf: Dict[str, str]) -> float:
    try:
        with open(last_seen_file(conf), "r", encoding="utf-8") as handle:
            return float(handle.read().strip() or "0")
    except Exception:
        return 0.0


def idle_limit_seconds(conf: Dict[str, str]) -> int:
    minutes = int_conf(conf, "TERMINAL_DISCONNECTION_MINUTES", 5, minimum=0, maximum=10080)
    return minutes * 60


def idle_seconds(conf: Dict[str, str]) -> int:
    last_seen = read_last_seen(conf)
    if last_seen <= 0:
        return 0
    return max(0, int(time.time() - last_seen))


def append_log(conf: Dict[str, str], message: str) -> None:
    path = conf.get("TERMINAL_LOG_FILE_RESOLVED", "")
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8", errors="replace") as log:
            log.write(message.rstrip() + "\n")
    except Exception:
        pass


def ensure_idle_reaper() -> None:
    global IDLE_THREAD_STARTED
    with IDLE_THREAD_LOCK:
        if IDLE_THREAD_STARTED:
            return
        thread = threading.Thread(target=idle_reaper_loop, name="terminal-idle-reaper", daemon=True)
        thread.start()
        IDLE_THREAD_STARTED = True


def idle_reaper_loop() -> None:
    while True:
        try:
            conf = get_config()
            check_every = int_conf(conf, "TERMINAL_IDLE_CHECK_SECONDS", 30, minimum=5, maximum=3600)
            limit = idle_limit_seconds(conf)
            pid = read_pid(conf)
            running = process_running(pid)
            if not running and pid:
                try:
                    os.unlink(pid_file(conf))
                except OSError:
                    pass
            if limit > 0 and running:
                idle = idle_seconds(conf)
                if idle >= limit:
                    append_log(conf, f"=== Arrêt automatique ttyd : inactif depuis {idle} secondes, limite {limit} secondes ===")
                    stop_terminal(conf)
            time.sleep(check_every)
        except Exception:
            time.sleep(30)


def status_data(conf: Dict[str, str]) -> Dict[str, object]:
    pid = read_pid(conf)
    running = process_running(pid)
    if not running:
        try:
            os.unlink(pid_file(conf))
        except OSError:
            pass
        pid = 0
    bin_path = conf.get("TERMINAL_BIN_RESOLVED", "")
    idle = idle_seconds(conf)
    limit = idle_limit_seconds(conf)
    remaining = max(0, limit - idle) if limit > 0 and running else 0
    return {
        "running": running,
        "pid": pid,
        "port": conf.get("TERMINAL_PORT", "7681"),
        "listen": conf.get("TERMINAL_LISTEN", "127.0.0.1"),
        "start_dir": conf.get("TERMINAL_START_DIR_RESOLVED", "/"),
        "shell": conf.get("TERMINAL_SHELL", "/bin/bash"),
        "binary": bin_path,
        "binary_ok": os.path.isfile(bin_path),
        "config_file": conf.get("CONFIG_FILE", CONFIG_FILE),
        "log_file": conf.get("TERMINAL_LOG_FILE_RESOLVED", ""),
        "public_url": public_url(conf),
        "idle_seconds": idle,
        "idle_limit_seconds": limit,
        "idle_remaining_seconds": remaining,
        "disconnection_minutes": int_conf(conf, "TERMINAL_DISCONNECTION_MINUTES", 5, minimum=0, maximum=10080),
    }


def host_without_port(host: str) -> str:
    host = (host or "").strip()
    if host.startswith("[") and "]" in host:
        return host.split("]", 1)[0] + "]"
    return host.split(":", 1)[0]


def request_public_host() -> str:
    if not has_request_context():
        return ""
    raw = (request.headers.get("X-Forwarded-Host") or request.host or "").split(",", 1)[0].strip()
    return raw


def request_public_scheme() -> str:
    if not has_request_context():
        return "http"
    raw = (request.headers.get("X-Forwarded-Proto") or request.scheme or "http").split(",", 1)[0].strip().lower()
    return "https" if raw == "https" else "http"


def request_public_origin() -> str:
    host = request_public_host()
    if not host:
        return ""
    return f"{request_public_scheme()}://{host}"


def is_local_or_lan_host(host: str) -> bool:
    raw = host_without_port(host).strip().lower().strip("[]")
    if not raw:
        return False
    if raw in {"localhost", "localhost.localdomain"}:
        return True
    if raw.endswith(".local") or raw.endswith(".lan") or raw.endswith(".home"):
        return True
    if "." not in raw:
        return True
    try:
        ip = ipaddress.ip_address(raw)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


def should_use_public_reverse_proxy(conf: Dict[str, str]) -> bool:
    if not has_request_context():
        return bool(
            strip_quotes(conf.get("TERMINAL_PUBLIC_URL", ""))
            or strip_quotes(conf.get("TERMINAL_PUBLIC_DYNAMIC_URL", ""))
        )
    # Si Yoleo est ouvert en local/LAN, le terminal doit rester local.
    # En accès extérieur, Nginx Proxy Manager expose ttyd sous /terminal/ttyd/<port>/.
    return not is_local_or_lan_host(request_public_host())


def ttyd_base_path_for_port(conf: Dict[str, str], port: int) -> str:
    wanted_port = int(port or int_conf(conf, "TERMINAL_PORT", 7681, minimum=1, maximum=65535))
    main_port = int_conf(conf, "TERMINAL_PORT", 7681, minimum=1, maximum=65535)
    if wanted_port == main_port:
        return strip_quotes(conf.get("TERMINAL_BASE_PATH", "")).strip().rstrip("/")
    return f"/ttyd/{wanted_port}"


def append_url_path(base_url: str, path: str) -> str:
    clean_path = str(path or "").strip("/")
    if not clean_path:
        return base_url
    return base_url.rstrip("/") + "/" + clean_path + "/"


def automatic_ttyd_url_for_port(port: int, base_path: str = "") -> str:
    port = int(port or 7681)
    if has_request_context():
        # request.host = nom réellement utilisé dans le navigateur en accès direct local
        # ex: tower.local:5055 ou 192.168.1.2:5055.
        host = host_without_port(request.host or request_public_host()) or "127.0.0.1"
        return append_url_path(f"//{host}:{port}", base_path)
    return append_url_path(f"http://127.0.0.1:{port}", base_path)


def ttyd_proxy_port_allowed(conf: Dict[str, str], port: int) -> bool:
    port = int(port or 0)
    if port < 1 or port > 65535:
        return False
    main_port = int_conf(conf, "TERMINAL_PORT", 7681, minimum=1, maximum=65535)
    if port == main_port:
        return True
    min_port = int_conf(conf, "TERMINAL_TTYD_PROXY_MIN_PORT", 7700, minimum=1024, maximum=65535)
    max_port = int_conf(conf, "TERMINAL_TTYD_PROXY_MAX_PORT", 8999, minimum=1024, maximum=65535)
    if min_port > max_port:
        min_port, max_port = max_port, min_port
    return min_port <= port <= max_port


def automatic_ttyd_url(conf: Dict[str, str]) -> str:
    return automatic_ttyd_url_for_port(int_conf(conf, "TERMINAL_PORT", 7681, minimum=1, maximum=65535))


def ttyd_url_with_args(conf: Dict[str, str], args: List[str]) -> str:
    base = ttyd_url_for_port(conf, int_conf(conf, "TERMINAL_PORT", 7681, minimum=1, maximum=65535))
    query = urllib.parse.urlencode([("arg", str(arg)) for arg in args])
    if not query:
        return base
    separator = "&" if "?" in base else "?"
    return base + separator + query


def public_ttyd_url_for_port(conf: Dict[str, str], port: int) -> str:
    """URL publique ttyd sans exposer de port côté navigateur.

    En mode normal, la base publique est déduite du domaine réellement utilisé
    dans le navigateur, puis on monte ttyd sous /terminal/ttyd/<port>/.
    TERMINAL_PUBLIC_URL reste accepté comme override technique caché, surtout
    pour compatibilité avec d'anciens déploiements.
    """
    configured = strip_quotes(conf.get("TERMINAL_PUBLIC_URL", ""))
    dynamic = strip_quotes(
        conf.get("TERMINAL_PUBLIC_DYNAMIC_URL", "")
        or conf.get("TERMINAL_PUBLIC_TTYD_URL", "")
        or conf.get("TERMINAL_PUBLIC_PORT_URL", "")
    )
    wanted_port = int(port or 0)
    main_port = int_conf(conf, "TERMINAL_PORT", 7681, minimum=1, maximum=65535)
    auto_base = request_public_origin() if has_request_context() else ""

    if dynamic:
        return dynamic.replace("{port}", str(wanted_port))
    if configured.lower() in {"auto", "public", "reverse-proxy"}:
        configured = ""
    if configured and "{port}" in configured:
        return configured.replace("{port}", str(wanted_port))
    if configured and wanted_port == main_port:
        return configured
    if configured:
        return append_url_path(configured, ttyd_base_path_for_port(conf, wanted_port))
    if auto_base:
        return append_url_path(auto_base, f"/terminal/ttyd/{wanted_port}")
    return ""


def ttyd_url_for_port(conf: Dict[str, str], port: int = 0) -> str:
    """Retourne l'URL ttyd adaptee au contexte.

    Depuis une page Yoleo, le navigateur passe toujours par la route Flask
    protegee /terminal/ttyd/<port>/ au lieu d'ouvrir directement le port ttyd.
    """
    wanted_port = int(port or int_conf(conf, "TERMINAL_PORT", 7681, minimum=1, maximum=65535))
    if has_request_context():
        public_url_value = public_ttyd_url_for_port(conf, wanted_port)
        if public_url_value:
            return public_url_value
    base_path = ttyd_base_path_for_port(conf, wanted_port)
    if should_use_public_reverse_proxy(conf):
        public_url_value = public_ttyd_url_for_port(conf, wanted_port)
        if public_url_value:
            return public_url_value
    return automatic_ttyd_url_for_port(wanted_port, base_path)


def public_url(conf: Dict[str, str]) -> str:
    return ttyd_url_for_port(conf, int_conf(conf, "TERMINAL_PORT", 7681, minimum=1, maximum=65535))


def process_cmdline(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as handle:
            return handle.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except Exception:
        return ""


def terminal_process_supports_url_args(pid: int) -> bool:
    cmdline = process_cmdline(pid)
    return " -a " in f" {cmdline} " and "terminal_entrypoint.py" in cmdline


def ensure_terminal_url_args(conf: Dict[str, str]) -> Tuple[bool, str]:
    if not bool_conf(conf, "TERMINAL_ENABLE_URL_ARGS", "1"):
        return False, "Arguments URL ttyd désactivés."
    data = status_data(conf)
    pid = int(data.get("pid") or 0)
    if data.get("running") and terminal_process_supports_url_args(pid):
        return True, "Terminal prêt pour les arguments URL."
    if data.get("running"):
        stop_terminal(conf)
        time.sleep(0.5)
    return start_terminal(get_config())


def build_command(conf: Dict[str, str]) -> List[str]:
    ttyd_bin = conf.get("TERMINAL_BIN_RESOLVED", "ttyd")
    theme = json.dumps({
        "background": conf.get("TERMINAL_THEME_BACKGROUND", "#000000"),
        "foreground": conf.get("TERMINAL_THEME_FOREGROUND", "#00ff00"),
        "cursor": conf.get("TERMINAL_THEME_CURSOR", "#ffffff"),
    }, separators=(",", ":"))

    cmd = [
        ttyd_bin,
        "-p", str(conf.get("TERMINAL_PORT", "7681")),
        "-i", str(conf.get("TERMINAL_LISTEN", "127.0.0.1")),
        "-t", f"theme={theme}",
        "-t", f"titleFixed={conf.get('TERMINAL_TITLE', 'Terminal NAS')}",
        "-m", str(conf.get("TERMINAL_MAX_CLIENTS", "8")),
    ]
    if bool_conf(conf, "TERMINAL_WRITABLE", "1"):
        cmd.append("-W")
    if bool_conf(conf, "TERMINAL_RECONNECT", "1"):
        cmd.extend(["-t", "enableReconnect=true"])
    if bool_conf(conf, "TERMINAL_DISABLE_LEAVE_ALERT", "1"):
        # Supprime la vieille confirmation navigateur de ttyd quand on quitte la page.
        cmd.extend(["-t", "disableLeaveAlert=true"])
    if bool_conf(conf, "TERMINAL_LOGIN_REQUIRED", "0"):
        user = conf.get("TERMINAL_LOGIN_USER", "admin")
        password = conf.get("TERMINAL_LOGIN_PASSWORD", "admin")
        cmd.extend(["-c", f"{user}:{password}"])
    extra = strip_quotes(conf.get("TERMINAL_EXTRA_ARGS", ""))
    if extra:
        cmd.extend(shlex.split(extra))

    shell = strip_quotes(conf.get("TERMINAL_SHELL", "/bin/bash")) or "/bin/bash"
    start_dir = conf.get("TERMINAL_START_DIR_RESOLVED", "/")
    entrypoint = os.path.join(BASE_DIR, "terminal_entrypoint.py")
    if bool_conf(conf, "TERMINAL_ENABLE_URL_ARGS", "1") and os.path.isfile(entrypoint):
        cmd.append("-a")
        cmd.extend([
            sys.executable or "python3",
            entrypoint,
            "--start-dir", start_dir,
            "--shell", shell,
        ])
    else:
        shell_name = os.path.basename(shell)
        if shell_name in {"bash", "zsh", "sh", "dash"}:
            shell_cmd = f"cd -- {shlex.quote(start_dir)} && exec {shlex.quote(shell)} -l"
            cmd.extend(["/bin/sh", "-lc", shell_cmd])
        else:
            cmd.append(shell)
    return cmd


def start_terminal(conf: Dict[str, str]) -> Tuple[bool, str]:
    touch_presence(conf)
    ensure_idle_reaper()
    current = status_data(conf)
    if current["running"]:
        return True, f"Terminal déjà démarré (PID {current['pid']})."

    bin_path = conf.get("TERMINAL_BIN_RESOLVED", "")
    if not os.path.isfile(bin_path):
        search_dirs = conf.get("TERMINAL_BIN_SEARCH_DIRS", conf.get("TERMINAL_BIN_DIR_RESOLVED", "../bin"))
        return False, (
            f"Binaire ttyd introuvable : {bin_path}. "
            f"Copie ttyd.x86_64 / ttyd.aarch64 dans ../bin. "
            f"Dossiers testés : {search_dirs}"
        )
    try:
        os.chmod(bin_path, 0o755)
    except OSError:
        pass

    log_file = conf.get("TERMINAL_LOG_FILE_RESOLVED", "")
    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(pid_file(conf)) or "/tmp", exist_ok=True)

    cmd = build_command(conf)
    with open(log_file, "a", encoding="utf-8", errors="replace") as log:
        log.write("\n\n=== Démarrage terminal Flask System ===\n")
        log.write("Commande: " + " ".join(shlex.quote(x) for x in cmd) + "\n")
        log.flush()
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=conf.get("TERMINAL_START_DIR_RESOLVED", "/"),
            close_fds=True,
        )
    with open(pid_file(conf), "w", encoding="utf-8") as handle:
        handle.write(str(proc.pid))
    time.sleep(0.35)
    if not process_running(proc.pid):
        return False, f"ttyd s'est arrêté juste après le démarrage. Regarde le log : {log_file}"
    return True, f"Terminal démarré sur le port {conf.get('TERMINAL_PORT', '7681')} (PID {proc.pid})."


def stop_terminal(conf: Dict[str, str]) -> Tuple[bool, str]:
    pid = read_pid(conf)
    if not process_running(pid):
        try:
            os.unlink(pid_file(conf))
        except OSError:
            pass
        return True, "Terminal déjà arrêté."
    try:
        os.killpg(pid, signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception as exc:
            return False, f"Impossible d'arrêter le terminal : {exc}"
    for _ in range(20):
        if not process_running(pid):
            break
        time.sleep(0.15)
    if process_running(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
    try:
        os.unlink(pid_file(conf))
    except OSError:
        pass
    return True, "Terminal arrêté."


def write_config_updates(updates: Dict[str, str]) -> Tuple[bool, str]:
    path = CONFIG_FILE
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    original = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            original = handle.read()
    lines = original.splitlines()
    seen = set()
    output = []
    for raw in lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in WRITABLE_KEYS and key in updates:
                output.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        output.append(raw)
    for key in sorted(WRITABLE_KEYS):
        if key in updates and key not in seen:
            if output and output[-1].strip():
                output.append("")
            output.append(f"{key}={updates[key]}")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(output).rstrip() + "\n")
    return True, "Configuration sauvegardée."


def render_terminal_page(section: str = "terminal", autostart: bool = False):
    ensure_idle_reaper()
    conf = get_config()
    if autostart:
        touch_presence(conf)
        if bool_conf(conf, "TERMINAL_AUTOSTART_ON_PAGE", "1"):
            start_terminal(conf)
            conf = get_config()
    return render_template("terminal.html", conf=conf, status=status_data(conf), terminal_section=section)


def ttyd_proxy_error(message: str, status: int = 500) -> Response:
    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<title>Proxy ttyd</title>"
        "<body style='background:#050505;color:#f5f5f5;font:14px monospace;padding:24px;white-space:pre-wrap'>"
        + message
        + "</body>"
    )
    return Response(html, status=status, mimetype="text/html")


def ttyd_backend_path(conf: Dict[str, str], port: int, path: str = "") -> str:
    base = ttyd_base_path_for_port(conf, port).rstrip("/")
    clean_path = str(path or "").lstrip("/")
    if clean_path:
        return base + "/" + urllib.parse.quote(clean_path, safe="/:@")
    return base + "/"


def ttyd_backend_url(conf: Dict[str, str], port: int, path: str = "") -> str:
    target = f"http://127.0.0.1:{int(port)}{ttyd_backend_path(conf, port, path)}"
    query = request.query_string.decode("latin1") if has_request_context() and request.query_string else ""
    if query:
        target += "?" + query
    return target


def ttyd_hop_by_hop_headers() -> set:
    return {
        "connection",
        "content-encoding",
        "content-length",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }


def terminal_ttyd_http_proxy(conf: Dict[str, str], port: int, path: str) -> Response:
    headers = {}
    blocked = ttyd_hop_by_hop_headers() | {"host", "accept-encoding"}
    for key, value in request.headers.items():
        if key.lower() not in blocked:
            headers[key] = value
    headers["Host"] = f"127.0.0.1:{int(port)}"
    headers["Accept-Encoding"] = "identity"
    data = request.get_data() if request.method in {"POST", "PUT", "PATCH"} else None
    target = ttyd_backend_url(conf, port, path)

    try:
        backend_request = urllib.request.Request(target, data=data, headers=headers, method=request.method)
        with urllib.request.urlopen(backend_request, timeout=15) as backend_response:
            body = backend_response.read()
            response_headers = [
                (key, value)
                for key, value in backend_response.headers.items()
                if key.lower() not in ttyd_hop_by_hop_headers()
            ]
            return Response(body, status=backend_response.status, headers=response_headers)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        response_headers = [
            (key, value)
            for key, value in exc.headers.items()
            if key.lower() not in ttyd_hop_by_hop_headers()
        ]
        return Response(body, status=exc.code, headers=response_headers)
    except Exception as exc:
        append_log(conf, f"Proxy ttyd HTTP erreur port={port} path={path}: {exc}")
        return ttyd_proxy_error(f"Impossible de joindre ttyd sur le port interne {port}.\n\n{exc}", 502)


def terminal_ttyd_ws_proxy(conf: Dict[str, str], port: int, path: str) -> Response:
    connection = (request.headers.get("Connection", "") or request.environ.get("HTTP_CONNECTION", "") or "").lower()
    upgrade = (request.headers.get("Upgrade", "") or request.environ.get("HTTP_UPGRADE", "") or "").lower()
    ws_key = request.headers.get("Sec-WebSocket-Key") or request.environ.get("HTTP_SEC_WEBSOCKET_KEY", "")
    if "upgrade" not in connection and upgrade != "websocket" and not ws_key:
        return ttyd_proxy_error("Cette route attend une connexion WebSocket ttyd.", 400)

    try:
        from simple_websocket import Server
        import websocket as ws_client_lib
    except Exception as exc:
        return ttyd_proxy_error(
            "Modules Python manquants pour le proxy WebSocket ttyd.\n\n"
            "Installe dans l'environnement Flask : pip install simple-websocket websocket-client\n\n"
            f"Erreur import : {exc}",
            500,
        )

    query = request.query_string.decode("latin1") if request.query_string else ""
    backend_target = f"ws://127.0.0.1:{int(port)}{ttyd_backend_path(conf, port, path)}"
    if query:
        backend_target += "?" + query

    client_ws = None
    backend_ws = None
    stop = threading.Event()
    try:
        append_log(
            conf,
            "Proxy ttyd WebSocket entrée "
            f"port={port} path={path} "
            f"gunicorn_socket={'gunicorn.socket' in request.environ} "
            f"werkzeug_socket={'werkzeug.socket' in request.environ}",
        )
        client_ws = Server.accept(request.environ, subprotocols=["tty"])
        backend_headers = []
        cookie = request.headers.get("Cookie")
        if cookie:
            backend_headers.append(f"Cookie: {cookie}")
        origin = request.headers.get("Origin")
        if origin:
            backend_headers.append(f"Origin: {origin}")
        backend_ws = ws_client_lib.create_connection(
            backend_target,
            timeout=5,
            header=backend_headers,
            subprotocols=["tty"],
        )

        # IMPORTANT : le timeout=5 ci-dessus ne doit servir qu'à établir la
        # connexion vers ttyd. Si on le laisse sur la socket WebSocket, une
        # session locale sans sortie pendant quelques secondes se coupe toute
        # seule. 0 = aucune coupure de lecture; valeur >0 = timeout volontaire.
        ws_idle_timeout = int_conf(conf, "TERMINAL_TTYD_WS_IDLE_TIMEOUT_SECONDS", 0, minimum=0, maximum=86400)
        try:
            backend_ws.settimeout(None if ws_idle_timeout <= 0 else ws_idle_timeout)
        except Exception:
            try:
                backend_ws.sock.settimeout(None if ws_idle_timeout <= 0 else ws_idle_timeout)
            except Exception:
                pass

        def client_to_backend():
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

        def backend_to_client():
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

        t1 = threading.Thread(target=client_to_backend, daemon=True)
        t2 = threading.Thread(target=backend_to_client, daemon=True)
        t1.start()
        t2.start()
        while not stop.is_set():
            time.sleep(0.1)
        return ""
    except BaseException as exc:
        append_log(conf, f"Proxy ttyd WebSocket erreur port={port} path={path}: {type(exc).__name__}: {exc}")
        return ttyd_proxy_error(f"Erreur proxy WebSocket ttyd sur le port interne {port}.\n\n{exc}", 502)
    finally:
        stop.set()
        try:
            if backend_ws:
                backend_ws.close()
        except Exception:
            pass
        try:
            if client_ws:
                client_ws.close()
        except Exception:
            pass


@terminal_bp.route("/terminal/ttyd/<int:port>/ws", websocket=True)
def terminal_ttyd_proxy_ws(port: int):
    conf = get_config()
    if not ttyd_proxy_port_allowed(conf, port):
        return ttyd_proxy_error(f"Port ttyd non autorisé : {port}", 403)
    return terminal_ttyd_ws_proxy(conf, port, "ws")


@terminal_bp.route("/terminal/ttyd/<int:port>", methods=["GET", "POST"])
@terminal_bp.route("/terminal/ttyd/<int:port>/", defaults={"path": ""}, methods=["GET", "POST"])
@terminal_bp.route("/terminal/ttyd/<int:port>/<path:path>", methods=["GET", "POST"])
def terminal_ttyd_proxy(port: int, path: str = ""):
    conf = get_config()
    if not ttyd_proxy_port_allowed(conf, port):
        return ttyd_proxy_error(f"Port ttyd non autorisé : {port}", 403)
    if str(path or "").strip("/") == "ws":
        return terminal_ttyd_ws_proxy(conf, port, path)
    return terminal_ttyd_http_proxy(conf, port, path)


@terminal_bp.route("/terminal/ttyd-auth", methods=["GET"])
def terminal_ttyd_auth():
    if session.get("yoleo_authenticated"):
        return Response("", status=204)
    return Response("Unauthorized", status=401, mimetype="text/plain")


@terminal_bp.route("/terminal", methods=["GET"])
def terminal_home():
    return redirect("/terminal/console")


@terminal_bp.route("/terminal/console", methods=["GET"])
def terminal_console_page():
    return render_terminal_page("terminal", autostart=True)


@terminal_bp.route("/terminal/config", methods=["GET"])
def terminal_config_page():
    return render_terminal_page("config")


@terminal_bp.route("/terminal/logs", methods=["GET"])
def terminal_logs_page():
    return render_terminal_page("logs")


@terminal_bp.route("/terminal/status", methods=["GET"])
def terminal_status_page():
    return redirect("/terminal/config")


@terminal_bp.route("/terminal/api/status", methods=["GET"])
def terminal_status():
    ensure_idle_reaper()
    conf = get_config()
    return jsonify({"ok": True, "status": status_data(conf)})


@terminal_bp.route("/terminal/api/presence", methods=["POST"])
def terminal_presence():
    ensure_idle_reaper()
    conf = get_config()
    touch_presence(conf)
    return jsonify({"ok": True, "status": status_data(conf)})


@terminal_bp.route("/terminal/api/start", methods=["POST"])
def terminal_start():
    conf = get_config()
    ok, message = start_terminal(conf)
    return jsonify({"ok": ok, "message": message, "status": status_data(conf)})


@terminal_bp.route("/terminal/api/stop", methods=["POST"])
def terminal_stop():
    conf = get_config()
    ok, message = stop_terminal(conf)
    return jsonify({"ok": ok, "message": message, "status": status_data(conf)})


@terminal_bp.route("/terminal/api/restart", methods=["POST"])
def terminal_restart():
    conf = get_config()
    stop_terminal(conf)
    time.sleep(0.25)
    ok, message = start_terminal(get_config())
    return jsonify({"ok": ok, "message": message, "status": status_data(get_config())})


@terminal_bp.route("/terminal/api/save", methods=["POST"])
def terminal_save():
    payload = request.get_json(silent=True) or request.form.to_dict()
    updates = {key: str(payload.get(key, "")).strip() for key in WRITABLE_KEYS if key in payload}
    ok, message = write_config_updates(updates)
    return jsonify({"ok": ok, "message": message, "status": status_data(get_config())})


@terminal_bp.route("/terminal/api/log", methods=["GET"])
def terminal_log():
    conf = get_config()
    path = conf.get("TERMINAL_LOG_FILE_RESOLVED", "")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            data = handle.read()[-12000:]
    except Exception as exc:
        data = f"Log indisponible : {exc}"
    return jsonify({"ok": True, "log": data})
