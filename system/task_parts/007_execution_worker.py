def run_task(task_id, source="Manuel"):
    """Point d'entrée public : démarre la tâche dans tmux et rend la main."""
    ok, message = launch_task_tmux(task_id, source)
    if not ok:
        try:
            add_log(task_id, message, source)
        except Exception:
            pass
    return ok


def run_task_worker(task_id, source="Manuel"):
    """Worker réel lancé dans la session tmux.

    Flask/Gunicorn ne porte plus les commandes longues : il ne fait que lancer
    tmux. Ce worker vit dans tmux, garde les logs SQLite/fichier,
    et protège seulement le double lancement de la même tâche.
    """
    init_db()
    cleanup_legacy_global_lock()
    task = get_task(task_id)
    if not task:
        return False

    commands = [cmd.strip() for cmd in task.get("commands", []) if cmd.strip()]
    title = task.get("title") or f"Tâche {task_id}"
    chain_mode = task.get("chain_mode") or "and"
    conf = read_task_conf()
    shell_bin = conf.get("SHELL_BIN") or "/bin/bash"
    runtime_status = get_status(task_id)
    session_name = runtime_status.get("tmux_session") or task_tmux_session_name(task_id, title)
    unit_name = runtime_status.get("systemd_unit") or ""
    lock_file = runtime_status.get("lock_path") or str(task_lock_path(task_id))

    if not commands:
        add_log(task_id, "Aucune commande à exécuter.", source)
        set_status(
            task_id,
            running=0,
            status="⚠️ Aucune commande",
            source=source,
            result="Ignoré",
            last_end=now_str(),
            last_message="Aucune commande configurée.",
        )
        clear_task_runtime(task_id)
        return False

    runner_lock = acquire_task_runner_lock(task_id, source)
    if runner_lock is None:
        return False

    begin_task_db_log_buffer(task_id)

    try:
        start_time = now_str()
        set_status(
            task_id,
            running=1,
            status="🔄 En cours",
            last_run=start_time,
            last_end="—",
            source=source,
            result="En cours",
            last_message=f"Session tmux : {session_name}",
        )
        try:
            worker_pgid = os.getpgid(os.getpid())
        except Exception:
            worker_pgid = os.getpid()
        set_task_runtime(task_id, os.getpid(), worker_pgid, 0, session_name, unit_name, lock_file)
        add_log(task_id, f"DÉMARRAGE TMUX - {title} - source={source} - session={session_name}", source)
        print(f"[{now_str()}] DÉMARRAGE - {title} - source={source}", flush=True)

        had_error = False
        last_message = ""
        last_live_status_update = 0.0

        for index, command in enumerate(commands, 1):
            if is_stop_requested(task_id):
                msg = "⛔ Arrêt demandé avant le lancement de la commande suivante."
                add_log(task_id, msg, source)
                print(f"[{now_str()}] {msg}", flush=True)
                set_status(
                    task_id,
                    running=0,
                    status="⛔ Arrêté",
                    last_end=now_str(),
                    source=source,
                    result="Arrêté",
                    last_message=msg,
                )
                clear_task_runtime(task_id)
                trim_logs(task_id)
                return False

            add_log(task_id, f"--- COMMANDE {index}/{len(commands)} ---", source)
            add_log(task_id, command, source)
            print(f"\n[{now_str()}] --- COMMANDE {index}/{len(commands)} ---", flush=True)
            print(command, flush=True)
            set_status(task_id, status=f"🔄 Commande {index}/{len(commands)}", last_message=command[:300])

            # Le subprocess n'est plus un enfant de Gunicorn : ce code tourne déjà dans tmux.
            # On garde Popen ici uniquement pour capturer stdout/stderr et alimenter les logs Yoleo.
            process = subprocess.Popen(
                command,
                shell=True,
                executable=shell_bin,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
                start_new_session=True,
            )
            try:
                process_pgid = os.getpgid(process.pid)
            except Exception:
                process_pgid = process.pid
            set_task_runtime(task_id, process.pid, process_pgid, 0, session_name, unit_name, lock_file)
            add_log(task_id, f"PID commande={process.pid} PGID={process_pgid} TMUX={session_name}", source)
            print(f"[{now_str()}] PID commande={process.pid} PGID={process_pgid}", flush=True)

            if process.stdout is not None:
                for raw_line in process.stdout:
                    clean = raw_line.rstrip("\n")
                    if not clean:
                        continue
                    last_message = clean[:500]
                    add_log(task_id, clean, source)
                    print(clean, flush=True)
                    now_mono = time.monotonic()
                    if now_mono - last_live_status_update >= 1.0:
                        set_status(task_id, status=f"🔄 Commande {index}/{len(commands)}", last_message=last_message)
                        last_live_status_update = now_mono

            return_code = process.wait()
            set_task_runtime(
                task_id,
                os.getpid(),
                worker_pgid,
                safe_int(get_status(task_id).get("stop_requested"), 0),
                session_name,
                unit_name,
                lock_file,
            )

            if is_stop_requested(task_id):
                msg = f"⛔ Tâche arrêtée de force pendant la commande {index}. Code retour {return_code}."
                last_message = msg
                add_log(task_id, msg, source)
                print(f"[{now_str()}] {msg}", flush=True)
                set_status(
                    task_id,
                    running=0,
                    status="⛔ Arrêté",
                    last_end=now_str(),
                    source=source,
                    result="Arrêté",
                    last_message=msg,
                )
                clear_task_runtime(task_id)
                trim_logs(task_id)
                return False

            if return_code != 0:
                had_error = True
                msg = f"❌ Commande {index} terminée avec code retour {return_code}"
                last_message = msg
                add_log(task_id, msg, source)
                print(f"[{now_str()}] {msg}", flush=True)
                if chain_mode == "and":
                    add_log(task_id, "Arrêt de la chaîne car le mode est : stop si erreur (&&).", source)
                    print(f"[{now_str()}] Arrêt de la chaîne : mode &&", flush=True)
                    break
            else:
                msg = f"✅ Commande {index} terminée avec succès."
                add_log(task_id, msg, source)
                print(f"[{now_str()}] {msg}", flush=True)

        end_time = now_str()
        if had_error:
            set_status(
                task_id,
                running=0,
                status="❌ Erreur",
                last_end=end_time,
                source=source,
                result="Erreur",
                last_message=last_message or "Une commande a échoué.",
            )
            add_log(task_id, f"FIN - {title} - ERREUR", source)
            print(f"[{now_str()}] FIN - {title} - ERREUR", flush=True)
            notify_task_failure(task_id, task, end_time, last_message or "Une commande a échoué.")
        else:
            set_status(
                task_id,
                running=0,
                status="✅ Terminé",
                last_end=end_time,
                source=source,
                result="Succès",
                last_message=last_message or "Traitement terminé avec succès.",
            )
            add_log(task_id, f"FIN - {title} - SUCCÈS", source)
            print(f"[{now_str()}] FIN - {title} - SUCCÈS", flush=True)
            notify_task_success(task_id, task, end_time)

        clear_task_runtime(task_id)
        trim_logs(task_id)
        return not had_error

    except Exception as e:
        msg = f"💥 Erreur inattendue : {e}"
        add_log(task_id, msg, source)
        print(f"[{now_str()}] {msg}", flush=True)
        end_time = now_str()
        set_status(
            task_id,
            running=0,
            status="❌ Erreur",
            last_end=end_time,
            source=source,
            result="Erreur",
            last_message=msg,
        )
        notify_task_failure(task_id, task, end_time, msg)
        clear_task_runtime(task_id)
        trim_logs(task_id)
        return False
    finally:
        try:
            flush_task_db_log_buffer(task_id)
            trim_logs(task_id)
        except Exception as e:
            os.environ.pop(TASK_DB_LOG_BUFFER_ENV, None)
            print(f"[{now_str()}] Import final des logs SQLite impossible : {e}", flush=True)
        release_task_runner_lock(runner_lock)
        cleanup_stale_task_runtime(task_id, source)


def run_task_background(task_id, source="Manuel"):
    """Compat route Flask : démarre en tmux, ne crée plus de thread long Gunicorn."""
    return launch_task_tmux(task_id, source)


# ==========================================================
# FORMULAIRE / ROUTES FLASK
# ==========================================================
