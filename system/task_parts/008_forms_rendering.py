def blank_task():
    return {
        "id": "",
        "title": "",
        "description": "",
        "enabled": 1,
        "schedule_type": "manual",
        "time_hour": 12,
        "time_minute": 0,
        "every_minutes": 5,
        "every_hours": 1,
        "week_days": "",
        "month_day": 1,
        "month": 1,
        "custom_cron_minute": "*",
        "custom_cron_hour": "*",
        "custom_cron_day": "*",
        "custom_cron_month": "*",
        "custom_cron_weekday": "*",
        "notify_success": 0,
        "chain_mode": "and",
        "commands": ["", "", "", "", ""],
    }


def form_to_task_data():
    week_days = request.form.getlist("week_days")
    commands = []
    for i in range(1, 6):
        commands.append((request.form.get(f"command_{i}") or "").strip())

    schedule_type = request.form.get("schedule_type") or "manual"
    if schedule_type not in SCHEDULE_LABELS:
        schedule_type = "manual"

    chain_mode = request.form.get("chain_mode") or "and"
    if chain_mode not in CHAIN_LABELS:
        chain_mode = "and"

    return {
        "title": (request.form.get("title") or "").strip(),
        "description": (request.form.get("description") or "").strip(),
        "enabled": 1 if request.form.get("enabled") == "1" else 0,
        "schedule_type": schedule_type,
        "time_hour": safe_int(request.form.get("time_hour"), 0, 0, 23),
        "time_minute": safe_int(request.form.get("time_minute"), 0, 0, 59),
        "every_minutes": safe_int(request.form.get("every_minutes"), 5, 1, 59),
        "every_hours": safe_int(request.form.get("every_hours"), 1, 1, 23),
        "week_days": normalize_week_days(",".join(week_days)),
        "month_day": safe_int(request.form.get("month_day"), 1, 1, 31),
        "month": safe_int(request.form.get("month"), 1, 1, 12),
        "custom_cron_minute": clean_cron_field(request.form.get("custom_cron_minute"), "*"),
        "custom_cron_hour": clean_cron_field(request.form.get("custom_cron_hour"), "*"),
        "custom_cron_day": clean_cron_field(request.form.get("custom_cron_day"), "*"),
        "custom_cron_month": clean_cron_field(request.form.get("custom_cron_month"), "*"),
        "custom_cron_weekday": clean_cron_field(request.form.get("custom_cron_weekday"), "*"),
        "notify_success": 1 if request.form.get("notify_success") == "1" else 0,
        "chain_mode": chain_mode,
        "commands": commands,
    }


def save_task_from_form(task_id=None):
    data = form_to_task_data()
    if not data["title"]:
        data["title"] = "Tâche sans titre"

    stamp = now_str()
    with connect_db() as db:
        if task_id:
            existing = db.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not existing:
                task_id = None

        if task_id:
            db.execute(
                """
                UPDATE tasks SET
                    title=?, description=?, enabled=?, schedule_type=?, time_hour=?, time_minute=?,
                    every_minutes=?, every_hours=?, week_days=?, month_day=?, month=?,
                    custom_cron_minute=?, custom_cron_hour=?, custom_cron_day=?, custom_cron_month=?, custom_cron_weekday=?,
                    notify_success=?, chain_mode=?, updated_at=?
                WHERE id=?
                """,
                (
                    data["title"],
                    data["description"],
                    data["enabled"],
                    data["schedule_type"],
                    data["time_hour"],
                    data["time_minute"],
                    data["every_minutes"],
                    data["every_hours"],
                    data["week_days"],
                    data["month_day"],
                    data["month"],
                    data["custom_cron_minute"],
                    data["custom_cron_hour"],
                    data["custom_cron_day"],
                    data["custom_cron_month"],
                    data["custom_cron_weekday"],
                    data["notify_success"],
                    data["chain_mode"],
                    stamp,
                    task_id,
                ),
            )
        else:
            cur = db.execute(
                """
                INSERT INTO tasks(
                    title, description, enabled, schedule_type, time_hour, time_minute,
                    every_minutes, every_hours, week_days, month_day, month,
                    custom_cron_minute, custom_cron_hour, custom_cron_day, custom_cron_month, custom_cron_weekday,
                    notify_success, chain_mode, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["title"],
                    data["description"],
                    data["enabled"],
                    data["schedule_type"],
                    data["time_hour"],
                    data["time_minute"],
                    data["every_minutes"],
                    data["every_hours"],
                    data["week_days"],
                    data["month_day"],
                    data["month"],
                    data["custom_cron_minute"],
                    data["custom_cron_hour"],
                    data["custom_cron_day"],
                    data["custom_cron_month"],
                    data["custom_cron_weekday"],
                    data["notify_success"],
                    data["chain_mode"],
                    stamp,
                    stamp,
                ),
            )
            task_id = cur.lastrowid
            db.execute(
                "INSERT OR IGNORE INTO task_status(task_id, running, status, updated_at) VALUES(?, 0, 'Jamais lancé', ?)",
                (task_id, stamp),
            )

        for pos, command in enumerate(data["commands"], 1):
            db.execute(
                """
                INSERT INTO task_commands(task_id, position, command)
                VALUES(?, ?, ?)
                ON CONFLICT(task_id, position) DO UPDATE SET command=excluded.command
                """,
                (task_id, pos, command),
            )

        db.commit()

    ok, message = regenerate_cron()
    add_log(task_id, f"Configuration sauvegardée. Cron rechargé={ok}. {message}", "Configuration")
    return task_id


def render_task_page(active_tab="tasks", edit_task=None, selected_log_task="", task_view="tasks"):
    tasks = get_all_tasks()
    archived_tasks = get_archived_tasks()
    conf = read_task_conf()
    conf_for_display = dict(conf)
    for key in PATH_CONFIG_KEYS:
        conf_for_display[key] = display_task_path(conf_for_display.get(key, ""))
    allowed_tabs = {"tasks", "create", "progress", "info"}
    if active_tab not in allowed_tabs:
        active_tab = "tasks"
    if task_view not in {"tasks", "archive"}:
        task_view = "tasks"
    stats = build_task_stats(tasks)

    return render_template(
        "system_task.html",
        tasks=tasks,
        archived_tasks=archived_tasks,
        archived_count=len(archived_tasks),
        task_view=task_view,
        edit_task=edit_task or blank_task(),
        is_edit=bool(edit_task and edit_task.get("id")),
        active_tab=active_tab,
        selected_log_task=str(selected_log_task or ""),
        stats=stats,
        conf=conf_for_display,
        days_labels=DAYS_LABELS,
        schedule_labels=SCHEDULE_LABELS,
        chain_labels=CHAIN_LABELS,
        push_subscription_count=push_subscription_count(),
        webpush_available=webpush is not None,
    )

