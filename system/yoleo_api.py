#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""API JSON commune aux clients Android et Windows de Yoleo.

Le TLS mutuel reste terminé par Nginx Proxy Manager. Cette API ajoute la
seconde couche d'authentification (PAM vers jeton révocable) et expose seulement
des opérations métier déjà présentes dans Yoleo. Aucune commande shell libre
n'est acceptée.
"""

from __future__ import annotations

import hashlib
import importlib
import mimetypes
import os
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Callable

from flask import Blueprint, current_app, g, jsonify, request, send_file


API_VERSION = "1"
DEFAULT_TOKEN_DAYS = 365
MIN_TOKEN_DAYS = 1
MAX_TOKEN_DAYS = 3650

CONTAINER_ACTIONS = {"start", "stop", "restart", "start_all", "stop_all"}
SERVICE_ACTIONS = {
    "start_docker_service",
    "restart_docker_service",
    "stop_docker_service",
}
ALLOWED_DOCKER_ACTIONS = CONTAINER_ACTIONS | SERVICE_ACTIONS
ALLOWED_VM_ACTIONS = {"start", "shutdown", "reboot", "destroy"}
ALLOWED_TASK_ACTIONS = {"start", "stop"}
ALLOWED_BACKUP_ACTIONS = {"start", "stop"}
ALLOWED_FILE_ACTIONS = {"mkdir", "rename", "copy", "move", "delete"}


def _utc_iso(timestamp: int | float | None = None) -> str:
    value = time.time() if timestamp is None else float(timestamp)
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_text(value: Any, maximum: int = 120) -> str:
    return str(value or "").strip().replace("\r", " ").replace("\n", " ")[:maximum]


def _file_roots() -> list[str]:
    raw = str(os.environ.get("YOLEO_API_FILE_ROOTS", "/mnt,/media,/srv"))
    roots = []
    for item in raw.split(","):
        path = os.path.realpath(os.path.abspath(os.path.expanduser(item.strip())))
        if path and path != "/" and path not in roots:
            roots.append(path)
    return roots or ["/mnt"]


def _path_inside_root(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except (TypeError, ValueError):
        return False


def _safe_file_path(value: Any, *, must_exist: bool = True) -> str:
    raw = str(value or "").strip()
    if not raw or "\x00" in raw:
        raise ValueError("Chemin invalide")
    absolute = os.path.abspath(os.path.expanduser(raw))
    if must_exist:
        resolved = os.path.realpath(absolute)
    else:
        parent = os.path.realpath(os.path.dirname(absolute) or "/")
        resolved = os.path.join(parent, os.path.basename(absolute))
    if not any(_path_inside_root(resolved, root) for root in _file_roots()):
        raise PermissionError("Chemin hors des racines NAS autorisées")
    if must_exist and not os.path.exists(absolute):
        raise FileNotFoundError("Chemin introuvable")
    return absolute


def _safe_file_name(value: Any) -> str:
    name = str(value or "").strip().replace("\x00", "")
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("Nom invalide")
    return name[:255]


def _file_item(path: str) -> dict[str, Any]:
    info = os.lstat(path)
    is_link = os.path.islink(path)
    is_dir = os.path.isdir(path) and not is_link
    return {
        "name": os.path.basename(path.rstrip(os.sep)) or path,
        "path": path,
        "is_dir": is_dir,
        "is_symlink": is_link,
        "size": int(info.st_size),
        "mtime": int(info.st_mtime),
        "extension": "" if is_dir else os.path.splitext(path)[1].lower(),
    }


def _tree_contains_symlink(path: str) -> bool:
    if os.path.islink(path):
        return True
    if not os.path.isdir(path):
        return False
    for root, directories, files in os.walk(path):
        for name in directories + files:
            if os.path.islink(os.path.join(root, name)):
                return True
    return False


def _zip_directory(source: str, archive_path: str) -> None:
    parent = os.path.dirname(source.rstrip(os.sep))
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        allowZip64=True,
    ) as archive:
        for current, directories, files in os.walk(source, topdown=True, followlinks=False):
            directories[:] = [
                name
                for name in sorted(directories, key=str.lower)
                if not os.path.islink(os.path.join(current, name))
            ]
            relative_directory = os.path.relpath(current, parent).replace(os.sep, "/").rstrip("/") + "/"
            archive.writestr(relative_directory, b"")
            for name in sorted(files, key=str.lower):
                full_path = os.path.join(current, name)
                if os.path.islink(full_path):
                    continue
                archive.write(
                    full_path,
                    os.path.relpath(full_path, parent).replace(os.sep, "/"),
                )


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _catalog_max_entries() -> int:
    try:
        value = int(os.environ.get("YOLEO_API_CATALOG_MAX_ENTRIES", "100000"))
    except (TypeError, ValueError):
        value = 100000
    return max(1000, min(500000, value))


def _token_lifetime_days() -> int:
    raw = str(os.environ.get("YOLEO_API_TOKEN_DAYS", DEFAULT_TOKEN_DAYS)).strip()
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = DEFAULT_TOKEN_DAYS
    return max(MIN_TOKEN_DAYS, min(MAX_TOKEN_DAYS, days))


class ApiTokenStore:
    """Stockage SQLite partagé par les workers Gunicorn.

    Seule l'empreinte SHA-256 du jeton est conservée. Le jeton brut n'est
    retourné qu'une fois, au client qui vient de réussir l'authentification.
    """

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with closing(self._open()) as database:
                database.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS api_tokens (
                        token_hash TEXT PRIMARY KEY,
                        username TEXT NOT NULL,
                        device_name TEXT NOT NULL DEFAULT '',
                        platform TEXT NOT NULL DEFAULT '',
                        created_at INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL,
                        last_used_at INTEGER NOT NULL,
                        revoked_at INTEGER
                    );
                    CREATE INDEX IF NOT EXISTS idx_api_tokens_expiry
                        ON api_tokens(expires_at, revoked_at);
                    """
                )
                database.commit()
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            self._schema_ready = True

    def issue(self, username: str, device_name: str, platform: str) -> tuple[str, dict[str, Any]]:
        self._ensure_schema()
        now = int(time.time())
        expires_at = now + (_token_lifetime_days() * 86400)
        token = "yoleo_" + secrets.token_urlsafe(48)
        record = {
            "username": username,
            "device_name": device_name,
            "platform": platform,
            "created_at": now,
            "expires_at": expires_at,
            "last_used_at": now,
        }
        with closing(self._open()) as database:
            database.execute(
                "DELETE FROM api_tokens WHERE expires_at <= ? OR revoked_at IS NOT NULL",
                (now,),
            )
            database.execute(
                """
                INSERT INTO api_tokens(
                    token_hash, username, device_name, platform,
                    created_at, expires_at, last_used_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    self.token_hash(token),
                    username,
                    device_name,
                    platform,
                    now,
                    expires_at,
                    now,
                ),
            )
            database.commit()
        return token, record

    def lookup(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        self._ensure_schema()
        now = int(time.time())
        digest = self.token_hash(token)
        with closing(self._open()) as database:
            row = database.execute(
                """
                SELECT username, device_name, platform, created_at, expires_at, last_used_at
                FROM api_tokens
                WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?
                """,
                (digest, now),
            ).fetchone()
            if row is None:
                return None
            data = dict(row)
            if now - int(data.get("last_used_at") or 0) >= 300:
                database.execute(
                    "UPDATE api_tokens SET last_used_at=? WHERE token_hash=?",
                    (now, digest),
                )
                data["last_used_at"] = now
                database.commit()
            return data

    def revoke(self, token: str) -> bool:
        if not token:
            return False
        self._ensure_schema()
        now = int(time.time())
        with closing(self._open()) as database:
            cursor = database.execute(
                """
                UPDATE api_tokens SET revoked_at=?
                WHERE token_hash=? AND revoked_at IS NULL
                """,
                (now, self.token_hash(token)),
            )
            database.commit()
        return cursor.rowcount > 0


def create_yoleo_api_blueprint(
    authenticate_user: Callable[[str, str], bool],
    allowed_user: str,
    token_db_path: str,
) -> Blueprint:
    """Construit le Blueprint sans réimporter ``app.py`` en boucle."""

    api_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")
    token_store = ApiTokenStore(token_db_path)
    public_endpoints = {"api_v1.health", "api_v1.login"}

    def ok_response(**payload: Any):
        return jsonify({"ok": True, "api_version": API_VERSION, **payload})

    def error_response(code: str, message: str, status: int):
        return jsonify(
            {
                "ok": False,
                "api_version": API_VERSION,
                "error": {"code": code, "message": message},
            }
        ), status

    def bearer_token() -> str:
        header = str(request.headers.get("Authorization") or "").strip()
        scheme, separator, value = header.partition(" ")
        if not separator or scheme.lower() != "bearer":
            return ""
        return value.strip()

    @api_bp.before_request
    def require_api_token():
        if request.endpoint in public_endpoints:
            return None
        token = bearer_token()
        identity = token_store.lookup(token)
        if identity is None:
            return error_response(
                "authentication_required",
                "Jeton API absent, expiré ou invalide.",
                401,
            )
        g.yoleo_api_token = token
        g.yoleo_api_identity = identity
        return None

    @api_bp.after_request
    def secure_api_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @api_bp.get("")
    @api_bp.get("/")
    @api_bp.get("/health")
    def health():
        # Sur l'URL publique, atteindre cette route signifie déjà que Nginx
        # Proxy Manager a accepté le certificat client pendant le handshake.
        return ok_response(service="yoleo", server_time=_utc_iso())

    @api_bp.post("/auth/login")
    def login():
        if not request.is_json:
            return error_response(
                "json_required",
                "Le corps de la requête doit être au format JSON.",
                415,
            )
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return error_response("invalid_json", "Corps JSON invalide.", 400)

        username = _clean_text(payload.get("username"), 128)
        password = str(payload.get("password") or "")
        device_name = _clean_text(payload.get("device_name"), 120) or "Appareil Yoleo"
        platform = _clean_text(payload.get("platform"), 32).lower() or "unknown"

        authenticated = False
        if username and password and username == allowed_user:
            try:
                authenticated = bool(authenticate_user(username, password))
            except Exception:
                authenticated = False
        if not authenticated:
            return error_response(
                "invalid_credentials",
                "Nom d'utilisateur ou mot de passe invalide.",
                401,
            )

        token, record = token_store.issue(username, device_name, platform)
        return ok_response(
            authentication={
                "access_token": token,
                "token_type": "Bearer",
                "expires_at": _utc_iso(record["expires_at"]),
                "expires_in": int(record["expires_at"]) - int(time.time()),
                "username": username,
                "device_name": device_name,
                "platform": platform,
            }
        )

    @api_bp.post("/auth/logout")
    def logout():
        revoked = token_store.revoke(str(g.get("yoleo_api_token") or ""))
        return ok_response(revoked=revoked)

    @api_bp.get("/me")
    def me():
        identity = dict(g.get("yoleo_api_identity") or {})
        return ok_response(
            identity={
                "username": identity.get("username", ""),
                "device_name": identity.get("device_name", ""),
                "platform": identity.get("platform", ""),
                "created_at": _utc_iso(identity.get("created_at", 0)),
                "expires_at": _utc_iso(identity.get("expires_at", 0)),
            }
        )

    @api_bp.get("/capabilities")
    def capabilities():
        return ok_response(
            capabilities={
                "overview": True,
                "monitoring_snapshot": True,
                "monitoring_sections": [
                    "system",
                    "storage",
                    "docker",
                    "vms",
                    "samba",
                    "tasks",
                    "backup",
                    "build",
                ],
                "docker_actions": sorted(ALLOWED_DOCKER_ACTIONS),
                "vm_actions": sorted(ALLOWED_VM_ACTIONS),
                "task_actions": sorted(ALLOWED_TASK_ACTIONS),
                "backup_actions": sorted(ALLOWED_BACKUP_ACTIONS),
                "file_browser": True,
                "file_roots": _file_roots(),
                "file_actions": sorted(ALLOWED_FILE_ACTIONS),
                "file_upload": True,
                "file_download": True,
                "file_download_directory_zip": True,
                "file_catalog_sha256": True,
                "arbitrary_commands": False,
            }
        )

    @api_bp.get("/overview")
    def overview():
        try:
            system_module = importlib.import_module("system")
            collector = getattr(system_module, "collect_overview")
            data = collector()
        except Exception:
            return error_response(
                "overview_unavailable",
                "L'aperçu système est momentanément indisponible.",
                503,
            )
        return ok_response(overview=data)

    @api_bp.get("/monitoring/snapshot")
    def monitoring_snapshot():
        """Retourne le cliché léger consommé par les agents natifs.

        Le client effectue une seule requête par intervalle. Chaque section est
        isolée : une panne de Docker, Samba ou Task n'empêche pas de recevoir
        les autres mesures et l'agent peut continuer à surveiller le serveur.
        """

        errors: list[dict[str, str]] = []

        def section_error(section: str, code: str, message: str) -> None:
            errors.append({"section": section, "code": code, "message": message})

        overview_data: dict[str, Any] = {}
        system_module = None
        try:
            system_module = importlib.import_module("system")
            overview_data = dict(system_module.collect_overview() or {})
        except Exception:
            section_error(
                "system",
                "overview_unavailable",
                "Les mesures système sont momentanément indisponibles.",
            )

        cpu = overview_data.get("cpu") if isinstance(overview_data.get("cpu"), dict) else {}
        ram = overview_data.get("ram") if isinstance(overview_data.get("ram"), dict) else {}
        main_disk = overview_data.get("disk") if isinstance(overview_data.get("disk"), dict) else {}
        mount_data = overview_data.get("mounts") if isinstance(overview_data.get("mounts"), dict) else {}
        host_data = overview_data.get("host") if isinstance(overview_data.get("host"), dict) else {}
        network_data = overview_data.get("network") if isinstance(overview_data.get("network"), dict) else {}
        service_summary = overview_data.get("services") if isinstance(overview_data.get("services"), dict) else {}
        fan_data = overview_data.get("fans") if isinstance(overview_data.get("fans"), dict) else {}
        hardware_data: dict[str, Any] = {}
        if system_module is not None:
            hardware_collector = getattr(system_module, "collect_mobile_hardware_stats", None)
            if callable(hardware_collector):
                try:
                    hardware_data = dict(hardware_collector() or {})
                except Exception:
                    section_error(
                        "hardware",
                        "hardware_unavailable",
                        "Les températures et GPU sont momentanément indisponibles.",
                    )

        temperatures = []
        for raw_temperature in hardware_data.get("temperatures") or []:
            if not isinstance(raw_temperature, dict):
                continue
            temperatures.append({
                "id": _clean_text(raw_temperature.get("id"), 120),
                "chip": _clean_text(raw_temperature.get("chip"), 120),
                "label": _clean_text(raw_temperature.get("label"), 160),
                "current": raw_temperature.get("current"),
                "high": raw_temperature.get("high"),
                "critical": raw_temperature.get("critical"),
            })

        gpus = []
        for raw_gpu in hardware_data.get("gpus") or []:
            if not isinstance(raw_gpu, dict):
                continue
            gpus.append({
                "type": _clean_text(raw_gpu.get("type"), 32),
                "source": _clean_text(raw_gpu.get("source"), 32),
                "label": _clean_text(raw_gpu.get("label"), 120),
                "name": _clean_text(raw_gpu.get("name"), 200),
                "load": _clean_text(raw_gpu.get("load"), 32),
                "mem": _clean_text(raw_gpu.get("mem"), 32),
                "temp": _clean_text(raw_gpu.get("temp"), 32),
                "power": _clean_text(raw_gpu.get("power"), 32),
                "fan": _clean_text(raw_gpu.get("fan"), 32),
            })

        fans = []
        for raw_fan in fan_data.get("rows") or []:
            if not isinstance(raw_fan, dict):
                continue
            fans.append({
                "id": _clean_text(raw_fan.get("id"), 120),
                "label": _clean_text(raw_fan.get("label"), 160),
                "rpm": raw_fan.get("rpm", 0),
                "rpm_label": _clean_text(raw_fan.get("rpm_label"), 64),
                "status": _clean_text(raw_fan.get("status"), 32),
            })

        storage_volumes = []
        for raw_volume in mount_data.get("usage_rows") or []:
            if not isinstance(raw_volume, dict):
                continue
            storage_volumes.append(
                {
                    "path": _clean_text(raw_volume.get("path"), 512),
                    "percent": raw_volume.get("percent", 0),
                    "used": _clean_text(raw_volume.get("used"), 64),
                    "free": _clean_text(raw_volume.get("free"), 64),
                    "total": _clean_text(raw_volume.get("total"), 64),
                    "status": _clean_text(raw_volume.get("status"), 32),
                    "status_label": _clean_text(raw_volume.get("status_label"), 120),
                    "ok": bool(raw_volume.get("ok")),
                }
            )

        storage_mounts = []
        usage_by_path = {
            item["path"]: item
            for item in storage_volumes
            if item.get("path")
        }
        raw_mount_candidates = []
        if system_module is not None:
            candidate_collector = getattr(system_module, "disk_top_collect_candidates", None)
            if callable(candidate_collector):
                try:
                    candidate_data = candidate_collector() or {}
                    if isinstance(candidate_data, dict):
                        raw_mount_candidates = candidate_data.get("rows") or []
                except Exception:
                    section_error(
                        "storage",
                        "mount_inventory_unavailable",
                        "L'inventaire des points de montage est momentanément indisponible.",
                    )

        if not raw_mount_candidates:
            raw_mount_candidates = list(mount_data.get("rows") or [])
            raw_mount_candidates.extend(mount_data.get("usage_rows") or [])

        mounted_map = None
        usage_collector = getattr(system_module, "disk_top_usage_row", None) if system_module else None
        mounted_collector = getattr(system_module, "disk_top_findmnt_map", None) if system_module else None
        seen_mount_paths = set()
        for raw_mount in raw_mount_candidates:
            if not isinstance(raw_mount, dict):
                continue
            path = _clean_text(raw_mount.get("path"), 512)
            if not path or path in seen_mount_paths:
                continue
            seen_mount_paths.add(path)

            usage = dict(usage_by_path.get(path) or {})
            is_mount = bool(raw_mount.get("is_mount", raw_mount.get("ok")))
            if not usage and is_mount and callable(usage_collector) and callable(mounted_collector):
                try:
                    if mounted_map is None:
                        mounted_map = mounted_collector() or {}
                    usage = dict(usage_collector(path, mounted_map) or {})
                except Exception:
                    usage = {}

            status = _clean_text(usage.get("status") or raw_mount.get("status"), 32)
            status_label = _clean_text(
                usage.get("status_label") or raw_mount.get("status_label"),
                120,
            )
            storage_mounts.append(
                {
                    "path": path,
                    "label": _clean_text(raw_mount.get("label") or os.path.basename(path.rstrip("/")), 160),
                    "exists": bool(raw_mount.get("exists")),
                    "is_mount": is_mount,
                    "source": _clean_text(raw_mount.get("source") or usage.get("source"), 256),
                    "fstype": _clean_text(raw_mount.get("fstype") or usage.get("fstype"), 64),
                    "status": status,
                    "status_label": status_label,
                    "ok": bool(usage.get("ok")) if usage else is_mount,
                    "percent": usage.get("percent", 0),
                    "used": _clean_text(usage.get("used"), 64),
                    "free": _clean_text(usage.get("free"), 64),
                    "total": _clean_text(usage.get("total"), 64),
                    "home_selected": bool(raw_mount.get("selected")),
                    "home_usage_selected": bool(raw_mount.get("usage_selected")),
                }
            )

        storage_mounts.sort(key=lambda item: item["path"].lower())

        docker_data: dict[str, Any] = {
            "available": False,
            "service": {"state": "unknown", "label": "Inconnu", "active": None},
            "stats": {"total": 0, "running": 0, "stopped": 0},
            "containers": [],
        }
        docker_client = None
        try:
            dockers_module = importlib.import_module("dockers")
            service_status = dict(dockers_module.get_docker_service_status() or {})
            docker_data["service"] = service_status
            docker_data["available"] = service_status.get("active") is not None

            if service_status.get("active") is True:
                docker_client = dockers_module.get_docker_client()
                stacks = dict(dockers_module.list_stacks(docker_client) or {})
                docker_data["stats"] = dict(dockers_module.get_docker_stats(stacks) or {})
                containers = []
                for stack_name, stack_containers in stacks.items():
                    for container in stack_containers or []:
                        if not isinstance(container, dict):
                            continue
                        containers.append(
                            {
                                "id": _clean_text(container.get("id"), 128),
                                "name": _clean_text(container.get("name"), 160),
                                "state": _clean_text(container.get("state"), 32).lower() or "unknown",
                                "stack": _clean_text(stack_name, 160),
                                "icon": _clean_text(container.get("icon"), 1024),
                            }
                        )
                containers.sort(key=lambda item: (item["stack"].lower(), item["name"].lower()))
                docker_data["containers"] = containers
        except Exception as exc:
            try:
                message = dockers_module.clean_docker_error(exc)
            except Exception:
                message = "L'état Docker est momentanément indisponible."
            section_error("docker", "docker_unavailable", _clean_text(message, 300))
        finally:
            if docker_client is not None:
                try:
                    docker_client.close()
                except Exception:
                    pass

        vm_data: dict[str, Any] = {
            "available": False,
            "summary": {"total": 0, "running": 0, "stopped": 0, "paused": 0},
            "machines": [],
        }
        try:
            vm_module = importlib.import_module("vm")
            vm_conf = vm_module.get_config()
            names, vm_error = vm_module.list_vm_names(vm_conf)
            if vm_error:
                raise RuntimeError(vm_error)
            machines = []
            for name in names:
                state = _clean_text(vm_module.get_vm_state(vm_conf, name), 160) or "unknown"
                state_kind = _clean_text(vm_module.state_class(state), 32) or "unknown"
                machines.append(
                    {
                        "name": _clean_text(name, 160),
                        "state": state,
                        "state_class": state_kind,
                        "running": state_kind == "running",
                    }
                )
            vm_data = {
                "available": True,
                "summary": {
                    "total": len(machines),
                    "running": sum(1 for item in machines if item["state_class"] == "running"),
                    "stopped": sum(1 for item in machines if item["state_class"] == "stopped"),
                    "paused": sum(1 for item in machines if item["state_class"] == "paused"),
                },
                "machines": machines,
            }
        except Exception:
            section_error(
                "vms",
                "vm_unavailable",
                "L'état des machines virtuelles est momentanément indisponible.",
            )

        samba_data: dict[str, Any] = {"available": False, "ok": False, "services": []}
        try:
            partage_module = importlib.import_module("partage")
            service_names = list(getattr(partage_module, "SAMBA_SERVICES", ("smbd.service", "nmbd.service")))
            service_names.extend(
                getattr(partage_module, "DISTRO_WSDD_SERVICES", ("wsdd2.service", "wsdd.service"))
            )
            samba_services = []
            for service_name in dict.fromkeys(service_names):
                state = dict(partage_module.service_state(service_name) or {})
                if state.get("exists") or service_name in {"smbd.service", "nmbd.service"}:
                    samba_services.append(
                        {
                            "name": _clean_text(state.get("name") or service_name, 120),
                            "exists": bool(state.get("exists")),
                            "active": _clean_text(state.get("active"), 32) or "unknown",
                            "enabled": _clean_text(state.get("enabled"), 32) or "unknown",
                            "ok": bool(state.get("ok")),
                        }
                    )
            existing_services = [item for item in samba_services if item["exists"]]
            samba_data = {
                "available": bool(existing_services),
                "ok": bool(existing_services) and all(item["ok"] for item in existing_services),
                "services": samba_services,
            }
        except Exception:
            section_error(
                "samba",
                "samba_unavailable",
                "L'état des services Samba est momentanément indisponible.",
            )

        task_rows = []
        try:
            task_module = importlib.import_module("task")
            for task in task_module.get_all_tasks() or []:
                if not isinstance(task, dict):
                    continue
                status = task.get("status") if isinstance(task.get("status"), dict) else {}
                task_rows.append(
                    {
                        "id": task.get("id"),
                        "title": _clean_text(task.get("title"), 200),
                        "enabled": bool(task.get("enabled")),
                        "running": bool(status.get("running")),
                        "status": _clean_text(status.get("status"), 120),
                        "result": _clean_text(status.get("result"), 64),
                        "last_run": _clean_text(status.get("last_run"), 64),
                        "last_end": _clean_text(status.get("last_end"), 64),
                        "last_message": _clean_text(status.get("last_message"), 500),
                        "updated_at": _clean_text(status.get("updated_at"), 64),
                    }
                )
        except Exception:
            section_error(
                "tasks",
                "tasks_unavailable",
                "L'état des tâches est momentanément indisponible.",
            )

        backup_data: dict[str, Any] = {
            "available": False,
            "summary": {"total": 0, "running": 0},
            "scripts": [],
        }
        try:
            backup_module = importlib.import_module("backup")
            backup_scripts = []
            for script in backup_module.list_scripts() or []:
                if not isinstance(script, dict):
                    continue
                status = script.get("status") if isinstance(script.get("status"), dict) else {}
                progress = status.get("progress") if isinstance(status.get("progress"), dict) else {}
                mode = _clean_text(script.get("mode"), 32).lower() or "backup"
                backup_scripts.append(
                    {
                        "filename": _clean_text(script.get("name"), 255),
                        "title": _clean_text(script.get("title"), 200),
                        "mode": mode,
                        "source": _clean_text(script.get("source"), 512),
                        "target": _clean_text(script.get("target"), 512),
                        "running": bool(status.get("running")),
                        "result": _clean_text(status.get("result"), 64),
                        "message": _clean_text(status.get("message"), 500),
                        "started_at": _clean_text(status.get("started_at"), 64),
                        "ended_at": _clean_text(status.get("ended_at"), 64),
                        "phase": _clean_text(status.get("phase"), 64),
                        "progress_percent": progress.get("percent", 0),
                        "progress_text": _clean_text(status.get("progress_text"), 160),
                    }
                )
            backup_data = {
                "available": True,
                "summary": {
                    "total": len(backup_scripts),
                    "running": sum(1 for item in backup_scripts if item["running"]),
                },
                "scripts": backup_scripts,
            }
        except Exception:
            section_error(
                "backup",
                "backup_unavailable",
                "L'état des sauvegardes est momentanément indisponible.",
            )

        raw_build = overview_data.get("build") if isinstance(overview_data.get("build"), dict) else {}
        build_data = {
            "available": bool(raw_build.get("available")),
            "label": _clean_text(raw_build.get("label"), 160),
            "total": raw_build.get("total", 0),
            "projects": raw_build.get("projects", 0),
            "tars": raw_build.get("tars", 0),
            "to_build": raw_build.get("to_build", 0),
            "to_push": raw_build.get("to_push", 0),
            "meta_missing": raw_build.get("meta_missing", 0),
            "updated_at": _clean_text(raw_build.get("updated_at"), 64),
        }

        return ok_response(
            monitoring={
                "generated_at": _utc_iso(),
                "system": {
                    "cpu_percent": cpu.get("percent", 0),
                    "ram_percent": ram.get("percent", 0),
                    "uptime": _clean_text(overview_data.get("uptime"), 120),
                    "host": {
                        "hostname": _clean_text(host_data.get("hostname"), 160),
                        "os": _clean_text(host_data.get("os"), 240),
                        "kernel": _clean_text(host_data.get("kernel"), 120),
                        "cpu_model": _clean_text(host_data.get("cpu_model"), 240),
                        "local_ip": _clean_text(host_data.get("local_ip"), 64),
                        "boot_time": _clean_text(host_data.get("boot_time"), 64),
                    },
                    "network": {
                        "iface": _clean_text(network_data.get("iface"), 64),
                        "ip": _clean_text(network_data.get("ip") or host_data.get("local_ip"), 64),
                        "gateway": _clean_text(network_data.get("gateway"), 64),
                        "state": _clean_text(network_data.get("state"), 32),
                        "speed": _clean_text(network_data.get("speed"), 64),
                    },
                    "services": {
                        "total": service_summary.get("total", 0),
                        "active": service_summary.get("active", 0),
                        "running": service_summary.get("running", 0),
                        "failed": service_summary.get("failed", 0),
                        "enabled": service_summary.get("enabled", 0),
                    },
                    "temperatures": temperatures,
                    "fans": {
                        "available": bool(fans),
                        "count": len(fans),
                        "rows": fans,
                    },
                    "gpus": gpus,
                },
                "storage": {
                    "main": {
                        "path": _clean_text(main_disk.get("mount"), 512),
                        "percent": main_disk.get("percent", 0),
                        "used": _clean_text(main_disk.get("used"), 64),
                        "total": _clean_text(main_disk.get("total"), 64),
                    },
                    "volumes": storage_volumes,
                    "mounts": storage_mounts,
                    "mount_state": _clean_text(mount_data.get("state"), 32),
                    "mount_label": _clean_text(mount_data.get("label"), 120),
                },
                "docker": docker_data,
                "vms": vm_data,
                "samba": samba_data,
                "tasks": task_rows,
                "backup": backup_data,
                "build": build_data,
                "errors": errors,
            }
        )

    @api_bp.post("/docker/actions")
    def docker_actions():
        if not request.is_json:
            return error_response(
                "json_required",
                "Le corps de la requête doit être au format JSON.",
                415,
            )
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return error_response("invalid_json", "Corps JSON invalide.", 400)

        action = _clean_text(payload.get("action"), 64).lower()
        container_id = _clean_text(payload.get("container_id") or payload.get("id"), 128)
        if action not in ALLOWED_DOCKER_ACTIONS:
            return error_response(
                "action_not_allowed",
                "Action Docker inconnue ou non autorisée par l'API.",
                400,
            )
        if action in {"start", "stop", "restart"} and not container_id:
            return error_response(
                "container_required",
                "L'identifiant du conteneur est obligatoire pour cette action.",
                400,
            )

        try:
            dockers_module = importlib.import_module("dockers")
            if action in SERVICE_ACTIONS:
                result, status = dockers_module.do_docker_service_action(action)
            else:
                client = dockers_module.get_docker_client()
                try:
                    result, status = dockers_module.do_action(client, container_id, action)
                finally:
                    try:
                        client.close()
                    except Exception:
                        pass
        except Exception as exc:
            try:
                message = dockers_module.clean_docker_error(exc)
            except Exception:
                message = "Action Docker impossible."
            return error_response("docker_unavailable", message, 503)

        response_payload = dict(result or {})
        response_payload.setdefault(
            "ok",
            200 <= int(status) < 300 and response_payload.get("status") != "error",
        )
        response_payload["api_version"] = API_VERSION
        return jsonify(response_payload), int(status)

    @api_bp.post("/vm/actions")
    def vm_actions():
        if not request.is_json:
            return error_response(
                "json_required",
                "Le corps de la requête doit être au format JSON.",
                415,
            )
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return error_response("invalid_json", "Corps JSON invalide.", 400)

        action = _clean_text(payload.get("action"), 64).lower()
        vm_name = _clean_text(payload.get("name") or payload.get("vm_name"), 160)
        if action not in ALLOWED_VM_ACTIONS:
            return error_response(
                "action_not_allowed",
                "Action VM inconnue ou non autorisée par l'API.",
                400,
            )
        if not vm_name:
            return error_response(
                "vm_required",
                "Le nom de la machine virtuelle est obligatoire.",
                400,
            )

        try:
            vm_module = importlib.import_module("vm")
            result, status = vm_module.do_vm_action(vm_module.get_config(), vm_name, action)
        except Exception:
            return error_response(
                "vm_unavailable",
                "Action sur la machine virtuelle momentanément impossible.",
                503,
            )

        response_payload = dict(result or {})
        response_payload.setdefault(
            "ok",
            200 <= int(status) < 300 and response_payload.get("status") != "error",
        )
        response_payload["api_version"] = API_VERSION
        return jsonify(response_payload), int(status)

    @api_bp.post("/task/actions")
    def task_actions():
        if not request.is_json:
            return error_response(
                "json_required",
                "Le corps de la requête doit être au format JSON.",
                415,
            )
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return error_response("invalid_json", "Corps JSON invalide.", 400)

        action = _clean_text(payload.get("action"), 64).lower()
        try:
            task_id = int(payload.get("task_id") or payload.get("id") or 0)
        except (TypeError, ValueError):
            task_id = 0
        if action not in ALLOWED_TASK_ACTIONS:
            return error_response(
                "action_not_allowed",
                "Action de tâche inconnue ou non autorisée par l'API.",
                400,
            )
        if task_id <= 0:
            return error_response(
                "task_required",
                "L'identifiant de la tâche est obligatoire.",
                400,
            )

        try:
            task_module = importlib.import_module("task")
            if not task_module.get_task(task_id):
                return error_response("task_not_found", "Tâche introuvable.", 404)
            if action == "start":
                action_ok, message = task_module.run_task_background(task_id, "API Android")
            else:
                action_ok, message = task_module.force_stop_task(task_id, "API Android")
        except Exception:
            return error_response(
                "task_unavailable",
                "Action sur la tâche momentanément impossible.",
                503,
            )

        status = 200 if action_ok else 409
        clean_message = _clean_text(message, 500)
        return jsonify(
            {
                "ok": bool(action_ok),
                "api_version": API_VERSION,
                "message": clean_message,
                "task_action": {
                    "task_id": task_id,
                    "action": action,
                    "accepted": bool(action_ok),
                    "message": clean_message,
                },
            }
        ), status

    @api_bp.post("/backup/actions")
    def backup_actions():
        if not request.is_json:
            return error_response(
                "json_required",
                "Le corps de la requête doit être au format JSON.",
                415,
            )
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return error_response("invalid_json", "Corps JSON invalide.", 400)

        action = _clean_text(payload.get("action"), 64).lower()
        filename = _clean_text(payload.get("filename") or payload.get("name"), 255)
        if action not in ALLOWED_BACKUP_ACTIONS:
            return error_response(
                "action_not_allowed",
                "Action Backup inconnue ou non autorisée par l'API.",
                400,
            )
        if not filename:
            return error_response(
                "backup_required",
                "Le nom du script Backup est obligatoire.",
                400,
            )

        try:
            backup_module = importlib.import_module("backup")
            known_names = {
                str(item.get("name") or "")
                for item in backup_module.list_scripts() or []
                if isinstance(item, dict)
            }
            if filename not in known_names:
                return error_response("backup_not_found", "Script Backup introuvable.", 404)
            internal_result = (
                backup_module.run_script(filename)
                if action == "start"
                else backup_module.stop_script(filename)
            )
            internal_response = current_app.make_response(internal_result)
            internal_payload = internal_response.get_json(silent=True)
            if not isinstance(internal_payload, dict):
                raise RuntimeError("Réponse Backup non JSON")
            status_payload = internal_payload.get("status")
            if not isinstance(status_payload, dict):
                status_payload = {}
            clean_message = _clean_text(internal_payload.get("message"), 500)
            accepted = bool(internal_payload.get("ok", internal_response.status_code < 400))
            result = {
                "ok": accepted,
                "api_version": API_VERSION,
                "message": clean_message,
                "backup_action": {
                    "filename": filename,
                    "action": action,
                    "accepted": accepted,
                    "message": clean_message,
                    "status": {
                        "running": bool(status_payload.get("running")),
                        "result": _clean_text(status_payload.get("result"), 64),
                        "message": _clean_text(status_payload.get("message"), 500),
                        "started_at": _clean_text(status_payload.get("started_at"), 64),
                        "ended_at": _clean_text(status_payload.get("ended_at"), 64),
                    },
                },
            }
            return jsonify(result), int(internal_response.status_code)
        except Exception:
            return error_response(
                "backup_unavailable",
                "Action Backup momentanément impossible.",
                503,
            )

    @api_bp.post("/files/list")
    def files_list():
        if not request.is_json:
            return error_response("json_required", "Le corps doit être au format JSON.", 415)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return error_response("invalid_json", "Corps JSON invalide.", 400)
        requested_path = payload.get("path") or _file_roots()[0]
        try:
            path = _safe_file_path(requested_path)
            if not os.path.isdir(path):
                return error_response("directory_required", "Le chemin n'est pas un dossier.", 400)
            items = []
            with os.scandir(path) as entries:
                for entry in entries:
                    try:
                        items.append(_file_item(entry.path))
                    except OSError:
                        continue
            items.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
            truncated = len(items) > 2000
            if truncated:
                items = items[:2000]
            resolved = os.path.realpath(path)
            matching_root = next(
                (root for root in _file_roots() if _path_inside_root(resolved, root)),
                _file_roots()[0],
            )
            parent = os.path.dirname(path.rstrip(os.sep)) or matching_root
            if not _path_inside_root(os.path.realpath(parent), matching_root):
                parent = matching_root
            return ok_response(
                files={
                    "current": path,
                    "parent": parent,
                    "root": matching_root,
                    "roots": _file_roots(),
                    "items": items,
                    "truncated": truncated,
                }
            )
        except PermissionError:
            return error_response("path_forbidden", "Chemin hors des racines NAS autorisées.", 403)
        except FileNotFoundError:
            return error_response("path_not_found", "Dossier introuvable.", 404)
        except (OSError, ValueError):
            return error_response("files_unavailable", "Lecture du dossier impossible.", 503)

    @api_bp.post("/files/actions")
    def files_actions():
        if not request.is_json:
            return error_response("json_required", "Le corps doit être au format JSON.", 415)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return error_response("invalid_json", "Corps JSON invalide.", 400)
        action = _clean_text(payload.get("action"), 32).lower()
        if action not in ALLOWED_FILE_ACTIONS:
            return error_response("action_not_allowed", "Action fichier non autorisée.", 400)
        try:
            if action == "mkdir":
                directory = _safe_file_path(payload.get("directory") or payload.get("path"))
                if not os.path.isdir(directory):
                    raise ValueError("Dossier parent invalide")
                name = _safe_file_name(payload.get("name"))
                target = _safe_file_path(os.path.join(directory, name), must_exist=False)
                if os.path.exists(target):
                    return error_response("target_exists", "Un élément porte déjà ce nom.", 409)
                os.mkdir(target)
            elif action == "rename":
                source = _safe_file_path(payload.get("source"))
                if os.path.realpath(source) in _file_roots():
                    return error_response("root_protected", "Une racine NAS ne peut pas être renommée.", 403)
                name = _safe_file_name(payload.get("name"))
                target = _safe_file_path(os.path.join(os.path.dirname(source), name), must_exist=False)
                if os.path.exists(target):
                    return error_response("target_exists", "Un élément porte déjà ce nom.", 409)
                os.rename(source, target)
            elif action in {"copy", "move"}:
                source = _safe_file_path(payload.get("source"))
                destination = _safe_file_path(payload.get("destination") or payload.get("dest"))
                if not os.path.isdir(destination):
                    raise ValueError("Destination invalide")
                if os.path.realpath(source) in _file_roots():
                    return error_response("root_protected", "Une racine NAS ne peut pas être déplacée.", 403)
                if os.path.islink(source):
                    return error_response("symlink_forbidden", "Les liens symboliques ne sont pas copiés par mobile.", 400)
                if action == "copy" and _tree_contains_symlink(source):
                    return error_response(
                        "symlink_forbidden",
                        "Le dossier contient un lien symbolique et ne peut pas être copié par mobile.",
                        400,
                    )
                target = _safe_file_path(
                    os.path.join(destination, os.path.basename(source.rstrip(os.sep))),
                    must_exist=False,
                )
                if os.path.exists(target):
                    return error_response("target_exists", "La destination existe déjà.", 409)
                if action == "move":
                    shutil.move(source, target)
                elif os.path.isdir(source):
                    shutil.copytree(source, target, symlinks=False)
                else:
                    shutil.copy2(source, target)
            else:
                source = _safe_file_path(payload.get("source"))
                if os.path.realpath(source) in _file_roots():
                    return error_response("root_protected", "Une racine NAS ne peut pas être supprimée.", 403)
                if os.path.isdir(source) and not os.path.islink(source):
                    shutil.rmtree(source)
                else:
                    os.remove(source)
            return ok_response(
                message="Opération fichier terminée.",
                file_action={"action": action},
            )
        except PermissionError:
            return error_response("path_forbidden", "Chemin hors des racines NAS autorisées.", 403)
        except FileNotFoundError:
            return error_response("path_not_found", "Élément introuvable.", 404)
        except ValueError as exc:
            return error_response("invalid_file_operation", _clean_text(exc, 200), 400)
        except OSError:
            return error_response("file_operation_failed", "Opération fichier impossible.", 500)

    @api_bp.post("/files/catalog")
    def files_catalog():
        if not request.is_json:
            return error_response("json_required", "Le corps doit être au format JSON.", 415)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return error_response("invalid_json", "Corps JSON invalide.", 400)
        try:
            root = _safe_file_path(payload.get("path"))
            if not os.path.isdir(root):
                return error_response("directory_required", "Le chemin n'est pas un dossier.", 400)
            maximum = _catalog_max_entries()
            entries = []
            skipped_symlinks = 0
            truncated = False
            for current, directories, files in os.walk(root, topdown=True, followlinks=False):
                safe_directories = []
                for name in sorted(directories, key=str.lower):
                    full = os.path.join(current, name)
                    if os.path.islink(full):
                        skipped_symlinks += 1
                        continue
                    relative = os.path.relpath(full, root).replace(os.sep, "/")
                    info = os.stat(full)
                    entries.append({
                        "relative_path": relative,
                        "is_dir": True,
                        "size": 0,
                        "mtime": int(info.st_mtime),
                        "sha256": "",
                    })
                    safe_directories.append(name)
                    if len(entries) >= maximum:
                        truncated = True
                        break
                directories[:] = safe_directories
                if truncated:
                    break
                for name in sorted(files, key=str.lower):
                    full = os.path.join(current, name)
                    if name.startswith(".yoleo-upload-") and name.endswith(".tmp"):
                        try:
                            if time.time() - os.path.getmtime(full) > 3600:
                                os.remove(full)
                        except OSError:
                            pass
                        continue
                    if os.path.islink(full):
                        skipped_symlinks += 1
                        continue
                    relative = os.path.relpath(full, root).replace(os.sep, "/")
                    info = os.stat(full)
                    entries.append({
                        "relative_path": relative,
                        "is_dir": False,
                        "size": int(info.st_size),
                        "mtime": int(info.st_mtime),
                        "sha256": _sha256_file(full),
                    })
                    if len(entries) >= maximum:
                        truncated = True
                        break
                if truncated:
                    break
            return ok_response(
                catalog={
                    "root": root,
                    "algorithm": "SHA-256",
                    "generated_at": _utc_iso(),
                    "entries": entries,
                    "truncated": truncated,
                    "skipped_symlinks": skipped_symlinks,
                }
            )
        except PermissionError:
            return error_response("path_forbidden", "Chemin hors des racines NAS autorisées.", 403)
        except FileNotFoundError:
            return error_response("path_not_found", "Dossier introuvable.", 404)
        except (OSError, ValueError):
            return error_response("catalog_unavailable", "Création du catalogue SHA-256 impossible.", 503)

    @api_bp.post("/files/upload")
    def files_upload():
        temporary = ""
        try:
            directory = _safe_file_path(request.form.get("path"))
            if not os.path.isdir(directory):
                raise ValueError("Dossier de destination invalide")
            uploaded = request.files.get("file")
            if uploaded is None or not uploaded.filename:
                return error_response("file_required", "Aucun fichier reçu.", 400)
            filename = _safe_file_name(os.path.basename(uploaded.filename.replace("\\", "/")))
            target = _safe_file_path(os.path.join(directory, filename), must_exist=False)
            overwrite = str(request.form.get("overwrite", "false")).lower() in {"1", "true", "yes"}
            if os.path.exists(target) and not overwrite:
                return error_response("target_exists", "Le fichier existe déjà.", 409)
            if os.path.islink(target):
                return error_response("symlink_forbidden", "Un lien symbolique ne peut pas être remplacé.", 400)
            if os.path.isdir(target):
                return error_response("target_exists", "Un dossier porte déjà ce nom.", 409)
            expected_sha256 = _clean_text(request.form.get("sha256"), 64).lower()
            if expected_sha256 and (
                len(expected_sha256) != 64 or
                any(character not in "0123456789abcdef" for character in expected_sha256)
            ):
                return error_response("invalid_sha256", "Empreinte SHA-256 invalide.", 400)
            temporary = _safe_file_path(
                os.path.join(directory, ".yoleo-upload-" + secrets.token_hex(12) + ".tmp"),
                must_exist=False,
            )
            uploaded.save(temporary)
            actual_sha256 = _sha256_file(temporary)
            if expected_sha256 and actual_sha256 != expected_sha256:
                os.remove(temporary)
                temporary = ""
                return error_response("sha256_mismatch", "Le fichier reçu ne correspond pas au SHA-256 annoncé.", 422)
            os.replace(temporary, target)
            temporary = ""
            return ok_response(
                message="Fichier envoyé.",
                upload={
                    "name": filename,
                    "path": target,
                    "size": os.path.getsize(target),
                    "sha256": actual_sha256,
                },
            )
        except PermissionError:
            return error_response("path_forbidden", "Chemin hors des racines NAS autorisées.", 403)
        except FileNotFoundError:
            return error_response("path_not_found", "Dossier de destination introuvable.", 404)
        except ValueError as exc:
            return error_response("invalid_file_operation", _clean_text(exc, 200), 400)
        except OSError:
            return error_response("file_operation_failed", "Envoi du fichier impossible.", 500)
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.remove(temporary)
                except OSError:
                    pass

    @api_bp.get("/files/download")
    def files_download():
        temporary_archive = ""
        try:
            path = _safe_file_path(request.args.get("path"))
            archive_requested = str(request.args.get("archive") or "").strip().lower() == "zip"
            if os.path.islink(path):
                return error_response("symlink_forbidden", "Un lien symbolique ne peut pas être téléchargé.", 400)
            if os.path.isdir(path):
                if not archive_requested:
                    return error_response(
                        "archive_required",
                        "Un dossier doit être demandé sous forme d'archive ZIP.",
                        400,
                    )
                descriptor, temporary_archive = tempfile.mkstemp(prefix="yoleo-download-", suffix=".zip")
                os.close(descriptor)
                _zip_directory(path, temporary_archive)
                response = send_file(
                    temporary_archive,
                    mimetype="application/zip",
                    as_attachment=True,
                    download_name=os.path.basename(path.rstrip(os.sep)) + ".zip",
                    conditional=False,
                    max_age=0,
                )
                os.remove(temporary_archive)
                temporary_archive = ""
                return response
            if not os.path.isfile(path):
                return error_response("file_required", "Le chemin n'est pas un fichier.", 400)
            mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
            return send_file(
                path,
                mimetype=mime_type,
                as_attachment=True,
                download_name=os.path.basename(path),
                conditional=True,
            )
        except PermissionError:
            return error_response("path_forbidden", "Chemin hors des racines NAS autorisées.", 403)
        except FileNotFoundError:
            return error_response("path_not_found", "Fichier introuvable.", 404)
        except (OSError, ValueError):
            return error_response("files_unavailable", "Téléchargement impossible.", 503)
        finally:
            if temporary_archive and os.path.exists(temporary_archive):
                try:
                    os.remove(temporary_archive)
                except OSError:
                    pass

    @api_bp.route("/<path:unknown_path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def api_not_found(unknown_path: str):
        return error_response("not_found", "Route API inconnue.", 404)

    return api_bp
