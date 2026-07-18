DOCKER_EXEC_FLAGS = {
    "-d", "--detach",
    "-i", "--interactive",
    "-t", "--tty",
    "--privileged",
}
DOCKER_EXEC_OPTIONS_WITH_VALUE = {
    "-e", "--env",
    "--env-file",
    "-u", "--user",
    "-w", "--workdir",
    "--detach-keys",
}


def current_task_command(task, status):
    commands = [cmd.strip() for cmd in (task or {}).get("commands", []) if str(cmd or "").strip()]
    if not commands:
        return ""
    state = str((status or {}).get("status") or "")
    match = re.search(r"Commande\s+(\d+)\s*/", state)
    if match:
        index = safe_int(match.group(1), 0)
        if 1 <= index <= len(commands):
            return commands[index - 1]
    if len(commands) == 1:
        return commands[0]
    return ""


def parse_docker_exec_command(command):
    try:
        argv = shlex.split(str(command or ""))
    except Exception:
        return None
    if len(argv) >= 2 and argv[0] == "docker" and argv[1] == "exec":
        index = 2
    elif len(argv) >= 3 and argv[0] == "docker" and argv[1] == "container" and argv[2] == "exec":
        index = 3
    else:
        return None

    while index < len(argv):
        token = argv[index]
        if token == "--":
            index += 1
            break
        if not token.startswith("-"):
            break
        if token in DOCKER_EXEC_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if any(token.startswith(opt + "=") for opt in DOCKER_EXEC_OPTIONS_WITH_VALUE if opt.startswith("--")):
            index += 1
            continue
        if len(token) > 2 and token[:2] in {"-e", "-u", "-w"}:
            index += 1
            continue
        if token in DOCKER_EXEC_FLAGS or (token.startswith("-") and set(token[1:]).issubset({"i", "t"})):
            index += 1
            continue
        index += 1

    if index >= len(argv):
        return None
    container = argv[index]
    inner_argv = argv[index + 1:]
    if not container or not inner_argv:
        return None
    return {"container": container, "inner_argv": inner_argv}


def shell_payload_from_argv(argv):
    if not argv:
        return ""
    shell_name = Path(str(argv[0])).name
    if shell_name not in {"sh", "bash", "dash", "ash"}:
        return ""
    for index, token in enumerate(argv[1:], 1):
        if token == "-c" or (token.startswith("-") and "c" in token[1:]):
            if index + 1 < len(argv):
                return str(argv[index + 1] or "").strip()
            return ""
    return ""


def docker_exec_match_patterns(inner_argv):
    patterns = []
    payload = shell_payload_from_argv(inner_argv)
    candidates = [payload] if payload else [" ".join(str(part) for part in inner_argv)]

    for candidate in candidates:
        candidate = str(candidate or "").strip()
        if not candidate:
            continue
        patterns.append(candidate)
        try:
            payload_argv = shlex.split(candidate)
        except Exception:
            payload_argv = []
        if payload_argv and payload_argv[0] == "exec":
            payload_argv = payload_argv[1:]
        if payload_argv:
            joined = " ".join(payload_argv)
            patterns.append(joined)
            if len(payload_argv) >= 2 and ("/" in payload_argv[1] or payload_argv[0].startswith(("python", "ffmpeg"))):
                patterns.append(" ".join(payload_argv[:2]))

    clean = []
    for pattern in patterns:
        pattern = " ".join(str(pattern or "").split())
        if len(pattern) >= 8 and pattern not in clean:
            clean.append(pattern)
    return clean


def ps_process_rows():
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,pgid=,sid=,comm=,args="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []

    rows = []
    for raw in (result.stdout or "").splitlines():
        parts = raw.strip().split(None, 5)
        if len(parts) < 6:
            continue
        rows.append({
            "pid": safe_int(parts[0], 0),
            "ppid": safe_int(parts[1], 0),
            "pgid": safe_int(parts[2], 0),
            "sid": safe_int(parts[3], 0),
            "comm": parts[4],
            "args": parts[5],
        })
    return rows


def terminate_process_group(pgid, label, messages, term_wait=2.0):
    pgid = safe_int(pgid, 0)
    if pgid <= 1:
        return False

    killed = False
    if process_group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGTERM)
            killed = True
            messages.append(f"SIGTERM envoye au groupe {pgid} ({label})")
        except ProcessLookupError:
            messages.append(f"Groupe {pgid} deja termine ({label})")
        except Exception as e:
            messages.append(f"Erreur SIGTERM groupe {pgid} ({label}): {e}")

    deadline = time.monotonic() + max(0.1, term_wait)
    while time.monotonic() < deadline:
        if not process_group_alive(pgid):
            break
        time.sleep(0.1)

    if process_group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
            killed = True
            messages.append(f"SIGKILL envoye au groupe {pgid} ({label})")
        except ProcessLookupError:
            messages.append(f"Groupe {pgid} deja termine ({label})")
        except Exception as e:
            messages.append(f"Erreur SIGKILL groupe {pgid} ({label}): {e}")
    return killed


def docker_exec_ids(container):
    if not container or not command_exists("docker"):
        return []
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{json .ExecIDs}}", str(container)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    raw = (result.stdout or "").strip()
    if not raw or raw in {"<no value>", "null"}:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    return [str(item) for item in data or [] if str(item or "").strip()]


def docker_inspect_exec(exec_id):
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{json .}}", str(exec_id)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return {}
    if result.returncode != 0:
        return {}
    try:
        return json.loads((result.stdout or "").strip() or "{}")
    except Exception:
        return {}


def docker_exec_process_text(info):
    process_config = info.get("ProcessConfig") if isinstance(info, dict) else {}
    if not isinstance(process_config, dict):
        return ""
    parts = []
    entrypoint = process_config.get("entrypoint") or process_config.get("Entrypoint") or ""
    if isinstance(entrypoint, list):
        parts.extend(str(item) for item in entrypoint)
    elif entrypoint:
        parts.append(str(entrypoint))
    args = process_config.get("arguments") or process_config.get("Args") or process_config.get("args") or []
    if isinstance(args, list):
        parts.extend(str(item) for item in args)
    elif args:
        parts.append(str(args))
    return " ".join(parts)


def terminate_docker_exec_runtime(command, messages):
    parsed = parse_docker_exec_command(command)
    if not parsed:
        return False

    container = parsed["container"]
    patterns = docker_exec_match_patterns(parsed["inner_argv"])
    if not patterns:
        return False

    killed = False
    matched_groups = set()

    for exec_id in docker_exec_ids(container):
        info = docker_inspect_exec(exec_id)
        if not info:
            continue
        if info.get("Running") is False:
            continue
        process_text = docker_exec_process_text(info)
        if process_text and not any(pattern in process_text for pattern in patterns):
            continue
        exec_pid = safe_int(info.get("Pid") or info.get("PID") or info.get("pid"), 0)
        if exec_pid > 1:
            try:
                exec_pgid = os.getpgid(exec_pid)
            except Exception:
                exec_pgid = exec_pid
            if exec_pgid > 1 and exec_pgid not in matched_groups:
                matched_groups.add(exec_pgid)
                killed = terminate_process_group(exec_pgid, f"docker exec {container}", messages, term_wait=2.0) or killed

    if not matched_groups:
        for row in ps_process_rows():
            args = row.get("args") or ""
            if not any(pattern in args for pattern in patterns):
                continue
            pgid = safe_int(row.get("pgid"), 0)
            if pgid > 1 and pgid not in matched_groups:
                matched_groups.add(pgid)
                killed = terminate_process_group(pgid, f"processus docker exec {container}", messages, term_wait=2.0) or killed

    if matched_groups:
        messages.append(f"Runtime docker exec cible : {container} ({len(matched_groups)} groupe(s))")
    return killed


def task_runtime_alive(task_id, status=None):
    """Retourne True seulement si le statut running correspond à un vrai runtime vivant."""
    status = status or get_status(task_id)
    if safe_int(status.get("running"), 0) != 1:
        return False

    session_name = status.get("tmux_session") or ""
    if session_name and tmux_session_alive(session_name):
        return True

    pgid = safe_int(status.get("process_pgid"), 0)
    if pgid > 1 and process_group_alive(pgid):
        return True

    pid = safe_int(status.get("process_pid"), 0)
    if pid > 1 and process_alive(pid):
        return True

    return False


def cleanup_stale_task_runtime(task_id, source="Système"):
    """Nettoie un running/lock ancien quand plus aucun tmux/PID n'existe."""
    status = get_status(task_id)
    state = str(status.get("status") or "").lower()
    if "lancement tmux" in state:
        launch_stamp = parse_task_datetime(status.get("updated_at") or status.get("last_run"))
        if launch_stamp and (datetime.now() - launch_stamp).total_seconds() < 10:
            return False

    if safe_int(status.get("running"), 0) != 1:
        # Même si SQLite est propre, on peut supprimer un vieux fichier lock non verrouillé.
        stale_path = status.get("lock_path") or str(task_lock_path(task_id))
    elif task_runtime_alive(task_id, status):
        return False
    else:
        stale_path = status.get("lock_path") or str(task_lock_path(task_id))
        msg = "🧹 Ancien état en cours nettoyé : plus aucune session tmux/PID vivant."
        add_log(task_id, msg, source)
        set_status(
            task_id,
            running=0,
            status="⚠️ Runtime nettoyé",
            last_end=now_str(),
            source=source,
            result="Nettoyé",
            last_message=msg,
        )
        clear_task_runtime(task_id)

    try:
        lock_file = Path(stale_path)
        if lock_file.exists() and fcntl is not None:
            with lock_file.open("a+", encoding="utf-8") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    lock_file.unlink(missing_ok=True)
                except BlockingIOError:
                    pass
    except Exception:
        pass
    return True


def cleanup_legacy_global_lock():
    """Supprime l'ancien verrou global seulement s'il n'est plus tenu par un ancien runner."""
    try:
        conf = read_task_conf()
        lock_path = Path(conf.get("TASK_LOCK_FILE") or DEFAULTS["TASK_LOCK_FILE"])
        if not lock_path.exists() or fcntl is None:
            return
        with lock_path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                handle.seek(0)
                handle.truncate()
                handle.flush()
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                lock_path.unlink(missing_ok=True)
            except BlockingIOError:
                pass
    except Exception:
        pass


def acquire_task_runner_lock(task_id, source=""):
    """Verrou par tâche : bloque seulement un double lancement de la même tâche."""
    lock_path = task_lock_path(task_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None:
        # Cas très rare : on garde une protection minimale dans le processus courant.
        add_log(task_id, "⚠️ fcntl indisponible : verrou par tâche limité au processus courant.", source)
        RUNNER_THREAD_LOCK.acquire()
        return {"mode": "thread", "handle": None, "path": str(lock_path)}

    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            msg = "⛔ Cette tâche est déjà en cours dans une autre session."
            add_log(task_id, msg, source)
            set_status(task_id, running=1, status="⛔ Déjà en cours", source=source, result="Déjà en cours", last_message=msg)
            handle.close()
            return None

        handle.seek(0)
        handle.truncate()
        handle.write(
            f"task_id={task_id}\n"
            f"source={source}\n"
            f"pid={os.getpid()}\n"
            f"acquired_at={now_str()}\n"
        )
        handle.flush()
        return {"mode": "file", "handle": handle, "path": str(lock_path)}
    except Exception:
        try:
            handle.close()
        except Exception:
            pass
        raise


def release_task_runner_lock(lock_handle):
    if not lock_handle:
        return

    mode = lock_handle.get("mode")
    handle = lock_handle.get("handle")
    path = lock_handle.get("path")

    if mode == "thread":
        try:
            RUNNER_THREAD_LOCK.release()
        except Exception:
            pass
        return

    if mode == "file" and handle is not None:
        try:
            try:
                handle.seek(0)
                handle.truncate()
                handle.flush()
            except Exception:
                pass
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        finally:
            try:
                handle.close()
            except Exception:
                pass
            try:
                if path:
                    Path(path).unlink(missing_ok=True)
            except Exception:
                pass


def resolve_python_command(conf):
    python_bin = conf.get("PYTHON_BIN") or default_python_bin()
    if os.path.isabs(python_bin):
        return python_bin
    if python_bin_uses_path_lookup(python_bin):
        return python_bin
    return str(Path(APP_DIR) / python_bin)


def build_worker_shell_command(task_id, source):
    conf = read_task_conf()
    python_bin = resolve_python_command(conf)
    runner_file = cron_runner_file()
    return "cd {app_dir} && exec {python_bin} {runner} --worker-run-task {task_id} --source {source}".format(
        app_dir=shlex.quote(str(APP_DIR)),
        python_bin=shlex.quote(str(python_bin)),
        runner=shlex.quote(str(runner_file)),
        task_id=safe_int(task_id, 0),
        source=shlex.quote(str(source or "CLI")),
    )


def launch_task_tmux(task_id, source="Manuel"):
    """Lance une tâche dans une session tmux détachée."""
    init_db()
    cleanup_legacy_global_lock()
    task = get_task(task_id)
    if not task:
        return False, "Tâche introuvable"

    commands = [cmd.strip() for cmd in task.get("commands", []) if cmd.strip()]
    if not commands:
        msg = "Aucune commande configurée."
        add_log(task_id, msg, source)
        set_status(task_id, running=0, status="⚠️ Aucune commande", source=source, result="Ignoré", last_end=now_str(), last_message=msg)
        return False, msg

    cleanup_stale_task_runtime(task_id, source)
    current = get_status(task_id)
    if task_runtime_alive(task_id, current):
        msg = "Tâche déjà en cours."
        add_log(task_id, f"Lancement ignoré : {msg}", source)
        return False, msg

    title = task.get("title") or f"Tâche {task_id}"
    session_name = task_tmux_session_name(task_id, title)
    unit_name = task_systemd_unit_name(task_id, title)
    lock_file = task_lock_path(task_id)

    if tmux_session_alive(session_name):
        msg = f"Session tmux {session_name} déjà active."
        set_status(task_id, running=1, status="⛔ Déjà en cours", source=source, result="Déjà en cours", last_message=msg)
        set_task_runtime(task_id, tmux_session=session_name, systemd_unit=unit_name, lock_path=str(lock_file))
        add_log(task_id, msg, source)
        return False, msg

    set_status(
        task_id,
        running=1,
        status="🚀 Lancement tmux",
        last_run=now_str(),
        last_end="—",
        source=source,
        result="En cours",
        last_message=f"Session tmux : {session_name}",
    )
    set_task_runtime(task_id, 0, 0, 0, session_name, unit_name, str(lock_file))
    add_log(task_id, "=" * 90, source)
    add_log(task_id, f"LANCEMENT TMUX - {title} - session={session_name} - source={source}", source)

    shell_bin = read_task_conf().get("SHELL_BIN") or "/bin/bash"
    worker_cmd = build_worker_shell_command(task_id, source)
    tmux_cmd = ["tmux", "new-session", "-d", "-s", session_name, shell_bin, "-lc", worker_cmd]

    if not command_exists("tmux"):
        msg = "Commande tmux introuvable : tâche non lancée."
        add_log(task_id, msg, source)
        set_status(task_id, running=0, status="❌ Erreur", last_end=now_str(), source=source, result="Erreur", last_message=msg)
        clear_task_runtime(task_id)
        return False, msg

    # Ne pas envelopper tmux new-session -d dans systemd-run : systemd considère
    # la commande terminée dès que tmux s'est détaché et peut nettoyer le cgroup,
    # tuant alors le serveur tmux avant que le worker n'écrive ses vrais logs.
    launcher_used = "tmux direct"
    launch_result = subprocess.run(tmux_cmd, capture_output=True, text=True, timeout=20)

    if launch_result.returncode != 0:
        msg = (launch_result.stderr or launch_result.stdout or "Échec lancement tmux").strip()
        add_log(task_id, f"❌ {msg}", source)
        set_status(task_id, running=0, status="❌ Erreur", last_end=now_str(), source=source, result="Erreur", last_message=msg[:500])
        clear_task_runtime(task_id)
        return False, msg

    for _ in range(15):
        if tmux_session_alive(session_name):
            break
        finished = task_finished_after_launch(task_id, source)
        if finished is not None:
            ok, final_message = finished
            add_log(task_id, f"ℹ️ Session tmux déjà terminée, statut worker conservé : {final_message}", source)
            clear_task_runtime(task_id)
            return ok, final_message
        time.sleep(0.2)
    if not tmux_session_alive(session_name):
        finished = task_finished_after_launch(task_id, source)
        if finished is not None:
            ok, final_message = finished
            add_log(task_id, f"ℹ️ Session tmux terminée avant détection, statut worker conservé : {final_message}", source)
            clear_task_runtime(task_id)
            return ok, final_message
        msg = f"Session tmux {session_name} non détectée après lancement."
        add_log(task_id, f"❌ {msg}", source)
        set_status(task_id, running=0, status="❌ Erreur", last_end=now_str(), source=source, result="Erreur", last_message=msg)
        clear_task_runtime(task_id)
        return False, msg

    pane_pid = tmux_session_pane_pid(session_name)
    set_task_runtime(task_id, pane_pid, pane_pid, 0, session_name, unit_name, str(lock_file))
    msg = f"Tâche lancée dans {launcher_used} : session {session_name}"
    add_log(task_id, f"✅ {msg} PID tmux={pane_pid or '—'}", source)
    set_status(task_id, running=1, status="🔄 En cours", source=source, result="En cours", last_message=msg)
    return True, msg


def force_stop_task(task_id, source="Interface"):
    """Arrêt forcé d'une tâche lancée dans tmux."""
    task = get_task(task_id)
    if not task:
        return False, "Tâche introuvable"

    status = get_status(task_id)
    pid = safe_int(status.get("process_pid"), 0)
    pgid = safe_int(status.get("process_pgid"), 0)
    session_name = status.get("tmux_session") or ""
    was_running = safe_int(status.get("running"), 0) == 1

    request_task_stop(task_id)
    add_log(
        task_id,
        f"⛔ Demande d'arrêt forcé depuis {source}. tmux={session_name or '—'} PID={pid or '—'} PGID={pgid or '—'}",
        source,
    )

    killed = False
    messages = []

    command = current_task_command(task, status)
    if command:
        killed = terminate_docker_exec_runtime(command, messages) or killed

    if session_name:
        tmux_killed, tmux_msg = kill_tmux_session(session_name)
        messages.append(tmux_msg)
        killed = killed or tmux_killed
        time.sleep(0.2)

    if pgid > 1 and process_group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGTERM)
            killed = True
            messages.append(f"SIGTERM envoyé au groupe {pgid}")
        except ProcessLookupError:
            messages.append(f"Groupe {pgid} déjà terminé")
        except Exception as e:
            messages.append(f"Erreur SIGTERM groupe {pgid}: {e}")

        for _ in range(20):
            if not process_group_alive(pgid):
                break
            time.sleep(0.1)

        if process_group_alive(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
                killed = True
                messages.append(f"SIGKILL envoyé au groupe {pgid}")
            except ProcessLookupError:
                messages.append(f"Groupe {pgid} déjà terminé")
            except Exception as e:
                messages.append(f"Erreur SIGKILL groupe {pgid}: {e}")

    elif pid > 1 and process_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            killed = True
            messages.append(f"SIGTERM envoyé au PID {pid}")
        except ProcessLookupError:
            messages.append(f"PID {pid} déjà terminé")
        except Exception as e:
            messages.append(f"Erreur SIGTERM PID {pid}: {e}")

        for _ in range(20):
            if not process_alive(pid):
                break
            time.sleep(0.1)

        if process_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
                killed = True
                messages.append(f"SIGKILL envoyé au PID {pid}")
            except ProcessLookupError:
                messages.append(f"PID {pid} déjà terminé")
            except Exception as e:
                messages.append(f"Erreur SIGKILL PID {pid}: {e}")

    if not messages:
        messages.append("Aucun tmux/PID actif connu. Statut SQLite remis à zéro.")

    message = "; ".join([m for m in messages if m]) or "Arrêt forcé demandé."
    add_log(task_id, message, source)

    set_status(
        task_id,
        running=0,
        status="⛔ Arrêté",
        last_end=now_str(),
        source=source,
        result="Arrêté",
        last_message=message,
    )

    clear_task_runtime(task_id)
    cleanup_stale_task_runtime(task_id, source)

    return True, message if (was_running or killed) else "Aucun runtime actif, état nettoyé."


