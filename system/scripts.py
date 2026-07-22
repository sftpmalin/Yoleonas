#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gestionnaire de scripts ponctuels Yoleo.

Route principale : /system/scripts/
Configuration : ../conf/manager_sripts.conf (nom demandé, conservé tel quel)
Scripts acceptés : .sh et .py
Exécution : unité systemd indépendante + session tmux ; fallback double-fork
réadopté par PID 1 lorsque systemd-run n'est pas disponible.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from script_manager_runtime import (
    TMUX_SOCKET_NAME,
    merge_status,
    read_json,
    run_argv_capture,
    tmux_has_session,
    tmux_kill_session,
)

scripts_bp = Blueprint("scripts_bp", __name__)

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(MODULE_DIR, ".."))
NAS_CONF_DIR = os.path.abspath(
    os.path.expanduser(
        os.path.expandvars(os.environ.get("NAS_CONF_DIR", os.path.join(PROJECT_DIR, "conf")))
    )
)
CONFIG_FILE = os.path.join(NAS_CONF_DIR, "manager_sripts.conf")
RUNTIME_MODULE = os.path.join(MODULE_DIR, "script_manager_runtime.py")
DEFAULT_SCRIPTS_DIR = "/mnt/user/scripts"
DEFAULT_LOG_DIR = "/var/log/flask-system/scripts"
DEFAULT_RUN_DIR = "/run/flask-system/script-manager"
FALLBACK_RUN_DIR = "/tmp/flask-system-script-manager"
ALLOWED_EXTENSIONS = {".sh", ".py"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico", ".bmp", ".avif"}
SAFE_FILENAME_RE = re.compile(r"^[^/\\\x00]{1,180}$")
METADATA_RE = re.compile(
    r"^\s*#\s*(title|titre|description|detail|détail|icon|icone|icône)\s*=\s*(.*?)\s*$",
    re.IGNORECASE,
)


def _ensure_dir(preferred: str, fallback: str = "") -> str:
    for candidate in [preferred, fallback]:
        if not candidate:
            continue
        target = os.path.abspath(os.path.expanduser(os.path.expandvars(candidate)))
        try:
            os.makedirs(target, exist_ok=True)
            return target
        except OSError:
            continue
    return tempfile.gettempdir()


RUN_DIR = _ensure_dir(DEFAULT_RUN_DIR, FALLBACK_RUN_DIR)
LOG_DIR = _ensure_dir(DEFAULT_LOG_DIR, os.path.join(FALLBACK_RUN_DIR, "logs"))


def _single_line(value: Any, maximum: int = 500) -> str:
    text = str(value if value is not None else "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:maximum]


def _read_kv(path: str) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or line.startswith(";") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                data[key.strip().upper()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return data


def _write_config(scripts_dir: str) -> None:
    target = os.path.abspath(CONFIG_FILE)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    clean_dir = os.path.abspath(os.path.expanduser(os.path.expandvars(scripts_dir)))
    text = (
        "# ============================================================\n"
        "# Gestionnaire de scripts Yoleo\n"
        "# Nom du fichier conservé tel que demandé : manager_sripts.conf\n"
        "# ============================================================\n\n"
        f"SCRIPTS_DIR={clean_dir}\n"
    )
    fd, temp_path = tempfile.mkstemp(prefix="manager_sripts.", suffix=".tmp", dir=os.path.dirname(target))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, target)
    finally:
        try:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        except OSError:
            pass


def ensure_config() -> bool:
    if os.path.exists(CONFIG_FILE):
        return False
    _write_config(DEFAULT_SCRIPTS_DIR)
    return True


def get_config() -> dict[str, str]:
    ensure_config()
    raw = _read_kv(CONFIG_FILE)
    scripts_dir = raw.get("SCRIPTS_DIR", DEFAULT_SCRIPTS_DIR).strip() or DEFAULT_SCRIPTS_DIR
    scripts_dir = os.path.abspath(os.path.expanduser(os.path.expandvars(scripts_dir)))
    try:
        os.makedirs(scripts_dir, exist_ok=True)
    except OSError:
        pass
    return {
        "SCRIPTS_DIR": scripts_dir,
        "CONFIG_FILE": CONFIG_FILE,
        "LOG_DIR": LOG_DIR,
        "RUN_DIR": RUN_DIR,
        "TMUX_SOCKET": TMUX_SOCKET_NAME,
    }


def normalize_filename(value: str) -> str:
    name = str(value or "").strip()
    if not SAFE_FILENAME_RE.fullmatch(name) or name in {".", ".."}:
        raise ValueError("Nom de fichier invalide.")
    name = os.path.basename(name)
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError("Le nom doit se terminer par .sh ou .py.")
    return name


def script_path(filename: str, must_exist: bool = False) -> str:
    name = normalize_filename(filename)
    root = get_config()["SCRIPTS_DIR"]
    path = os.path.abspath(os.path.join(root, name))
    if os.path.commonpath([root, path]) != root:
        raise ValueError("Chemin de script refusé.")
    if must_exist and os.path.islink(path):
        raise ValueError("Les liens symboliques ne sont pas acceptés comme scripts gérés.")
    if must_exist and not os.path.isfile(path):
        raise FileNotFoundError(name)
    return path


def metadata_key(raw_key: str) -> str:
    key = raw_key.lower().strip()
    if key in {"title", "titre"}:
        return "title"
    if key in {"description", "detail", "détail"}:
        return "description"
    return "icon"


def parse_script_text(text: str, fallback_title: str = "") -> dict[str, str]:
    lines = str(text or "").splitlines(keepends=True)
    meta = {"title": "", "description": "", "icon": ""}
    body_lines: list[str] = []
    header_open = True
    for index, line in enumerate(lines):
        stripped = line.rstrip("\r\n")
        match = METADATA_RE.match(stripped) if header_open and index < 80 else None
        if match:
            meta[metadata_key(match.group(1))] = _single_line(match.group(2), 1000)
            continue

        body_lines.append(line)
        clean = stripped.strip()
        if index == 0 and clean.startswith("#!"):
            continue
        if header_open and (not clean or clean.startswith("#")):
            continue
        header_open = False

    meta["title"] = meta["title"] or fallback_title
    meta["body"] = "".join(body_lines)
    return meta


def normalize_icon(value: str) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    if raw.startswith("/static/"):
        raw = raw[len("/static/"):]
    elif raw.startswith("static/"):
        raw = raw[len("static/"):]
    raw = raw.lstrip("/")
    normalized = os.path.normpath(raw).replace("\\", "/")
    if normalized.startswith("../") or normalized == "..":
        raise ValueError("L'icône doit rester dans le dossier static.")
    if Path(normalized).suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError("Format d'icône non reconnu.")
    real = os.path.abspath(os.path.join(MODULE_DIR, "static", normalized))
    static_root = os.path.abspath(os.path.join(MODULE_DIR, "static"))
    if os.path.commonpath([static_root, real]) != static_root:
        raise ValueError("Chemin d'icône refusé.")
    if not os.path.isfile(real):
        raise ValueError("Fichier d'icône introuvable dans static.")
    return normalized


def icon_url(icon: str) -> str:
    clean = str(icon or "").strip().replace("\\", "/").lstrip("/")
    return f"/static/{clean}" if clean else "/static/logo/Terminal.png"


def default_shebang(suffix: str) -> str:
    return "#!/bin/bash\n" if suffix == ".sh" else "#!/usr/bin/env python3\n"


def compose_filename(base_name: str, script_type: str) -> str:
    base = str(base_name or "").strip()
    kind = str(script_type or "").strip().lower()
    if kind in {"python", "py", ".py"}:
        suffix = ".py"
    elif kind in {"shell", "bash", "sh", ".sh"}:
        suffix = ".sh"
    else:
        raise ValueError("Choisis le type Shell (.sh) ou Python (.py).")

    lowered = base.lower()
    if lowered.endswith(".sh") or lowered.endswith(".py"):
        base = base[:-3].rstrip()
    if not base:
        raise ValueError("Indique le nom du script.")
    return normalize_filename(base + suffix)


def build_script_text(filename: str, title: str, description: str, icon: str, body: str) -> str:
    suffix = Path(filename).suffix.lower()
    parsed = parse_script_text(body)
    clean_body = parsed["body"].replace("\r\n", "\n").replace("\r", "\n")
    lines = clean_body.splitlines(keepends=True)

    # Le type choisi dans le formulaire impose le bon shebang. Un shebang
    # collé dans le corps est retiré puis remplacé afin d'éviter un fichier
    # .py lancé avec Bash ou un fichier .sh lancé avec Python.
    if lines and lines[0].startswith("#!"):
        lines.pop(0)
    shebang = default_shebang(suffix)
    coding = ""

    if suffix == ".py" and lines and re.match(r"^#.*coding[:=]", lines[0]):
        coding = lines.pop(0).rstrip("\n") + "\n"

    metadata = [
        f"# title={_single_line(title, 240)}\n",
        f"# description={_single_line(description, 1000)}\n",
        f"# icon={icon}\n",
    ]
    remaining = "".join(lines)
    if remaining and not remaining.startswith("\n"):
        remaining = "\n" + remaining
    result = shebang + coding + "".join(metadata) + remaining
    if not result.endswith("\n"):
        result += "\n"
    return result


def ensure_executable(path: str) -> None:
    try:
        mode = stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)
        wanted = mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        if wanted != mode:
            os.chmod(path, wanted, follow_symlinks=False)
    except (OSError, TypeError):
        pass


def session_name_for_path(path: str) -> str:
    digest = hashlib.sha256(os.path.abspath(path).encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return f"ys_{digest}"


def status_path_for_session(session: str) -> str:
    return os.path.join(RUN_DIR, f"{session}.json")


def log_path_for_script(path: str) -> str:
    session = session_name_for_path(path)
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(path).stem).strip("._") or "script"
    return os.path.join(LOG_DIR, f"{stem}-{session[3:]}.log")


def current_status(path: str) -> dict[str, Any]:
    session = session_name_for_path(path)
    status_file = status_path_for_session(session)
    data = read_json(status_file)
    alive = False
    try:
        alive = tmux_has_session(session)
    except Exception:
        alive = False

    state = str(data.get("state") or "idle")
    if alive and state not in {"error", "success", "stopped"}:
        state = "running"
    elif alive and state == "success":
        state = "running"
    elif not alive and state in {"starting", "running"}:
        state = "stopped"
    if not data:
        state = "idle"

    return {
        **data,
        "state": state,
        "session": session,
        "session_alive": alive,
        "status_file": status_file,
        "log_file": log_path_for_script(path),
    }


def display_status_label(state: str) -> str:
    return {
        "idle": "Prêt",
        "starting": "Démarrage",
        "running": "En cours",
        "success": "Succès",
        "error": "Erreur",
        "stopped": "Arrêté",
    }.get(state, state or "Prêt")


def list_scripts() -> list[dict[str, Any]]:
    root = get_config()["SCRIPTS_DIR"]
    rows: list[dict[str, Any]] = []
    try:
        names = sorted(os.listdir(root), key=str.lower)
    except OSError:
        return rows

    for name in names:
        if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        try:
            path = script_path(name, must_exist=True)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            ensure_executable(path)
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            meta = parse_script_text(text, fallback_title=Path(name).stem)
            st = os.stat(path)
            status = current_status(path)
            rows.append({
                "name": name,
                "path": path,
                "title": meta["title"] or Path(name).stem,
                "description": meta["description"] or "Aucune description.",
                "icon": meta["icon"],
                "icon_url": icon_url(meta["icon"]),
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%d/%m/%Y %H:%M"),
                "status": status,
                "status_label": display_status_label(status["state"]),
            })
        except Exception:
            continue
    return rows


def get_script_for_form(filename: str = "") -> dict[str, str]:
    if not filename:
        return {
            "old_filename": "",
            "filename": "",
            "filename_base": "",
            "script_type": "sh",
            "title": "",
            "description": "",
            "icon": "",
            "icon_url": icon_url(""),
            "body": "#!/bin/bash\n\n",
        }
    path = script_path(filename, must_exist=True)
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        parsed = parse_script_text(handle.read(), fallback_title=Path(filename).stem)
    suffix = Path(filename).suffix.lower()
    return {
        "old_filename": filename,
        "filename": filename,
        "filename_base": Path(filename).stem,
        "script_type": "py" if suffix == ".py" else "sh",
        "title": parsed["title"],
        "description": parsed["description"],
        "icon": parsed["icon"],
        "icon_url": icon_url(parsed["icon"]),
        "body": parsed["body"],
    }


def _daemonize_exec(argv: list[str], launcher_log: str) -> tuple[bool, str]:
    """Double fork : le processus durable est réadopté par PID 1."""
    try:
        first_pid = os.fork()
    except OSError as exc:
        return False, f"fork impossible : {exc}"

    if first_pid > 0:
        while True:
            try:
                os.waitpid(first_pid, 0)
                break
            except InterruptedError:
                continue
        return True, "Lanceur détaché et réadopté par PID 1."

    try:
        os.setsid()
        second_pid = os.fork()
        if second_pid > 0:
            os._exit(0)

        os.chdir("/")
        os.umask(0o027)
        devnull = os.open("/dev/null", os.O_RDONLY)
        os.dup2(devnull, 0)
        if devnull > 2:
            os.close(devnull)
        os.makedirs(os.path.dirname(launcher_log), exist_ok=True)
        log_fd = os.open(launcher_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        if log_fd > 2:
            os.close(log_fd)
        os.execvpe(argv[0], argv, os.environ.copy())
    except BaseException:
        os._exit(127)
    os._exit(127)


def _systemd_is_booted() -> bool:
    return os.path.isdir("/run/systemd/system")


def launch_script_service(path: str, title: str) -> tuple[bool, str, dict[str, Any]]:
    if not shutil.which("tmux") and not os.path.isfile("/usr/bin/tmux"):
        return False, "tmux est introuvable sur le serveur. Installe le paquet tmux avant de lancer un script.", current_status(path)

    session = session_name_for_path(path)
    status_file = status_path_for_session(session)
    log_file = log_path_for_script(path)
    runtime_argv = [
        sys.executable or "/usr/bin/python3",
        RUNTIME_MODULE,
        "--service",
        "--session", session,
        "--script", path,
        "--status", status_file,
        "--log", log_file,
        "--title", title,
    ]
    launch_mode = "pid1-double-fork"
    unit = ""

    merge_status(
        status_file,
        state="starting",
        session=session,
        script=path,
        title=title,
        log_file=log_file,
        launcher_pid=os.getpid(),
        launch_mode=launch_mode,
        message="Préparation du service indépendant.",
        started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        finished_at="",
        exit_code=None,
    )

    if _systemd_is_booted():
        systemd_run = shutil.which("systemd-run")
        if not systemd_run:
            message = "systemd est actif mais systemd-run est introuvable : lancement refusé pour ne pas rattacher tmux au groupe Gunicorn."
            merge_status(status_file, state="error", message=message, finished_at=datetime.now().astimezone().isoformat(timespec="seconds"))
            return False, message, current_status(path)

        unit = f"yoleo-script-{session[3:]}-{int(time.time())}.service"
        command = [
            systemd_run,
            "--quiet",
            "--collect",
            f"--unit={unit}",
            "--property=Type=simple",
            "--property=KillMode=control-group",
            "--property=TimeoutStopSec=8",
            "--",
            *runtime_argv,
        ]
        code, output = run_argv_capture(command, timeout=12.0)
        if code != 0:
            message = output or f"systemd-run a retourné le code {code}."
            merge_status(
                status_file,
                state="error",
                systemd_unit=unit,
                systemd_error=message,
                message="Impossible de créer l'unité systemd indépendante ; aucun fallback enfant de Gunicorn n'a été utilisé.",
                finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            )
            return False, f"Impossible de créer l'unité systemd indépendante : {message}", current_status(path)

        launch_mode = "systemd-unit"
        merge_status(status_file, launch_mode=launch_mode, systemd_unit=unit, message="Service systemd indépendant lancé.")
    else:
        # Sans systemd (conteneur ou démarrage direct), le double fork + setsid
        # est la seule méthode valable : le runtime durable est réadopté par PID 1.
        ok, message = _daemonize_exec(runtime_argv, os.path.join(LOG_DIR, "launcher.log"))
        if not ok:
            merge_status(status_file, state="error", message=message, finished_at=datetime.now().astimezone().isoformat(timespec="seconds"))
            return False, message, current_status(path)
        merge_status(status_file, launch_mode=launch_mode, message=message)

    # Attend seulement la création de la session, jamais l'exécution du script.
    for _ in range(30):
        if tmux_has_session(session):
            status = current_status(path)
            return True, "Script lancé dans une session tmux indépendante.", status
        data = read_json(status_file)
        if data.get("state") == "error":
            return False, str(data.get("message") or "Erreur de lancement."), current_status(path)
        time.sleep(0.1)
    return False, "La session tmux n'a pas été créée.", current_status(path)


def terminal_url_for_session(session: str) -> tuple[bool, str, str]:
    try:
        import terminal as yoleo_terminal

        conf = yoleo_terminal.get_config()
        ok, message = yoleo_terminal.ensure_terminal_url_args(conf)
        conf = yoleo_terminal.get_config()
        url = yoleo_terminal.ttyd_url_with_args(conf, ["script-manager", session]) if ok else ""
        return bool(ok and url), message, url
    except Exception as exc:
        return False, f"Terminal TTyd indisponible : {exc}", ""



try:
    # Seul le fichier de réglages du gestionnaire est précréé.
    # Les menus YoLeo sont gérés ailleurs et ne doivent jamais être créés,
    # modifiés ou réécrits par ce module.
    ensure_config()
except Exception as exc:
    print(f"⚠️ Initialisation Gestionnaire de scripts : {exc}")


def render_scripts_page(view: str = "list", form_data: dict | None = None, message: str = "", error: str = ""):
    script_rows = list_scripts()
    stats = {
        "total": len(script_rows),
        "shell": sum(1 for row in script_rows if str(row.get("name", "")).lower().endswith(".sh")),
        "python": sum(1 for row in script_rows if str(row.get("name", "")).lower().endswith(".py")),
        "running": sum(1 for row in script_rows if row.get("status", {}).get("state") in {"starting", "running"}),
    }
    return render_template(
        "system_scripts.html",
        scripts=script_rows,
        stats=stats,
        conf=get_config(),
        view=view,
        form_data=form_data or get_script_for_form(),
        message=message,
        error=error,
    )


@scripts_bp.route("/system/scripts")
def home_no_slash():
    return redirect(url_for("scripts_bp.home"))


@scripts_bp.route("/system/scripts/")
def home():
    return render_scripts_page("list", message=request.args.get("message", ""), error=request.args.get("error", ""))


@scripts_bp.route("/system/scripts/create")
def create_page():
    return render_scripts_page("form")


@scripts_bp.route("/system/scripts/edit/<path:filename>")
def edit_page(filename: str):
    try:
        return render_scripts_page("form", get_script_for_form(filename))
    except Exception as exc:
        return redirect(url_for("scripts_bp.home", error=str(exc)))


@scripts_bp.route("/system/scripts/settings")
def settings_page():
    return render_scripts_page("settings", message=request.args.get("message", ""), error=request.args.get("error", ""))


@scripts_bp.route("/system/scripts/settings", methods=["POST"])
def settings_save():
    try:
        raw = str(request.form.get("scripts_dir", "")).strip()
        if not raw:
            raise ValueError("Choisis un dossier de scripts.")
        expanded = os.path.expanduser(os.path.expandvars(raw))
        if not os.path.isabs(expanded):
            raise ValueError("Le dossier doit être un chemin absolu.")
        target = os.path.abspath(expanded)
        os.makedirs(target, exist_ok=True)
        if not os.path.isdir(target):
            raise ValueError("Le chemin choisi n'est pas un dossier.")
        if not os.access(target, os.R_OK | os.W_OK | os.X_OK):
            raise PermissionError("Flask n'a pas les droits de lecture/écriture dans ce dossier.")
        _write_config(target)
        return redirect(url_for("scripts_bp.settings_page", message="Dossier des scripts enregistré."))
    except Exception as exc:
        return redirect(url_for("scripts_bp.settings_page", error=str(exc)))


@scripts_bp.route("/system/scripts/save", methods=["POST"])
def save_script():
    old_filename = str(request.form.get("old_filename", "")).strip()
    filename_base = str(request.form.get("filename_base", request.form.get("filename", ""))).strip()
    script_type = str(request.form.get("script_type", "sh")).strip().lower()
    form = {
        "old_filename": old_filename,
        "filename_base": filename_base,
        "script_type": script_type,
        "filename": "",
        "title": str(request.form.get("title", "")).strip(),
        "description": str(request.form.get("description", "")).strip(),
        "icon": str(request.form.get("icon", "")).strip(),
        "body": str(request.form.get("body", "")),
    }
    try:
        filename = compose_filename(filename_base, script_type)
        form["filename"] = filename
        title = _single_line(form["title"], 240) or Path(filename).stem
        description = _single_line(form["description"], 1000)
        icon = normalize_icon(form["icon"]) if form["icon"] else ""
        destination = script_path(filename)

        old_path = ""
        if old_filename:
            old_path = script_path(old_filename, must_exist=True)
            old_status = current_status(old_path)
            if old_status["session_alive"]:
                raise RuntimeError("Arrête d'abord la session tmux de ce script avant de le modifier.")
        if destination != old_path and os.path.exists(destination):
            raise FileExistsError("Un script porte déjà ce nom.")

        content = build_script_text(filename, title, description, icon, form["body"])
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=f".{filename}.", suffix=".tmp", dir=os.path.dirname(destination))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o755)
            os.replace(temp_path, destination)
        finally:
            try:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            except OSError:
                pass

        if old_path and old_path != destination:
            try:
                os.unlink(old_path)
            except OSError:
                pass
        ensure_executable(destination)
        for stale_path in {old_path, destination}:
            if not stale_path:
                continue
            stale_session = session_name_for_path(stale_path)
            stale_status = status_path_for_session(stale_session)
            if not tmux_has_session(stale_session):
                try:
                    os.unlink(stale_status)
                except OSError:
                    pass
        return redirect(url_for("scripts_bp.home", message=f"Script {filename} enregistré."))
    except Exception as exc:
        form["icon_url"] = icon_url(form.get("icon", ""))
        return render_scripts_page("form", form, error=str(exc)), 400


@scripts_bp.route("/system/scripts/run/<path:filename>", methods=["POST"])
def run_script(filename: str):
    try:
        path = script_path(filename, must_exist=True)
        ensure_executable(path)
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            meta = parse_script_text(handle.read(), fallback_title=Path(filename).stem)
        status = current_status(path)
        reused = status["session_alive"]
        if not reused:
            ok, message, status = launch_script_service(path, meta["title"] or Path(filename).stem)
            if not ok:
                return jsonify({"ok": False, "message": message, "status": status}), 500
        terminal_ok, terminal_message, terminal_url = terminal_url_for_session(status["session"])
        return jsonify({
            "ok": True,
            "message": "Session tmux existante ouverte." if reused else "Script lancé dans tmux.",
            "reused": reused,
            "terminal_ok": terminal_ok,
            "terminal_message": terminal_message,
            "terminal_url": terminal_url,
            "status": current_status(path),
        })
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@scripts_bp.route("/system/scripts/stop/<path:filename>", methods=["POST"])
def stop_script(filename: str):
    try:
        path = script_path(filename, must_exist=True)
        status = current_status(path)
        if not status["session_alive"]:
            return jsonify({"ok": True, "message": "Aucune session tmux active.", "status": status})
        ok, message = tmux_kill_session(status["session"])
        if ok:
            merge_status(
                status["status_file"],
                state="stopped",
                message="Script arrêté depuis l'interface.",
                finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            )
        return jsonify({"ok": ok, "message": message, "status": current_status(path)}), (200 if ok else 500)
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@scripts_bp.route("/system/scripts/close/<path:filename>", methods=["POST"])
def close_finished_session(filename: str):
    """Ferme une session conservée après erreur lorsque la popup est quittée."""
    try:
        path = script_path(filename, must_exist=True)
        status = current_status(path)
        if status["session_alive"] and status["state"] in {"error", "success", "stopped"}:
            tmux_kill_session(status["session"])
        return jsonify({"ok": True, "status": current_status(path)})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@scripts_bp.route("/system/scripts/delete/<path:filename>", methods=["POST"])
def delete_script(filename: str):
    try:
        path = script_path(filename, must_exist=True)
        status = current_status(path)
        if status["session_alive"]:
            ok, message = tmux_kill_session(status["session"])
            if not ok:
                raise RuntimeError(message)
        os.unlink(path)
        try:
            os.unlink(status["status_file"])
        except OSError:
            pass
        return jsonify({"ok": True, "message": f"Script {filename} supprimé."})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@scripts_bp.route("/system/scripts/api/status/<path:filename>")
def api_status(filename: str):
    try:
        path = script_path(filename, must_exist=True)
        status = current_status(path)
        status["label"] = display_status_label(status["state"])
        return jsonify({"ok": True, "status": status})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 404


@scripts_bp.route("/system/scripts/api/list")
def api_list():
    return jsonify({"ok": True, "scripts": list_scripts(), "conf": get_config()})
