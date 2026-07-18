BUILD_TMUX_ACTIONS = {"build_one", "build_all", "build_registry_one", "build_registry_all"}


def build_tmux_safe_part(value: str, default: str = "all") -> str:
    value = normalize_item_name(str(value or "").strip())
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-")
    return value or default


def build_tmux_session_name(action: str, name: str) -> str:
    if action in {"build_one", "build_registry_one"}:
        base = build_tmux_safe_part(name, "docker")
    else:
        base = "all"
    suffix = "main" if action in {"build_registry_one", "build_registry_all"} else "build"
    session = f"{base}-{suffix}"
    # tmux accepte les points/tirets/underscores, mais on garde un nom court et lisible.
    return session[:90]


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


def tmux_session_exists(session: str) -> bool:
    session = str(session or "").strip()
    if not session or not tmux_available():
        return False
    rc, _out = run_capture(["tmux", "has-session", "-t", session])
    return rc == 0


def systemd_run_available() -> bool:
    return shutil.which("systemd-run") is not None


def systemd_unit_is_active(unit: str) -> bool:
    unit = str(unit or "").strip()
    if not unit or shutil.which("systemctl") is None:
        return False
    try:
        completed = subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=5,
        )
        return completed.returncode == 0
    except Exception:
        return False


def build_tmux_systemd_unit_name(session: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(session or "build")).strip(".-_") or "build"
    return f"yoleo-build-{safe}"[:120]


def launch_build_tmux_worker(state: Dict[str, object]) -> Tuple[bool, str]:
    """Lance uniquement le build Docker dans une session tmux autonome.

    Point important : la session tmux ne doit pas être enfant du service
    Flask/Gunicorn. Sinon systemctl restart flask-system tue le cgroup complet
    et emmène tmux/buildx avec lui. Sur Debian/systemd, on lance donc un
    service transitoire avec systemd-run, qui démarre tmux puis attend la fin
    de la session. Flask redevient seulement le panneau de contrôle.

    Les imports TAR -> registre restent en subprocess/thread Flask car ils sont courts.
    Le worker écrit dans le même JSON d'état et dans les mêmes logs que l'ancien thread.
    """
    if not tmux_available():
        return False, "tmux introuvable. Installe tmux ou relance le service après installation."

    session = str(state.get("tmux_session") or "").strip()
    if not session:
        return False, "Nom de session tmux vide."

    if tmux_session_exists(session):
        return True, f"Session tmux déjà active : {session}"

    python_bin = sys.executable or "python3"
    script_path = os.path.abspath(__file__)
    cmd = [
        python_bin,
        script_path,
        "--build-worker",
        "--job-id", str(state.get("id") or ""),
        "--action", str(state.get("action") or ""),
        "--mode", str(state.get("mode") or "normal"),
    ]
    name = str(state.get("name") or "").strip()
    if name:
        cmd.extend(["--name", name])

    worker_cmd = "exec " + shlex.join(cmd)

    # Méthode propre : systemd-run crée une vraie unité indépendante de Flask.
    # Le wrapper garde l'unité active tant que la session tmux existe.
    if systemd_run_available():
        unit = build_tmux_systemd_unit_name(session)
        state["systemd_unit"] = f"{unit}.service"
        run_capture(["systemctl", "reset-failed", f"{unit}.service"])
        wrapper = "\n".join([
            "set -u",
            f"SESSION={shlex.quote(session)}",
            "if tmux has-session -t \"$SESSION\" 2>/dev/null; then",
            "  exit 0",
            "fi",
            "tmux new-session -d -s \"$SESSION\" " + shlex.quote(worker_cmd),
            "rc=$?",
            "if [ $rc -ne 0 ]; then",
            "  exit $rc",
            "fi",
            "while tmux has-session -t \"$SESSION\" 2>/dev/null; do",
            "  sleep 2",
            "done",
        ])
        rc, out = run_capture([
            "systemd-run",
            "--unit", unit,
            "--description", f"Yoleo Build tmux {session}",
            "--collect",
            "--property=Type=simple",
            "--property=Restart=no",
            "/bin/bash", "-lc", wrapper,
        ])
        if rc == 0:
            # systemd-run rend la main immédiatement : on attend juste que tmux apparaisse.
            for _ in range(20):
                if tmux_session_exists(session):
                    return True, f"Build lancé dans tmux autonome : {session} ({unit}.service)"
                time.sleep(0.15)
            return True, f"Build lancé via systemd-run : {unit}.service"
        # On ne masque pas l'erreur systemd-run, mais on tente l'ancien mode
        # pour ne pas bloquer totalement une machine sans systemd opérationnel.
        systemd_error = out.strip() or f"systemd-run impossible pour {unit}.service"
    else:
        systemd_error = "systemd-run introuvable"

    # Fallback : compatible, mais non protégé contre un restart du service Flask.
    rc, out = run_capture(["tmux", "new-session", "-d", "-s", session, worker_cmd])
    if rc != 0:
        return False, (out.strip() or systemd_error or f"Impossible de lancer la session tmux : {session}")
    return True, f"Build lancé dans tmux direct : {session} (⚠️ {systemd_error}; non isolé du cgroup Flask)"


def run_build_worker_from_cli(job_id: str, action: str, name: str, mode: str) -> int:
    conf = get_config()
    state = read_build_background_state(conf)
    if not isinstance(state, dict) or state.get("id") != job_id:
        state = {
            "id": job_id or uuid.uuid4().hex,
            "action": action,
            "name": name,
            "mode": mode,
            "log_path": build_background_default_log_path(conf, action),
            "log_tail": "",
            "progress": 0,
            "progress_seq": 0,
            "progress_events": [],
            "failed": 0,
            "had_error": False,
            "running": True,
            "done": False,
            "success": None,
            "already_running": False,
            "started_at": build_background_now(),
        }

    session = build_tmux_session_name(action, name)
    state.update({
        "pid": os.getpid(),
        "pid_start": current_process_start_token(),
        "boot_id": current_boot_id(),
        "tmux_session": session,
        "systemd_unit": build_tmux_systemd_unit_name(session) + ".service",
        "runner": "tmux-systemd",
        "running": True,
        "done": False,
        "updated_at": build_background_now(),
        "status": f"Build en cours dans tmux : {session}",
    })
    write_build_background_state(conf, state)
    run_build_background_job(conf, state)
    return 0



def stream_build_tmux_status(conf: Dict[str, str], action: str, name: str, mode: str) -> Iterator[str]:
    """Compat route /builds/run_stream : ne lance plus de build dans Flask.

    La route démarre ou réutilise la session tmux, puis ne fait que suivre
    l'état JSON/log_tail. Si Flask/Gunicorn tombe, le build continue dans tmux.
    """
    state = start_build_background_job(conf, action, name, mode)
    last_tail = ""

    initial = str(state.get("log_tail") or "")
    if initial:
        yield initial
        last_tail = initial
    else:
        yield f"Build lancé dans tmux : {state.get('tmux_session', '')}\n"

    # Tant que la tâche est active, on suit le log sans porter le process build.
    while True:
        time.sleep(1)
        state = read_build_background_state(conf)
        tail = str(state.get("log_tail") or "")
        if tail != last_tail:
            if last_tail and tail.startswith(last_tail):
                yield tail[len(last_tail):]
            else:
                yield "\n--- reprise du log tmux ---\n" + tail
            last_tail = tail
        if not state.get("running"):
            break


BACKGROUND_TAIL_CHARS = 70000
BACKGROUND_STALE_SECONDS_DEFAULT = 7200


def build_background_state_path(conf: Dict[str, str]) -> str:
    log_dir = conf.get("DOCKER_LOG_DIR") or tempfile.gettempdir()
    return os.path.join(log_dir, "builds_background_job.json")


def build_background_default_log_path(conf: Dict[str, str], action: str) -> str:
    log_dir = conf.get("DOCKER_LOG_DIR") or tempfile.gettempdir()
    action_s = str(action or "")
    if action_s in {"build_registry_one", "build_registry_all"}:
        return os.path.join(log_dir, "build_main.log")
    if action_s.startswith("build_"):
        return os.path.join(log_dir, "builds_python.log")
    if "registry" in action_s and not action_s.startswith("registry_service_"):
        return os.path.join(log_dir, "registry_python.log")
    return os.path.join(log_dir, "builds_background.log")


def build_background_now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def current_boot_id() -> str:
    try:
        return local_read_text("/proc/sys/kernel/random/boot_id").strip()
    except Exception:
        return ""


def pid_is_alive(pid_value) -> bool:
    try:
        pid = int(pid_value or 0)
    except Exception:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return False


def process_start_token(pid_value) -> str:
    try:
        pid = int(pid_value or 0)
        if pid <= 0:
            return ""
        stat = local_read_text(f"/proc/{pid}/stat").strip()
        if not stat:
            return ""
        parts = stat.split()
        return parts[21] if len(parts) > 21 else ""
    except Exception:
        return ""


def current_process_start_token() -> str:
    return process_start_token(os.getpid())


def parse_background_timestamp(value: str) -> float:
    value = str(value or "").strip()
    if not value:
        return 0.0
    for fmt in ("%Y-%m-%d %H:%M:%S",):
        try:
            return time.mktime(time.strptime(value, fmt))
        except Exception:
            pass
    return 0.0


def background_stale_seconds(conf: Dict[str, str]) -> int:
    try:
        return max(60, int(str(conf.get("BACKGROUND_STALE_SECONDS", BACKGROUND_STALE_SECONDS_DEFAULT)).strip()))
    except Exception:
        return BACKGROUND_STALE_SECONDS_DEFAULT


def background_state_stale_reason(conf: Dict[str, str], state: Dict) -> str:
    if not isinstance(state, dict) or not state.get("running"):
        return ""

    tmux_session = str(state.get("tmux_session") or "").strip()
    if tmux_session and tmux_session_exists(tmux_session):
        # Le build est volontairement détaché de Flask : tant que tmux vit,
        # on ne marque pas la tâche comme interrompue juste parce que le PID Flask change.
        return ""
    if str(state.get("runner") or "") == "tmux-systemd" and tmux_session:
        unit = str(state.get("systemd_unit") or "").strip()
        if unit and systemd_unit_is_active(unit):
            return ""
        updated_at = parse_background_timestamp(str(state.get("updated_at") or ""))
        if updated_at <= 0 or (time.time() - updated_at) > 15:
            return f"session tmux absente ({tmux_session}) et unite systemd inactive ({unit or 'absente'})"

    saved_boot = str(state.get("boot_id") or "").strip()
    boot_now = current_boot_id()
    if not saved_boot:
        return "ancien etat background sans identifiant de boot"
    if saved_boot and boot_now and saved_boot != boot_now:
        return "redemarrage Linux detecte"

    pid = state.get("pid")
    if not pid_is_alive(pid):
        return f"processus Flask termine (pid {pid or 'absent'})"

    saved_pid_start = str(state.get("pid_start") or "").strip()
    current_pid_start = process_start_token(pid)
    if saved_pid_start and current_pid_start and saved_pid_start != current_pid_start:
        return f"pid {pid} reutilise par un autre processus"
    if not saved_pid_start:
        return "ancien etat background sans identifiant de processus"

    updated_at = parse_background_timestamp(str(state.get("updated_at") or ""))
    if updated_at > 0:
        age = time.time() - updated_at
        if age > background_stale_seconds(conf):
            return f"aucune progression depuis {int(age)} secondes"
    elif state.get("started_at"):
        started_at = parse_background_timestamp(str(state.get("started_at") or ""))
        if started_at > 0 and (time.time() - started_at) > background_stale_seconds(conf):
            return "horodatage de progression absent"

    return ""


def normalize_background_state(conf: Dict[str, str], state: Dict) -> Dict:
    if not isinstance(state, dict):
        return {}
    reason = background_state_stale_reason(conf, state)
    if not reason:
        return state

    fixed = dict(state)
    fixed["running"] = False
    fixed["done"] = True
    fixed["success"] = False
    fixed["had_error"] = True
    fixed["already_running"] = False
    fixed["stale"] = True
    fixed["stale_reason"] = reason
    fixed["updated_at"] = build_background_now()
    fixed["status"] = f"Ancienne tache interrompue : {reason}. Tu peux relancer."
    tail = str(fixed.get("log_tail") or "")
    fixed["log_tail"] = (tail + f"\n⚠️ Ancienne tache interrompue : {reason}. Verrou libere, relance possible.\n")[-BACKGROUND_TAIL_CHARS:]
    return fixed


def read_build_background_state(conf: Dict[str, str]) -> Dict:
    path = build_background_state_path(conf)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return {}
        fixed = normalize_background_state(conf, data)
        reconciled = reconcile_build_background_success(fixed)
        if (fixed is not data and fixed != data) or reconciled != fixed:
            write_build_background_state(conf, reconciled)
        return reconciled
    except Exception:
        return {}


def write_build_background_state(conf: Dict[str, str], state: Dict) -> None:
    path = build_background_state_path(conf)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = f"{path}.{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
    except Exception:
        pass



def stream_cache_refresh_action(conf: Dict[str, str]) -> Iterator[str]:
    os.makedirs(conf["DOCKER_LOG_DIR"], exist_ok=True)
    log_path = os.path.join(conf["DOCKER_LOG_DIR"], "build_cache.log")
    try:
        open(log_path, "w", encoding="utf-8").close()
    except Exception:
        pass
    logger = StreamLogger(log_path)
    try:
        cache_path = build_cache_path(conf)
        yield logger.line("🔄 Mise à jour manuelle du cache Build")
        yield logger.line(f"📁 Cache : {cache_path}")
        yield logger.line(f"📁 Builds : {conf.get('DOCKER_BUILDS_DIR', '')}")
        yield logger.line(f"📦 TAR    : {conf.get('DOCKER_TAR_DIR', '')}")
        yield logger.line("-------------------------------------------------------")

        # Mise à jour manuelle = vrai point de cohérence.
        # Le dossier des builds est le point zéro : on aligne la mini-base dessus
        # et on supprime seulement ici les TAR orphelins. L'affichage normal reste
        # basé sur build.jdom pour ne pas rescanner les dossiers à chaque clic.
        try:
            sync_ok, sync_message, sync_stats = sync_build_database_and_optional_tars_from_dirs(
                conf,
                remove_orphan_tars=True,
            )
            if sync_ok:
                yield logger.line(f"🧩 {sync_message}")
                added = sync_stats.get("added_db") or []
                removed_db = sync_stats.get("removed_db") or []
                removed_tars = sync_stats.get("removed_tars") or []
                if added:
                    yield logger.line("Base complétée : " + ", ".join(list(added)[:20]))
                if removed_db:
                    yield logger.line("Base nettoyée : " + ", ".join(list(removed_db)[:20]))
                if removed_tars:
                    yield logger.line("TAR orphelins supprimés : " + ", ".join(list(removed_tars)[:20]))
            else:
                yield logger.line(f"⚠️ {sync_message}")
        except Exception as exc:
            yield logger.line(f"⚠️ Synchronisation dossiers/base/TAR ignorée : {exc}")

        try:
            registry, platforms, modes, names = build_inventory_sources(conf)
        except Exception as exc:
            yield logger.line(f"❌ Lecture des sources impossible : {exc}")
            yield logger.line(f"@@PROGRESS {json.dumps({'action': 'cache', 'current': 0, 'total': 1, 'percent': 100, 'done': 0, 'failed': 1}, ensure_ascii=False)}")
            return

        total = len(names)
        yield logger.line(f"Entrées détectées : {total}")
        yield logger.line(f"@@PROGRESS {json.dumps({'action': 'cache', 'current': 0, 'total': max(total, 1), 'percent': 0, 'done': 0, 'failed': 0}, ensure_ascii=False)}")

        builds: List[Dict[str, object]] = []
        failed = 0
        if total == 0:
            summary = summarize_build_inventory(conf, builds)
            warning = build_inventory_warning(conf)
            ok, message = write_build_inventory_cache(conf, builds, summary, warning, source="manual")
            if ok:
                yield logger.line(f"✅ Cache initialisé vide : {message}")
                yield logger.line(f"@@PROGRESS {json.dumps({'action': 'cache', 'current': 1, 'total': 1, 'percent': 100, 'done': 1, 'failed': 0}, ensure_ascii=False)}")
            else:
                yield logger.line(f"❌ Écriture cache impossible : {message}")
                yield logger.line(f"@@PROGRESS {json.dumps({'action': 'cache', 'current': 1, 'total': 1, 'percent': 100, 'done': 0, 'failed': 1}, ensure_ascii=False)}")
            return

        for idx, name in enumerate(names, start=1):
            try:
                item = build_inventory_item(conf, name, registry, platforms, modes, check_registry=True)
                builds.append(item)
                done = idx - failed
                pct = int((idx / total) * 95)
                yield logger.line(f"{idx}/{total} {name} : cache OK")
                yield logger.line(f"@@PROGRESS {json.dumps({'action': 'cache', 'current': idx, 'total': total, 'percent': pct, 'done': done, 'failed': failed, 'name': name}, ensure_ascii=False)}")
            except Exception as exc:
                failed += 1
                pct = int((idx / total) * 95)
                yield logger.line(f"❌ {name} : {exc}")
                yield logger.line(f"@@PROGRESS {json.dumps({'action': 'cache', 'current': idx, 'total': total, 'percent': pct, 'done': idx - failed, 'failed': failed, 'name': name}, ensure_ascii=False)}")

        summary = summarize_build_inventory(conf, builds)
        warning = build_inventory_warning(conf)
        ok, message = write_build_inventory_cache(conf, builds, summary, warning, source="manual")
        if not ok:
            failed += 1
            yield logger.line(f"❌ Écriture cache impossible : {message}")
            yield logger.line(f"@@PROGRESS {json.dumps({'action': 'cache', 'current': total, 'total': total, 'percent': 100, 'done': max(0, total - failed), 'failed': failed}, ensure_ascii=False)}")
            return

        yield logger.line("-------------------------------------------------------")
        yield logger.line(f"✅ Cache Build mis à jour : {message}")
        yield logger.line(f"Entrées : {summary.get('total', 0)} | Dossiers : {summary.get('projects', 0)} | TAR : {summary.get('tars', 0)} | Registre : {summary.get('registry', 0)}")
        if warning:
            yield logger.line(f"⚠️ {warning}")
        yield logger.line(f"@@PROGRESS {json.dumps({'action': 'cache', 'current': total, 'total': total, 'percent': 100, 'done': total - failed, 'failed': failed}, ensure_ascii=False)}")
    finally:
        logger.close()

def build_background_generator(conf: Dict[str, str], action: str, name: str, mode: str) -> Iterator[str]:
    if action in {"build_registry_one", "build_registry_all"}:
        return stream_build_registry_action(conf, action, name, mode)
    if action in {"build_one", "build_all"}:
        return stream_build_action(conf, action, name, mode)
    if action in {"registry_one", "registry_all", "dry_registry_one", "dry_registry_all"}:
        return stream_registry_action(conf, action, name, dry_run=action.startswith("dry_"))
    if action.startswith("registry_service_"):
        return stream_registry_host_service_action(conf, action)
    if action == "cache_refresh":
        return stream_cache_refresh_action(conf)
    raise ValueError("Action inconnue.")


def append_progress_event(state: Dict, progress: Dict) -> None:
    """Conserve un petit historique des événements @@PROGRESS.

    Le front /build/main interroge l'état en polling. Avec un seul
    progress_payload, une phase courte comme registry_done peut être écrasée
    par le build suivant avant que le navigateur la voie. L'historique permet
    donc au tableau de rejouer les transitions manquées et d'afficher
    "Registre OK" ligne par ligne, au fur et à mesure.
    """
    try:
        seq = int(state.get("progress_seq") or 0) + 1
    except Exception:
        seq = 1
    state["progress_seq"] = seq

    event = {
        "seq": seq,
        "at": build_background_now(),
        "payload": progress,
    }
    events = state.get("progress_events")
    if not isinstance(events, list):
        events = []
    events.append(event)
    # On garde assez d'événements pour les gros lots, sans gonfler le JSON.
    state["progress_events"] = events[-240:]


def build_background_line_is_error(raw_line: str) -> bool:
    """Détecte une vraie erreur dans le flux background Build.

    Le log Docker/Build contient beaucoup de texte libre. Une détection trop
    large sur le mot "ERREUR" rendait la barre de progression rouge à cause
    de lignes de bilan parfaitement normales comme "Erreurs : 0" ou
    "0 erreur(s)". On garde donc les vrais marqueurs d'échec, mais on ignore
    explicitement les compteurs à zéro.
    """
    text = str(raw_line or "")
    lower = text.lower()
    upper = text.upper()

    # Bilan normal : ne doit jamais transformer un job réussi en erreur.
    if re.search(r"\bERREURS?\s*[:=]\s*0\b", upper):
        return False
    if re.search(r"\b0\s+ERREURS?\b", upper):
        return False
    if re.search(r"\b0\s+ERREUR\(S\)\b", upper):
        return False

    if "update-alternatives: error: no alternatives for" in lower:
        return False
    if re.search(r"\bliberror[-\w]*\b", lower):
        return False

    # Marqueurs d'échec explicites générés par Yoleo ou les commandes.
    if "❌" in text or "âŒ" in text:
        return True
    if "BUILDX ÉCHEC" in upper or "BUILDX ECHEC" in upper:
        return True
    if "BUILD PRINCIPAL TERMINÉ AVEC ERREURS" in upper or "BUILD PRINCIPAL TERMINE AVEC ERREURS" in upper:
        return True
    if re.search(r"\bERREURS?\s*[:=]\s*[1-9]", upper):
        return True
    if re.search(r"\bERROR\b", upper):
        return True

    return False


def build_background_plain_upper(value: str) -> str:
    text = str(value or "").upper()
    replacements = (
        ("À", "A"), ("Â", "A"), ("Ä", "A"),
        ("Ç", "C"),
        ("É", "E"), ("È", "E"), ("Ê", "E"), ("Ë", "E"),
        ("Î", "I"), ("Ï", "I"),
        ("Ô", "O"), ("Ö", "O"),
        ("Ù", "U"), ("Û", "U"), ("Ü", "U"),
    )
    for src, dst in replacements:
        text = text.replace(src, dst)
    return text


def build_background_has_clean_final_summary(state: Dict) -> bool:
    text = build_background_plain_upper(state.get("log_tail", ""))
    if not re.search(r"\bERREURS?\s*[:=]\s*0\b", text):
        return False
    success_markers = (
        "BUILD PRINCIPAL TERMINE",
        "BUILD TERMINE",
        "IMPORT REGISTRE TERMINE",
    )
    return any(marker in text for marker in success_markers)


def reconcile_build_background_success(state: Dict) -> Dict:
    if not isinstance(state, dict) or state.get("running") or state.get("stale"):
        return state
    action = str(state.get("action") or "")
    if action not in BUILD_TMUX_ACTIONS:
        return state
    if str(state.get("status") or "") == "Erreur Python.":
        return state

    failed = int(state.get("failed") or 0)
    progress = state.get("progress_payload") if isinstance(state.get("progress_payload"), dict) else {}
    progress_failed = int(progress.get("failed") or failed or 0)
    progress_percent = int(state.get("progress") or progress.get("percent") or 0)
    clean_progress = progress_failed == 0 and progress_percent >= 100

    if failed == 0 and clean_progress and build_background_has_clean_final_summary(state):
        fixed = dict(state)
        fixed["had_error"] = False
        fixed["success"] = True
        fixed["status"] = "Termine."
        return fixed
    return state


def update_build_background_from_text(state: Dict, text: str) -> None:
    if not text:
        return
    for raw_line in text.splitlines(True):
        line = raw_line.strip()
        if line.startswith("@@PROGRESS "):
            try:
                progress = json.loads(line[len("@@PROGRESS "):])
                if isinstance(progress, dict):
                    state["progress"] = int(progress.get("percent") or 0)
                    state["progress_payload"] = progress
                    state["failed"] = int(progress.get("failed") or 0)
                    append_progress_event(state, progress)
            except Exception:
                pass
            continue
        if build_background_line_is_error(raw_line):
            state["had_error"] = True
        state["log_tail"] = (state.get("log_tail", "") + raw_line)[-BACKGROUND_TAIL_CHARS:]


def run_build_background_job(conf: Dict[str, str], initial_state: Dict) -> None:
    state = dict(initial_state)
    action = state.get("action", "")
    name = state.get("name", "")
    mode = state.get("mode", "normal")
    try:
        generator = build_background_generator(conf, action, name, mode)
        for chunk in generator:
            state["updated_at"] = build_background_now()
            update_build_background_from_text(state, str(chunk or ""))
            write_build_background_state(conf, state)
        state["running"] = False
        state["done"] = True
        state["updated_at"] = build_background_now()
        state["progress"] = max(int(state.get("progress") or 0), 100)
        failed = int(state.get("failed") or 0)
        if failed == 0 and build_background_has_clean_final_summary(state):
            state["had_error"] = False
        state["success"] = failed == 0 and not bool(state.get("had_error"))
        force_cache_refresh = action in {"registry_service_delete_all_tags", "registry_service_clean_storage"}
        if (state["success"] or force_cache_refresh) and action != "cache_refresh":
            state["status"] = "Rafraîchissement du cache Build."
            update_build_background_from_text(state, "\n🔄 Rafraîchissement du cache Build après action UI...\n")
            if force_cache_refresh and not state["success"]:
                update_build_background_from_text(state, "⚠️ Action registre partiellement en erreur, mais le registre a peut-être changé : recalcul du cache forcé.\n")
            write_build_background_state(conf, state)
            if refresh_build_cache_silent(conf, source=f"job:{action}", check_registry=True):
                update_build_background_from_text(state, "✅ Cache Build mis à jour.\n")
            else:
                update_build_background_from_text(state, "⚠️ Cache Build non mis à jour automatiquement. Utilise le bouton manuel si besoin.\n")
        state["status"] = "Termine." if state["success"] else "Termine avec erreur."
    except Exception as exc:
        state["running"] = False
        state["done"] = True
        state["success"] = False
        state["had_error"] = True
        state["updated_at"] = build_background_now()
        state["status"] = "Erreur Python."
        update_build_background_from_text(state, f"\nErreur Python background : {exc}\n")
    finally:
        write_build_background_state(conf, state)


def start_build_background_job(conf: Dict[str, str], action: str, name: str, mode: str) -> Dict:
    current = read_build_background_state(conf)
    if current.get("running"):
        current["already_running"] = True
        return current

    state = {
        "id": uuid.uuid4().hex,
        "pid": os.getpid(),
        "pid_start": current_process_start_token(),
        "boot_id": current_boot_id(),
        "action": action,
        "name": name,
        "mode": mode,
        "log_path": build_background_default_log_path(conf, action),
        "log_tail": "",
        "progress": 0,
        "progress_seq": 0,
        "progress_events": [],
        "failed": 0,
        "had_error": False,
        "running": True,
        "done": False,
        "success": None,
        "already_running": False,
        "started_at": build_background_now(),
        "updated_at": build_background_now(),
        "status": "Demarrage.",
    }
    if action in BUILD_TMUX_ACTIONS:
        session = build_tmux_session_name(action, name)
        state["tmux_session"] = session
        state["systemd_unit"] = build_tmux_systemd_unit_name(session) + ".service"
        state["runner"] = "tmux-systemd"
        state["status"] = f"Demarrage tmux : {session}."
        state["log_tail"] = f"Lancement du build dans tmux : {session}\n"

        if tmux_session_exists(session):
            state["already_running"] = True
            state["status"] = f"Session tmux déjà active : {session}"
            write_build_background_state(conf, state)
            return state

        write_build_background_state(conf, state)
        ok_tmux, tmux_msg = launch_build_tmux_worker(state)
        if not ok_tmux:
            state["running"] = False
            state["done"] = True
            state["success"] = False
            state["had_error"] = True
            state["status"] = tmux_msg
            state["updated_at"] = build_background_now()
            state["log_tail"] = (state.get("log_tail", "") + f"❌ {tmux_msg}\n")[-BACKGROUND_TAIL_CHARS:]
            write_build_background_state(conf, state)
        else:
            state["status"] = tmux_msg
            state["updated_at"] = build_background_now()
            state["log_tail"] = (state.get("log_tail", "") + f"✅ {tmux_msg}\n")[-BACKGROUND_TAIL_CHARS:]
            write_build_background_state(conf, state)
        return state

    write_build_background_state(conf, state)
    thread = threading.Thread(target=run_build_background_job, args=(conf.copy(), state), daemon=True)
    thread.start()
    return state


def validate_build_background_request(action: str, name: str) -> Optional[str]:
    if action in {"build_one", "build_registry_one", "registry_one", "dry_registry_one"} and not is_valid_name(name):
        return "Nom Docker invalide."
    if action in {"build_one", "build_all", "build_registry_one", "build_registry_all"}:
        return None
    if action in {"registry_one", "registry_all", "dry_registry_one", "dry_registry_all"}:
        return None
    if action.startswith("registry_service_"):
        return None
    if action == "cache_refresh":
        return None
    return "Action inconnue."
