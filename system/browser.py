import os
import stat
import pwd
import grp
from urllib.parse import quote
from datetime import datetime
from flask import Blueprint, jsonify, redirect, render_template, request, url_for, send_file, abort

browser_bp = Blueprint("browser", __name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.abspath(os.path.join(BASE_DIR, "static"))

IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg", ".ico", ".tif", ".tiff"
}

MODE_LABELS = {
    "file": "Parcourir un fichier",
    "folder": "Parcourir un dossier",
    "image": "Parcourir une image",
}


def _normalize_mode(value: str) -> str:
    mode = (value or "file").strip().lower()
    if mode in {"picture", "photo", "img"}:
        return "image"
    if mode not in MODE_LABELS:
        return "file"
    return mode


def _expand_path(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raw = "/"
    raw = os.path.expanduser(os.path.expandvars(raw))
    if not os.path.isabs(raw):
        raw = os.path.abspath(raw)
    return os.path.abspath(raw)


def _is_static_virtual_path(value: str) -> bool:
    raw = (value or "").strip()
    return raw == "/static" or raw.startswith("/static/")


def _resolve_path_pair(value: str) -> tuple[str, str, bool]:
    """Return (real_path, display_path, is_static_virtual)."""
    raw = (value or "").strip() or "/"
    if _is_static_virtual_path(raw):
        rel = raw[len("/static"):].lstrip("/")
        real = os.path.abspath(os.path.join(STATIC_DIR, rel))
        if not real.startswith(STATIC_DIR):
            real = STATIC_DIR
            rel = ""
        display = "/static" + (("/" + rel) if rel else "")
        return real, display, True
    real = _expand_path(raw)
    return real, real, False


def _display_child_path(parent_display: str, name: str) -> str:
    if parent_display == "/":
        return "/" + name
    return parent_display.rstrip("/") + "/" + name


def _safe_owner_group(st: os.stat_result) -> str:
    try:
        user = pwd.getpwuid(st.st_uid).pw_name
    except Exception:
        user = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except Exception:
        group = str(st.st_gid)
    return f"{user}:{group}"


def _human_size(size: int) -> str:
    try:
        value = float(size)
    except Exception:
        return "—"
    units = ["o", "Ko", "Mo", "Go", "To", "Po"]
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    if index == 0:
        return f"{int(value)} {units[index]}"
    return f"{value:.1f} {units[index]}"


def _is_image_name(name: str) -> bool:
    return os.path.splitext(name or "")[1].lower() in IMAGE_EXTENSIONS


def _entry_for(path: str, name: str, mode: str, display_path: str | None = None) -> dict | None:
    full_path = os.path.join(path, name)
    try:
        st = os.stat(full_path)
    except OSError:
        return None

    is_dir = stat.S_ISDIR(st.st_mode)
    is_file = stat.S_ISREG(st.st_mode)
    is_image = is_file and _is_image_name(name)

    if mode == "folder" and not is_dir:
        return None
    if mode == "image" and not (is_dir or is_image):
        return None
    if mode == "file" and not (is_dir or is_file):
        return None

    child_display_path = _display_child_path(display_path or path, name)
    preview_url = ""
    if is_image:
        if child_display_path.startswith("/static/"):
            preview_url = child_display_path
        else:
            preview_url = "/browser/api/preview?path=" + quote(child_display_path, safe="")

    return {
        "name": name,
        "path": child_display_path,
        "type": "dossier" if is_dir else ("image" if is_image else "fichier"),
        "is_dir": is_dir,
        "is_file": is_file,
        "is_image": is_image,
        "preview_url": preview_url,
        "selectable": (mode == "folder" and is_dir) or (mode == "file" and is_file) or (mode == "image" and is_image),
        "size": "—" if is_dir else _human_size(st.st_size),
        "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        "perms": stat.filemode(st.st_mode),
        "owner": _safe_owner_group(st),
    }


def _nearest_existing_directory(real_path: str, display_path: str, is_static: bool) -> tuple[str, str]:
    """Return the closest existing parent directory for a missing browser path."""
    real = os.path.abspath(real_path or "/")
    display = (display_path or real or "/").rstrip("/") or "/"

    if is_static:
        static_root = os.path.abspath(STATIC_DIR)
        while real != static_root and not os.path.isdir(real):
            next_real = os.path.dirname(real.rstrip(os.sep)) or static_root
            if next_real == real:
                break
            real = next_real
            display = os.path.dirname(display.rstrip("/")) or "/static"
            if not display.startswith("/static"):
                display = "/static"
        if not os.path.isdir(real) or not real.startswith(static_root):
            real = static_root
            display = "/static"
        return real, display

    while real != os.path.dirname(real) and not os.path.isdir(real):
        real = os.path.dirname(real.rstrip(os.sep)) or "/"
    if not os.path.isdir(real):
        real = "/"
    return real, real


def _list_directory(path: str, mode: str) -> tuple[dict, int]:
    current, display_current, is_static = _resolve_path_pair(path)
    fallback_from = ""
    if not os.path.exists(current):
        fallback_from = display_current
        current, display_current = _nearest_existing_directory(current, display_current, is_static)
    if not os.path.isdir(current):
        current = os.path.dirname(current) or "/"
        if is_static and current.startswith(STATIC_DIR):
            rel = os.path.relpath(current, STATIC_DIR)
            display_current = "/static" if rel == "." else "/static/" + rel.replace(os.sep, "/")
        else:
            display_current = current

    try:
        names = os.listdir(current)
    except PermissionError:
        return {"ok": False, "error": "Permission refusée", "path": display_current, "entries": []}, 403
    except OSError as exc:
        return {"ok": False, "error": str(exc), "path": display_current, "entries": []}, 500

    entries = []
    for name in names:
        if name in {".", ".."}:
            continue
        item = _entry_for(current, name, mode, display_current)
        if item is not None:
            entries.append(item)

    entries.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
    if is_static:
        if display_current == "/static":
            parent = "/static"
        else:
            parent = os.path.dirname(display_current.rstrip("/")) or "/static"
            if not parent.startswith("/static"):
                parent = "/static"
    else:
        parent = os.path.dirname(current.rstrip(os.sep)) or "/"
    payload = {
        "ok": True,
        "mode": mode,
        "path": display_current,
        "parent": parent,
        "can_select_current": mode == "folder",
        "entries": entries,
    }
    if fallback_from and fallback_from != display_current:
        payload["fallback_from"] = fallback_from
        payload["warning"] = f"Chemin introuvable, retour à {display_current}"
    return payload, 200

@browser_bp.route("/browser")
def browser_home():
    return redirect(url_for("browser.browser_file"))


@browser_bp.route("/browser/file")
def browser_file():
    return _render_browser("file")


@browser_bp.route("/browser/folder")
def browser_folder():
    return _render_browser("folder")


@browser_bp.route("/browser/image")
def browser_image():
    return _render_browser("image")


@browser_bp.route("/browser/picture")
def browser_picture():
    return redirect(url_for("browser.browser_image", **request.args))


def _render_browser(mode: str):
    mode = _normalize_mode(mode)
    start_path = request.args.get("path") or request.args.get("start") or "/"
    target = request.args.get("target", "")
    return render_template(
        "browser/browser.html",
        mode=mode,
        mode_label=MODE_LABELS[mode],
        start_path=_resolve_path_pair(start_path)[1],
        target=target,
    )



@browser_bp.route("/browser/api/preview")
def browser_preview():
    raw_path = request.args.get("path", "")
    real_path, _display_path, _is_static = _resolve_path_pair(raw_path)
    if not real_path or not os.path.isfile(real_path):
        abort(404)
    if not _is_image_name(real_path):
        abort(404)
    try:
        return send_file(real_path, conditional=True, max_age=60)
    except OSError:
        abort(404)

@browser_bp.route("/browser/list")
@browser_bp.route("/browser/api/list")
def browser_list():
    mode = _normalize_mode(request.args.get("mode", "file"))
    path = request.args.get("path") or request.args.get("start") or "/"
    payload, status = _list_directory(path, mode)
    return jsonify(payload), status
