def begin_task_db_log_buffer(task_id):
    conf = read_task_conf()
    runtime_dir = Path(conf.get("TASK_RUNTIME_DIR") or DEFAULTS["TASK_RUNTIME_DIR"])
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        runtime_dir = Path("/tmp")
    path = runtime_dir / f"task_{safe_int(task_id, 0)}_{os.getpid()}_{int(time.time())}.db-log.jsonl"
    os.environ[TASK_DB_LOG_BUFFER_ENV] = str(path)
    return path


def flush_task_db_log_buffer(task_id):
    path_value = os.environ.get(TASK_DB_LOG_BUFFER_ENV, "").strip()
    if not path_value:
        return 0

    path = Path(path_value)
    if not path.exists():
        os.environ.pop(TASK_DB_LOG_BUFFER_ENV, None)
        return 0

    inserted = 0
    with connect_db() as db:
        batch = []
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    item = json.loads(raw)
                except Exception:
                    continue
                if safe_int(item.get("task_id"), 0) != safe_int(task_id, 0):
                    continue
                batch.append((
                    safe_int(task_id, 0),
                    str(item.get("created_at") or now_str()),
                    str(item.get("source") or ""),
                    str(item.get("line") or ""),
                ))
                if len(batch) >= 1000:
                    db.executemany(
                        "INSERT INTO task_log(task_id, created_at, source, line) VALUES(?, ?, ?, ?)",
                        batch,
                    )
                    inserted += len(batch)
                    batch = []
            if batch:
                db.executemany(
                    "INSERT INTO task_log(task_id, created_at, source, line) VALUES(?, ?, ?, ?)",
                    batch,
                )
                inserted += len(batch)
        db.commit()

    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
    os.environ.pop(TASK_DB_LOG_BUFFER_ENV, None)
    return inserted


def add_log(task_id, line, source=""):
    conf = read_task_conf()
    created = now_str()
    line = str(line).rstrip("\n")

    # Log fichier en plus de SQLite, pratique dans le terminal.
    try:
        log_file = Path(conf["LOG_DIR"]) / f"task_{task_id}.log"
        with log_file.open("a", encoding="utf-8") as f:
            f.write(f"[{created}] {line}\n")
    except Exception:
        pass

    buffer_path = os.environ.get(TASK_DB_LOG_BUFFER_ENV, "").strip()
    if buffer_path:
        try:
            with Path(buffer_path).open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "task_id": safe_int(task_id, 0),
                    "created_at": created,
                    "source": str(source or ""),
                    "line": line,
                }, ensure_ascii=False) + "\n")
            return
        except Exception:
            pass

    with connect_db() as db:
        db.execute(
            "INSERT INTO task_log(task_id, created_at, source, line) VALUES(?, ?, ?, ?)",
            (task_id, created, source, line),
        )
        db.commit()


def trim_logs(task_id):
    conf = read_task_conf()
    keep = safe_int(conf.get("MAX_LOG_LINES_PER_TASK"), 5000, 200, 200000)
    with connect_db() as db:
        db.execute(
            """
            DELETE FROM task_log
            WHERE task_id=? AND id NOT IN (
                SELECT id FROM task_log WHERE task_id=? ORDER BY id DESC LIMIT ?
            )
            """,
            (task_id, task_id, keep),
        )
        db.commit()


def find_live_task_db_log_buffer(task_id):
    conf = read_task_conf()
    runtime_dir = Path(conf.get("TASK_RUNTIME_DIR") or DEFAULTS["TASK_RUNTIME_DIR"])
    pattern = f"task_{safe_int(task_id, 0)}_*.db-log.jsonl"
    try:
        matches = [p for p in runtime_dir.glob(pattern) if p.is_file()]
    except Exception:
        return None
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def read_live_task_db_log_tail(task_id, limit):
    path = find_live_task_db_log_buffer(task_id)
    if not path:
        return []

    items = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    item = json.loads(raw)
                except Exception:
                    continue
                if safe_int(item.get("task_id"), 0) != safe_int(task_id, 0):
                    continue
                items.append({
                    "created_at": str(item.get("created_at") or ""),
                    "source": str(item.get("source") or ""),
                    "line": str(item.get("line") or ""),
                })
                if len(items) > limit:
                    items = items[-limit:]
    except Exception:
        return []
    return items


def get_log_tail(task_id, limit=250):
    limit = safe_int(limit, 250, 1, 2000)
    try:
        status = get_status(task_id)
    except Exception:
        status = {}
    if safe_int(status.get("running"), 0) == 1:
        live_rows = read_live_task_db_log_tail(task_id, limit)
        if live_rows:
            return live_rows

    with connect_db() as db:
        rows = db.execute(
            "SELECT created_at, source, line FROM task_log WHERE task_id=? ORDER BY id DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
    return [row_to_dict(r) for r in reversed(rows)]



# ==========================================================
# NOTIFICATIONS WEB PUSH
# ==========================================================
def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def vapid_private_key_file():
    return Path(CONF_FILE).parent / "vapid_private.pem"


def vapid_public_key_file():
    return Path(CONF_FILE).parent / "vapid_public_key.txt"


def parse_openssl_public_key_from_text(text):
    """Extrait la clé publique P-256 non compressée depuis `openssl ec -text`."""
    lines = []
    in_pub = False
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if line.startswith("pub:"):
            in_pub = True
            continue
        if in_pub and (line.startswith("ASN1 OID") or line.startswith("NIST CURVE") or line.startswith("Private-Key")):
            break
        if in_pub:
            lines.append(line)

    hex_text = "".join(lines).replace(":", "").replace(" ", "")
    if not hex_text:
        return b""
    try:
        public_bytes = bytes.fromhex(hex_text)
    except Exception:
        return b""

    # Le navigateur attend 65 octets : 0x04 + X(32) + Y(32).
    if len(public_bytes) == 64:
        public_bytes = b"\x04" + public_bytes
    if len(public_bytes) != 65 or public_bytes[0] != 4:
        return b""
    return public_bytes


def ensure_vapid_keys():
    """Crée une paire VAPID locale si elle n'existe pas encore.

    Le navigateur a besoin de la clé publique. Le serveur garde la clé privée
    dans le dossier technique du module. Cette clé reste stable tant que ce
    dossier est conservé, donc les abonnements restent valides après redémarrage.
    """
    private_path = vapid_private_key_file()
    public_path = vapid_public_key_file()
    private_path.parent.mkdir(parents=True, exist_ok=True)

    if private_path.exists() and public_path.exists():
        public_key = public_path.read_text(encoding="utf-8", errors="replace").strip()
        if public_key:
            return public_key

    try:
        if not private_path.exists():
            subprocess.run(
                ["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(private_path)],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            try:
                private_path.chmod(0o600)
            except Exception:
                pass

        result = subprocess.run(
            ["openssl", "ec", "-in", str(private_path), "-noout", "-text"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        public_bytes = parse_openssl_public_key_from_text(result.stdout + "\n" + result.stderr)
        if not public_bytes:
            return ""
        public_key = b64url(public_bytes)
        public_path.write_text(public_key + "\n", encoding="utf-8")
        return public_key
    except Exception:
        return ""


def get_active_push_subscriptions():
    with connect_db() as db:
        rows = db.execute(
            """
            SELECT id, endpoint, subscription_json, user_agent, enabled, created_at, updated_at, last_error
            FROM push_subscriptions
            WHERE enabled=1
            ORDER BY id ASC
            """
        ).fetchall()
    return [row_to_dict(r) for r in rows]


def load_subscription_payload(raw):
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def push_subscription_client_id(subscription):
    subscription = subscription or {}
    for key in ("yoleo_client_id", "client_id", "browser_id"):
        value = str(subscription.get(key) or "").strip()
        if value:
            return value[:128]
    return ""


def push_user_agent_fingerprint(user_agent):
    """Empreinte large pour nettoyer les anciens abonnements sans ID navigateur."""
    ua = str(user_agent or "").lower()
    if "firefox" in ua:
        browser = "firefox"
    elif "edg/" in ua or "edga/" in ua:
        browser = "edge"
    elif "chrome" in ua or "chromium" in ua or "crios" in ua:
        browser = "chromium"
    elif "safari" in ua:
        browser = "safari"
    else:
        browser = "unknown"

    if "android" in ua:
        platform = "android"
    elif "iphone" in ua or "ipad" in ua or "ios" in ua:
        platform = "ios"
    elif "windows" in ua:
        platform = "windows"
    elif "mac os" in ua or "macintosh" in ua:
        platform = "macos"
    elif "linux" in ua:
        platform = "linux"
    else:
        platform = "unknown"

    device = "mobile" if any(token in ua for token in ("mobile", "android", "iphone", "ipad")) else "desktop"
    return f"{browser}:{platform}:{device}"


def cleanup_push_subscriptions(reason="nettoyage manuel"):
    """Supprime les abonnements inutilisables et les anciens doublons."""
    removed = 0
    kept = 0
    now = now_str()

    with connect_db() as db:
        rows = [
            row_to_dict(r)
            for r in db.execute(
                """
                SELECT id, endpoint, subscription_json, user_agent, enabled, created_at, updated_at, last_error
                FROM push_subscriptions
                ORDER BY id ASC
                """
            ).fetchall()
        ]

        delete_ids = set()
        grouped = {}
        newest_identified_by_fingerprint = {}

        for row in rows:
            row_id = safe_int(row.get("id"), 0)
            endpoint = str(row.get("endpoint") or "").strip()
            payload = load_subscription_payload(row.get("subscription_json"))
            payload_endpoint = str(payload.get("endpoint") or "").strip()
            enabled = safe_int(row.get("enabled"), 0)

            if not row_id or enabled != 1 or not endpoint or not payload_endpoint:
                if row_id:
                    delete_ids.add(row_id)
                continue

            client_id = push_subscription_client_id(payload)
            if client_id:
                group_key = f"client:{client_id}"
                fingerprint = push_user_agent_fingerprint(row.get("user_agent"))
                current = newest_identified_by_fingerprint.get(fingerprint)
                if current is None or (
                    parse_task_datetime(row.get("updated_at")) or parse_task_datetime(row.get("created_at")) or datetime.min,
                    row_id,
                ) > (
                    parse_task_datetime(current.get("updated_at")) or parse_task_datetime(current.get("created_at")) or datetime.min,
                    safe_int(current.get("id"), 0),
                ):
                    newest_identified_by_fingerprint[fingerprint] = row
            else:
                user_agent = " ".join(str(row.get("user_agent") or "").split()).strip().lower()
                group_key = f"ua:{user_agent}" if user_agent else f"endpoint:{endpoint}"
            grouped.setdefault(group_key, []).append(row)

        for group_rows in grouped.values():
            if len(group_rows) <= 1:
                kept += len(group_rows)
                continue

            newest = sorted(
                group_rows,
                key=lambda r: (
                    parse_task_datetime(r.get("updated_at")) or parse_task_datetime(r.get("created_at")) or datetime.min,
                    safe_int(r.get("id"), 0),
                ),
                reverse=True,
            )[0]
            newest_id = safe_int(newest.get("id"), 0)
            for row in group_rows:
                row_id = safe_int(row.get("id"), 0)
                if row_id and row_id != newest_id:
                    delete_ids.add(row_id)
            kept += 1

        for row in rows:
            row_id = safe_int(row.get("id"), 0)
            if not row_id or row_id in delete_ids:
                continue
            payload = load_subscription_payload(row.get("subscription_json"))
            if push_subscription_client_id(payload):
                continue
            fingerprint = push_user_agent_fingerprint(row.get("user_agent"))
            identified = newest_identified_by_fingerprint.get(fingerprint)
            if not identified:
                continue
            old_stamp = parse_task_datetime(row.get("updated_at")) or parse_task_datetime(row.get("created_at")) or datetime.min
            new_stamp = parse_task_datetime(identified.get("updated_at")) or parse_task_datetime(identified.get("created_at")) or datetime.min
            if old_stamp <= new_stamp:
                delete_ids.add(row_id)

        if delete_ids:
            placeholders = ",".join("?" for _ in delete_ids)
            db.execute(f"DELETE FROM push_subscriptions WHERE id IN ({placeholders})", tuple(sorted(delete_ids)))
            removed = len(delete_ids)
        db.commit()

    return {"removed": removed, "kept": kept, "reason": reason, "updated_at": now}


def save_push_subscription(subscription, user_agent=""):
    endpoint = str((subscription or {}).get("endpoint") or "").strip()
    if not endpoint:
        return False, "endpoint manquant"

    raw = json.dumps(subscription, ensure_ascii=False, sort_keys=True)
    stamp = now_str()
    client_id = push_subscription_client_id(subscription)
    with connect_db() as db:
        if client_id:
            rows = db.execute(
                """
                SELECT id, endpoint, subscription_json
                FROM push_subscriptions
                WHERE endpoint<>?
                """,
                (endpoint,),
            ).fetchall()
            stale_ids = []
            for row in rows:
                payload = load_subscription_payload(row["subscription_json"])
                if push_subscription_client_id(payload) == client_id:
                    stale_ids.append(row["id"])
            if stale_ids:
                placeholders = ",".join("?" for _ in stale_ids)
                db.execute(f"DELETE FROM push_subscriptions WHERE id IN ({placeholders})", tuple(stale_ids))

        db.execute(
            """
            INSERT INTO push_subscriptions(endpoint, subscription_json, user_agent, enabled, created_at, updated_at, last_error)
            VALUES(?, ?, ?, 1, ?, ?, '')
            ON CONFLICT(endpoint) DO UPDATE SET
                subscription_json=excluded.subscription_json,
                user_agent=excluded.user_agent,
                enabled=1,
                updated_at=excluded.updated_at,
                last_error=''
            """,
            (endpoint, raw, str(user_agent or "")[:500], stamp, stamp),
        )
        db.commit()
    cleanup = cleanup_push_subscriptions("réabonnement navigateur")
    if cleanup.get("removed", 0):
        return True, f"abonnement enregistré, {cleanup['removed']} ancien(s) abonnement(s) nettoyé(s)"
    return True, "abonnement enregistré"


def disable_push_subscription(endpoint, error=""):
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        return
    with connect_db() as db:
        db.execute(
            "UPDATE push_subscriptions SET enabled=0, last_error=?, updated_at=? WHERE endpoint=?",
            (str(error or "")[:1000], now_str(), endpoint),
        )
        db.commit()


def delete_push_subscription(endpoint):
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        return False
    with connect_db() as db:
        db.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
        db.commit()
    return True


def push_subscription_count():
    with connect_db() as db:
        row = db.execute("SELECT COUNT(*) AS n FROM push_subscriptions WHERE enabled=1").fetchone()
    return safe_int(row["n"] if row else 0, 0)


def send_web_push_payload(payload, task_id=None, with_details=False):
    """Envoie un payload à tous les navigateurs abonnés.

    Retour normal : (sent, failed), pour ne pas casser le reste du moteur.
    Retour debug : (sent, failed, details), utilisé par le bouton Test.
    """
    def row_value(row, key, default=""):
        try:
            if hasattr(row, "keys") and key in row.keys():
                return row[key]
        except Exception:
            pass
        try:
            return row.get(key, default)
        except Exception:
            return default

    def endpoint_kind(endpoint):
        endpoint = endpoint or ""
        if "mozilla" in endpoint or "push.services.mozilla" in endpoint:
            return "Firefox / Mozilla"
        if "fcm.googleapis.com" in endpoint or "googleapis" in endpoint:
            return "Chrome / Chromium"
        return "Inconnu"

    def short_text(value, size=240):
        value = str(value or "")
        return value[:size]

    def finish(sent_value, failed_value, details_value):
        if with_details:
            return sent_value, failed_value, details_value
        return sent_value, failed_value

    def build_vapid_claims(endpoint, subject):
        """Construit les claims VAPID avec le bon aud pour chaque push service.

        Firefox/Mozilla refuse les notifications si aud pointe vers ton domaine
        Flask/NPM au lieu du domaine réel de l'endpoint Push Mozilla.
        Exemple attendu : https://updates.push.services.mozilla.com
        """
        parsed = urlparse(str(endpoint or ""))
        claims = {"sub": subject}
        if parsed.scheme and parsed.netloc:
            claims["aud"] = f"{parsed.scheme}://{parsed.netloc}"
        return claims

    details = []

    public_key = ensure_vapid_keys()
    if not public_key:
        msg = "Impossible de générer/lire les clés VAPID."
        if task_id:
            add_log(task_id, f"⚠️ Notification non envoyée : {msg}", "Notification")
        details.append({"ok": False, "id": "—", "type": "Configuration", "http": None, "error": msg})
        return finish(0, 0, details)

    if webpush is None:
        msg = "Module pywebpush absent. Ajoute `pywebpush` dans requirements.txt puis relance l’installation."
        if task_id:
            add_log(task_id, f"⚠️ Notification non envoyée : {msg}", "Notification")
        details.append({"ok": False, "id": "—", "type": "Configuration", "http": None, "error": msg})
        return finish(0, 0, details)

    subscriptions = get_active_push_subscriptions()
    if not subscriptions:
        msg = "Aucun navigateur/téléphone abonné."
        if task_id:
            add_log(task_id, f"🔕 Notification non envoyée : {msg}", "Notification")
        details.append({"ok": False, "id": "—", "type": "Aucun abonnement", "http": None, "error": msg})
        return finish(0, 0, details)

    conf = read_task_conf()
    vapid_subject = conf.get("VAPID_SUBJECT") or DEFAULTS.get("VAPID_SUBJECT", "mailto:admin@localhost")
    sent = 0
    failed = 0

    for row in subscriptions:
        sub_id = row_value(row, "id", "—")
        endpoint = row_value(row, "endpoint", "")
        user_agent = row_value(row, "user_agent", "")
        kind = endpoint_kind(endpoint)

        item = {
            "id": sub_id,
            "ok": False,
            "type": kind,
            "endpoint": short_text(endpoint, 220),
            "user_agent": short_text(user_agent, 220),
            "http": None,
            "error": "",
        }

        try:
            subscription_info = json.loads(row_value(row, "subscription_json", "{}") or "{}")
            endpoint_for_claims = subscription_info.get("endpoint") or endpoint
            claims = build_vapid_claims(endpoint_for_claims, vapid_subject)
            item["aud"] = claims.get("aud", "")

            response = webpush(
                subscription_info=subscription_info,
                data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=str(vapid_private_key_file()),
                vapid_claims=claims,
            )
            sent += 1
            item["ok"] = True
            item["http"] = getattr(response, "status_code", None)

        except WebPushException as e:
            failed += 1
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            body = getattr(getattr(e, "response", None), "text", "") or ""
            item["http"] = status_code
            item["error"] = short_text(f"{type(e).__name__}: {e} {body}", 1000).strip()

            if status_code in (404, 410):
                disable_push_subscription(endpoint, f"abonnement expiré HTTP {status_code}")
            if task_id:
                add_log(task_id, f"⚠️ Échec notification Web Push ID {sub_id} {kind} HTTP {status_code} : {e}", "Notification")

        except Exception as e:
            failed += 1
            item["error"] = short_text(f"{type(e).__name__}: {e}", 1000)
            if task_id:
                add_log(task_id, f"⚠️ Échec notification Web Push ID {sub_id} {kind} : {e}", "Notification")

        details.append(item)

    if task_id:
        add_log(task_id, f"🔔 Notifications Web Push : {sent} envoyée(s), {failed} échec(s).", "Notification")

    return finish(sent, failed, details)

def notify_task_success(task_id, task, end_time):
    if safe_int((task or {}).get("notify_success"), 0) != 1:
        return

    title = (task or {}).get("title") or f"Tâche {task_id}"
    payload = {
        "title": "Task Manager",
        "body": f"{title} : c’est bien exécuté.",
        "icon": "/static/logo/Tasks.png",
        "badge": "/static/logo/Tasks.png",
        "tag": f"task-{task_id}-success",
        "url": f"/system/task/progress?log_task={task_id}",
        "task_id": task_id,
        "finished_at": end_time,
    }
    send_web_push_payload(payload, task_id=task_id)


def notify_task_failure(task_id, task, end_time, message=""):
    """Envoie toujours une notification quand une tâche finit en erreur.

    Contrairement au succès, il n'y a pas de case à cocher : une erreur doit
    alerter par défaut sur tous les navigateurs/téléphones abonnés.
    """
    title = (task or {}).get("title") or f"Tâche {task_id}"
    msg = str(message or "Une commande a échoué.").strip()
    if len(msg) > 160:
        msg = msg[:157] + "..."

    payload = {
        "title": "Task Manager",
        "body": f"{title} : échec. {msg}",
        "icon": "/static/logo/Tasks.png",
        "badge": "/static/logo/Tasks.png",
        # Tag unique : deux erreurs de suite doivent produire deux notifications.
        "tag": f"task-{task_id}-failure-{int(time.time())}",
        "url": f"/system/task/progress?log_task={task_id}",
        "task_id": task_id,
        "finished_at": end_time,
        "result": "Erreur",
    }
    send_web_push_payload(payload, task_id=task_id)

# ==========================================================
# PLANIFICATION
# ==========================================================
