from __future__ import annotations

import datetime as _confbackup_datetime
import hashlib as _confbackup_hashlib
import tarfile as _confbackup_tarfile
import tempfile as _confbackup_tempfile
import threading as _confbackup_threading
from pathlib import Path as _ConfBackupPath


CONFBACKUP_SYSTEM_DIR = _ConfBackupPath(NAS_CONF_DIR).resolve()
CONFBACKUP_INIT_DIR = _ConfBackupPath(os.environ.get("YOLEO_INIT_DIR", nas_root_path("init"))).resolve()
CONFBACKUP_BACKUPS_DIR = CONFBACKUP_INIT_DIR / "conf"
CONFBACKUP_MANIFEST = CONFBACKUP_BACKUPS_DIR / "conf.sha256"
CONFBACKUP_REFERENCE_ARCHIVE = CONFBACKUP_BACKUPS_DIR / "conf.tar.gz"
CONFBACKUP_REFERENCE_ARCHIVE_SHA = CONFBACKUP_BACKUPS_DIR / "conf.tar.gz.sha256"
CONFBACKUP_SERVICE_NAME = os.environ.get("YOLEO_FLASK_SERVICE", "flask-system.service")

CONFBACKUP_EXCLUDED_DIRS = {".git", "__pycache__", ".pytest_cache", "uibackup", "uibackupconf"}
CONFBACKUP_EXCLUDED_FILES = {".DS_Store", "gunicorn.ctl"}
CONFBACKUP_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}

_confbackup_job_lock = _confbackup_threading.Lock()
_confbackup_latest_job_id = ""


def _confbackup_now_label() -> str:
    return _confbackup_datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _confbackup_status_dir() -> _ConfBackupPath:
    preferred = _ConfBackupPath(os.environ.get("YOLEO_UIBACKUPCONF_STATUS_DIR", "/run/yoleo/uibackupconf"))
    for candidate in (preferred, _ConfBackupPath(NAS_CONF_DIR) / "uibackupconf"):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            continue
    fallback = _ConfBackupPath(_confbackup_tempfile.gettempdir()) / "yoleo-uibackupconf"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _confbackup_job_path(job_id: str) -> _ConfBackupPath:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(job_id or "")).strip("._-") or "latest"
    return _confbackup_status_dir() / f"{clean}.json"


def _confbackup_latest_path() -> _ConfBackupPath:
    return _confbackup_status_dir() / "latest.json"


def _confbackup_write_json_atomic(path: _ConfBackupPath, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _confbackup_read_json(path: _ConfBackupPath) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _confbackup_save_job(job: Dict[str, Any]) -> Dict[str, Any]:
    job = dict(job)
    job["updated_at"] = _confbackup_now_label()
    _confbackup_write_json_atomic(_confbackup_job_path(job.get("id", "")), job)
    _confbackup_write_json_atomic(_confbackup_latest_path(), job)
    return job


def _confbackup_update_job(job: Dict[str, Any], **updates: Any) -> Dict[str, Any]:
    job.update(updates)
    if job.get("total"):
        done = max(0, int(job.get("done") or 0))
        total = max(1, int(job.get("total") or 1))
        job["percent"] = max(0, min(100, int(done * 100 / total)))
    return _confbackup_save_job(job)


def _confbackup_file_sha256(path: _ConfBackupPath) -> str:
    digest = _confbackup_hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _confbackup_size_label(size: int) -> str:
    value = float(size or 0)
    units = ["o", "Ko", "Mo", "Go", "To"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "o":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}".replace(".0 ", " ")
        value /= 1024
    return f"{int(size or 0)} o"


def _confbackup_safe_rel_from_member(name: str) -> str:
    rel = str(name or "").replace("\\", "/").lstrip("/")
    while rel.startswith("./"):
        rel = rel[2:]
    parts = [part for part in rel.split("/") if part]
    if not parts or any(part in {"..", "."} for part in parts):
        raise ValueError(f"Chemin archive refuse : {name}")
    return "/".join(parts)


def _confbackup_rel_for(path: _ConfBackupPath) -> str:
    return path.resolve().relative_to(CONFBACKUP_SYSTEM_DIR).as_posix()


def _confbackup_wanted_file(path: _ConfBackupPath) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    try:
        rel = path.resolve().relative_to(CONFBACKUP_SYSTEM_DIR)
    except Exception:
        return False
    if any(part in CONFBACKUP_EXCLUDED_DIRS for part in rel.parts):
        return False
    if path.name in CONFBACKUP_EXCLUDED_FILES:
        return False
    if path.suffix in CONFBACKUP_EXCLUDED_SUFFIXES:
        return False
    return True


def _confbackup_system_files() -> List[_ConfBackupPath]:
    if not CONFBACKUP_SYSTEM_DIR.is_dir():
        return []
    return sorted(
        (path for path in CONFBACKUP_SYSTEM_DIR.rglob("*") if _confbackup_wanted_file(path)),
        key=lambda item: _confbackup_rel_for(item),
    )


def _confbackup_archive_sha_ok(path: _ConfBackupPath, verify_content: bool = True) -> Tuple[bool, str]:
    sha_path = path.with_name(path.name + ".sha256")
    if not sha_path.exists():
        return False, ""
    expected = ""
    try:
        expected = sha_path.read_text(encoding="utf-8", errors="replace").split()[0].strip()
    except Exception:
        return False, ""
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        return False, ""
    expected = expected.lower()
    if not verify_content:
        # La liste lit le SHA enregistré sans recalculer toute l'archive.
        # Une restauration effectue toujours la vérification complète.
        return True, expected
    try:
        actual = _confbackup_file_sha256(path)
    except Exception:
        return False, expected
    return actual == expected, actual


def _confbackup_validate_archive(path: _ConfBackupPath) -> None:
    ok, _sha = _confbackup_archive_sha_ok(path)
    sha_path = path.with_name(path.name + ".sha256")
    if sha_path.exists() and not ok:
        raise RuntimeError(f"SHA-256 invalide pour {path.name}")
    if not path.exists() or not path.is_file():
        raise RuntimeError("Archive introuvable")
    try:
        with _confbackup_tarfile.open(path, "r:gz") as archive:
            archive.getmembers()
    except Exception as exc:
        raise RuntimeError(f"Archive illisible : {exc}") from exc


def _confbackup_backup_path(name: str) -> _ConfBackupPath:
    clean = os.path.basename(str(name or "").strip())
    if not re.match(r"^conf-\d{8}-\d{6}\.tar\.gz$", clean):
        raise ValueError("Nom de backup conf invalide")
    path = (CONFBACKUP_BACKUPS_DIR / clean).resolve()
    if path.parent != CONFBACKUP_BACKUPS_DIR.resolve():
        raise ValueError("Chemin backup refuse")
    return path


def _confbackup_backup_rows() -> List[Dict[str, Any]]:
    CONFBACKUP_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, Any]] = []
    for path in sorted(CONFBACKUP_BACKUPS_DIR.glob("conf-*.tar.gz"), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.name.startswith("."):
            continue
        stat = path.stat()
        sha_ok, sha = _confbackup_archive_sha_ok(path, verify_content=False)
        created = _confbackup_datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        match = re.match(r"conf-(\d{8})-(\d{6})\.tar\.gz$", path.name)
        if match:
            try:
                created = _confbackup_datetime.datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        rows.append({
            "name": path.name,
            "created_at": created,
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            "size_label": _confbackup_size_label(stat.st_size),
            "sha256": sha,
            "sha_short": sha[:12] if sha else "",
            "sha_ok": bool(sha_ok),
            "sha_file": path.with_name(path.name + ".sha256").exists(),
        })
    return rows


def _confbackup_reference_state() -> Dict[str, Any]:
    files = _confbackup_system_files()
    state = {
        "system_files": len(files),
        "system_size": sum(path.stat().st_size for path in files),
        "system_size_label": _confbackup_size_label(sum(path.stat().st_size for path in files)),
        "manifest_exists": CONFBACKUP_MANIFEST.exists(),
        "archive_exists": CONFBACKUP_REFERENCE_ARCHIVE.exists(),
        "archive_sha_exists": CONFBACKUP_REFERENCE_ARCHIVE_SHA.exists(),
        "archive_sha_ok": False,
        "archive_size_label": "0 o",
    }
    if CONFBACKUP_REFERENCE_ARCHIVE.exists():
        state["archive_size_label"] = _confbackup_size_label(CONFBACKUP_REFERENCE_ARCHIVE.stat().st_size)
        state["archive_sha_ok"] = _confbackup_archive_sha_ok(CONFBACKUP_REFERENCE_ARCHIVE, verify_content=False)[0]
    return state


def _confbackup_write_archive(path: _ConfBackupPath, files: List[_ConfBackupPath], job: Dict[str, Any], base: int, span: int) -> None:
    total = max(1, sum(item.stat().st_size for item in files))
    done = 0
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with _confbackup_tarfile.open(tmp, "w:gz") as archive:
            for index, file_path in enumerate(files, 1):
                rel = _confbackup_rel_for(file_path)
                archive.add(file_path, arcname=rel, recursive=False)
                done += file_path.stat().st_size
                if index == len(files) or index % 10 == 0:
                    percent = base + int(span * done / total)
                    _confbackup_update_job(
                        job,
                        phase="archive",
                        message=f"Archive en cours : {index}/{len(files)} fichiers",
                        done=percent,
                        total=100,
                    )
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def _confbackup_write_reference_archive(job: Dict[str, Any]) -> None:
    CONFBACKUP_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    files = _confbackup_system_files()
    records = []
    total = max(1, len(files))
    for index, file_path in enumerate(files, 1):
        stat = file_path.stat()
        records.append(f"{_confbackup_file_sha256(file_path)}\t{stat.st_size}\t{_confbackup_rel_for(file_path)}")
        if index == total or index % 25 == 0:
            _confbackup_update_job(
                job,
                phase="catalog",
                message=f"Catalogue SHA-256 : {index}/{total} fichiers",
                done=50 + int(20 * index / total),
                total=100,
            )

    tmp_manifest = CONFBACKUP_MANIFEST.with_name(f".{CONFBACKUP_MANIFEST.name}.{uuid.uuid4().hex}.tmp")
    tmp_manifest.write_text(
        "# YoLeo conf integrity catalog v1\n"
        "# Format: sha256<TAB>size_bytes<TAB>relative_path\n"
        "# Archive: conf.tar.gz\n"
        + "\n".join(records).rstrip()
        + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_manifest, CONFBACKUP_MANIFEST)
    _confbackup_write_archive(CONFBACKUP_REFERENCE_ARCHIVE, files, job, 70, 25)
    archive_sha = _confbackup_file_sha256(CONFBACKUP_REFERENCE_ARCHIVE)
    CONFBACKUP_REFERENCE_ARCHIVE_SHA.write_text(
        f"{archive_sha}\t{CONFBACKUP_REFERENCE_ARCHIVE.stat().st_size}\t{CONFBACKUP_REFERENCE_ARCHIVE.name}\n",
        encoding="utf-8",
    )
    _confbackup_update_job(job, phase="reference", message="Reference conf reconstruite", done=96, total=100)


def _confbackup_create_backup(job: Dict[str, Any]) -> None:
    CONFBACKUP_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _confbackup_datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_path = CONFBACKUP_BACKUPS_DIR / f"conf-{stamp}.tar.gz"
    files = _confbackup_system_files()
    _confbackup_update_job(job, backup=archive_path.name, total=100, done=4, phase="scan", message=f"{len(files)} fichiers detectes")
    _confbackup_write_archive(archive_path, files, job, 8, 86)
    sha = _confbackup_file_sha256(archive_path)
    archive_path.with_name(archive_path.name + ".sha256").write_text(
        f"{sha}\t{archive_path.stat().st_size}\t{archive_path.name}\n",
        encoding="utf-8",
    )
    _confbackup_update_job(
        job,
        running=False,
        ok=True,
        done=100,
        total=100,
        percent=100,
        phase="done",
        message=f"Backup cree : {archive_path.name}",
        ended_at=_confbackup_now_label(),
    )


def _confbackup_extract_archive_to_tmp(archive_path: _ConfBackupPath, tmp_dir: _ConfBackupPath) -> Dict[str, _ConfBackupPath]:
    extracted: Dict[str, _ConfBackupPath] = {}
    with _confbackup_tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            rel = _confbackup_safe_rel_from_member(member.name)
            if any(part in CONFBACKUP_EXCLUDED_DIRS for part in rel.split("/")):
                continue
            target = tmp_dir / rel
            resolved = target.resolve()
            if not str(resolved).startswith(str(tmp_dir.resolve()) + os.sep):
                raise RuntimeError(f"Chemin archive refuse : {member.name}")
            if member.isdir():
                resolved.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise RuntimeError(f"Type archive refuse : {member.name}")
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"Fichier archive illisible : {member.name}")
            resolved.parent.mkdir(parents=True, exist_ok=True)
            with source, resolved.open("wb") as output:
                shutil.copyfileobj(source, output)
            try:
                os.chmod(resolved, member.mode & 0o777)
            except Exception:
                pass
            extracted[rel] = resolved
    return extracted


def _confbackup_replace_file(source: _ConfBackupPath, destination: _ConfBackupPath) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    shutil.copy2(source, tmp)
    os.replace(tmp, destination)


def _confbackup_restore_backup(job: Dict[str, Any], backup_name: str) -> None:
    archive_path = _confbackup_backup_path(backup_name)
    _confbackup_update_job(job, backup=archive_path.name, total=100, done=3, phase="verify", message="Verification SHA-256")
    _confbackup_validate_archive(archive_path)
    with _confbackup_tempfile.TemporaryDirectory(prefix="yoleo-confbackup-") as tmp_name:
        tmp_dir = _ConfBackupPath(tmp_name).resolve()
        _confbackup_update_job(job, done=10, total=100, phase="extract", message="Extraction temporaire")
        extracted = _confbackup_extract_archive_to_tmp(archive_path, tmp_dir)
        if not extracted:
            raise RuntimeError("Archive vide")

        wanted = set(extracted)
        active_files = _confbackup_system_files()
        removed = 0
        for index, active in enumerate(active_files, 1):
            rel = _confbackup_rel_for(active)
            if rel not in wanted:
                try:
                    active.unlink()
                    removed += 1
                except FileNotFoundError:
                    pass
            if index == len(active_files) or index % 25 == 0:
                _confbackup_update_job(job, done=15 + int(15 * index / max(1, len(active_files))), total=100, phase="cleanup", message=f"Nettoyage : {removed} fichiers hors backup")

        total = max(1, len(extracted))
        for index, rel in enumerate(sorted(extracted), 1):
            _confbackup_replace_file(extracted[rel], CONFBACKUP_SYSTEM_DIR / rel)
            if index == total or index % 20 == 0:
                _confbackup_update_job(job, done=30 + int(20 * index / total), total=100, phase="restore", message=f"Restauration : {index}/{total} fichiers")

    _confbackup_write_reference_archive(job)
    _confbackup_update_job(
        job,
        running=False,
        ok=True,
        restart_requested=True,
        done=100,
        total=100,
        percent=100,
        phase="restart",
        message="Backup /conf restaure, redemarrage Flask demande",
        ended_at=_confbackup_now_label(),
    )
    _confbackup_restart_flask_later(job.get("id", ""))


def _confbackup_delete_backup(job: Dict[str, Any], backup_name: str) -> None:
    archive_path = _confbackup_backup_path(backup_name)
    _confbackup_update_job(job, backup=archive_path.name, total=100, done=25, phase="delete", message="Suppression archive")
    removed = []
    for path in (archive_path, archive_path.with_name(archive_path.name + ".sha256")):
        if path.exists():
            path.unlink()
            removed.append(path.name)
    if not removed:
        raise RuntimeError("Backup introuvable")
    _confbackup_update_job(
        job,
        running=False,
        ok=True,
        done=100,
        total=100,
        percent=100,
        phase="done",
        message="Backup supprime : " + ", ".join(removed),
        ended_at=_confbackup_now_label(),
    )


def _confbackup_restart_flask_later(job_id: str) -> None:
    def runner() -> None:
        time.sleep(3.0)
        try:
            subprocess.Popen(["systemctl", "restart", CONFBACKUP_SERVICE_NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            job = _confbackup_read_json(_confbackup_job_path(job_id))
            if job:
                _confbackup_update_job(job, restart_error=str(exc), message=f"Restaure, mais restart impossible : {exc}")

    _confbackup_threading.Thread(target=runner, daemon=True).start()


def _confbackup_run_job(job: Dict[str, Any]) -> None:
    try:
        action = job.get("action")
        if action == "create":
            _confbackup_create_backup(job)
        elif action == "restore":
            _confbackup_restore_backup(job, str(job.get("backup") or ""))
        elif action == "delete":
            _confbackup_delete_backup(job, str(job.get("backup") or ""))
        else:
            raise RuntimeError("Action inconnue")
    except Exception as exc:
        _confbackup_update_job(
            job,
            running=False,
            ok=False,
            phase="error",
            message=str(exc),
            error=str(exc),
            ended_at=_confbackup_now_label(),
        )


def _confbackup_latest_job() -> Dict[str, Any]:
    return _confbackup_read_json(_confbackup_latest_path())


def _confbackup_start_job(action: str, backup_name: str = "") -> Dict[str, Any]:
    global _confbackup_latest_job_id
    latest = _confbackup_latest_job()
    if latest.get("running"):
        raise RuntimeError("Une action backup est deja en cours")
    job_id = _confbackup_datetime.datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
    job = {
        "id": job_id,
        "action": action,
        "backup": backup_name,
        "running": True,
        "ok": False,
        "restart_requested": False,
        "percent": 0,
        "done": 0,
        "total": 100,
        "phase": "queued",
        "message": "Action en attente",
        "started_at": _confbackup_now_label(),
        "ended_at": "",
    }
    with _confbackup_job_lock:
        _confbackup_latest_job_id = job_id
        _confbackup_save_job(job)
        _confbackup_threading.Thread(target=_confbackup_run_job, args=(job,), daemon=True).start()
    return job


def _confbackup_context() -> Dict[str, Any]:
    backups = _confbackup_backup_rows()
    ref = _confbackup_reference_state()
    total_size = sum(int(item.get("size") or 0) for item in backups)
    conf_files = int(ref.get("system_files") or 0)
    source_exists = CONFBACKUP_SYSTEM_DIR.is_dir()
    return {
        "backups": backups,
        "latest_job": _confbackup_latest_job(),
        "reference": ref,
        "source": {
            "source_exists": source_exists,
            "files": conf_files,
            "size_label": ref.get("system_size_label", "0 o"),
        },
        "stats": {
            "count": len(backups),
            "verified": sum(1 for item in backups if item.get("sha_ok")),
            "total_size": total_size,
            "total_size_label": _confbackup_size_label(total_size),
            "system_files": conf_files,
            "conf_files": conf_files,
        },
        "init_dir": str(CONFBACKUP_INIT_DIR),
        "backups_dir": str(CONFBACKUP_BACKUPS_DIR),
        "system_dir": str(CONFBACKUP_SYSTEM_DIR),
        "conf_dir": str(CONFBACKUP_SYSTEM_DIR),
        "source_dir": str(CONFBACKUP_SYSTEM_DIR),
    }


@system_bp.route("/system/uibackupconf")
def system_uibackupconf_route():
    return render_template("system_uibackupconf.html", **_confbackup_context())


@system_bp.route("/system/api/uibackupconf/backups")
def system_confbackup_backups_api():
    context = _confbackup_context()
    return jsonify({
        "ok": True,
        "backups": context["backups"],
        "stats": context["stats"],
        "reference": context["reference"],
        "latest_job": context["latest_job"],
    })


@system_bp.route("/system/api/uibackupconf/job/<job_id>")
def system_confbackup_job_api(job_id: str):
    job = _confbackup_read_json(_confbackup_job_path(job_id))
    if not job:
        return jsonify({"ok": False, "message": "Job introuvable"}), 404
    return jsonify({"ok": True, "job": job})


@system_bp.route("/system/api/uibackupconf/latest")
def system_confbackup_latest_api():
    return jsonify({"ok": True, "job": _confbackup_latest_job()})


@system_bp.route("/system/api/uibackupconf/action", methods=["POST"])
def system_confbackup_action_api():
    payload = request.get_json(silent=True) or request.form or {}
    action = str(payload.get("action") or "").strip().lower()
    backup_name = str(payload.get("backup") or payload.get("name") or "").strip()
    if action not in {"create", "restore", "delete"}:
        return jsonify({"ok": False, "message": "Action inconnue"}), 400
    if action in {"restore", "delete"} and not backup_name:
        return jsonify({"ok": False, "message": "Backup requis"}), 400
    try:
        if action in {"restore", "delete"}:
            _confbackup_backup_path(backup_name)
        job = _confbackup_start_job(action, backup_name)
        return jsonify({"ok": True, "job": job})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
