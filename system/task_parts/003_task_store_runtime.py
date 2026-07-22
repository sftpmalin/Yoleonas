def get_commands(task_id):
    with connect_db() as db:
        rows = db.execute(
            "SELECT position, command FROM task_commands WHERE task_id=? ORDER BY position",
            (task_id,),
        ).fetchall()
    commands = [""] * 5
    for row in rows:
        pos = safe_int(row["position"], 0)
        if 1 <= pos <= 5:
            commands[pos - 1] = row["command"] or ""
    return commands


def get_status(task_id):
    with connect_db() as db:
        row = db.execute("SELECT * FROM task_status WHERE task_id=?", (task_id,)).fetchone()
    if row:
        return row_to_dict(row)
    return {
        "task_id": task_id,
        "running": 0,
        "status": "Jamais lancé",
        "last_run": "—",
        "last_end": "—",
        "source": "—",
        "result": "—",
        "last_message": "",
        "process_pid": 0,
        "process_pgid": 0,
        "tmux_session": "",
        "systemd_unit": "",
        "lock_path": "",
        "stop_requested": 0,
        "updated_at": now_str(),
    }


def task_category(task):
    """Catégorie simple utilisée par le tableau HTML pour filtrer les tâches."""
    stype = task.get("schedule_type") or "manual"
    if stype in ("every_minutes", "every_hours"):
        return "multi_day"
    if stype == "week_days":
        return "weekly"
    if stype == "monthly":
        return "monthly"
    if stype == "yearly":
        return "yearly"
    if stype == "custom":
        return "custom"
    if stype == "daily":
        return "daily"
    return "manual"


def enrich_task(task):
    task["commands"] = get_commands(task["id"])
    task["status"] = get_status(task["id"])
    task["schedule_label"] = schedule_label(task)
    task["schedule_category"] = task_category(task)
    task["commands_count"] = len([c for c in task["commands"] if c.strip()])
    return task


def get_task(task_id, include_archived=False):
    with connect_db() as db:
        if include_archived:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        else:
            row = db.execute("SELECT * FROM tasks WHERE id=? AND archived=0", (task_id,)).fetchone()
    if not row:
        return None
    return enrich_task(row_to_dict(row))


def get_all_tasks():
    with connect_db() as db:
        rows = db.execute("SELECT * FROM tasks WHERE archived=0 ORDER BY id DESC").fetchall()
    tasks = []
    for row in rows:
        tasks.append(enrich_task(row_to_dict(row)))
    return tasks


def get_archived_tasks():
    with connect_db() as db:
        rows = db.execute("SELECT * FROM tasks WHERE archived=1 ORDER BY id DESC").fetchall()
    return [enrich_task(row_to_dict(row)) for row in rows]


def get_archived_task_count():
    with connect_db() as db:
        row = db.execute("SELECT COUNT(*) AS count FROM tasks WHERE archived=1").fetchone()
    return safe_int(row["count"] if row else 0, 0)


def set_status(task_id, **updates):
    fields = {
        "running": updates.get("running", 0),
        "status": updates.get("status", "Jamais lancé"),
        "last_run": updates.get("last_run", "—"),
        "last_end": updates.get("last_end", "—"),
        "source": updates.get("source", "—"),
        "result": updates.get("result", "—"),
        "last_message": updates.get("last_message", ""),
        "updated_at": now_str(),
    }
    existing = get_status(task_id)
    for key in ["last_run", "last_end", "source", "result", "last_message", "status", "running"]:
        if key not in updates:
            fields[key] = existing.get(key, fields[key])

    with connect_db() as db:
        db.execute(
            """
            INSERT INTO task_status(task_id, running, status, last_run, last_end, source, result, last_message, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                running=excluded.running,
                status=excluded.status,
                last_run=excluded.last_run,
                last_end=excluded.last_end,
                source=excluded.source,
                result=excluded.result,
                last_message=excluded.last_message,
                updated_at=excluded.updated_at
            """,
            (
                task_id,
                fields["running"],
                fields["status"],
                fields["last_run"],
                fields["last_end"],
                fields["source"],
                fields["result"],
                fields["last_message"],
                fields["updated_at"],
            ),
        )
        db.commit()


def set_task_runtime(task_id, pid=0, pgid=0, stop_requested=0, tmux_session=None, systemd_unit=None, lock_path=None):
    """Mémorise le runtime réel pour arrêter/nettoyer une tâche proprement."""
    current = get_status(task_id)
    session_value = current.get("tmux_session", "") if tmux_session is None else str(tmux_session or "")
    unit_value = current.get("systemd_unit", "") if systemd_unit is None else str(systemd_unit or "")
    lock_value = current.get("lock_path", "") if lock_path is None else str(lock_path or "")

    with connect_db() as db:
        db.execute(
            """
            INSERT INTO task_status(
                task_id, running, status, process_pid, process_pgid,
                tmux_session, systemd_unit, lock_path, stop_requested, updated_at
            )
            VALUES(?, 0, 'Jamais lancé', ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                process_pid=excluded.process_pid,
                process_pgid=excluded.process_pgid,
                tmux_session=excluded.tmux_session,
                systemd_unit=excluded.systemd_unit,
                lock_path=excluded.lock_path,
                stop_requested=excluded.stop_requested,
                updated_at=excluded.updated_at
            """,
            (
                task_id,
                safe_int(pid, 0),
                safe_int(pgid, 0),
                session_value,
                unit_value,
                lock_value,
                1 if safe_int(stop_requested, 0) else 0,
                now_str(),
            ),
        )
        db.commit()


def request_task_stop(task_id):
    """Marque une demande d'arrêt. Le worker tmux la relit entre deux commandes."""
    with connect_db() as db:
        db.execute(
            """
            INSERT INTO task_status(task_id, running, status, stop_requested, updated_at)
            VALUES(?, 0, 'Arrêt demandé', 1, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                stop_requested=1,
                updated_at=excluded.updated_at
            """,
            (task_id, now_str()),
        )
        db.commit()


def clear_task_runtime(task_id):
    with connect_db() as db:
        db.execute(
            """
            UPDATE task_status
            SET process_pid=0, process_pgid=0, tmux_session='', systemd_unit='', lock_path='',
                stop_requested=0, updated_at=?
            WHERE task_id=?
            """,
            (now_str(), task_id),
        )
        db.commit()


def is_stop_requested(task_id):
    return safe_int(get_status(task_id).get("stop_requested"), 0) == 1


def process_group_alive(pgid):
    pgid = safe_int(pgid, 0)
    if pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def process_alive(pid):
    pid = safe_int(pid, 0)
    if pid <= 1:
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


def slugify_task_name(value, fallback="task", max_len=64):
    """Nom court utilisable par tmux/systemd : lettres, chiffres, tirets, points."""
    text = str(value or fallback).strip().lower()
    text = re.sub(r"[^a-z0-9_.-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-._")
    if not text:
        text = fallback
    return text[:max_len].strip("-._") or fallback


def task_runtime_dir():
    conf = read_task_conf()
    runtime = Path(conf.get("TASK_RUNTIME_DIR") or DEFAULTS["TASK_RUNTIME_DIR"])
    try:
        runtime.mkdir(parents=True, exist_ok=True)
        return runtime
    except Exception:
        fallback = Path(conf.get("TASK_LOCK_FILE", DEFAULTS["TASK_LOCK_FILE"])).parent / "runtime"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def task_lock_path(task_id):
    return task_runtime_dir() / f"task_{safe_int(task_id, 0)}.lock"


def task_tmux_session_name(task_id, title=None):
    return f"yoleo-task-{safe_int(task_id, 0)}-{slugify_task_name(title, 'task')}"


def task_systemd_unit_name(task_id, title=None):
    # systemd accepte peu de caractères ; on garde volontairement un nom simple.
    return f"yoleo-task-{safe_int(task_id, 0)}-{int(time.time())}"


def command_exists(command_name):
    return shutil.which(str(command_name or "")) is not None


def tmux_session_alive(session_name):
    if not session_name:
        return False
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", str(session_name)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False
    except Exception:
        return False


def tmux_session_pane_pid(session_name):
    if not session_name:
        return 0
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", str(session_name), "#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return safe_int((result.stdout or "").strip(), 0)
    except Exception:
        pass
    return 0


def task_finished_after_launch(task_id, source=""):
    """Retourne le statut final si le worker a fini avant la vérification tmux."""
    status = get_status(task_id)
    if safe_int(status.get("running"), 0) == 1:
        return None
    result = str(status.get("result") or "").strip()
    state = str(status.get("status") or "").strip()
    message = str(status.get("last_message") or "").strip()
    if "ancien état en cours nettoy" in message.lower() or "runtime nettoy" in state.lower():
        return None
    if result in {"Succès", "Erreur", "Arrêté", "Ignoré"} or state:
        ok = result == "Succès" or state.startswith("✅")
        return ok, message or state or result or "Tâche terminée."
    return None


def kill_tmux_session(session_name):
    if not session_name:
        return False, "Aucune session tmux connue"
    if not tmux_session_alive(session_name):
        return False, f"Session tmux {session_name} déjà absente"
    try:
        result = subprocess.run(
            ["tmux", "kill-session", "-t", str(session_name)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True, f"Session tmux {session_name} arrêtée"
        return False, (result.stderr or result.stdout or f"Échec arrêt tmux {session_name}").strip()
    except FileNotFoundError:
        return False, "Commande tmux introuvable"
    except Exception as e:
        return False, f"Erreur arrêt tmux {session_name}: {e}"

