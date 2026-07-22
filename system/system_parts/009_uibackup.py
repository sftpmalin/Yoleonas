from __future__ import annotations

import datetime as _uibackup_datetime
import hashlib as _uibackup_hashlib
import tarfile as _uibackup_tarfile
import tempfile as _uibackup_tempfile
import threading as _uibackup_threading
from pathlib import Path as _UiBackupPath


UIBACKUP_SYSTEM_DIR = _UiBackupPath(_NAS_MODULE_DIR).resolve()
UIBACKUP_ROOT_DIR = _UiBackupPath(
    os.environ.get("YOLEO_INTEGRITY_ROOT_DIR")
    or os.environ.get("INTEGRITY_ROOT_DIR")
    or nas_root_path()
).resolve()
UIBACKUP_INCLUDE_DIRS = tuple(
    part.strip().strip("/")
    for part in (
        os.environ.get("YOLEO_INTEGRITY_INCLUDE_DIRS")
        or os.environ.get("INTEGRITY_INCLUDE_DIRS")
        or "system scripts offline bin"
    ).split()
    if part.strip().strip("/")
)
UIBACKUP_INCLUDE_NAMES = set(UIBACKUP_INCLUDE_DIRS)
UIBACKUP_INIT_DIR = _UiBackupPath(os.environ.get("YOLEO_INIT_DIR", nas_root_path("init"))).resolve()
UIBACKUP_BACKUPS_DIR = UIBACKUP_INIT_DIR / "backups"
UIBACKUP_MANIFEST = UIBACKUP_INIT_DIR / "system.sha256"
UIBACKUP_REFERENCE_ARCHIVE = UIBACKUP_INIT_DIR / "system.tar.gz"
UIBACKUP_REFERENCE_ARCHIVE_SHA = UIBACKUP_INIT_DIR / "system.tar.gz.sha256"
UIBACKUP_SERVICE_NAME = os.environ.get("YOLEO_FLASK_SERVICE", "flask-system.service")

UIBACKUP_EXCLUDED_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules", "backups"}
UIBACKUP_EXCLUDED_FILES = {".DS_Store", "gunicorn.ctl"}
UIBACKUP_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}

_uibackup_job_lock = _uibackup_threading.Lock()
_uibackup_latest_job_id = ""


def _uibackup_now_label() -> str:
    return _uibackup_datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _uibackup_status_dir() -> _UiBackupPath:
    preferred = _UiBackupPath(os.environ.get("YOLEO_UIBACKUP_STATUS_DIR", "/run/yoleo/uibackup"))
    for candidate in (preferred, _UiBackupPath(NAS_CONF_DIR) / "uibackup"):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            continue
    fallback = _UiBackupPath(_uibackup_tempfile.gettempdir()) / "yoleo-uibackup"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _uibackup_job_path(job_id: str) -> _UiBackupPath:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(job_id or "")).strip("._-") or "latest"
    return _uibackup_status_dir() / f"{clean}.json"


def _uibackup_latest_path() -> _UiBackupPath:
    return _uibackup_status_dir() / "latest.json"


def _uibackup_write_json_atomic(path: _UiBackupPath, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _uibackup_read_json(path: _UiBackupPath) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _uibackup_save_job(job: Dict[str, Any]) -> Dict[str, Any]:
    job = dict(job)
    job["updated_at"] = _uibackup_now_label()
    _uibackup_write_json_atomic(_uibackup_job_path(job.get("id", "")), job)
    _uibackup_write_json_atomic(_uibackup_latest_path(), job)
    return job


def _uibackup_update_job(job: Dict[str, Any], **updates: Any) -> Dict[str, Any]:
    job.update(updates)
    if job.get("total"):
        done = max(0, int(job.get("done") or 0))
        total = max(1, int(job.get("total") or 1))
        job["percent"] = max(0, min(100, int(done * 100 / total)))
    return _uibackup_save_job(job)


def _uibackup_file_sha256(path: _UiBackupPath) -> str:
    digest = _uibackup_hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _uibackup_size_label(size: int) -> str:
    value = float(size or 0)
    units = ["o", "Ko", "Mo", "Go", "To"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "o":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}".replace(".0 ", " ")
        value /= 1024
    return f"{int(size or 0)} o"


def _uibackup_safe_include_dir(name: str) -> bool:
    clean = str(name or "").replace("\\", "/").strip().strip("/")
    parts = [part for part in clean.split("/") if part]
    return bool(parts) and all(part not in {"", ".", ".."} for part in parts)


def _uibackup_validate_scope() -> None:
    if not UIBACKUP_INCLUDE_DIRS:
        raise RuntimeError("Aucun dossier protege configure")
    for include_dir in UIBACKUP_INCLUDE_DIRS:
        if not _uibackup_safe_include_dir(include_dir):
            raise RuntimeError(f"Dossier protege refuse : {include_dir}")


def _uibackup_safe_rel_from_member(name: str) -> Tuple[str, bool]:
    rel = str(name or "").replace("\\", "/").lstrip("/")
    while rel.startswith("./"):
        rel = rel[2:]
    parts = [part for part in rel.split("/") if part]
    if not parts or any(part in {"..", "."} for part in parts):
        raise ValueError(f"Chemin archive refuse : {name}")

    legacy = False
    if parts[0] not in UIBACKUP_INCLUDE_NAMES:
        # Anciennes archives UI : chemins relatifs a /system.
        # On les restaure sous system/... sans toucher scripts/offline/bin.
        parts = ["system", *parts]
        legacy = True

    if parts[0] not in UIBACKUP_INCLUDE_NAMES:
        raise ValueError(f"Chemin archive hors dossiers proteges : {name}")
    if any(part in UIBACKUP_EXCLUDED_DIRS for part in parts):
        raise ValueError(f"Chemin archive runtime refuse : {name}")
    if parts[-1] in UIBACKUP_EXCLUDED_FILES:
        raise ValueError(f"Fichier archive refuse : {name}")
    if _UiBackupPath(parts[-1]).suffix in UIBACKUP_EXCLUDED_SUFFIXES:
        raise ValueError(f"Fichier archive runtime refuse : {name}")
    return "/".join(parts), legacy


def _uibackup_rel_for(path: _UiBackupPath) -> str:
    return path.resolve().relative_to(UIBACKUP_ROOT_DIR).as_posix()


def _uibackup_rel_in_scope(rel: _UiBackupPath) -> bool:
    return bool(rel.parts) and rel.parts[0] in UIBACKUP_INCLUDE_NAMES


def _uibackup_wanted_file(path: _UiBackupPath) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    try:
        rel = path.resolve().relative_to(UIBACKUP_ROOT_DIR)
    except Exception:
        return False
    if not _uibackup_rel_in_scope(rel):
        return False
    if any(part in UIBACKUP_EXCLUDED_DIRS for part in rel.parts):
        return False
    if path.name in UIBACKUP_EXCLUDED_FILES:
        return False
    if path.suffix in UIBACKUP_EXCLUDED_SUFFIXES:
        return False
    return True


def _uibackup_system_files() -> List[_UiBackupPath]:
    _uibackup_validate_scope()
    files: List[_UiBackupPath] = []
    for include_dir in UIBACKUP_INCLUDE_DIRS:
        include_path = (UIBACKUP_ROOT_DIR / include_dir).resolve()
        try:
            include_path.relative_to(UIBACKUP_ROOT_DIR)
        except Exception:
            continue
        if not include_path.is_dir():
            continue
        files.extend(path for path in include_path.rglob("*") if _uibackup_wanted_file(path))
    return sorted(files, key=lambda item: _uibackup_rel_for(item))


def _uibackup_archive_sha_ok(path: _UiBackupPath, verify_content: bool = True) -> Tuple[bool, str]:
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
        # L'affichage ne doit pas relire plusieurs dizaines de Go à chaque page.
        # La restauration conserve, elle, la vérification complète ci-dessous.
        return True, expected
    try:
        actual = _uibackup_file_sha256(path)
    except Exception:
        return False, expected
    return actual == expected, actual


def _uibackup_validate_archive(path: _UiBackupPath) -> None:
    ok, _sha = _uibackup_archive_sha_ok(path)
    sha_path = path.with_name(path.name + ".sha256")
    if sha_path.exists() and not ok:
        raise RuntimeError(f"SHA-256 invalide pour {path.name}")
    if not path.exists() or not path.is_file():
        raise RuntimeError("Archive introuvable")
    try:
        with _uibackup_tarfile.open(path, "r:gz") as archive:
            archive.getmembers()
    except Exception as exc:
        raise RuntimeError(f"Archive illisible : {exc}") from exc


def _uibackup_backup_path(name: str) -> _UiBackupPath:
    clean = os.path.basename(str(name or "").strip())
    if not clean.endswith(".tar.gz") or clean.startswith("."):
        raise ValueError("Nom de backup invalide")
    path = (UIBACKUP_BACKUPS_DIR / clean).resolve()
    if path.parent != UIBACKUP_BACKUPS_DIR.resolve():
        raise ValueError("Chemin backup refuse")
    return path


def _uibackup_backup_rows() -> List[Dict[str, Any]]:
    UIBACKUP_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, Any]] = []
    for path in sorted(UIBACKUP_BACKUPS_DIR.glob("*.tar.gz"), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.name.startswith("."):
            continue
        stat = path.stat()
        sha_ok, sha = _uibackup_archive_sha_ok(path, verify_content=False)
        created = _uibackup_datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        match = re.match(r"system-(\d{8})-(\d{6})\.tar\.gz$", path.name)
        if match:
            try:
                created = _uibackup_datetime.datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        rows.append({
            "name": path.name,
            "created_at": created,
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            "size_label": _uibackup_size_label(stat.st_size),
            "sha256": sha,
            "sha_short": sha[:12] if sha else "",
            "sha_ok": bool(sha_ok),
            "sha_file": path.with_name(path.name + ".sha256").exists(),
        })
    return rows


def _uibackup_reference_state() -> Dict[str, Any]:
    files = _uibackup_system_files()
    state = {
        "system_files": len(files),
        "system_size": sum(path.stat().st_size for path in files),
        "system_size_label": _uibackup_size_label(sum(path.stat().st_size for path in files)),
        "root_dir": str(UIBACKUP_ROOT_DIR),
        "protected_dirs": list(UIBACKUP_INCLUDE_DIRS),
        "protected_dirs_label": ", ".join(UIBACKUP_INCLUDE_DIRS),
        "manifest_exists": UIBACKUP_MANIFEST.exists(),
        "archive_exists": UIBACKUP_REFERENCE_ARCHIVE.exists(),
        "archive_sha_exists": UIBACKUP_REFERENCE_ARCHIVE_SHA.exists(),
        "archive_sha_ok": False,
        "archive_size_label": "0 o",
    }
    if UIBACKUP_REFERENCE_ARCHIVE.exists():
        state["archive_size_label"] = _uibackup_size_label(UIBACKUP_REFERENCE_ARCHIVE.stat().st_size)
        state["archive_sha_ok"] = _uibackup_archive_sha_ok(UIBACKUP_REFERENCE_ARCHIVE, verify_content=False)[0]
    return state


def _uibackup_write_archive(path: _UiBackupPath, files: List[_UiBackupPath], job: Dict[str, Any], base: int, span: int) -> None:
    total = max(1, sum(item.stat().st_size for item in files))
    done = 0
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with _uibackup_tarfile.open(tmp, "w:gz") as archive:
            for index, file_path in enumerate(files, 1):
                rel = _uibackup_rel_for(file_path)
                archive.add(file_path, arcname=rel, recursive=False)
                done += file_path.stat().st_size
                if index == len(files) or index % 10 == 0:
                    percent = base + int(span * done / total)
                    _uibackup_update_job(
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


def _uibackup_write_reference_archive(job: Dict[str, Any]) -> None:
    UIBACKUP_INIT_DIR.mkdir(parents=True, exist_ok=True)
    files = _uibackup_system_files()
    records = []
    total = max(1, len(files))
    for index, file_path in enumerate(files, 1):
        stat = file_path.stat()
        records.append(f"{_uibackup_file_sha256(file_path)}\t{stat.st_size}\t{_uibackup_rel_for(file_path)}")
        if index == total or index % 25 == 0:
            _uibackup_update_job(
                job,
                phase="catalog",
                message=f"Catalogue SHA-256 : {index}/{total} fichiers",
                done=50 + int(20 * index / total),
                total=100,
            )

    tmp_manifest = UIBACKUP_MANIFEST.with_name(f".{UIBACKUP_MANIFEST.name}.{uuid.uuid4().hex}.tmp")
    tmp_manifest.write_text(
        "# YoLeo system integrity catalog v2\n"
        "# Format: sha256<TAB>size_bytes<TAB>relative_path\n"
        "# Racine: INTEGRITY_ROOT_DIR ; dossiers: system scripts offline bin\n"
        "# Archive: system.tar.gz\n"
        + "\n".join(records).rstrip()
        + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_manifest, UIBACKUP_MANIFEST)
    _uibackup_write_archive(UIBACKUP_REFERENCE_ARCHIVE, files, job, 70, 25)
    archive_sha = _uibackup_file_sha256(UIBACKUP_REFERENCE_ARCHIVE)
    UIBACKUP_REFERENCE_ARCHIVE_SHA.write_text(
        f"{archive_sha}\t{UIBACKUP_REFERENCE_ARCHIVE.stat().st_size}\t{UIBACKUP_REFERENCE_ARCHIVE.name}\n",
        encoding="utf-8",
    )
    _uibackup_update_job(job, phase="reference", message="Reference init reconstruite", done=96, total=100)


def _uibackup_create_backup(job: Dict[str, Any]) -> None:
    UIBACKUP_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _uibackup_datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_path = UIBACKUP_BACKUPS_DIR / f"system-{stamp}.tar.gz"
    files = _uibackup_system_files()
    _uibackup_update_job(job, backup=archive_path.name, total=100, done=4, phase="scan", message=f"{len(files)} fichiers detectes dans {', '.join(UIBACKUP_INCLUDE_DIRS)}")
    _uibackup_write_archive(archive_path, files, job, 8, 86)
    sha = _uibackup_file_sha256(archive_path)
    archive_path.with_name(archive_path.name + ".sha256").write_text(
        f"{sha}\t{archive_path.stat().st_size}\t{archive_path.name}\n",
        encoding="utf-8",
    )
    _uibackup_update_job(
        job,
        running=False,
        ok=True,
        done=100,
        total=100,
        percent=100,
        phase="done",
        message=f"Backup cree : {archive_path.name}",
        ended_at=_uibackup_now_label(),
    )


def _uibackup_extract_archive_to_tmp(archive_path: _UiBackupPath, tmp_dir: _UiBackupPath) -> Tuple[Dict[str, _UiBackupPath], bool]:
    extracted: Dict[str, _UiBackupPath] = {}
    legacy_archive = False
    tmp_root = tmp_dir.resolve()
    with _uibackup_tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            rel, legacy_member = _uibackup_safe_rel_from_member(member.name)
            legacy_archive = legacy_archive or legacy_member
            target = tmp_dir / rel
            resolved = target.resolve()
            if not str(resolved).startswith(str(tmp_root) + os.sep):
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
    return extracted, legacy_archive


def _uibackup_replace_file(source: _UiBackupPath, destination: _UiBackupPath) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    shutil.copy2(source, tmp)
    os.replace(tmp, destination)


def _uibackup_restore_backup(job: Dict[str, Any], backup_name: str) -> None:
    archive_path = _uibackup_backup_path(backup_name)
    _uibackup_update_job(job, backup=archive_path.name, total=100, done=3, phase="verify", message="Verification SHA-256")
    _uibackup_validate_archive(archive_path)
    with _uibackup_tempfile.TemporaryDirectory(prefix="yoleo-uibackup-") as tmp_name:
        tmp_dir = _UiBackupPath(tmp_name).resolve()
        _uibackup_update_job(job, done=10, total=100, phase="extract", message="Extraction temporaire")
        extracted, legacy_archive = _uibackup_extract_archive_to_tmp(archive_path, tmp_dir)
        if not extracted:
            raise RuntimeError("Archive vide")

        wanted = set(extracted)
        active_files = _uibackup_system_files()
        removed = 0
        for index, active in enumerate(active_files, 1):
            rel = _uibackup_rel_for(active)
            if legacy_archive and not rel.startswith("system/"):
                continue
            if rel not in wanted:
                try:
                    active.unlink()
                    removed += 1
                except FileNotFoundError:
                    pass
            if index == len(active_files) or index % 25 == 0:
                _uibackup_update_job(job, done=15 + int(15 * index / max(1, len(active_files))), total=100, phase="cleanup", message=f"Nettoyage : {removed} fichiers hors backup")

        total = max(1, len(extracted))
        for index, rel in enumerate(sorted(extracted), 1):
            _uibackup_replace_file(extracted[rel], UIBACKUP_ROOT_DIR / rel)
            if index == total or index % 20 == 0:
                _uibackup_update_job(job, done=30 + int(20 * index / total), total=100, phase="restore", message=f"Restauration : {index}/{total} fichiers" + (" (archive legacy system/)" if legacy_archive else ""))

    _uibackup_write_reference_archive(job)
    _uibackup_update_job(
        job,
        running=False,
        ok=True,
        restart_requested=True,
        done=100,
        total=100,
        percent=100,
        phase="restart",
        message="Backup restaure, redemarrage Flask demande",
        ended_at=_uibackup_now_label(),
    )
    _uibackup_restart_flask_later(job.get("id", ""))


def _uibackup_delete_backup(job: Dict[str, Any], backup_name: str) -> None:
    archive_path = _uibackup_backup_path(backup_name)
    _uibackup_update_job(job, backup=archive_path.name, total=100, done=25, phase="delete", message="Suppression archive")
    removed = []
    for path in (archive_path, archive_path.with_name(archive_path.name + ".sha256")):
        if path.exists():
            path.unlink()
            removed.append(path.name)
    if not removed:
        raise RuntimeError("Backup introuvable")
    _uibackup_update_job(
        job,
        running=False,
        ok=True,
        done=100,
        total=100,
        percent=100,
        phase="done",
        message="Backup supprime : " + ", ".join(removed),
        ended_at=_uibackup_now_label(),
    )


def _uibackup_restart_flask_later(job_id: str) -> None:
    def runner() -> None:
        time.sleep(3.0)
        try:
            subprocess.Popen(["systemctl", "restart", UIBACKUP_SERVICE_NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            job = _uibackup_read_json(_uibackup_job_path(job_id))
            if job:
                _uibackup_update_job(job, restart_error=str(exc), message=f"Restaure, mais restart impossible : {exc}")

    _uibackup_threading.Thread(target=runner, daemon=True).start()


def _uibackup_run_job(job: Dict[str, Any]) -> None:
    try:
        action = job.get("action")
        if action == "create":
            _uibackup_create_backup(job)
        elif action == "restore":
            _uibackup_restore_backup(job, str(job.get("backup") or ""))
        elif action == "delete":
            _uibackup_delete_backup(job, str(job.get("backup") or ""))
        else:
            raise RuntimeError("Action inconnue")
    except Exception as exc:
        _uibackup_update_job(
            job,
            running=False,
            ok=False,
            phase="error",
            message=str(exc),
            error=str(exc),
            ended_at=_uibackup_now_label(),
        )


def _uibackup_latest_job() -> Dict[str, Any]:
    return _uibackup_read_json(_uibackup_latest_path())


def _uibackup_start_job(action: str, backup_name: str = "") -> Dict[str, Any]:
    global _uibackup_latest_job_id
    latest = _uibackup_latest_job()
    if latest.get("running"):
        raise RuntimeError("Une action backup est deja en cours")
    job_id = _uibackup_datetime.datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
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
        "started_at": _uibackup_now_label(),
        "ended_at": "",
    }
    with _uibackup_job_lock:
        _uibackup_latest_job_id = job_id
        _uibackup_save_job(job)
        _uibackup_threading.Thread(target=_uibackup_run_job, args=(job,), daemon=True).start()
    return job


def _uibackup_context() -> Dict[str, Any]:
    backups = _uibackup_backup_rows()
    ref = _uibackup_reference_state()
    return {
        "backups": backups,
        "latest_job": _uibackup_latest_job(),
        "reference": ref,
        "stats": {
            "count": len(backups),
            "verified": sum(1 for item in backups if item.get("sha_ok")),
            "total_size": sum(int(item.get("size") or 0) for item in backups),
            "total_size_label": _uibackup_size_label(sum(int(item.get("size") or 0) for item in backups)),
            "system_files": ref.get("system_files", 0),
        },
        "init_dir": str(UIBACKUP_INIT_DIR),
        "backups_dir": str(UIBACKUP_BACKUPS_DIR),
        "system_dir": str(UIBACKUP_SYSTEM_DIR),
        "root_dir": str(UIBACKUP_ROOT_DIR),
        "protected_dirs": list(UIBACKUP_INCLUDE_DIRS),
        "protected_dirs_label": ", ".join(UIBACKUP_INCLUDE_DIRS),
    }


@system_bp.route("/system/uibackup")
def system_uibackup_route():
    return render_template("system_uibackup.html", **_uibackup_context())


@system_bp.route("/system/api/uibackup/backups")
def system_uibackup_backups_api():
    context = _uibackup_context()
    return jsonify({
        "ok": True,
        "backups": context["backups"],
        "stats": context["stats"],
        "reference": context["reference"],
        "latest_job": context["latest_job"],
    })


@system_bp.route("/system/api/uibackup/job/<job_id>")
def system_uibackup_job_api(job_id: str):
    job = _uibackup_read_json(_uibackup_job_path(job_id))
    if not job:
        return jsonify({"ok": False, "message": "Job introuvable"}), 404
    return jsonify({"ok": True, "job": job})


@system_bp.route("/system/api/uibackup/latest")
def system_uibackup_latest_api():
    return jsonify({"ok": True, "job": _uibackup_latest_job()})


@system_bp.route("/system/api/uibackup/action", methods=["POST"])
def system_uibackup_action_api():
    payload = request.get_json(silent=True) or request.form or {}
    action = str(payload.get("action") or "").strip().lower()
    backup_name = str(payload.get("backup") or payload.get("name") or "").strip()
    if action not in {"create", "restore", "delete"}:
        return jsonify({"ok": False, "message": "Action inconnue"}), 400
    if action in {"restore", "delete"} and not backup_name:
        return jsonify({"ok": False, "message": "Backup requis"}), 400
    try:
        if action in {"restore", "delete"}:
            _uibackup_backup_path(backup_name)
        job = _uibackup_start_job(action, backup_name)
        return jsonify({"ok": True, "job": job})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
