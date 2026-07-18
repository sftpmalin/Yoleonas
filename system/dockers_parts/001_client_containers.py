import base64
import json
import glob
import ipaddress
import os
import re
import signal
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - module prévu pour Linux/Unraid/Debian.
    fcntl = None

from flask import Blueprint, Response, flash, has_request_context, jsonify, redirect, render_template, request, stream_with_context, url_for

try:
    import docker
    from docker.errors import APIError, DockerException, NotFound
except ImportError:  # Le reste du module Stacks doit rester chargé même si le SDK Docker manque.
    docker = None

    class DockerException(Exception):
        pass

    class APIError(DockerException):
        pass

    class NotFound(DockerException):
        pass


# IMPORTANT : ce fichier ne doit PAS s'appeler docker.py.
# Sinon il masque le paquet Python officiel `docker` utilisé par le SDK Docker.
# Nom conseillé maintenant : dockers.py, avec route publique /dockers.
dockers_bp = Blueprint("dockers_bp", __name__)
# Alias de compat : app.py peut importer dockers_bp maintenant, ou garder les anciens noms pendant la transition.
stacks_bp = dockers_bp
docker_bp = dockers_bp
snacks_bp = dockers_bp

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



# ---------------------------------------------------------------------------
# Onglet Docker intégré depuis l’ancien module dockers.py
# ---------------------------------------------------------------------------
SERVER_IP = os.environ.get("SERVER_IP", "").strip() or os.environ.get("HOST_IP", "").strip()
DOCKER_SERVICE_NAME = os.environ.get("DOCKER_SERVICE_NAME", "docker").strip() or "docker"
DOCKER_SERVICE_ACTIONS = {"start_docker_service", "restart_docker_service", "stop_docker_service"}


# TTYD Docker dépannage : vrai terminal dédié par conteneur.
# On ne réutilise pas le terminal global /terminal/console : un log Docker ou un exec
# doit pouvoir s'ouvrir en popup sans casser la console NAS principale.
DOCKER_TTYD_PID_DIR = "/tmp"
DOCKER_TTYD_BASE_PORT = int(os.environ.get("YOLEO_DOCKER_TTYD_BASE_PORT", "7780") or "7780")
DOCKER_TTYD_PORT_COUNT = int(os.environ.get("YOLEO_DOCKER_TTYD_PORT_COUNT", "40") or "40")


def _docker_ttyd_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return slug[:80] or "container"


def _docker_ttyd_pid_file(kind: str, container_name: str) -> str:
    return os.path.join(DOCKER_TTYD_PID_DIR, f"yoleo_docker_ttyd_{_docker_ttyd_slug(kind)}_{_docker_ttyd_slug(container_name)}.pid")


def _docker_ttyd_port_file(kind: str, container_name: str) -> str:
    return os.path.join(DOCKER_TTYD_PID_DIR, f"yoleo_docker_ttyd_{_docker_ttyd_slug(kind)}_{_docker_ttyd_slug(container_name)}.port")


def _docker_ttyd_log_file(kind: str, container_name: str) -> str:
    return os.path.join(DOCKER_TTYD_PID_DIR, f"yoleo_docker_ttyd_{_docker_ttyd_slug(kind)}_{_docker_ttyd_slug(container_name)}.log")


def _docker_ttyd_read_int(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return int(str(handle.read()).strip() or "0")
    except Exception:
        return 0


def _docker_ttyd_process_running(pid: int) -> bool:
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


def _docker_ttyd_cleanup(kind: str, container_name: str) -> None:
    for path in (_docker_ttyd_pid_file(kind, container_name), _docker_ttyd_port_file(kind, container_name)):
        try:
            os.unlink(path)
        except OSError:
            pass


def _docker_ttyd_terminal_conf() -> Dict[str, str]:
    """Récupère la configuration ttyd du module Terminal sans dépendre de son UI."""
    try:
        import terminal as yoleo_terminal  # même dossier que dockers.py
        return yoleo_terminal.get_config()
    except Exception:
        return {}


def _docker_ttyd_binary(conf: Dict[str, str]) -> str:
    candidate = str(conf.get("TERMINAL_BIN_RESOLVED") or "").strip()
    if candidate and os.path.isfile(candidate):
        return candidate
    for candidate in (
        shutil.which("ttyd") or "",
        os.path.abspath(os.path.join(_NAS_MODULE_DIR, "..", "bin", "ttyd.x86_64")),
        os.path.abspath(os.path.join(_NAS_MODULE_DIR, "..", "bin", "ttyd.aarch64")),
        "/usr/bin/ttyd",
        "/usr/local/bin/ttyd",
    ):
        if candidate and os.path.isfile(candidate):
            return candidate
    return str(conf.get("TERMINAL_BIN_RESOLVED") or "ttyd")


def _docker_ttyd_port_is_free(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", int(port)))
        return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _docker_ttyd_find_free_port() -> int:
    start = max(1024, DOCKER_TTYD_BASE_PORT)
    for port in range(start, start + max(1, DOCKER_TTYD_PORT_COUNT)):
        if _docker_ttyd_port_is_free(port):
            return port
    raise RuntimeError(f"Aucun port libre pour ttyd Docker entre {start} et {start + DOCKER_TTYD_PORT_COUNT - 1}.")


def _docker_ttyd_url(port: int) -> str:
    try:
        import terminal as yoleo_terminal
        return yoleo_terminal.ttyd_url_for_port(yoleo_terminal.get_config(), int(port))
    except Exception:
        host = "127.0.0.1"
        if has_request_context():
            try:
                host = request.host.split(":", 1)[0]
            except Exception:
                host = get_host_lan_ip()
        return f"//{host}:{int(port)}"


def _docker_ttyd_command(kind: str, container_name: str) -> Tuple[str, str]:
    quoted = shlex.quote(container_name)
    if kind == "compose_logs":
        conf = get_config()
        log_path = str(conf.get("SYSTEM_LOG_FILE", "") or DEFAULT_CONFIG.get("SYSTEM_LOG_FILE", "/var/log/dockers.log"))
        quoted_log = shlex.quote(log_path)
        title = "Logs Docker Compose"
        script = (
            "clear; "
            "echo '=== Logs Docker Compose ==='; "
            f"echo '{log_path}'; "
            "echo; "
            f"touch {quoted_log}; "
            f"tail -n 300 -f {quoted_log}; "
            "code=$?; "
            "echo; echo '--- Fin du suivi Compose ---'; "
            "echo \"Code retour: $code\"; "
            "echo 'Le terminal reste ouvert pour dépannage.'; "
            "exec /bin/bash -l"
        )
        return title, script

    if kind == "logs":
        title = f"Logs Docker · {container_name}"
        script = (
            "clear; "
            f"echo '=== docker logs --tail=300 -f {container_name} ==='; "
            "echo; "
            f"docker logs --tail=300 -f {quoted}; "
            "code=$?; "
            "echo; echo '--- Fin du suivi logs Docker ---'; "
            "echo \"Code retour: $code\"; "
            "echo 'Le terminal reste ouvert pour dépannage.'; "
            "exec /bin/bash -l"
        )
        return title, script

    title = f"Terminal Docker · {container_name}"
    script = (
        "clear; "
        f"echo '=== docker exec -it {container_name} ==='; "
        "echo; "
        f"docker exec -it {quoted} /bin/bash || docker exec -it {quoted} /bin/sh; "
        "code=$?; "
        "echo; echo '--- Session docker exec terminée ---'; "
        "echo \"Code retour: $code\"; "
        "echo 'Le terminal reste ouvert pour dépannage.'; "
        "exec /bin/bash -l"
    )
    return title, script


def _docker_ttyd_start(kind: str, container_ref: str) -> Dict[str, object]:
    kind = str(kind or "logs").strip().lower()
    if kind not in {"logs", "exec", "compose_logs"}:
        raise ValueError("Action ttyd inconnue.")

    container_ref = str(container_ref or "").strip()
    if kind == "compose_logs":
        container_name = "compose"
    else:
        if not container_ref:
            raise ValueError("Nom du conteneur manquant.")

        client = get_docker_client()
        container = client.containers.get(container_ref)
        container_name = str(container.name or container_ref).lstrip("/")
        container_state = str(getattr(container, "status", "") or "").lower()
        if kind == "exec" and container_state != "running":
            raise ValueError("Le terminal docker exec est disponible seulement si le conteneur est démarré.")

    title, _ = _docker_ttyd_command(kind, container_name)
    try:
        import terminal as yoleo_terminal
        term_conf = yoleo_terminal.get_config()
        ok, message = yoleo_terminal.ensure_terminal_url_args(term_conf)
        if ok:
            action_args = {
                "compose_logs": ["compose-logs"],
                "logs": ["docker-logs", container_name],
                "exec": ["docker-exec", container_name],
            }[kind]
            fresh_conf = yoleo_terminal.get_config()
            status = yoleo_terminal.status_data(fresh_conf)
            return {
                "title": title,
                "url": yoleo_terminal.ttyd_url_with_args(fresh_conf, action_args),
                "pid": int(status.get("pid") or 0),
                "port": int(status.get("port") or 7681),
                "reused": True,
            }
        raise RuntimeError(message)
    except Exception:
        pass

    pid_file = _docker_ttyd_pid_file(kind, container_name)
    port_file = _docker_ttyd_port_file(kind, container_name)
    pid = _docker_ttyd_read_int(pid_file)
    port = _docker_ttyd_read_int(port_file)
    if pid and port and _docker_ttyd_process_running(pid):
        return {"title": title, "url": _docker_ttyd_url(port), "pid": pid, "port": port, "reused": True}

    _docker_ttyd_cleanup(kind, container_name)
    port = _docker_ttyd_find_free_port()
    conf = _docker_ttyd_terminal_conf()
    ttyd_bin = _docker_ttyd_binary(conf)
    if not os.path.isfile(ttyd_bin):
        raise RuntimeError(f"Binaire ttyd introuvable : {ttyd_bin}")
    try:
        os.chmod(ttyd_bin, 0o755)
    except OSError:
        pass

    title, shell_script = _docker_ttyd_command(kind, container_name)
    base_path = ""
    try:
        import terminal as yoleo_terminal
        base_path = yoleo_terminal.ttyd_base_path_for_port(conf, int(port))
    except Exception:
        base_path = ""
    theme = json.dumps({
        "background": str(conf.get("TERMINAL_THEME_BACKGROUND") or "#000000"),
        "foreground": str(conf.get("TERMINAL_THEME_FOREGROUND") or "#00ff00"),
        "cursor": str(conf.get("TERMINAL_THEME_CURSOR") or "#ffffff"),
    }, separators=(",", ":"))

    cmd = [
        ttyd_bin,
        "-p", str(port),
        "-i", "0.0.0.0",
        "-W",
        "-t", f"theme={theme}",
        "-t", f"titleFixed={title}",
        "-t", "enableReconnect=true",
        "-t", "disableLeaveAlert=true",
    ]
    if base_path:
        cmd.extend(["-b", base_path])
    cmd.extend(["/bin/bash", "-lc", shell_script])

    log_file = _docker_ttyd_log_file(kind, container_name)
    with open(log_file, "a", encoding="utf-8", errors="replace") as log:
        log.write("\n\n=== Démarrage ttyd Docker ===\n")
        log.write("Commande: " + " ".join(shlex.quote(x) for x in cmd) + "\n")
        log.flush()
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            cwd="/root" if os.path.isdir("/root") else "/",
        )

    with open(pid_file, "w", encoding="utf-8") as handle:
        handle.write(str(proc.pid))
    with open(port_file, "w", encoding="utf-8") as handle:
        handle.write(str(port))

    time.sleep(0.35)
    if not _docker_ttyd_process_running(proc.pid):
        _docker_ttyd_cleanup(kind, container_name)
        raise RuntimeError(f"ttyd Docker s'est arrêté juste après le démarrage. Log : {log_file}")

    return {"title": title, "url": _docker_ttyd_url(port), "pid": proc.pid, "port": port, "reused": False}


def get_host_lan_ip():
    """
    IP affichée pour les conteneurs en network_mode: host.

    Pourquoi :
      En mode host, Docker ne donne pas d'IPAddress dans NetworkSettings.Networks.
      L'ancienne version retombait sur SERVER_IP='192.168.1.2', donc affichage faux.
    """
    if SERVER_IP:
        return SERVER_IP

    # Méthode simple : IP de sortie réseau du namespace où tourne Flask.
    # Si Flask System tourne en host network, ça donne directement l'IP LAN du serveur.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127.") and not ip.startswith("172."):
            return ip
    except Exception:
        pass

    # Fallback hostname -I.
    try:
        ips = socket.gethostbyname_ex(socket.gethostname())[2]
        for ip in ips:
            if ip and not ip.startswith("127.") and not ip.startswith("172."):
                return ip
    except Exception:
        pass

    return "127.0.0.1"


def get_docker_client():
    if docker is None:
        raise DockerException("Module Python docker introuvable. Installe python3-docker ou docker dans le venv.")
    return docker.from_env()


def clean_docker_error(err):
    msg = str(err)

    if hasattr(err, "explanation") and err.explanation:
        msg = str(err.explanation)

    low = msg.lower()

    if "already in progress" in low:
        return "Une action Docker est déjà en cours sur ce conteneur."
    if "is restarting" in low or "container is restarting" in low:
        return "Le conteneur est en cours de redémarrage."
    if "is not running" in low:
        return "Le conteneur est déjà arrêté."
    if "is already running" in low:
        return "Le conteneur est déjà démarré."
    if "no such container" in low or "not found" in low:
        return "Le conteneur n'existe plus."
    if (
        "no such file or directory" in low
        and ("docker.sock" in low or "/run/docker" in low or "file" in low)
    ) or "connection aborted" in low or "connection refused" in low:
        return "Service Docker arrêté."
    if "permission denied" in low:
        return "Permission refusée par Docker."

    return msg



def get_docker_service_status():
    """
    État du service système Docker.

    Important : quand Docker est arrêté, on ne doit pas appeler docker.from_env(),
    sinon docker.socket peut réveiller le daemon tout seul sur certaines Debian.
    """
    status = {
        "service_name": DOCKER_SERVICE_NAME,
        "state": "unknown",
        "label": "Inconnu",
        "active": None,
    }

    if not shutil.which("systemctl"):
        status["label"] = "systemctl indisponible"
        return status

    try:
        res = subprocess.run(
            ["systemctl", "is-active", f"{DOCKER_SERVICE_NAME}.service"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        state = (res.stdout or res.stderr or "unknown").strip().splitlines()[0].strip()
    except Exception as e:
        status["label"] = f"Erreur systemctl : {clean_docker_error(e)}"
        return status

    labels = {
        "active": "Actif",
        "inactive": "Arrêté",
        "failed": "Erreur",
        "activating": "Démarrage",
        "deactivating": "Arrêt en cours",
        "unknown": "Inconnu",
    }

    status["state"] = state or "unknown"
    status["label"] = labels.get(status["state"], status["state"])
    status["active"] = status["state"] == "active"
    return status


def do_docker_service_action(action):
    if action not in DOCKER_SERVICE_ACTIONS:
        return {"status": "error", "message": f"Action service Docker invalide : {action}"}, 400

    if not shutil.which("systemctl"):
        return {"status": "error", "message": "systemctl est indisponible sur cet hôte."}, 500

    system_action = {
        "start_docker_service": "start",
        "restart_docker_service": "restart",
        "stop_docker_service": "stop",
    }[action]

    try:
        res = subprocess.run(
            ["systemctl", system_action, f"{DOCKER_SERVICE_NAME}.service"],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "Timeout pendant l'action sur le service Docker."}, 500
    except Exception as e:
        return {"status": "error", "message": clean_docker_error(e)}, 500

    output = "\n".join(part.strip() for part in (res.stdout, res.stderr) if part and part.strip()).strip()
    if res.returncode != 0:
        msg = output or f"systemctl {system_action} {DOCKER_SERVICE_NAME}.service a échoué."
        return {"status": "error", "message": msg}, 500

    status = get_docker_service_status()
    action_labels = {
        "start_docker_service": "Service Docker démarré.",
        "restart_docker_service": "Service Docker redémarré.",
        "stop_docker_service": "Service Docker arrêté.",
    }
    msg = action_labels[action]
    msg += f"\nÉtat actuel : {status.get('label', 'Inconnu')}"
    if output:
        msg += f"\n\nSortie systemctl :\n{output}"

    return {"status": "success", "message": msg, "docker_service_status": status}, 200

def get_container_ip(attrs):
    host_config = attrs.get("HostConfig", {}) or {}
    network_mode = str(host_config.get("NetworkMode", "") or "").strip()

    # network_mode: host = pas d'IP container dédiée.
    # Il faut afficher l'IP LAN de l'hôte, pas un fallback codé en dur.
    if network_mode == "host":
        return get_host_lan_ip()

    networks = attrs.get('NetworkSettings', {}).get('Networks', {}) or {}

    # Réseaux classiques : bridge / br0 / ipvlan / macvlan.
    for net in networks.values():
        ip = net.get('IPAddress')
        if ip:
            return ip

    # Fallback propre si Docker ne donne aucune IP.
    return get_host_lan_ip()


def get_container_webui(labels, ip):
    webui_label = labels.get('net.unraid.docker.webui', '')
    if not webui_label:
        return ""

    link = webui_label.replace('[IP]', ip)
    link = re.sub(r'\[PORT:(\d+)\]', r'\1', link)
    return link


def get_container_icon(labels):
    raw_icon = labels.get('net.unraid.docker.icon', '')
    if raw_icon:
        if raw_icon.startswith('http://') or raw_icon.startswith('https://'):
            return raw_icon
        return f"/static/logo/{raw_icon.split('/')[-1]}"
    return 'https://logo.sftpmalin.com/docker1.png'


def build_container_data(container):
    attrs = container.attrs
    cfg = attrs.get('Config', {})
    labels = cfg.get('Labels', {}) or {}

    stack_name = labels.get('com.docker.compose.project', 'Autre')
    stack_name = str(stack_name).strip() or "Autre"
    stack_name = stack_name.capitalize()

    ip = get_container_ip(attrs)
    final_weblink = get_container_webui(labels, ip)
    icon_url = get_container_icon(labels)

    mounts = attrs.get('Mounts', []) or []

    c_data = {
        'id': container.id,
        'name': container.name.lstrip('/'),
        'image': cfg.get('Image', '?'),
        'state': container.status,
        'ip': ip,
        'network_mode': (attrs.get("HostConfig", {}) or {}).get("NetworkMode", ""),
        'icon': icon_url,
        'webui': final_weblink,
        'mounts': mounts,
        'vols_mobile': [f"{m.get('Source', '?')} -> {m.get('Destination', '?')}" for m in mounts]
    }

    return stack_name, c_data


def list_stacks(client):
    stacks = {}
    containers_raw = client.containers.list(all=True)

    for c in containers_raw:
        try:
            stack_name, c_data = build_container_data(c)
            stacks.setdefault(stack_name, []).append(c_data)
        except Exception as e:
            print(f"[DOCKERS] Erreur build data pour {getattr(c, 'name', '?')}: {e}")

    for stack_name in stacks:
        stacks[stack_name].sort(key=lambda x: x["name"].lower())

    return dict(sorted(stacks.items(), key=lambda kv: kv[0].lower()))


def get_docker_stats(stacks):
    """
    Calcule les compteurs affichés en haut du tableau.
    - total    : tous les conteneurs connus par Docker
    - running  : conteneurs réellement démarrés
    - stopped  : tout ce qui n'est pas running : exited, created, paused, restarting, etc.
    """
    containers = []
    for stack_containers in stacks.values():
        containers.extend(stack_containers)

    total = len(containers)
    running = sum(1 for c in containers if c.get('state') == 'running')
    stopped = total - running

    return {
        'total': total,
        'running': running,
        'stopped': stopped,
    }



def _read_text_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def get_self_container_refs(client):
    """
    Sécurité pour l'action "arrêter tous".
    Dans un conteneur Docker, /etc/hostname vaut généralement le short-id du conteneur.
    On résout ce hostname via Docker pour récupérer aussi l'id complet et le nom réel.
    """
    raw_refs = set()

    for key in ("HOSTNAME", "SELF_CONTAINER_NAME", "FLASK_SYSTEM_CONTAINER_NAME"):
        value = os.environ.get(key, "").strip()
        if value:
            raw_refs.add(value)

    for value in (_read_text_file("/etc/hostname"), socket.gethostname()):
        if value:
            raw_refs.add(value)

    # Fallback par nom si tu appelles ton conteneur Flask_System.
    raw_refs.update({
        "Flask_System",
        "flask_system",
        "flask-system",
        "FlaskSystem",
        "flasksystem",
    })

    # Cgroups v1/v2 : parfois l'id complet du conteneur apparaît dedans.
    cgroup = _read_text_file("/proc/self/cgroup")
    for match in re.findall(r"[0-9a-f]{64}", cgroup):
        raw_refs.add(match)

    refs = set()
    for ref in raw_refs:
        ref = str(ref).strip().lstrip("/")
        if not ref:
            continue
        refs.add(ref.lower())
        try:
            c = client.containers.get(ref)
            refs.add(c.id.lower())
            refs.add(c.short_id.lower().replace("sha256:", ""))
            refs.add(c.name.lower().lstrip("/"))
        except Exception:
            pass

    return refs


def is_self_container(container, self_refs):
    name = container.name.lower().lstrip("/")
    cid = container.id.lower()
    short_id = container.short_id.lower().replace("sha256:", "")

    for ref in self_refs:
        ref = ref.lower().lstrip("/")
        if not ref:
            continue
        if ref == name or ref == short_id or ref == cid:
            return True
        if len(ref) >= 8 and cid.startswith(ref):
            return True
        if len(short_id) >= 8 and ref.startswith(short_id):
            return True
    return False


def do_bulk_action(client, action):
    if action not in {'start_all', 'stop_all'}:
        return {"status": "error", "message": f"Action globale invalide : {action}"}, 400

    stopped = []
    started = []
    skipped_already = 0
    errors = []

    for container in client.containers.list(all=True):
        name = container.name.lstrip('/')
        try:
            container.reload()
            current_state = container.status

            if action == 'stop_all':
                if current_state == 'running':
                    container.stop(timeout=10)
                    stopped.append(name)
                else:
                    skipped_already += 1

            elif action == 'start_all':
                if current_state != 'running':
                    container.start()
                    started.append(name)
                else:
                    skipped_already += 1

        except Exception as e:
            errors.append(f"{name}: {clean_docker_error(e)}")

    if action == 'stop_all':
        msg = (
            "Arrêt massif terminé.\n"
            f"Conteneurs arrêtés : {len(stopped)}"
        )
        if stopped:
            msg += "\n- " + "\n- ".join(stopped)
        msg += f"\n\nDéjà arrêtés ignorés : {skipped_already}"
    else:
        msg = (
            "Démarrage massif terminé.\n"
            f"Conteneurs démarrés : {len(started)}"
        )
        if started:
            msg += "\n- " + "\n- ".join(started)
        msg += f"\n\nDéjà démarrés ignorés : {skipped_already}"

    if errors:
        msg += "\n\nErreurs :\n- " + "\n- ".join(errors)
        return {"status": "error", "message": msg}, 500

    return {"status": "success", "message": msg}, 200


def do_action(client, c_id, action):
    if action in {'start_all', 'stop_all'}:
        return do_bulk_action(client, action)

    if not c_id:
        return {"status": "error", "message": "ID conteneur manquant."}, 400

    if action not in {'start', 'stop', 'restart', 'rm', 'rmi'}:
        return {"status": "error", "message": f"Action invalide : {action}"}, 400

    try:
        container = client.containers.get(c_id)
        container.reload()
        current_state = container.status

        if action == 'start':
            if current_state != 'running':
                container.start()
            return {"status": "success", "message": "Conteneur démarré."}, 200

        if action == 'stop':
            if current_state == 'running':
                container.stop(timeout=10)
            return {"status": "success", "message": "Conteneur arrêté."}, 200

        if action == 'restart':
            if current_state == 'running':
                container.restart(timeout=10)
            else:
                container.start()
            return {"status": "success", "message": "Conteneur redémarré."}, 200

        if action == 'rm':
            container.remove(force=True)
            return {"status": "success", "message": "Conteneur supprimé."}, 200

        if action == 'rmi':
            img_tag = container.attrs.get('Config', {}).get('Image')
            container.remove(force=True)
            if img_tag:
                try:
                    client.images.remove(image=img_tag, force=True)
                except Exception as e:
                    return {
                        "status": "error",
                        "message": f"Conteneur supprimé, mais image non supprimée : {clean_docker_error(e)}"
                    }, 500
            return {"status": "success", "message": "Conteneur et image supprimés."}, 200

    except NotFound:
        # Pour éviter les erreurs rouges inutiles si le refresh a pris du retard
        if action in {'stop', 'rm', 'rmi'}:
            return {"status": "success", "message": "Le conteneur n'existe déjà plus."}, 200
        return {"status": "error", "message": "Le conteneur est introuvable."}, 404

    except APIError as e:
        return {"status": "error", "message": clean_docker_error(e)}, 500

    except Exception as e:
        return {"status": "error", "message": clean_docker_error(e)}, 500


