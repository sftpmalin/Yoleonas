import json

NFS_CLIENT_CONF_FILE = resolve_module_path(
    os.environ.get("PARTAGE_NFS_CLIENT_CONF")
    or os.environ.get("PARTAGE_NFS_CLIENT_INI")
    or "../conf/nfs_client.conf"
)
NFS_CLIENT_LEGACY_INI_FILE = resolve_module_path("../conf/nfs_client.ini")
NFS_CLIENT_INI_FILE = NFS_CLIENT_CONF_FILE  # compatibilité interne : ancien nom de variable
NFS_CLIENT_LOG_FILE = resolve_module_path(os.environ.get("PARTAGE_NFS_CLIENT_LOG", "/var/log/yoleo/partage/nfs_client.log"))
NFS_CLIENT_STATUS_FILE = resolve_module_path(os.environ.get("PARTAGE_NFS_CLIENT_STATUS", "/var/lib/yoleo/partage/nfs_client_status.json"))

NFS_CLIENT_STARTUP_DONE = False
NFS_CLIENT_REFRESH_RUNNING = False
NFS_CLIENT_REFRESH_LOCK = threading.Lock()
NFS_CLIENT_QUEUE_RUNNING = False
NFS_CLIENT_QUEUE_LOCK = threading.Lock()


def nfs_client_now_label() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_folder_slash(path_value: str | Path) -> str:
    value = str(path_value or "").strip()
    if not value:
        return value
    return value if value.endswith("/") else value + "/"


def nfs_client_safe_int(value, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        number = int(str(value).strip())
    except Exception:
        number = int(default)
    if minimum is not None:
        number = max(int(minimum), number)
    if maximum is not None:
        number = min(int(maximum), number)
    return number


def append_nfs_client_log(message: str) -> None:
    try:
        NFS_CLIENT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with NFS_CLIENT_LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"[{nfs_client_now_label()}] {message}\n")
    except Exception:
        # Ne jamais casser une route uniquement parce que le log est indisponible.
        pass


def read_nfs_client_status() -> dict:
    try:
        if not NFS_CLIENT_STATUS_FILE.exists():
            return {}
        raw = json.loads(NFS_CLIENT_STATUS_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def write_nfs_client_status(status: dict) -> None:
    try:
        NFS_CLIENT_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = NFS_CLIENT_STATUS_FILE.with_suffix(NFS_CLIENT_STATUS_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, NFS_CLIENT_STATUS_FILE)
    except Exception as exc:
        append_nfs_client_log(f"[STATUS][ERREUR] Écriture impossible : {exc}")


def set_nfs_client_mount_status(section: str, state: str, message: str = "") -> None:
    section = str(section or "").strip()
    if not section:
        return
    status = read_nfs_client_status()
    status[section] = {
        "state": state,
        "message": str(message or ""),
        "updated_at": nfs_client_now_label(),
    }
    write_nfs_client_status(status)


def nfs_client_migrate_legacy_ini() -> None:
    """Renomme l'ancien ../conf/nfs_client.ini en ../conf/nfs_client.conf si besoin."""
    if NFS_CLIENT_CONF_FILE == NFS_CLIENT_LEGACY_INI_FILE:
        return
    if NFS_CLIENT_CONF_FILE.exists() or not NFS_CLIENT_LEGACY_INI_FILE.exists():
        return
    try:
        NFS_CLIENT_CONF_FILE.parent.mkdir(parents=True, exist_ok=True)
        os.replace(NFS_CLIENT_LEGACY_INI_FILE, NFS_CLIENT_CONF_FILE)
        append_nfs_client_log("[CONF] Migration nfs_client.ini -> nfs_client.conf effectuée.")
    except Exception as exc:
        append_nfs_client_log(f"[CONF][ERREUR] Migration nfs_client.ini -> nfs_client.conf impossible : {exc}")


def tail_file(path: Path, lines: int = 250) -> str:
    lines = max(20, min(int(lines or 250), 3000))
    try:
        if not path.exists():
            return ""
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])
    except Exception as exc:
        return f"ERREUR lecture {path} : {exc}"


def sanitize_nfs_client_machine(value: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return value.strip("._-") or "machine"


def sanitize_nfs_client_mount_name(export_path: str) -> str:
    value = str(export_path or "").strip().strip("/") or "export"
    value = value.replace("/mnt/user/", "")
    value = value.replace("/mnt/", "")
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return value.strip("._-") or "export"


def ensure_nfs_client_ini() -> configparser.ConfigParser:
    nfs_client_migrate_legacy_ini()
    NFS_CLIENT_CONF_FILE.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if NFS_CLIENT_CONF_FILE.exists():
        try:
            parser.read(NFS_CLIENT_CONF_FILE, encoding="utf-8")
        except Exception as exc:
            append_nfs_client_log(f"[CONF][ERREUR] Lecture impossible, recréation minimale : {exc}")
            parser = configparser.ConfigParser()

    if not parser.has_section("GENERAL"):
        parser.add_section("GENERAL")

    defaults = {
        "HOST_BASE": "/mnt/remotes",
        "DEFAULT_OPTIONS": "rw,soft,timeo=30,retrans=2",
        "MOUNT_RETRIES": "2",
        "MOUNT_TIMEOUT": "12",
        "RETRY_SLEEP": "2",
        "SHOWMOUNT_TIMEOUT": "8",
    }
    changed = False
    for key, value in defaults.items():
        if not parser["GENERAL"].get(key):
            parser["GENERAL"][key] = value
            changed = True

    # Migration douce de l'ancien petit Flask : cette option forçait NFSv4 et
    # casse certains serveurs/Unraid qui répondent mieux en NFSv3.
    if parser["GENERAL"].get("DEFAULT_OPTIONS", "").strip() == "rw,nfsvers=4,soft,timeo=30,retrans=2":
        parser["GENERAL"]["DEFAULT_OPTIONS"] = defaults["DEFAULT_OPTIONS"]
        changed = True

    if nfs_client_safe_int(parser["GENERAL"].get("MOUNT_RETRIES", "2"), 2, 1, 10) > 2:
        parser["GENERAL"]["MOUNT_RETRIES"] = "2"
        changed = True

    if changed or not NFS_CLIENT_CONF_FILE.exists():
        write_nfs_client_ini(parser)
    return parser


def write_nfs_client_ini(parser: configparser.ConfigParser) -> None:
    nfs_client_migrate_legacy_ini()
    NFS_CLIENT_CONF_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = NFS_CLIENT_CONF_FILE.with_suffix(NFS_CLIENT_CONF_FILE.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    os.replace(tmp, NFS_CLIENT_CONF_FILE)
    try:
        NFS_CLIENT_CONF_FILE.chmod(0o644)
    except OSError:
        pass


def nfs_client_general() -> dict:
    parser = ensure_nfs_client_ini()
    g = parser["GENERAL"]
    return {
        "host_base": ensure_folder_slash(g.get("HOST_BASE", "/mnt/remotes")),
        "default_options": g.get("DEFAULT_OPTIONS", "rw,soft,timeo=30,retrans=2"),
        "mount_retries": nfs_client_safe_int(g.get("MOUNT_RETRIES", "2"), 2, 1, 2),
        "mount_timeout": nfs_client_safe_int(g.get("MOUNT_TIMEOUT", "12"), 12, 3, 120),
        "retry_sleep": nfs_client_safe_int(g.get("RETRY_SLEEP", "2"), 2, 0, 30),
        "showmount_timeout": nfs_client_safe_int(g.get("SHOWMOUNT_TIMEOUT", "8"), 8, 3, 60),
        "conf_file": str(NFS_CLIENT_CONF_FILE),
        "ini_file": str(NFS_CLIENT_CONF_FILE),  # compat template ancien
        "log_file": str(NFS_CLIENT_LOG_FILE),
    }


def nfs_client_run_cmd(cmd: list[str], timeout: int = 40, *, log: bool = True) -> tuple[int, str, str]:
    if log:
        append_nfs_client_log("$ " + shlex.join([str(x) for x in cmd]))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if log and out:
            append_nfs_client_log("[stdout] " + out.replace("\n", "\n[stdout] "))
        if log and err:
            append_nfs_client_log("[stderr] " + err.replace("\n", "\n[stderr] "))
        return proc.returncode, out, err
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="replace")
        if log:
            append_nfs_client_log(f"[TIMEOUT] Commande interrompue après {timeout}s")
        return 124, str(out).strip(), str(err).strip() or "timeout"
    except Exception as exc:
        if log:
            append_nfs_client_log("[ERREUR] " + str(exc))
        return 127, "", str(exc)


def nfs_client_shell(shell_command: str, timeout: int = 60, *, log: bool = True) -> tuple[int, str, str]:
    prefix = "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; "
    wrapped = prefix + str(shell_command or "")
    # Si le module tourne un jour dans un conteneur privilégié, on garde la
    # possibilité d'exécuter côté hôte comme dans l'ancien petit Flask backup.
    if os.path.exists("/.dockerenv") and (os.path.exists("/usr/bin/nsenter") or os.path.exists("/bin/nsenter")):
        return nfs_client_run_cmd(["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", "/bin/bash", "-lc", wrapped], timeout=timeout, log=log)
    return nfs_client_run_cmd(["/bin/bash", "-lc", wrapped], timeout=timeout, log=log)


def nfs_client_shell_quiet(shell_command: str, timeout: int = 5) -> tuple[int, str, str]:
    return nfs_client_shell(shell_command, timeout=timeout, log=False)


def nfs_client_shell_timeout_command(seconds: int, inner_command: str) -> str:
    seconds = nfs_client_safe_int(seconds, 5, 1, 300)
    quoted_inner = shlex.quote(str(inner_command or ""))
    return (
        "if command -v timeout >/dev/null 2>&1; then "
        f"timeout --foreground {seconds}s /bin/bash -lc {quoted_inner}; "
        "else "
        f"/bin/bash -lc {quoted_inner}; "
        "fi"
    )


def nfs_client_show_exports(machine: str) -> list[str]:
    machine = str(machine or "").strip()
    if not machine:
        return []
    timeout = nfs_client_general().get("showmount_timeout", 8)
    append_nfs_client_log(f"Affichage des exports NFS : {machine} (timeout {timeout}s)")
    code, out, err = nfs_client_run_cmd(["showmount", "-e", machine], timeout=timeout)
    if code != 0:
        code, out, err = nfs_client_shell("showmount -e " + shlex.quote(machine), timeout=timeout)
    exports: list[str] = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line or line.startswith("Export list"):
            continue
        parts = line.split()
        if parts and parts[0].startswith("/"):
            exports.append(parts[0])
    if not exports and code != 0:
        append_nfs_client_log(f"[SHOWMOUNT][ERREUR] Aucun export récupéré pour {machine} : {(err or out or '').strip()}")
    return exports


def nfs_client_section(machine: str, export_path: str) -> str:
    return "NFS:" + sanitize_nfs_client_machine(machine) + ":" + sanitize_nfs_client_mount_name(export_path)


def nfs_client_mountpoint_for(machine: str, export_path: str) -> str:
    g = nfs_client_general()
    machine_clean = sanitize_nfs_client_machine(machine)
    share_clean = sanitize_nfs_client_mount_name(export_path)
    host_path = os.path.join(g["host_base"].rstrip("/"), machine_clean, share_clean)
    return ensure_folder_slash(host_path)


def save_nfs_client_mount(machine: str, export_path: str, options: str | None = None, auto_mount: bool = True) -> None:
    parser = ensure_nfs_client_ini()
    section = nfs_client_section(machine, export_path)
    if not parser.has_section(section):
        parser.add_section(section)
    host_path = nfs_client_mountpoint_for(machine, export_path)
    parser[section]["MACHINE"] = str(machine)
    parser[section]["EXPORT"] = str(export_path)
    parser[section]["HOST_PATH"] = host_path
    parser[section]["OPTIONS"] = options or nfs_client_general()["default_options"]
    parser[section]["ENABLED"] = "1" if auto_mount else "0"
    parser[section]["AUTO_MOUNT"] = "1" if auto_mount else "0"
    write_nfs_client_ini(parser)


def nfs_client_split_options(options: str | None) -> list[str]:
    return [part.strip() for part in str(options or "").split(",") if part.strip()]


def nfs_client_join_options(parts: Iterable[str]) -> str:
    cleaned: list[str] = []
    seen: set[tuple[str, str]] = set()
    for part in parts:
        part = str(part or "").strip()
        if not part:
            continue
        key = part.split("=", 1)[0].strip().lower()
        sig = (key, part.lower())
        if sig in seen:
            continue
        seen.add(sig)
        cleaned.append(part)
    return ",".join(cleaned) or "rw"


def nfs_client_options_without_version(options: str) -> str:
    return nfs_client_join_options([
        part for part in nfs_client_split_options(options)
        if part.split("=", 1)[0].strip().lower() not in {"nfsvers", "vers"}
    ])


def nfs_client_options_with_version(options: str, version: int) -> str:
    base = nfs_client_split_options(nfs_client_options_without_version(options))
    base.append("nfsvers=" + str(version))
    return nfs_client_join_options(base)


def nfs_client_option_candidates(options: str) -> list[str]:
    original = nfs_client_join_options(nfs_client_split_options(options) or nfs_client_split_options(nfs_client_general()["default_options"]))
    candidates = [
        original,
        nfs_client_options_without_version(original),
        nfs_client_options_with_version(original, 3),
        nfs_client_options_with_version(original, 4),
    ]
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.lower()
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return result


def nfs_client_server_reachable(machine: str, timeout: int = 4) -> bool:
    machine = str(machine or "").strip()
    if not machine:
        return False
    host = shlex.quote(machine)
    tcp_probe = shlex.quote(f"</dev/tcp/{machine}/2049")
    probe = (
        "if command -v rpcinfo >/dev/null 2>&1; then "
        f"rpcinfo -t {host} nfs 3 >/dev/null 2>&1 || rpcinfo -t {host} nfs 4 >/dev/null 2>&1; "
        "else "
        f"/bin/bash -lc {tcp_probe}; "
        "fi"
    )
    code, _, _ = nfs_client_shell_quiet(nfs_client_shell_timeout_command(timeout, probe), timeout=timeout + 2)
    return code == 0


def load_nfs_client_mounts() -> list[dict]:
    parser = ensure_nfs_client_ini()
    status = read_nfs_client_status()
    items: list[dict] = []
    for section in parser.sections():
        if not section.startswith("NFS:"):
            continue
        row = dict(parser[section])
        machine = row.get("machine", "")
        export_path = row.get("export", "")
        host_path = row.get("host_path", "") or nfs_client_mountpoint_for(machine, export_path)
        mounted = False
        reachable = False
        count = ""
        state = "Non monté"
        if host_path:
            code, _, _ = nfs_client_shell_quiet("mountpoint -q " + shlex.quote(host_path), timeout=5)
            mounted = code == 0
            if mounted:
                inner = "ls -1A " + shlex.quote(host_path) + " 2>/dev/null | wc -l"
                count_code, count_out, _ = nfs_client_shell_quiet(nfs_client_shell_timeout_command(2, inner), timeout=4)
                if count_code == 0:
                    reachable = True
                    count = (count_out or "").strip().splitlines()[-1] if (count_out or "").strip() else "0"
                    state = "Monté"
                else:
                    reachable = False
                    count = "?"
                    state = "Monté mais inaccessible"
        items.append({
            "section": section,
            "machine": machine,
            "export": export_path,
            "host_path": host_path,
            "options": row.get("options", ""),
            "enabled": row.get("enabled", "1") == "1",
            "auto_mount": row.get("auto_mount", row.get("enabled", "1")) == "1",
            "mounted": mounted and reachable,
            "raw_mounted": mounted,
            "reachable": reachable,
            "state": state,
            "count": count,
            "job": status.get(section, {}),
        })
    items.sort(key=lambda x: (x["machine"], x["export"]))
    return items


def mount_nfs_client_export(machine: str, export_path: str, options: str | None = None, *, force: bool = False, auto_mount: bool = True) -> bool:
    g = nfs_client_general()
    requested_options = nfs_client_join_options(nfs_client_split_options(options or g["default_options"]))
    option_candidates = nfs_client_option_candidates(requested_options)
    retries = g.get("mount_retries", 3)
    attempt_timeout = g.get("mount_timeout", 12)
    retry_sleep = g.get("retry_sleep", 2)
    host_path = nfs_client_mountpoint_for(machine, export_path)
    remote = f"{machine}:{export_path}"

    save_nfs_client_mount(machine, export_path, requested_options, auto_mount=auto_mount)

    already_code, _, _ = nfs_client_shell_quiet("mountpoint -q " + shlex.quote(host_path), timeout=4)
    if already_code == 0 and not force:
        check_inner = "ls -1A " + shlex.quote(host_path) + " >/dev/null 2>&1"
        check_code, _, _ = nfs_client_shell_quiet(nfs_client_shell_timeout_command(2, check_inner), timeout=4)
        if check_code == 0:
            append_nfs_client_log(f"[OK] Déjà monté : {remote} -> {host_path}")
        else:
            append_nfs_client_log(f"[WARN] Déjà monté, aperçu inaccessible : {remote} -> {host_path}")
        return True

    if not nfs_client_server_reachable(machine, timeout=4):
        message = f"Serveur NFS injoignable ou port 2049 fermé : {machine}"
        append_nfs_client_log(f"[NON MONTÉ] {message} ({remote} -> {host_path})")
        set_nfs_client_mount_status(nfs_client_section(machine, export_path), "failed", "Serveur injoignable")
        return False

    if force:
        append_nfs_client_log(f"[NFS CLIENT] Démontage forcé avant remontage : {host_path}")
        nfs_client_shell("umount -l " + shlex.quote(host_path) + " 2>/dev/null || true", timeout=10)

    for attempt in range(1, retries + 1):
        append_nfs_client_log(f"[NFS CLIENT] Essai montage {attempt}/{retries} : {remote} -> {host_path} (timeout {attempt_timeout}s)")
        for current_options in option_candidates:
            if len(option_candidates) > 1:
                append_nfs_client_log(f"[NFS CLIENT] Options testées : {current_options}")
            inner_mount = (
                "mkdir -p " + shlex.quote(host_path) +
                " && if mountpoint -q " + shlex.quote(host_path) +
                "; then exit 0; fi; "
                "mount -t nfs -o " + shlex.quote(current_options) + " " + shlex.quote(remote) + " " + shlex.quote(host_path)
            )
            code, out, err = nfs_client_shell(nfs_client_shell_timeout_command(attempt_timeout, inner_mount), timeout=attempt_timeout + 5)
            if code == 0:
                check_inner = "ls -1A " + shlex.quote(host_path) + " >/dev/null 2>&1"
                check_code, _, _ = nfs_client_shell_quiet(nfs_client_shell_timeout_command(2, check_inner), timeout=4)
                if check_code == 0:
                    if current_options != requested_options:
                        append_nfs_client_log(f"[NFS CLIENT] Fallback validé : {requested_options} -> {current_options}")
                        save_nfs_client_mount(machine, export_path, current_options, auto_mount=auto_mount)
                    append_nfs_client_log(f"[OK] Monté : {remote} -> {host_path}")
                    return True
                append_nfs_client_log(f"[WARN] Monté, aperçu inaccessible : {remote} -> {host_path}")
                return True
            else:
                detail = (err or out or "").strip()
                append_nfs_client_log(f"[NFS CLIENT] Échec avec options [{current_options}]" + (f" : {detail}" if detail else ""))
        if attempt < retries and retry_sleep > 0:
            time.sleep(retry_sleep)

    append_nfs_client_log(f"[NON MONTÉ] Partage non monté après {retries} essai(s) : {remote} -> {host_path}")
    set_nfs_client_mount_status(nfs_client_section(machine, export_path), "failed", "Montage impossible")
    return False


def _nfs_client_mount_queue_worker(items: list[dict], reason: str = "manuel") -> None:
    global NFS_CLIENT_QUEUE_RUNNING
    try:
        total = len(items)
        append_nfs_client_log(f"[QUEUE] Démarrage file {reason} : {total} montage(s).")
        for index, item in enumerate(items, start=1):
            machine = item.get("machine", "")
            export_path = item.get("export", "")
            section = nfs_client_section(machine, export_path)
            label = f"{machine}:{export_path}"
            set_nfs_client_mount_status(section, "running", f"Montage {index}/{total}")
            ok = mount_nfs_client_export(
                machine,
                export_path,
                item.get("options"),
                force=bool(item.get("force")),
                auto_mount=bool(item.get("auto_mount", True)),
            )
            set_nfs_client_mount_status(section, "done" if ok else "failed", "Monté" if ok else "Montage impossible")
            append_nfs_client_log(f"[QUEUE] {label} : {'OK' if ok else 'échec'}")
        append_nfs_client_log(f"[QUEUE] File {reason} terminée.")
    except Exception as exc:
        append_nfs_client_log(f"[QUEUE][ERREUR] {exc}")
    finally:
        with NFS_CLIENT_QUEUE_LOCK:
            NFS_CLIENT_QUEUE_RUNNING = False


def start_nfs_client_mount_queue(items: list[dict], *, reason: str = "manuel") -> bool:
    global NFS_CLIENT_QUEUE_RUNNING
    cleaned = [item for item in items if item.get("machine") and item.get("export")]
    if not cleaned:
        return False
    with NFS_CLIENT_QUEUE_LOCK:
        if NFS_CLIENT_QUEUE_RUNNING:
            append_nfs_client_log(f"[QUEUE] File déjà en cours, demande ignorée : {reason}")
            return False
        NFS_CLIENT_QUEUE_RUNNING = True
    total = len(cleaned)
    for index, item in enumerate(cleaned, start=1):
        section = nfs_client_section(item["machine"], item["export"])
        set_nfs_client_mount_status(section, "queued", f"En attente {index}/{total}")
        save_nfs_client_mount(item["machine"], item["export"], item.get("options"), auto_mount=bool(item.get("auto_mount", True)))
    thread = threading.Thread(target=_nfs_client_mount_queue_worker, args=(cleaned,), kwargs={"reason": reason}, daemon=True)
    thread.start()
    return True


def nfs_client_queue_is_running() -> bool:
    with NFS_CLIENT_QUEUE_LOCK:
        return NFS_CLIENT_QUEUE_RUNNING


def refresh_nfs_client_mounts(*, force: bool = True, reason: str = "manuel") -> int:
    ok_count = 0
    total = 0
    failed = 0
    for item in load_nfs_client_mounts():
        if item.get("auto_mount"):
            total += 1
            if mount_nfs_client_export(item["machine"], item["export"], item.get("options"), force=force, auto_mount=True):
                ok_count += 1
            else:
                failed += 1
    append_nfs_client_log(f"Rafraîchissement {reason} terminé : {ok_count}/{total} montage(s) actif(s), {failed} non monté(s).")
    return ok_count


def nfs_client_refresh_is_running() -> bool:
    with NFS_CLIENT_REFRESH_LOCK:
        return NFS_CLIENT_REFRESH_RUNNING


def _nfs_client_refresh_worker(force: bool = True, reason: str = "manuel") -> None:
    global NFS_CLIENT_REFRESH_RUNNING
    try:
        refresh_nfs_client_mounts(force=force, reason=reason)
    except Exception as exc:
        append_nfs_client_log("[AUTO][ERREUR] " + str(exc))
    finally:
        with NFS_CLIENT_REFRESH_LOCK:
            NFS_CLIENT_REFRESH_RUNNING = False


def start_nfs_client_refresh_async(*, force: bool = True, reason: str = "manuel") -> bool:
    global NFS_CLIENT_REFRESH_RUNNING
    with NFS_CLIENT_REFRESH_LOCK:
        if NFS_CLIENT_REFRESH_RUNNING:
            append_nfs_client_log(f"[AUTO] Rafraîchissement déjà en cours, demande ignorée : {reason}")
            return False
        NFS_CLIENT_REFRESH_RUNNING = True
    thread = threading.Thread(target=_nfs_client_refresh_worker, kwargs={"force": force, "reason": reason}, daemon=True)
    thread.start()
    return True


def startup_nfs_client_automount_once() -> None:
    global NFS_CLIENT_STARTUP_DONE
    if NFS_CLIENT_STARTUP_DONE:
        return
    NFS_CLIENT_STARTUP_DONE = True
    try:
        auto_count = sum(1 for item in load_nfs_client_mounts() if item.get("auto_mount"))
        if auto_count:
            append_nfs_client_log(f"[AUTO] Démarrage Flask : vérification en arrière-plan de {auto_count} montage(s).")
            start_nfs_client_refresh_async(force=False, reason="démarrage Flask")
        else:
            append_nfs_client_log("[AUTO] Démarrage Flask : aucun montage automatique à vérifier.")
    except Exception as exc:
        append_nfs_client_log("[AUTO][ERREUR] " + str(exc))


def is_safe_nfs_client_host_path(path_value: str) -> bool:
    try:
        base = os.path.abspath(nfs_client_general()["host_base"].rstrip("/"))
        path = os.path.abspath(str(path_value or "").rstrip("/"))
        return bool(path) and (path == base or path.startswith(base + os.sep))
    except Exception:
        return False


def set_nfs_client_auto(section: str, enabled: bool) -> None:
    parser = ensure_nfs_client_ini()
    section = str(section or "").strip()
    if parser.has_section(section):
        parser[section]["AUTO_MOUNT"] = "1" if enabled else "0"
        parser[section]["ENABLED"] = "1" if enabled else "0"
        write_nfs_client_ini(parser)
        append_nfs_client_log(f"[AUTO] {section} -> {'activé' if enabled else 'désactivé'}")
    else:
        append_nfs_client_log(f"[AUTO][ERREUR] Section introuvable : {section}")


def cleanup_nfs_client_mount_dir(host_path: str) -> bool:
    """Supprime uniquement le dossier local de montage s'il est vide et démonté.

    Sécurité volontaire : pas de rm -rf ici. On utilise seulement rmdir.
    Si le partage est encore monté, ou si le dossier contient encore quelque
    chose, Linux refuse la suppression et on garde le dossier.
    """
    host_path = str(host_path or "").rstrip("/")
    if not host_path or not is_safe_nfs_client_host_path(host_path):
        append_nfs_client_log(f"[CLEAN][WARN] Chemin hors HOST_BASE, nettoyage ignoré : {host_path}")
        return False

    base = os.path.abspath(nfs_client_general()["host_base"].rstrip("/"))
    path = os.path.abspath(host_path)
    if path == base:
        append_nfs_client_log(f"[CLEAN][WARN] Refus de supprimer la base NFS client : {path}")
        return False

    mounted_code, _, _ = nfs_client_shell_quiet("mountpoint -q " + shlex.quote(path), timeout=5)
    if mounted_code == 0:
        append_nfs_client_log(f"[CLEAN][WARN] Dossier encore monté, suppression locale ignorée : {path}")
        return False

    parent = os.path.dirname(path)
    commands = ["rmdir " + shlex.quote(path) + " 2>/dev/null || true"]
    if parent and parent != base and is_safe_nfs_client_host_path(parent):
        commands.append("rmdir " + shlex.quote(parent) + " 2>/dev/null || true")
    nfs_client_shell("; ".join(commands), timeout=20)
    append_nfs_client_log(f"[CLEAN] Nettoyage local demandé avec rmdir uniquement : {path}")
    return True


def unmount_nfs_client_section(section: str, *, remove_empty_dir: bool = False) -> bool:
    parser = ensure_nfs_client_ini()
    section = str(section or "").strip()
    if not section.startswith("NFS:") or not parser.has_section(section):
        append_nfs_client_log(f"[UNMOUNT][ERREUR] Montage inconnu : {section}")
        return False
    row = dict(parser[section])
    host_path = (row.get("host_path") or "").rstrip("/")
    label = f"{row.get('machine', '')}:{row.get('export', '')}"
    if not host_path or not is_safe_nfs_client_host_path(host_path):
        append_nfs_client_log(f"[UNMOUNT][WARN] Chemin hors HOST_BASE, démontage ignoré : {host_path}")
        return False
    nfs_client_shell("if mountpoint -q " + shlex.quote(host_path) + "; then umount -l " + shlex.quote(host_path) + "; fi", timeout=30)
    if remove_empty_dir:
        cleanup_nfs_client_mount_dir(host_path)
    append_nfs_client_log(f"[UNMOUNT] Démontage demandé : {label} -> {host_path}")
    return True


def delete_nfs_client_mount(section: str, *, unmount: bool = True, remove_empty_dir: bool = True) -> bool:
    parser = ensure_nfs_client_ini()
    section = str(section or "").strip()
    if not section.startswith("NFS:") or not parser.has_section(section):
        append_nfs_client_log(f"[DELETE][ERREUR] Montage inconnu : {section}")
        return False
    row = dict(parser[section])
    label = f"{row.get('machine', '')}:{row.get('export', '')}"
    if unmount:
        unmount_nfs_client_section(section, remove_empty_dir=remove_empty_dir)
    parser.remove_section(section)
    write_nfs_client_ini(parser)
    append_nfs_client_log(f"[DELETE] Montage supprimé de la liste : {label}")
    return True


def nfs_client_payload() -> dict:
    return {
        "general": nfs_client_general(),
        "mounts": load_nfs_client_mounts(),
        "refresh_running": nfs_client_refresh_is_running() or nfs_client_queue_is_running(),
        "queue_running": nfs_client_queue_is_running(),
        "job_status": read_nfs_client_status(),
        "log_text": tail_file(NFS_CLIENT_LOG_FILE, int(partage_setting("LOG_LINES", "250") or 250)),
        "updated_at": nfs_client_now_label(),
    }
