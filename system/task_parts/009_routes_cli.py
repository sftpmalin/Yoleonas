@task_bp.route("/system/task", methods=["GET"])
def task_home():
    # À chaque ouverture de la page, SQLite reste le maître.
    # Si le fichier cron généré a été touché à la main, on le reconstruit.
    ensure_cron_synced()
    active_tab = "tasks"
    task_view = request.args.get("view") or "tasks"
    selected_log_task = request.args.get("log_task") or ""
    return render_task_page(active_tab=active_tab, selected_log_task=selected_log_task, task_view=task_view)


def task_from_query_prefill():
    """Pré-remplit le formulaire de création depuis une URL.

    Utilisé par les autres modules (ex. SnapRAID) pour envoyer vers le
    gestionnaire de tâches sans dupliquer la logique cron/tmux ailleurs.
    Les paramètres absents gardent les valeurs d'une tâche vide.
    """
    task = blank_task()
    args = request.args
    if not args:
        return task

    for key in ("title", "description", "schedule_type", "chain_mode"):
        value = args.get(key)
        if value is not None:
            task[key] = str(value).strip()

    if task.get("schedule_type") not in SCHEDULE_LABELS:
        task["schedule_type"] = "manual"
    if task.get("chain_mode") not in CHAIN_LABELS:
        task["chain_mode"] = "and"

    for key, default, min_value, max_value in (
        ("enabled", task.get("enabled", 1), 0, 1),
        ("notify_success", task.get("notify_success", 0), 0, 1),
        ("time_hour", task.get("time_hour", 12), 0, 23),
        ("time_minute", task.get("time_minute", 0), 0, 59),
        ("every_minutes", task.get("every_minutes", 5), 1, 59),
        ("every_hours", task.get("every_hours", 1), 1, 23),
        ("month_day", task.get("month_day", 1), 1, 31),
        ("month", task.get("month", 1), 1, 12),
    ):
        if args.get(key) is not None:
            task[key] = safe_int(args.get(key), default, min_value, max_value)

    if args.get("week_days") is not None:
        task["week_days"] = normalize_week_days(args.get("week_days") or "")

    for key in CRON_FIELD_DEFAULTS:
        if args.get(key) is not None:
            task[key] = clean_cron_field(args.get(key), CRON_FIELD_DEFAULTS[key])

    commands = list(task.get("commands") or ["", "", "", "", ""])
    while len(commands) < 5:
        commands.append("")
    for i in range(1, 6):
        value = args.get(f"command_{i}")
        if value is not None:
            commands[i - 1] = str(value).strip()
    task["commands"] = commands[:5]
    return task


@task_bp.route("/system/task/create", methods=["GET"])
def task_new():
    # Route volontairement séparée : elle garantit une création neuve.
    # Elle accepte aussi des paramètres GET pour pré-remplir le formulaire
    # depuis un autre module, sans créer/modifier une tâche automatiquement.
    ensure_cron_synced()
    return render_task_page(active_tab="create", edit_task=task_from_query_prefill())


@task_bp.route("/system/task/progress", methods=["GET"])
def task_progress():
    ensure_cron_synced()
    return render_task_page(active_tab="progress", selected_log_task=request.args.get("log_task") or "")


@task_bp.route("/system/task/info", methods=["GET"])
def task_info():
    ensure_cron_synced()
    return render_task_page(active_tab="info", selected_log_task=request.args.get("log_task") or "")


@task_bp.route("/system/task/edit/<int:task_id>", methods=["GET"])
def task_edit(task_id):
    ensure_cron_synced()
    task = get_task(task_id)
    if not task:
        return redirect(url_for("task_bp.task_home"))
    return render_task_page(active_tab="create", edit_task=task)


@task_bp.route("/system/task/save", methods=["POST"])
def task_save():
    task_id_raw = request.form.get("task_id") or ""
    task_id = safe_int(task_id_raw, 0, 0, None) or None
    save_task_from_form(task_id)
    # Après création/modification, retour au tableau général.
    # On ne reste plus sur /task/edit/<id>, donc le formulaire ne garde plus le dernier ID.
    return redirect(url_for("task_bp.task_home"))


@task_bp.route("/system/task/toggle/<int:task_id>", methods=["POST"])
def task_toggle(task_id):
    ajax = is_ajax_request()
    task = get_task(task_id)
    if not task:
        if ajax:
            return jsonify({"ok": False, "task_id": task_id, "message": "Tâche introuvable"}), 404
        return redirect(url_for("task_bp.task_home"))

    new_enabled = 0 if safe_int(task.get("enabled"), 0) == 1 else 1
    with connect_db() as db:
        db.execute("UPDATE tasks SET enabled=?, updated_at=? WHERE id=?", (new_enabled, now_str(), task_id))
        db.commit()

    add_log(task_id, f"Tâche {'activée' if new_enabled else 'désactivée'} depuis l’interface.", "Configuration")
    cron_ok, cron_message = regenerate_cron()

    if ajax:
        updated_task = get_task(task_id)
        return jsonify(ajax_tasks_payload(
            task_id=task_id,
            enabled=new_enabled,
            task=updated_task,
            cron_ok=cron_ok,
            message=cron_message,
        ))

    return redirect(url_for("task_bp.task_home"))


@task_bp.route("/system/task/delete/<int:task_id>", methods=["POST"])
def task_delete(task_id):
    ajax = is_ajax_request()
    task = get_task(task_id)
    if not task:
        if ajax:
            return jsonify({"ok": False, "task_id": task_id, "message": "Tâche introuvable"}), 404
        return redirect(url_for("task_bp.task_home"))

    task_title = task.get("title") or f"Tâche {task_id}"
    with connect_db() as db:
        db.execute(
            "UPDATE tasks SET archived=1, archived_at=?, updated_at=? WHERE id=?",
            (now_str(), now_str(), task_id),
        )
        db.commit()

    cron_ok, cron_message = regenerate_cron()

    if ajax:
        return jsonify(ajax_tasks_payload(
            task_id=task_id,
            archived=True,
            archived_title=task_title,
            cron_ok=cron_ok,
            message=cron_message,
        ))

    return redirect(url_for("task_bp.task_home"))


@task_bp.route("/system/task/restore/<int:task_id>", methods=["POST"])
def task_restore(task_id):
    ajax = is_ajax_request()
    task = get_task(task_id, include_archived=True)
    if not task or not safe_int(task.get("archived"), 0):
        if ajax:
            return jsonify({"ok": False, "task_id": task_id, "message": "Tâche archivée introuvable"}), 404
        return redirect(url_for("task_bp.task_home", view="archive"))

    with connect_db() as db:
        db.execute(
            "UPDATE tasks SET archived=0, archived_at='', updated_at=? WHERE id=?",
            (now_str(), task_id),
        )
        db.commit()

    cron_ok, cron_message = regenerate_cron()
    if ajax:
        return jsonify(ajax_tasks_payload(
            task_id=task_id,
            restored=True,
            restored_title=task.get("title") or f"Tâche {task_id}",
            cron_ok=cron_ok,
            message=cron_message,
        ))
    return redirect(url_for("task_bp.task_home", view="archive"))


@task_bp.route("/system/task/delete-forever/<int:task_id>", methods=["POST"])
def task_delete_forever(task_id):
    ajax = is_ajax_request()
    task = get_task(task_id, include_archived=True)
    if not task or not safe_int(task.get("archived"), 0):
        if ajax:
            return jsonify({"ok": False, "task_id": task_id, "message": "Tâche archivée introuvable"}), 404
        return redirect(url_for("task_bp.task_home", view="archive"))

    task_title = task.get("title") or f"Tâche {task_id}"
    with connect_db() as db:
        db.execute("DELETE FROM tasks WHERE id=? AND archived=1", (task_id,))
        db.commit()

    cron_ok, cron_message = regenerate_cron()
    if ajax:
        return jsonify(ajax_tasks_payload(
            task_id=task_id,
            deleted_forever=True,
            deleted_title=task_title,
            cron_ok=cron_ok,
            message=cron_message,
        ))
    return redirect(url_for("task_bp.task_home", view="archive"))


@task_bp.route("/system/task/run/<int:task_id>", methods=["POST", "GET"])
def task_run(task_id):
    ajax = request.args.get("ajax") == "1" or request.headers.get("X-Requested-With") == "fetch"
    task = get_task(task_id)

    if not task:
        if ajax:
            return jsonify({"ok": False, "task_id": task_id, "message": "Tâche introuvable"}), 404
        return redirect(url_for("task_bp.task_home"))

    ok, message = run_task_background(task_id, "Manuel")

    if ajax:
        return jsonify({"ok": ok, "task_id": task_id, "message": message, "already_running": not ok and "déjà" in message.lower()})

    return redirect(url_for("task_bp.task_home"))


@task_bp.route("/system/task/stop/<int:task_id>", methods=["POST", "GET"])
def task_stop(task_id):
    ajax = request.args.get("ajax") == "1" or request.headers.get("X-Requested-With") == "fetch"
    ok, message = force_stop_task(task_id, "Arrêt manuel")

    if ajax:
        status_code = 200 if ok else 404
        return jsonify({"ok": ok, "task_id": task_id, "message": message}), status_code

    return redirect(url_for("task_bp.task_home"))


@task_bp.route("/system/task/api/status", methods=["GET"])
def task_api_status():
    ensure_cron_synced()
    tasks = get_all_tasks()
    rows = []
    for task in tasks:
        status = task.get("status") or {}
        if safe_int(status.get("running"), 0) == 1 and not task_runtime_alive(task["id"], status):
            cleanup_stale_task_runtime(task["id"], "Statut")
            status = get_status(task["id"])
        rows.append(
            {
                "id": task["id"],
                "title": task.get("title"),
                "enabled": safe_int(task.get("enabled"), 0),
                "notify_success": safe_int(task.get("notify_success"), 0),
                "schedule_label": task.get("schedule_label"),
                "schedule_type": task.get("schedule_type"),
                "schedule_category": task.get("schedule_category"),
                "running": safe_int(status.get("running"), 0),
                "status": status.get("status") or "Jamais lancé",
                "last_run": status.get("last_run") or "—",
                "last_end": status.get("last_end") or "—",
                "source": status.get("source") or "—",
                "result": status.get("result") or "—",
                "last_message": status.get("last_message") or "",
                "updated_at": status.get("updated_at") or "",
            }
        )
    return jsonify(
        {
            "updated_at": now_str(),
            "stats": {
                "total": len(rows),
                "enabled": len([r for r in rows if r["enabled"] == 1]),
                "running": len([r for r in rows if r["running"] == 1]),
                "error": len([r for r in rows if str(r["result"]).lower() == "erreur"]),
                "success": len([r for r in rows if str(r["result"]).lower() == "succès"]),
            },
            "tasks": rows,
        }
    )


@task_bp.route("/system/task/api/push/config", methods=["GET"])
def task_api_push_config():
    public_key = ensure_vapid_keys()
    return jsonify(
        {
            "ok": bool(public_key),
            "public_key": public_key,
            "webpush_available": webpush is not None,
            "subscription_count": push_subscription_count(),
            "message": "OK" if public_key else "Clé VAPID indisponible",
        }
    )


@task_bp.route("/system/task/api/push/subscribe", methods=["POST"])
def task_api_push_subscribe():
    subscription = request.get_json(silent=True) or {}
    ok, message = save_push_subscription(subscription, request.headers.get("User-Agent", ""))
    status = 200 if ok else 400
    return jsonify({"ok": ok, "message": message, "subscription_count": push_subscription_count()}), status


@task_bp.route("/system/task/api/push/unsubscribe", methods=["POST"])
def task_api_push_unsubscribe():
    payload = request.get_json(silent=True) or {}
    endpoint = payload.get("endpoint") or ""
    ok = delete_push_subscription(endpoint)
    return jsonify({"ok": ok, "subscription_count": push_subscription_count()})


@task_bp.route("/system/task/api/push/cleanup", methods=["POST"])
def task_api_push_cleanup():
    result = cleanup_push_subscriptions("nettoyage interface")
    return jsonify({
        "ok": True,
        "removed": result.get("removed", 0),
        "kept": result.get("kept", 0),
        "subscription_count": push_subscription_count(),
        "updated_at": result.get("updated_at", now_str()),
    })


@task_bp.route("/system/task/api/push/test", methods=["POST"])
def task_api_push_test():
    payload = {
        "title": "Task Manager",
        "body": "Notification de test : l’abonnement fonctionne.",
        "icon": "/static/logo/Tasks.png",
        "badge": "/static/logo/Tasks.png",
        "tag": f"task-manager-test-{int(time.time())}",
        "url": "/system/task/progress",
    }
    sent, failed, details = send_web_push_payload(payload, with_details=True)
    return jsonify({
        "ok": sent > 0,
        "sent": sent,
        "failed": failed,
        "subscription_count": push_subscription_count(),
        "details": details,
    })


@task_bp.route("/system/task/api/log/<int:task_id>", methods=["GET"])
def task_api_log(task_id):
    limit = safe_int(request.args.get("limit"), 250, 1, 2000)
    return jsonify({"task_id": task_id, "lines": get_log_tail(task_id, limit=limit)})


# ==========================================================
# CLI CRON
# ==========================================================
def cli_main():
    parser = argparse.ArgumentParser(description="Task Manager CLI")
    parser.add_argument("--run-task", type=int, help="ID de la tâche à lancer dans tmux")
    parser.add_argument("--worker-run-task", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--source", default="CLI", help="Source affichée dans les logs")
    parser.add_argument("--regen-cron", action="store_true", help="Régénère le cron depuis SQLite")
    parser.add_argument(
        "--db-maintenance",
        choices=["backup", "check", "check-repair", "repair-log", "restore-latest"],
        help="Maintenance de task.db: backup, check, reparation ou restauration.",
    )
    args = parser.parse_args()

    if args.regen_cron:
        ok, msg = regenerate_cron()
        print(f"Cron régénéré: {ok} - {msg}")
        return 0 if ok else 1

    if args.db_maintenance:
        ok, msg = run_db_maintenance(args.db_maintenance)
        print(f"DB maintenance {args.db_maintenance}: {ok} - {msg}")
        return 0 if ok else 1

    init_db()

    if args.worker_run_task:
        ok = run_task_worker(args.worker_run_task, args.source)
        return 0 if ok else 1

    if args.run_task:
        ok = run_task(args.run_task, args.source)
        return 0 if ok else 1

    parser.print_help()
    return 1


def main():
    cli_flags = {"--run-task", "--worker-run-task", "--regen-cron", "--db-maintenance"}
    if any(arg in cli_flags for arg in sys.argv[1:]):
        return cli_main()
    print("Module Yoleo Task : utilise /system/task via Flask, ou --run-task/--regen-cron en CLI.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
