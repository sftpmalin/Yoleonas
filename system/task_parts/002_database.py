def get_db_path():
    return read_task_conf()["TASK_DB"]


class LockedDbConnection:
    def __init__(self, db_path, lock_path):
        self.db_path = str(db_path)
        self.lock_path = Path(lock_path)
        self.lock_handle = None
        self.conn = None

    def __enter__(self):
        _DB_PROCESS_LOCK.acquire()
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            self.lock_handle = self.lock_path.open("a+", encoding="utf-8")
            if fcntl is not None:
                fcntl.flock(self.lock_handle.fileno(), fcntl.LOCK_EX)

            self.conn = sqlite3.connect(self.db_path, timeout=30)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA busy_timeout=30000")
            self.conn.execute("PRAGMA journal_mode=DELETE")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            return self.conn
        except Exception:
            self._cleanup()
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.conn is not None:
                return self.conn.__exit__(exc_type, exc, tb)
            return False
        finally:
            self._cleanup()

    def _cleanup(self):
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:
            pass
        self.conn = None
        try:
            if self.lock_handle is not None:
                if fcntl is not None:
                    fcntl.flock(self.lock_handle.fileno(), fcntl.LOCK_UN)
                self.lock_handle.close()
        except Exception:
            pass
        self.lock_handle = None
        try:
            _DB_PROCESS_LOCK.release()
        except RuntimeError:
            pass


def connect_db():
    conf = read_task_conf()
    lock_path = Path(conf.get("TASK_RUNTIME_DIR") or DEFAULTS["TASK_RUNTIME_DIR"]) / "sqlite.lock"
    return LockedDbConnection(conf["TASK_DB"], lock_path)


def ensure_maintenance_tasks(db):
    conf = read_task_conf()
    runner_file = cron_runner_file()
    runner_dir = runner_file.parent
    python_bin = conf.get("PYTHON_BIN") or default_python_bin()

    for spec in MAINTENANCE_TASKS:
        command = (
            f"cd {shlex.quote(str(runner_dir))} && "
            f"{shlex.quote(python_bin)} {shlex.quote(str(runner_file))} "
            f"--db-maintenance {shlex.quote(spec['action'])}"
        )
        existing = db.execute("SELECT id FROM tasks WHERE title=?", (spec["title"],)).fetchone()
        if existing:
            task_id = existing["id"]
            db.execute(
                """
                UPDATE tasks
                SET description=?, schedule_type='daily', time_hour=?, time_minute=?,
                    chain_mode='and', updated_at=?
                WHERE id=?
                """,
                (spec["description"], spec["hour"], spec["minute"], now_str(), task_id),
            )
        else:
            cur = db.execute(
                """
                INSERT INTO tasks(
                    title, description, enabled, schedule_type, time_hour, time_minute,
                    every_minutes, every_hours, month_day, month, notify_success,
                    chain_mode, created_at, updated_at
                ) VALUES(?, ?, 1, 'daily', ?, ?, 5, 1, 1, 1, 0, 'and', ?, ?)
                """,
                (spec["title"], spec["description"], spec["hour"], spec["minute"], now_str(), now_str()),
            )
            task_id = cur.lastrowid

        db.execute(
            """
            INSERT INTO task_commands(task_id, position, command)
            VALUES(?, 1, ?)
            ON CONFLICT(task_id, position) DO UPDATE SET command=excluded.command
            """,
            (task_id, command),
        )
        for pos in range(2, 6):
            db.execute(
                """
                INSERT INTO task_commands(task_id, position, command)
                VALUES(?, ?, '')
                ON CONFLICT(task_id, position) DO UPDATE SET command=''
                """,
                (task_id, pos),
            )


def init_db():
    read_task_conf()
    with connect_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                schedule_type TEXT NOT NULL DEFAULT 'manual',
                time_hour INTEGER NOT NULL DEFAULT 0,
                time_minute INTEGER NOT NULL DEFAULT 0,
                every_minutes INTEGER NOT NULL DEFAULT 5,
                every_hours INTEGER NOT NULL DEFAULT 1,
                week_days TEXT DEFAULT '',
                month_day INTEGER NOT NULL DEFAULT 1,
                month INTEGER NOT NULL DEFAULT 1,
                custom_cron_minute TEXT DEFAULT '*',
                custom_cron_hour TEXT DEFAULT '*',
                custom_cron_day TEXT DEFAULT '*',
                custom_cron_month TEXT DEFAULT '*',
                custom_cron_weekday TEXT DEFAULT '*',
                notify_success INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                archived_at TEXT DEFAULT '',
                chain_mode TEXT NOT NULL DEFAULT 'and',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_cron TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS task_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                command TEXT DEFAULT '',
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                UNIQUE(task_id, position)
            );

            CREATE TABLE IF NOT EXISTS task_status (
                task_id INTEGER PRIMARY KEY,
                running INTEGER NOT NULL DEFAULT 0,
                status TEXT DEFAULT 'Jamais lancé',
                last_run TEXT DEFAULT '—',
                last_end TEXT DEFAULT '—',
                source TEXT DEFAULT '—',
                result TEXT DEFAULT '—',
                last_message TEXT DEFAULT '',
                process_pid INTEGER DEFAULT 0,
                process_pgid INTEGER DEFAULT 0,
                tmux_session TEXT DEFAULT '',
                systemd_unit TEXT DEFAULT '',
                lock_path TEXT DEFAULT '',
                stop_requested INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS task_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                source TEXT DEFAULT '',
                line TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT NOT NULL UNIQUE,
                subscription_json TEXT NOT NULL,
                user_agent TEXT DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_error TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_task_log_task_id_id ON task_log(task_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_task_commands_task_pos ON task_commands(task_id, position);
            CREATE INDEX IF NOT EXISTS idx_push_subscriptions_enabled ON push_subscriptions(enabled);
            """
        )
        for column_sql in [
            "ALTER TABLE task_status ADD COLUMN process_pid INTEGER DEFAULT 0",
            "ALTER TABLE task_status ADD COLUMN process_pgid INTEGER DEFAULT 0",
            "ALTER TABLE task_status ADD COLUMN tmux_session TEXT DEFAULT ''",
            "ALTER TABLE task_status ADD COLUMN systemd_unit TEXT DEFAULT ''",
            "ALTER TABLE task_status ADD COLUMN lock_path TEXT DEFAULT ''",
            "ALTER TABLE task_status ADD COLUMN stop_requested INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tasks ADD COLUMN custom_cron_minute TEXT DEFAULT '*'",
            "ALTER TABLE tasks ADD COLUMN custom_cron_hour TEXT DEFAULT '*'",
            "ALTER TABLE tasks ADD COLUMN custom_cron_day TEXT DEFAULT '*'",
            "ALTER TABLE tasks ADD COLUMN custom_cron_month TEXT DEFAULT '*'",
            "ALTER TABLE tasks ADD COLUMN custom_cron_weekday TEXT DEFAULT '*'",
            "ALTER TABLE tasks ADD COLUMN notify_success INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tasks ADD COLUMN archived INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tasks ADD COLUMN archived_at TEXT DEFAULT ''",
        ]:
            try:
                db.execute(column_sql)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
        ensure_maintenance_tasks(db)
        db.commit()


# Initialisation au chargement du module Flask/CLI normal.
# La maintenance DB doit pouvoir demarrer meme si task.db est corrompue.
if "--db-maintenance" not in sys.argv[1:]:
    init_db()


# ==========================================================
# OUTILS DB
# ==========================================================
def row_to_dict(row):
    return dict(row) if row is not None else None


def task_backup_dir(conf=None):
    conf = conf or read_task_conf()
    path = Path(conf["TASK_BACKUP_DIR"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_timestamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def db_file_siblings(db_path):
    db_path = Path(db_path)
    return [db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")]


def snapshot_db_files(label="snapshot"):
    conf = read_task_conf()
    backup_dir = task_backup_dir(conf) / f"raw-{label}-{db_timestamp()}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for src in db_file_siblings(conf["TASK_DB"]):
        if src.exists():
            dst = backup_dir / src.name
            shutil.copy2(src, dst)
            copied.append(str(dst))
    return backup_dir, copied


def purge_old_db_backups(conf=None):
    conf = conf or read_task_conf()
    keep_days = safe_int(conf.get("TASK_BACKUP_KEEP_DAYS"), 30, 1, 3650)
    cutoff = time.time() - (keep_days * 86400)
    removed = 0
    for path in task_backup_dir(conf).glob("task.db.*.sqlite3"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except Exception:
            pass
    return removed


def integrity_check_path(db_path):
    lines = []
    try:
        with sqlite3.connect(str(db_path), timeout=30) as db:
            db.execute("PRAGMA busy_timeout=30000")
            cursor = db.execute("PRAGMA integrity_check")
            for row in cursor:
                lines.append(str(row[0]))
    except Exception as exc:
        lines.append(f"{type(exc).__name__}: {exc}")
        return False, lines
    return lines == ["ok"], lines


def check_task_db_integrity():
    conf = read_task_conf()
    with connect_db() as db:
        try:
            db.execute("PRAGMA wal_checkpoint(FULL)")
        except Exception:
            pass
    return integrity_check_path(conf["TASK_DB"])


def create_task_db_backup(label="manual"):
    conf = read_task_conf()
    backup_dir = task_backup_dir(conf)
    backup_path = backup_dir / f"task.db.{label}-{db_timestamp()}.sqlite3"
    tmp_path = backup_path.with_suffix(".tmp")
    if tmp_path.exists():
        tmp_path.unlink()

    with connect_db() as source:
        try:
            source.execute("PRAGMA wal_checkpoint(FULL)")
        except Exception:
            pass
        with sqlite3.connect(str(tmp_path), timeout=30) as dest:
            source.backup(dest)
            dest.execute("PRAGMA journal_mode=DELETE")
            dest.execute("PRAGMA synchronous=FULL")
            dest.commit()

    ok, lines = integrity_check_path(tmp_path)
    if not ok:
        try:
            tmp_path.unlink()
        except Exception:
            pass
        return False, f"Backup refuse: integrity_check={lines[:5]}", None

    os.replace(tmp_path, backup_path)
    removed = purge_old_db_backups(conf)
    return True, f"Backup OK: {backup_path} ; anciens supprimes={removed}", str(backup_path)


def rootpage_map(db_path):
    mapping = {}
    try:
        with sqlite3.connect(str(db_path), timeout=30) as db:
            for row in db.execute("SELECT name, rootpage FROM sqlite_master WHERE rootpage > 0"):
                mapping[int(row[1])] = str(row[0])
    except Exception:
        pass
    return mapping


def corruption_targets_task_log(lines, db_path):
    text = "\n".join(lines).lower()
    if "task_log" in text or "idx_task_log_task_id_id" in text:
        return True
    roots = rootpage_map(db_path)
    for root, name in roots.items():
        if name in {"task_log", "idx_task_log_task_id_id"} and f"tree {root} " in text:
            return True
    return False


def reset_task_log_table(reason="integrity_check"):
    snapshot_dir, copied = snapshot_db_files("before-log-reset")
    with connect_db() as db:
        db.execute("DROP INDEX IF EXISTS idx_task_log_task_id_id")
        db.execute("DROP TABLE IF EXISTS task_log")
        db.execute(
            """
            CREATE TABLE task_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                source TEXT DEFAULT '',
                line TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            )
            """
        )
        db.execute("CREATE INDEX idx_task_log_task_id_id ON task_log(task_id, id DESC)")
        db.commit()
        try:
            db.execute("VACUUM")
        except Exception:
            pass
    ok, lines = check_task_db_integrity()
    if ok:
        create_task_db_backup("after-log-reset")
        return True, f"task_log recree ({reason}). Snapshot: {snapshot_dir}. Fichiers copies={len(copied)}"
    return False, f"Reset task_log insuffisant: {lines[:8]}. Snapshot: {snapshot_dir}"


def valid_backup_candidates():
    conf = read_task_conf()
    candidates = sorted(
        task_backup_dir(conf).glob("task.db.*.sqlite3"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    valid = []
    for path in candidates:
        ok, _lines = integrity_check_path(path)
        if ok:
            valid.append(path)
    return valid


def restore_task_db_from_backup(backup_path):
    conf = read_task_conf()
    backup_path = Path(backup_path)
    if not backup_path.exists():
        return False, f"Backup introuvable: {backup_path}"
    ok, lines = integrity_check_path(backup_path)
    if not ok:
        return False, f"Backup invalide: {lines[:5]}"
    snapshot_dir, copied = snapshot_db_files("before-restore")
    db_path = Path(conf["TASK_DB"])
    tmp_restore = db_path.with_name(f"{db_path.name}.restore-{db_timestamp()}.tmp")
    shutil.copy2(backup_path, tmp_restore)
    os.replace(tmp_restore, db_path)
    for sibling in db_file_siblings(db_path)[1:]:
        try:
            sibling.unlink()
        except FileNotFoundError:
            pass
    return True, f"Base restauree depuis {backup_path}. Snapshot precedent: {snapshot_dir}. Fichiers copies={len(copied)}"


def table_columns(db, schema_name, table_name):
    pragma_name = f"{schema_name}.table_info({table_name})" if schema_name else f"table_info({table_name})"
    return [row[1] for row in db.execute(f"PRAGMA {pragma_name}")]


def rebuild_task_db_without_logs():
    conf = read_task_conf()
    db_path = Path(conf["TASK_DB"])
    snapshot_dir, copied = snapshot_db_files("before-rebuild")
    backup_dir = task_backup_dir(conf)
    tmp_path = backup_dir / f"task.db.rebuilt-{db_timestamp()}.tmp"
    if tmp_path.exists():
        tmp_path.unlink()

    with sqlite3.connect(str(tmp_path), timeout=30) as new_db:
        new_db.execute("PRAGMA journal_mode=DELETE")
        new_db.execute("PRAGMA synchronous=FULL")
        new_db.execute("PRAGMA foreign_keys=OFF")
        new_db.executescript(DB_SCHEMA_SQL)
        new_db.execute("ATTACH DATABASE ? AS old", (str(db_path),))
        for table in ("tasks", "task_commands", "task_status", "push_subscriptions"):
            new_cols = set(table_columns(new_db, "", table))
            old_cols = table_columns(new_db, "old", table)
            common = [col for col in old_cols if col in new_cols]
            if not common:
                continue
            cols_sql = ", ".join([f'"{col}"' for col in common])
            new_db.execute(f"INSERT INTO {table}({cols_sql}) SELECT {cols_sql} FROM old.{table}")
        new_db.commit()
        new_db.execute("DETACH DATABASE old")
        new_db.commit()

    ok, lines = integrity_check_path(tmp_path)
    if not ok:
        try:
            tmp_path.unlink()
        except Exception:
            pass
        return False, f"Rebuild refuse: {lines[:8]}. Snapshot: {snapshot_dir}"

    os.replace(tmp_path, db_path)
    for sibling in db_file_siblings(db_path)[1:]:
        try:
            sibling.unlink()
        except FileNotFoundError:
            pass
    ok, lines = check_task_db_integrity()
    if ok:
        create_task_db_backup("after-rebuild")
        return True, f"Base reconstruite sans anciens logs. Snapshot: {snapshot_dir}. Fichiers copies={len(copied)}"
    return False, f"Rebuild installe mais check encore KO: {lines[:8]}. Snapshot: {snapshot_dir}"


def repair_task_db_if_needed():
    conf = read_task_conf()
    ok, lines = check_task_db_integrity()
    if ok:
        return True, "integrity_check OK"

    if corruption_targets_task_log(lines, conf["TASK_DB"]):
        try:
            repaired, message = reset_task_log_table("corruption task_log")
            if repaired:
                return True, message
        except Exception as exc:
            message = f"Reset task_log impossible ({type(exc).__name__}: {exc}); passage au rebuild."

    backups = valid_backup_candidates()
    if backups:
        return restore_task_db_from_backup(backups[0])

    return rebuild_task_db_without_logs()


def run_db_maintenance(action):
    if action == "backup":
        ok, msg, _path = create_task_db_backup("daily")
        return ok, msg
    if action == "check":
        ok, lines = check_task_db_integrity()
        return ok, "integrity_check OK" if ok else "integrity_check KO: " + " | ".join(lines[:8])
    if action == "repair-log":
        return reset_task_log_table("commande manuelle")
    if action == "check-repair":
        return repair_task_db_if_needed()
    if action == "restore-latest":
        backups = valid_backup_candidates()
        if not backups:
            return False, "Aucun backup valide disponible"
        return restore_task_db_from_backup(backups[0])
    return False, f"Action maintenance inconnue: {action}"

