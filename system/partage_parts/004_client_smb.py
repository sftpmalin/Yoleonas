"""Client Samba/CIFS manuel.

Les profils sont conserves pour pouvoir etre remontes depuis l'interface, mais
aucun montage n'est ajoute a fstab et aucun hook de demarrage n'est installe.
"""

import socket
import tempfile


SMB_CLIENT_CONF_FILE = resolve_module_path(
    os.environ.get("PARTAGE_SMB_CLIENT_CONF", "../conf/smb_client.conf")
)
SMB_CLIENT_LOG_FILE = resolve_module_path(
    os.environ.get("PARTAGE_SMB_CLIENT_LOG", "/var/log/yoleo/partage/smb_client.log")
)
SMB_CLIENT_STATUS_FILE = resolve_module_path(
    os.environ.get("PARTAGE_SMB_CLIENT_STATUS", "/var/lib/yoleo/partage/smb_client_status.json")
)
SMB_CLIENT_CREDENTIALS_DIR = resolve_module_path(
    os.environ.get("PARTAGE_SMB_CLIENT_CREDENTIALS", "/var/lib/yoleo/partage/smb_credentials")
)

SMB_CLIENT_QUEUE_RUNNING = False
SMB_CLIENT_QUEUE_LOCK = threading.Lock()
SMB_CLIENT_CONF_LOCK = threading.RLock()


def smb_client_now_label() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def append_smb_client_log(message: str) -> None:
    try:
        SMB_CLIENT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with SMB_CLIENT_LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"[{smb_client_now_label()}] {message}\n")
    except Exception:
        pass


def smb_client_safe_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(str(value).strip())
    except Exception:
        number = int(default)
    return max(minimum, min(maximum, number))


def write_smb_client_conf(parser: configparser.ConfigParser) -> None:
    with SMB_CLIENT_CONF_LOCK:
        SMB_CLIENT_CONF_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SMB_CLIENT_CONF_FILE.with_suffix(SMB_CLIENT_CONF_FILE.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            parser.write(handle)
        os.replace(tmp, SMB_CLIENT_CONF_FILE)
        try:
            SMB_CLIENT_CONF_FILE.chmod(0o600)
        except OSError:
            pass


def ensure_smb_client_conf() -> configparser.ConfigParser:
    with SMB_CLIENT_CONF_LOCK:
        parser = configparser.ConfigParser(interpolation=None)
        if SMB_CLIENT_CONF_FILE.exists():
            try:
                parser.read(SMB_CLIENT_CONF_FILE, encoding="utf-8")
            except Exception as exc:
                append_smb_client_log(f"[CONF][ERREUR] Lecture impossible : {exc}")
                parser = configparser.ConfigParser(interpolation=None)
        if not parser.has_section("GENERAL"):
            parser.add_section("GENERAL")
        defaults = {
            "HOST_BASE": "/mnt/smb",
            "DEFAULT_OPTIONS": "rw,vers=3.0,noperm,iocharset=utf8",
            "MOUNT_RETRIES": "3",
            "MOUNT_TIMEOUT": "12",
            "CONNECT_TIMEOUT": "2",
            "RETRY_SLEEP": "1",
            "DISCOVERY_TIMEOUT": "10",
        }
        changed = not SMB_CLIENT_CONF_FILE.exists()
        for key, value in defaults.items():
            if not parser["GENERAL"].get(key):
                parser["GENERAL"][key] = value
                changed = True
        if changed:
            write_smb_client_conf(parser)
        return parser


def smb_client_general() -> dict:
    general = ensure_smb_client_conf()["GENERAL"]
    base = os.path.normpath(general.get("HOST_BASE", "/mnt/smb"))
    return {
        "host_base": ensure_folder_slash(base),
        "default_options": general.get("DEFAULT_OPTIONS", "rw,vers=3.0,noperm,iocharset=utf8"),
        "mount_retries": smb_client_safe_int(general.get("MOUNT_RETRIES"), 3, 2, 4),
        "mount_timeout": smb_client_safe_int(general.get("MOUNT_TIMEOUT"), 12, 4, 120),
        "connect_timeout": smb_client_safe_int(general.get("CONNECT_TIMEOUT"), 2, 1, 10),
        "retry_sleep": smb_client_safe_int(general.get("RETRY_SLEEP"), 1, 0, 15),
        "discovery_timeout": smb_client_safe_int(general.get("DISCOVERY_TIMEOUT"), 10, 3, 60),
        "conf_file": str(SMB_CLIENT_CONF_FILE),
        "log_file": str(SMB_CLIENT_LOG_FILE),
    }


def read_smb_client_status() -> dict:
    try:
        raw = json.loads(SMB_CLIENT_STATUS_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def write_smb_client_status(status: dict) -> None:
    try:
        SMB_CLIENT_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SMB_CLIENT_STATUS_FILE.with_suffix(SMB_CLIENT_STATUS_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, SMB_CLIENT_STATUS_FILE)
    except Exception as exc:
        append_smb_client_log(f"[STATUS][ERREUR] {exc}")


def set_smb_client_status(section: str, state: str, message: str = "") -> None:
    status = read_smb_client_status()
    status[str(section)] = {
        "state": str(state),
        "message": str(message),
        "updated_at": smb_client_now_label(),
    }
    write_smb_client_status(status)


def clear_smb_client_status(section: str) -> None:
    status = read_smb_client_status()
    if status.pop(str(section), None) is not None:
        write_smb_client_status(status)


def validate_smb_client_machine(value: str) -> str:
    machine = str(value or "").strip().strip("[]")
    if not machine or len(machine) > 253:
        raise ValueError("Adresse ou nom de machine invalide.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", machine):
        raise ValueError("Adresse ou nom de machine invalide.")
    return machine


def validate_smb_client_share(value: str) -> str:
    share = str(value or "").strip()
    if not share or len(share) > 255 or share in {".", ".."}:
        raise ValueError("Nom de partage invalide.")
    if any(char in share for char in ("/", "\\", "\x00", "\r", "\n")):
        raise ValueError("Nom de partage invalide.")
    return share


def validate_smb_client_identity(value: str, label: str) -> str:
    value = str(value or "").strip()
    if len(value) > 255 or any(char in value for char in ("\x00", "\r", "\n")):
        raise ValueError(f"{label} invalide.")
    return value


def sanitize_smb_client_mount_name(share: str) -> str:
    value = str(share or "").strip().lower()
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return value.strip("._-") or "partage"


def smb_client_default_mount_path(share: str) -> str:
    base = smb_client_general()["host_base"].rstrip("/")
    return ensure_folder_slash(os.path.join(base, sanitize_smb_client_mount_name(share)))


def normalize_smb_client_mount_path(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or not os.path.isabs(raw):
        raise ValueError("Le point de montage doit etre un chemin absolu sous /mnt/smb.")
    base = os.path.abspath(smb_client_general()["host_base"].rstrip("/"))
    path = os.path.abspath(os.path.normpath(raw))
    try:
        inside = os.path.commonpath([base, path]) == base
    except ValueError:
        inside = False
    if not inside or path == base:
        raise ValueError(f"Le point de montage doit etre un sous-dossier de {base}/.")
    return ensure_folder_slash(path)


def smb_client_source(machine: str, share: str) -> str:
    return f"//{validate_smb_client_machine(machine)}/{validate_smb_client_share(share)}"


def smb_client_source_key(source: str) -> str:
    return str(source or "").rstrip("/").casefold()


def smb_client_section(machine: str, share: str, mount_path: str) -> str:
    raw = "\x00".join((machine.casefold(), share.casefold(), os.path.normpath(mount_path)))
    return "SMB:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def smb_client_credential_path(section: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", section)
    return SMB_CLIENT_CREDENTIALS_DIR / f"{safe}.cred"


def write_smb_client_auth_file(path: Path, username: str, password: str, domain: str = "") -> None:
    username = validate_smb_client_identity(username, "Nom d'utilisateur")
    domain = validate_smb_client_identity(domain, "Domaine")
    password = str(password or "")
    if any(char in password for char in ("\x00", "\r", "\n")):
        raise ValueError("Le mot de passe ne peut pas contenir de retour a la ligne.")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    lines = [f"username={username}", f"password={password}"]
    if domain:
        lines.append(f"domain={domain}")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, path)


def save_smb_client_profile(
    machine: str,
    share: str,
    mount_path: str,
    username: str = "",
    password: str | None = None,
    domain: str = "",
    mode_0777: bool = True,
    options: str | None = None,
) -> str:
    machine = validate_smb_client_machine(machine)
    share = validate_smb_client_share(share)
    mount_path = normalize_smb_client_mount_path(mount_path)
    username = validate_smb_client_identity(username, "Nom d'utilisateur")
    domain = validate_smb_client_identity(domain, "Domaine")
    section = smb_client_section(machine, share, mount_path)
    parser = ensure_smb_client_conf()
    if not parser.has_section(section):
        parser.add_section(section)
    credential_path = smb_client_credential_path(section)
    if username:
        if password is not None:
            write_smb_client_auth_file(credential_path, username, password, domain)
    else:
        try:
            credential_path.unlink(missing_ok=True)
        except OSError:
            pass
    parser[section].update({
        "MACHINE": machine,
        "SHARE": share,
        "MOUNT_PATH": mount_path,
        "USERNAME": username,
        "DOMAIN": domain,
        "GUEST": "0" if username else "1",
        "MODE_0777": "1" if mode_0777 else "0",
        "OPTIONS": str(options or smb_client_general()["default_options"]),
        "CREDENTIAL_FILE": str(credential_path) if username else "",
    })
    write_smb_client_conf(parser)
    append_smb_client_log(
        f"[CONF] Profil enregistre : //{machine}/{share} -> {mount_path} "
        f"({'0777' if mode_0777 else 'droits serveur'}, {'utilisateur ' + username if username else 'invite'})"
    )
    return section


def smb_client_redact_command(cmd: list[str]) -> str:
    rendered = shlex.join([str(item) for item in cmd])
    rendered = re.sub(r"(?i)(password|pass)=[^,\s']+", r"\1=***", rendered)
    rendered = re.sub(r"(?i)credentials=[^,\s']+", "credentials=<fichier-protege>", rendered)
    return rendered


def smb_client_host_command(cmd: list[str]) -> list[str]:
    if os.path.exists("/.dockerenv") and (shutil.which("nsenter") or os.path.exists("/usr/bin/nsenter")):
        return ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", *cmd]
    return cmd


def smb_client_run_cmd(
    cmd: list[str], timeout: int = 20, *, log: bool = True, host: bool = True
) -> tuple[int, str, str]:
    command = smb_client_host_command(cmd) if host else list(cmd)
    if log:
        append_smb_client_log("$ " + smb_client_redact_command(cmd))
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if log and out:
            append_smb_client_log("[stdout] " + out.replace("\n", "\n[stdout] "))
        if log and err:
            append_smb_client_log("[stderr] " + err.replace("\n", "\n[stderr] "))
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        if log:
            append_smb_client_log(f"[TIMEOUT] Commande interrompue apres {timeout}s")
        return 124, "", "timeout"
    except Exception as exc:
        if log:
            append_smb_client_log(f"[ERREUR] {exc}")
        return 127, "", str(exc)


def smb_client_server_reachable(machine: str, attempts: int | None = None) -> bool:
    machine = validate_smb_client_machine(machine)
    general = smb_client_general()
    tries = attempts or general["mount_retries"]
    for attempt in range(1, tries + 1):
        try:
            with socket.create_connection((machine, 445), timeout=general["connect_timeout"]):
                return True
        except OSError as exc:
            append_smb_client_log(f"[RESEAU] Essai {attempt}/{tries} vers {machine}:445 : {exc}")
            if attempt < tries and general["retry_sleep"]:
                time.sleep(general["retry_sleep"])
    return False


def smb_client_temporary_auth_file(username: str, password: str, domain: str = "") -> Path:
    fd, name = tempfile.mkstemp(prefix="yoleo-smb-", suffix=".cred")
    os.close(fd)
    path = Path(name)
    write_smb_client_auth_file(path, username, password, domain)
    return path


def smb_client_discovery_error(detail: str, machine: str) -> str:
    upper = str(detail or "").upper()
    if any(token in upper for token in ("LOGON_FAILURE", "ACCESS_DENIED", "WRONG_PASSWORD")):
        return "Authentification Samba refusee. Verifie le nom d'utilisateur et le mot de passe."
    if "TIMEOUT" in upper:
        return f"Serveur Samba {machine} indisponible (delai depasse)."
    return f"Impossible de parcourir les partages de {machine}. {str(detail or '').strip()}".strip()


def smb_client_discover_shares(machine: str, username: str = "", password: str = "", domain: str = "") -> dict:
    machine = validate_smb_client_machine(machine)
    username = validate_smb_client_identity(username, "Nom d'utilisateur")
    domain = validate_smb_client_identity(domain, "Domaine")
    if not smb_client_server_reachable(machine):
        tries = smb_client_general()["mount_retries"]
        return {"ok": False, "shares": [], "message": f"Serveur Samba indisponible apres {tries} essais."}

    auth_path = None
    try:
        if username:
            auth_path = smb_client_temporary_auth_file(username, password, domain)
        timeout = smb_client_general()["discovery_timeout"]
        smbclient_bin = shutil.which("smbclient")
        if smbclient_bin:
            cmd = [smbclient_bin, "-g", "-L", machine]
            cmd += ["-A", str(auth_path)] if auth_path else ["-N"]
            code, out, err = smb_client_run_cmd(cmd, timeout=timeout)
            shares = []
            for line in out.splitlines():
                parts = line.split("|", 2)
                if len(parts) >= 2 and parts[0].strip().lower() == "disk":
                    name = parts[1].strip()
                    if name:
                        shares.append({"name": name, "comment": parts[2].strip() if len(parts) > 2 else ""})
        else:
            net_bin = shutil.which("net")
            if not net_bin:
                return {"ok": False, "shares": [], "message": "Outil Samba client absent (smbclient ou net)."}
            cmd = [net_bin]
            cmd += ["-A", str(auth_path)] if auth_path else ["-N"]
            cmd += ["rpc", "share", "list", "-S", machine]
            code, out, err = smb_client_run_cmd(cmd, timeout=timeout)
            shares = [
                {"name": line.strip(), "comment": ""}
                for line in out.splitlines()
                if line.strip()
                and line.strip().casefold() not in {"ipc$", "anonymous login successful"}
                and not line.lstrip().startswith(("WARNING", "Unable"))
            ]
        unique = {item["name"].casefold(): item for item in shares}
        shares = sorted(unique.values(), key=lambda item: item["name"].casefold())
        if code != 0:
            return {"ok": False, "shares": [], "message": smb_client_discovery_error(err or out, machine)}
        message = f"{len(shares)} partage(s) Samba trouve(s)." if shares else "Aucun partage disque visible avec ces identifiants."
        append_smb_client_log(f"[PARCOURIR] {machine} : {message}")
        return {"ok": True, "shares": shares, "message": message}
    finally:
        if auth_path:
            try:
                auth_path.unlink(missing_ok=True)
            except OSError:
                pass


def smb_client_live_mounts() -> list[dict]:
    code, out, _ = smb_client_run_cmd(
        ["findmnt", "--json", "-t", "cifs", "-o", "TARGET,SOURCE,OPTIONS"],
        timeout=8,
        log=False,
    )
    if code not in {0, 1} or not out:
        return []
    try:
        filesystems = json.loads(out).get("filesystems", [])
    except Exception:
        return []
    result = []
    for item in filesystems:
        target = str(item.get("target") or "")
        source = str(item.get("source") or "")
        options = str(item.get("options") or "")
        if not source.startswith("//") or "/" not in source[2:]:
            continue
        try:
            mount_path = normalize_smb_client_mount_path(target)
            machine, share = source[2:].split("/", 1)
            machine = validate_smb_client_machine(machine)
            share = validate_smb_client_share(share)
        except ValueError:
            continue
        result.append({
            "machine": machine,
            "share": share,
            "mount_path": mount_path,
            "source": source,
            "runtime_options": options,
            "guest": "sec=none" in options.split(","),
            "mode_0777": "file_mode=0777" in options and "dir_mode=0777" in options,
        })
    return result


def smb_client_current_source(mount_path: str) -> str:
    # --target renvoie aussi le système de fichiers parent d'un simple dossier
    # (par exemple /dev/nvme... pour /mnt/smb/backup). Seul un montage posé
    # exactement sur ce point doit bloquer le montage ou être démonté.
    code, out, _ = smb_client_run_cmd(
        ["findmnt", "-n", "-o", "SOURCE", "--mountpoint", mount_path.rstrip("/")],
        timeout=5,
        log=False,
    )
    return out.splitlines()[0].strip() if code == 0 and out else ""


def smb_client_clean_options(options: str, mode_0777: bool, guest: bool, credential_file: str = "") -> str:
    blocked = {"credentials", "cred", "username", "user", "password", "pass", "domain", "guest", "file_mode", "dir_mode"}
    cleaned = []
    seen = set()
    for raw in str(options or "").split(","):
        part = raw.strip()
        if not part:
            continue
        key = part.split("=", 1)[0].strip().lower()
        if key in blocked or key in seen:
            continue
        seen.add(key)
        cleaned.append(part)
    if mode_0777:
        cleaned.extend(["file_mode=0777", "dir_mode=0777"])
    if guest:
        cleaned.append("guest")
    elif credential_file:
        cleaned.append("credentials=" + credential_file)
    return ",".join(cleaned)


def load_smb_client_mounts() -> list[dict]:
    parser = ensure_smb_client_conf()
    status = read_smb_client_status()
    live = smb_client_live_mounts()
    live_by_path = {os.path.normpath(item["mount_path"]): item for item in live}
    known_keys = set()
    mounts = []
    for section in parser.sections():
        if not section.startswith("SMB:"):
            continue
        row = parser[section]
        try:
            machine = validate_smb_client_machine(row.get("MACHINE", ""))
            share = validate_smb_client_share(row.get("SHARE", ""))
            mount_path = normalize_smb_client_mount_path(row.get("MOUNT_PATH", ""))
        except ValueError as exc:
            append_smb_client_log(f"[CONF][WARN] Profil ignore {section} : {exc}")
            continue
        expected = smb_client_source(machine, share)
        live_item = live_by_path.get(os.path.normpath(mount_path))
        mounted = bool(live_item and smb_client_source_key(live_item["source"]) == smb_client_source_key(expected))
        conflict = bool(live_item and not mounted)
        known_keys.add((os.path.normpath(mount_path), smb_client_source_key(expected)))
        username = row.get("USERNAME", "")
        mounts.append({
            "section": section,
            "managed": True,
            "machine": machine,
            "share": share,
            "source": expected,
            "mount_path": mount_path,
            "username": username,
            "domain": row.get("DOMAIN", ""),
            "guest": row.getboolean("GUEST", fallback=not bool(username)),
            "mode_0777": row.getboolean("MODE_0777", fallback=True),
            "options": row.get("OPTIONS", smb_client_general()["default_options"]),
            "credential_file": row.get("CREDENTIAL_FILE", ""),
            "credentials_ready": (not username) or Path(row.get("CREDENTIAL_FILE", "")).is_file(),
            "mounted": mounted,
            "conflict": conflict,
            "state": "Monte" if mounted else ("Point local occupe" if conflict else "Non monte"),
            "job": status.get(section, {}),
            "can_adopt": False,
        })
    for item in live:
        key = (os.path.normpath(item["mount_path"]), smb_client_source_key(item["source"]))
        if key in known_keys:
            continue
        synthetic = "LIVE:" + hashlib.sha256((item["source"] + "\x00" + item["mount_path"]).encode("utf-8")).hexdigest()[:20]
        mounts.append({
            "section": synthetic,
            "managed": False,
            "machine": item["machine"],
            "share": item["share"],
            "source": item["source"],
            "mount_path": item["mount_path"],
            "username": "",
            "domain": "",
            "guest": item["guest"],
            "mode_0777": item["mode_0777"],
            "options": "",
            "credentials_ready": item["guest"],
            "mounted": True,
            "conflict": False,
            "state": "Monte (externe)",
            "job": {},
            "can_adopt": item["guest"],
        })
    mounts.sort(key=lambda item: (item["machine"].casefold(), item["share"].casefold(), item["mount_path"]))
    return mounts


def mount_smb_client_section(section: str) -> bool:
    parser = ensure_smb_client_conf()
    if not section.startswith("SMB:") or not parser.has_section(section):
        set_smb_client_status(section, "failed", "Profil inconnu")
        return False
    row = parser[section]
    machine = validate_smb_client_machine(row.get("MACHINE", ""))
    share = validate_smb_client_share(row.get("SHARE", ""))
    mount_path = normalize_smb_client_mount_path(row.get("MOUNT_PATH", ""))
    source = smb_client_source(machine, share)
    username = row.get("USERNAME", "")
    guest = row.getboolean("GUEST", fallback=not bool(username))
    credential_file = row.get("CREDENTIAL_FILE", "")
    mode_0777 = row.getboolean("MODE_0777", fallback=True)
    general = smb_client_general()

    current_source = smb_client_current_source(mount_path)
    if current_source:
        if smb_client_source_key(current_source) == smb_client_source_key(source):
            append_smb_client_log(f"[OK] Deja monte : {source} -> {mount_path}")
            return True
        set_smb_client_status(section, "failed", "Point local deja utilise")
        append_smb_client_log(f"[REFUS] {mount_path} est deja monte depuis {current_source}")
        return False
    if username and (not credential_file or not Path(credential_file).is_file()):
        set_smb_client_status(section, "failed", "Identifiants proteges manquants")
        return False
    if not smb_client_server_reachable(machine):
        message = f"Serveur indisponible apres {general['mount_retries']} essais"
        set_smb_client_status(section, "failed", message)
        append_smb_client_log(f"[NON MONTE] {source} : {message}")
        return False

    mkdir_code, _, mkdir_err = smb_client_run_cmd(["mkdir", "-p", mount_path.rstrip("/")], timeout=8)
    if mkdir_code != 0:
        set_smb_client_status(section, "failed", "Creation du point local impossible")
        append_smb_client_log(f"[NON MONTE] mkdir {mount_path} : {mkdir_err}")
        return False
    mount_bin = shutil.which("mount.cifs") or "/sbin/mount.cifs"
    if not os.path.exists(mount_bin):
        set_smb_client_status(section, "failed", "mount.cifs absent : installer cifs-utils")
        return False
    options = smb_client_clean_options(
        row.get("OPTIONS", general["default_options"]), mode_0777, guest, credential_file
    )
    for attempt in range(1, general["mount_retries"] + 1):
        set_smb_client_status(section, "running", f"Montage {attempt}/{general['mount_retries']}")
        append_smb_client_log(
            f"[MONTAGE] Essai {attempt}/{general['mount_retries']} : {source} -> {mount_path}"
        )
        code, out, err = smb_client_run_cmd(
            [mount_bin, source, mount_path.rstrip("/"), "-o", options],
            timeout=general["mount_timeout"],
        )
        if code == 0 and smb_client_source_key(smb_client_current_source(mount_path)) == smb_client_source_key(source):
            append_smb_client_log(f"[OK] Monte : {source} -> {mount_path}")
            return True
        detail = (err or out or "erreur inconnue").strip()
        append_smb_client_log(f"[ECHEC] Essai {attempt} : {detail}")
        if attempt < general["mount_retries"] and general["retry_sleep"]:
            time.sleep(general["retry_sleep"])
    set_smb_client_status(section, "failed", "Montage impossible apres plusieurs essais")
    return False


def _smb_client_mount_worker(section: str) -> None:
    global SMB_CLIENT_QUEUE_RUNNING
    try:
        set_smb_client_status(section, "running", "Montage en cours")
        ok = mount_smb_client_section(section)
        set_smb_client_status(section, "done" if ok else "failed", "Monte" if ok else read_smb_client_status().get(section, {}).get("message", "Montage impossible"))
    except Exception as exc:
        append_smb_client_log(f"[QUEUE][ERREUR] {exc}")
        set_smb_client_status(section, "failed", str(exc))
    finally:
        with SMB_CLIENT_QUEUE_LOCK:
            SMB_CLIENT_QUEUE_RUNNING = False


def start_smb_client_mount(section: str) -> bool:
    global SMB_CLIENT_QUEUE_RUNNING
    with SMB_CLIENT_QUEUE_LOCK:
        if SMB_CLIENT_QUEUE_RUNNING:
            return False
        SMB_CLIENT_QUEUE_RUNNING = True
    set_smb_client_status(section, "queued", "En attente")
    threading.Thread(target=_smb_client_mount_worker, args=(section,), daemon=True).start()
    return True


def smb_client_queue_is_running() -> bool:
    with SMB_CLIENT_QUEUE_LOCK:
        return SMB_CLIENT_QUEUE_RUNNING


def unmount_smb_client_section(section: str) -> tuple[bool, str]:
    parser = ensure_smb_client_conf()
    if not section.startswith("SMB:") or not parser.has_section(section):
        return False, "Profil Samba client inconnu."
    row = parser[section]
    mount_path = normalize_smb_client_mount_path(row.get("MOUNT_PATH", ""))
    expected = smb_client_source(row.get("MACHINE", ""), row.get("SHARE", ""))
    current = smb_client_current_source(mount_path)
    if not current:
        return True, "Partage deja demonte."
    if smb_client_source_key(current) != smb_client_source_key(expected):
        return False, "Le point local est occupe par un autre montage : demontage refuse."
    code, out, err = smb_client_run_cmd(["umount", "-l", mount_path.rstrip("/")], timeout=15)
    if code != 0:
        return False, (err or out or "Demontage impossible.")
    set_smb_client_status(section, "done", "Demonte")
    append_smb_client_log(f"[DEMONTAGE] {expected} -> {mount_path}")
    return True, "Partage demonte."


def cleanup_smb_client_mount_dir(mount_path: str) -> None:
    try:
        mount_path = normalize_smb_client_mount_path(mount_path)
        if not smb_client_current_source(mount_path):
            smb_client_run_cmd(["rmdir", mount_path.rstrip("/")], timeout=5, log=False)
    except Exception:
        pass


def delete_smb_client_profile(section: str) -> tuple[bool, str]:
    parser = ensure_smb_client_conf()
    if not section.startswith("SMB:") or not parser.has_section(section):
        return False, "Profil Samba client inconnu."
    mount_path = parser[section].get("MOUNT_PATH", "")
    ok, message = unmount_smb_client_section(section)
    if not ok:
        return False, message
    credential_file = parser[section].get("CREDENTIAL_FILE", "")
    parser.remove_section(section)
    write_smb_client_conf(parser)
    clear_smb_client_status(section)
    if credential_file:
        try:
            credential_path = Path(credential_file)
            if credential_path.parent == SMB_CLIENT_CREDENTIALS_DIR:
                credential_path.unlink(missing_ok=True)
        except OSError:
            pass
    cleanup_smb_client_mount_dir(mount_path)
    return True, "Profil supprime."


def adopt_live_smb_client_mount(section: str) -> tuple[bool, str]:
    for item in load_smb_client_mounts():
        if item.get("section") != section or item.get("managed"):
            continue
        if not item.get("can_adopt"):
            return False, "Ce montage authentifie doit etre ajoute avec ses identifiants."
        save_smb_client_profile(
            item["machine"], item["share"], item["mount_path"], mode_0777=item.get("mode_0777", True)
        )
        return True, "Montage actif enregistre comme profil manuel."
    return False, "Montage actif introuvable."


def adopt_all_live_smb_client_mounts() -> tuple[bool, str]:
    candidates = [
        item for item in load_smb_client_mounts()
        if not item.get("managed") and item.get("can_adopt")
    ]
    imported = 0
    for item in candidates:
        save_smb_client_profile(
            item["machine"],
            item["share"],
            item["mount_path"],
            mode_0777=item.get("mode_0777", True),
        )
        imported += 1
    if not imported:
        return False, "Aucun montage invite actif a enregistrer."
    return True, f"{imported} montage(s) actif(s) enregistre(s) comme profils manuels."


def smb_client_payload() -> dict:
    mounts = load_smb_client_mounts()
    return {
        "general": smb_client_general(),
        "mounts": mounts,
        "mounted_count": sum(1 for item in mounts if item.get("mounted")),
        "managed_count": sum(1 for item in mounts if item.get("managed")),
        "external_count": sum(1 for item in mounts if not item.get("managed")),
        "queue_running": smb_client_queue_is_running(),
        "updated_at": smb_client_now_label(),
    }
