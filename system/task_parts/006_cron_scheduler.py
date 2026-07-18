def clean_cron_field(value, default="*"):
    """Nettoie un champ cron personnalisé sans le sur-valider.

    On accepte la syntaxe cron classique : *, /, -, virgules, chiffres et lettres.
    On refuse volontairement les espaces et les caractères shell dangereux, car ces
    champs sont recopiés dans le fichier crontab. Si la valeur est vide ou sale,
    on remet une étoile pour éviter de casser la ligne entière.
    """
    raw = str(value or "").strip()
    if not raw:
        return default
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789*/,-")
    if any(ch.isspace() for ch in raw):
        return default
    if any(ch not in allowed for ch in raw):
        return default
    return raw[:64]


def custom_cron_expr(task):
    fields = [
        clean_cron_field(task.get("custom_cron_minute"), "*"),
        clean_cron_field(task.get("custom_cron_hour"), "*"),
        clean_cron_field(task.get("custom_cron_day"), "*"),
        clean_cron_field(task.get("custom_cron_month"), "*"),
        clean_cron_field(task.get("custom_cron_weekday"), "*"),
    ]
    return " ".join(fields)


def schedule_to_cron(task):
    stype = task.get("schedule_type") or "manual"
    minute = safe_int(task.get("time_minute"), 0, 0, 59)
    hour = safe_int(task.get("time_hour"), 0, 0, 23)

    if stype == "manual":
        return ""

    if stype == "every_minutes":
        every = safe_int(task.get("every_minutes"), 5, 1, 59)
        return f"*/{every} * * * *"

    if stype == "every_hours":
        every = safe_int(task.get("every_hours"), 1, 1, 23)
        return f"{minute} */{every} * * *"

    if stype == "daily":
        return f"{minute} {hour} * * *"

    if stype == "week_days":
        days = normalize_week_days(task.get("week_days") or "")
        if not days:
            days = "*"
        return f"{minute} {hour} * * {days}"

    if stype == "monthly":
        day = safe_int(task.get("month_day"), 1, 1, 31)
        return f"{minute} {hour} {day} * *"

    if stype == "yearly":
        day = safe_int(task.get("month_day"), 1, 1, 31)
        month = safe_int(task.get("month"), 1, 1, 12)
        return f"{minute} {hour} {day} {month} *"

    if stype == "custom":
        return custom_cron_expr(task)

    return ""


def normalize_week_days(value):
    allowed = {"0", "1", "2", "3", "4", "5", "6"}
    days = []
    for part in str(value or "").replace(";", ",").split(","):
        item = part.strip()
        if item in allowed and item not in days:
            days.append(item)
    # Ordre humain : lundi -> dimanche.
    order = {"1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "0": 7}
    days.sort(key=lambda x: order[x])
    return ",".join(days)


def schedule_label(task):
    stype = task.get("schedule_type") or "manual"
    h = safe_int(task.get("time_hour"), 0, 0, 23)
    m = safe_int(task.get("time_minute"), 0, 0, 59)
    hm = f"{h:02d}:{m:02d}"

    if stype == "manual":
        return "Manuel uniquement"
    if stype == "every_minutes":
        return f"Toutes les {safe_int(task.get('every_minutes'), 5, 1, 59)} minutes"
    if stype == "every_hours":
        return f"Toutes les {safe_int(task.get('every_hours'), 1, 1, 23)} heures à minute {m:02d}"
    if stype == "daily":
        return f"Tous les jours à {hm}"
    if stype == "week_days":
        days = normalize_week_days(task.get("week_days") or "")
        if not days:
            return f"Tous les jours à {hm}"
        labels = [DAYS_LABELS.get(x, x) for x in days.split(",")]
        return f"{', '.join(labels)} à {hm}"
    if stype == "monthly":
        day = safe_int(task.get("month_day"), 1, 1, 31)
        return f"Le {day} de chaque mois à {hm}"
    if stype == "yearly":
        day = safe_int(task.get("month_day"), 1, 1, 31)
        month = safe_int(task.get("month"), 1, 1, 12)
        return f"Chaque année le {day:02d}/{month:02d} à {hm}"
    if stype == "custom":
        return f"Personnalisé : {custom_cron_expr(task)}"
    return SCHEDULE_LABELS.get(stype, stype)


def build_expected_cron_text(tasks=None, conf=None):
    """Construit le contenu exact attendu du fichier cron depuis SQLite.

    Cette fonction ne modifie rien : elle sert à comparer le fichier cron réel
    avec la base task.db. La base SQLite reste la source officielle.
    """
    conf = conf or read_task_conf()
    python_bin = conf["PYTHON_BIN"]
    tasks = tasks if tasks is not None else get_all_tasks()

    lines = [
        "SHELL=/bin/bash",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "",
        "# ==========================================================",
        "# TASK MANAGER - CRON GÉNÉRÉ AUTOMATIQUEMENT",
        "# Ne pas modifier à la main : modifier les tâches dans Flask.",
        f"# Source officielle : {conf['TASK_DB']}",
        "# Si ce fichier est modifié ou abîmé, Flask le recrée depuis SQLite.",
        "# ==========================================================",
        "",
    ]
    last_crons = {}

    for task in tasks:
        cron_expr = schedule_to_cron(task)
        commands = [c for c in task.get("commands", []) if c.strip()]

        if not task.get("enabled") or not cron_expr or not commands:
            last_crons[task["id"]] = ""
            continue

        title = str(task.get("title") or f"Task {task['id']}")
        runner_file = cron_runner_file()
        runner_dir = runner_file.parent
        runner = (
            f"cd {shlex.quote(str(runner_dir))} && "
            f"{shlex.quote(python_bin)} {shlex.quote(str(runner_file))} "
            f"--run-task {task['id']} --source Automatique"
        )
        log_file = str(Path(conf["LOG_DIR"]) / f"task_{task['id']}.cron.log")
        cron_line = f"{cron_expr} {runner} >> {shlex.quote(log_file)} 2>&1"

        lines.append(f"# ===== TASK_START {task['id']} =====")
        lines.append(f"# TASK_ID={task['id']}")
        lines.append(f"# TITLE={title}")
        lines.append(cron_line)
        lines.append(f"# ===== TASK_END {task['id']} =====")
        lines.append("")
        last_crons[task["id"]] = cron_line

    return "\n".join(lines).rstrip() + "\n", last_crons


def update_last_crons(last_crons):
    """Mémorise dans SQLite la ligne cron réellement générée pour chaque tâche."""
    with connect_db() as db:
        for task_id, cron_line in last_crons.items():
            db.execute("UPDATE tasks SET last_cron=? WHERE id=?", (cron_line, task_id))
        db.commit()


def write_cron_file(cron_text, conf=None):
    conf = conf or read_task_conf()
    cron_file = conf["CRON_FILE"]
    Path(cron_file).parent.mkdir(parents=True, exist_ok=True)
    Path(cron_file).write_text(cron_text, encoding="utf-8")
    return cron_file


def read_text_file(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    except Exception:
        return ""


def read_installed_crontab():
    """Lit le crontab actuellement installé dans le conteneur."""
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return ""
        return result.stdout or ""
    except Exception:
        return ""


def regenerate_cron():
    """Génère le cron interne depuis SQLite, puis recharge crontab si possible."""
    conf = read_task_conf()
    cron_text, last_crons = build_expected_cron_text(conf=conf)
    update_last_crons(last_crons)
    cron_file = write_cron_file(cron_text, conf)
    return reload_cron(cron_file)


def ensure_cron_synced():
    """Vérifie que le fichier cron et le crontab installé correspondent à SQLite.

    On ne fait pas cron -> tableau. On fait toujours SQLite/tableau -> cron.
    Donc si le fichier cron a été modifié, vidé ou abîmé, il est recréé.
    """
    conf = read_task_conf()
    cron_file = conf["CRON_FILE"]
    expected_text, last_crons = build_expected_cron_text(conf=conf)
    current_file_text = read_text_file(cron_file)

    if current_file_text != expected_text:
        update_last_crons(last_crons)
        cron_file = write_cron_file(expected_text, conf)
        ok, msg = reload_cron(cron_file)
        return {
            "ok": ok,
            "changed": True,
            "message": f"Cron recréé depuis SQLite : {msg}",
        }

    installed_text = read_installed_crontab()
    if installed_text != expected_text:
        ok, msg = reload_cron(cron_file)
        return {
            "ok": ok,
            "changed": True,
            "message": f"Fichier cron correct, crontab rechargé : {msg}",
        }

    return {"ok": True, "changed": False, "message": "Cron synchronisé"}

def reload_cron(cron_file):
    """Recharge crontab dans le Docker. Si crontab absent, on laisse le fichier prêt."""
    try:
        result = subprocess.run(
            ["crontab", cron_file],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0, (result.stderr or result.stdout or "crontab rechargé").strip()
    except FileNotFoundError:
        return False, "Commande crontab introuvable dans le conteneur"
    except Exception as e:
        return False, str(e)

def cron_daemon_running():
    """Vérifie simplement si un démon cron/crond tourne sur l'hôte.

    Sur Debian le processus s'appelle souvent `cron` ; sur Unraid/Slackware,
    c'est plutôt `crond`. On ne se limite pas à systemd pour rester compatible
    avec Unraid.
    """
    checks = [
        ["pgrep", "-x", "cron"],
        ["pgrep", "-x", "crond"],
        ["pgrep", "-f", r"(^|/)(cron|crond)( |$)"],
    ]
    for cmd in checks:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and (result.stdout or "").strip():
                return True, (result.stdout or "").strip().splitlines()[0]
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return False, "aucun processus cron/crond détecté"


def startup_cron_sync_once():
    """Synchronise le cron au démarrage Flask.

    Avant ce hook, le cron était resynchronisé quand on ouvrait les pages du
    gestionnaire ou quand on modifiait une tâche. Si Flask redémarrait pendant
    une période de patch, une crontab absente/ancienne pouvait donc rester en
    place jusqu'à la prochaine ouverture de page. Maintenant, au chargement du
    module par Flask/Gunicorn, SQLite redevient immédiatement la source
    officielle et la crontab root est régénérée/rechargée si besoin.
    """
    if os.environ.get("YOLEO_TASK_STARTUP_CRON_SYNC", "1").strip().lower() in {"0", "false", "no", "off"}:
        return

    conf = read_task_conf()
    lock_path = Path(conf.get("TASK_RUNTIME_DIR", DEFAULTS["TASK_RUNTIME_DIR"])) / "startup_cron_sync.lock"
    lock_handle = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_handle = open(lock_path, "w", encoding="utf-8")
        if fcntl is not None:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Un autre worker Gunicorn est déjà en train de le faire.
                return

        result = ensure_cron_synced()
        daemon_ok, daemon_msg = cron_daemon_running()
        status = "✅" if result.get("ok") and daemon_ok else "⚠️"
        print(
            f"{status} Task Manager startup cron: {result.get('message', '')} ; "
            f"daemon={'actif' if daemon_ok else 'non détecté'} ({daemon_msg})",
            flush=True,
        )
    except Exception as exc:
        print(f"⚠️ Task Manager startup cron: synchronisation impossible : {exc}", flush=True)
    finally:
        if lock_handle is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()
            except Exception:
                pass


# Synchronisation automatique au chargement par Flask/Gunicorn uniquement.
# En mode CLI (`python task.py --run-task ...`), on évite de recharger la crontab
# à chaque exécution cron.
if __name__ != "__main__":
    startup_cron_sync_once()



# Verrou local de secours si fcntl n'est pas disponible.
# Le verrou réel est maintenant par tâche, pas global.
RUNNER_THREAD_LOCK = threading.RLock()


# ==========================================================
# EXÉCUTION DES TÂCHES
# ==========================================================
