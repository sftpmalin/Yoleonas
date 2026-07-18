DANGEROUS_EXACT_PATHS = {
    "/",
    "/mnt",
    "/mnt/user",
    "/mnt/user0",
    "/mnt/cache",
    "/home",
    "/root",
    "/boot",
}


@dataclass(frozen=True)
class EngineAction:
    key: str
    title: str
    source: Optional[Path]
    destination: Optional[Path]
    command_start: str = ""
    command_end: str = ""
    aliases: Tuple[str, ...] = ()
    enabled: bool = True
    include_in_all: bool = True
    archive: bool = False
    archive_format: str = "tar.7z"
    archive_children: bool = True
    archive_name: str = ""
    compression_level: str = "7"
    replace_existing: bool = True
    date_suffix: bool = False
    delete_extra: bool = False
    excludes: Tuple[str, ...] = ()
    allow_dangerous_source: bool = False
    allow_destination_inside_source: bool = False
    docker_exclude: Tuple[str, ...] = ()
    wait_start_docker_stopped: str = ""
    wait_end_docker_running: str = ""
    docker_wait_timeout: float = 0.0
    docker_wait_interval: float = 2.0

    @property
    def has_backup(self) -> bool:
        return self.source is not None and self.destination is not None

    @property
    def is_command_only(self) -> bool:
        return not self.has_backup and bool(self.command_start.strip() or self.command_end.strip())

    @property
    def mode_label(self) -> str:
        if not self.has_backup:
            return "commande"
        if self.archive:
            scope = "children" if self.archive_children else "source"
            return f"archive {self.archive_format} ({scope})"
        return "copie rsync"


@dataclass(frozen=True)
class EngineConfig:
    conf_file: Path
    base_dir: Path
    actions: Dict[str, EngineAction]
    aliases: Dict[str, str]
    lock_file: Path
    log_dir: Path


@dataclass
class EngineRuntimeContext:
    date_stamp: str
    stopped_by_backup: Dict[str, List[str]]
    running_before: Dict[str, List[str]]
    total_actions: int = 0
    completed_actions: int = 0
    current_action_index: int = 0


class EngineLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fp = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = self.path.open("w")
        try:
            fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"ERREUR : archive déjà en cours : {self.path}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fp:
            fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
            self.fp.close()


class ArchiveJob:
    def __init__(self, *, target: str, label: str, dry_run: bool, log_file: Path) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.target = target
        self.label = label
        self.dry_run = dry_run
        self.log_file = log_file
        self.status = "queued"
        self.returncode: Optional[int] = None
        self.error = ""
        self.started_at = now_text()
        self.ended_at = ""
        self.stop_requested = False
        self._lock = threading.RLock()
        self._processes: List[subprocess.Popen] = []
        self.thread: Optional[threading.Thread] = None
        self.tmux_session: str = ""
        self.tmux_managed: bool = False
        self.progress_percent: Optional[int] = None
        self.progress_label: str = ""
        self.progress_phase: str = "idle"
        self.progress_target: str = target
        self.progress_rows: Dict[str, Dict[str, object]] = {}
        self._progress_last_emit: Dict[str, Tuple[object, ...]] = {}

    def log(self, *parts: object) -> None:
        text = " ".join(str(p) for p in parts)
        with self._lock:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a", encoding="utf-8", errors="replace") as fp:
                fp.write(text + "\n")

    def read_log(self, max_bytes: int = 240000) -> str:
        try:
            data = self.log_file.read_bytes()
            if len(data) > max_bytes:
                data = data[-max_bytes:]
                prefix = b"... log tronque aux derniers octets ...\n"
                data = prefix + data
            return data.decode("utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    def request_stop(self) -> None:
        self.stop_requested = True
        self.log("\n⏹️ Arrêt demandé depuis Flask.")
        with self._lock:
            for proc in list(self._processes):
                try:
                    if proc.poll() is None:
                        proc.terminate()
                except Exception:
                    pass

        # Si le job tourne dans tmux, Flask ne possède pas les sous-processus.
        # On ferme donc la session tmux stable du profil, par exemple Toto-archive.
        if self.tmux_session and shutil.which("tmux"):
            try:
                subprocess.run(["tmux", "kill-session", "-t", self.tmux_session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                self.log(f"⏹️ Session tmux fermée : {self.tmux_session}")
            except Exception as exc:
                self.log(f"⚠️ Impossible de fermer la session tmux {self.tmux_session} : {exc}")

    def _register_proc(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._processes.append(proc)

    def _unregister_proc(self, proc: subprocess.Popen) -> None:
        with self._lock:
            try:
                self._processes.remove(proc)
            except ValueError:
                pass

    def run_process(self, cmd: List[str], *, input_text: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> int:
        if self.stop_requested:
            raise RuntimeError("Arrêt demandé avant lancement de commande.")
        self.log(">>>", quote_cmd(cmd))
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if input_text is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
        except FileNotFoundError:
            self.log(f"❌ Commande introuvable : {cmd[0]}")
            return 127
        self._register_proc(proc)
        try:
            if input_text is not None and proc.stdin is not None:
                try:
                    proc.stdin.write(input_text)
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
            assert proc.stdout is not None
            for line in proc.stdout:
                self.log(line.rstrip("\n"))
                if self.stop_requested and proc.poll() is None:
                    proc.terminate()
            rc = proc.wait()
            if self.stop_requested and rc not in (0, 130, 143, -15):
                self.log(f"⏹️ Commande interrompue : code {rc}")
            return rc
        finally:
            self._unregister_proc(proc)


JOBS: Dict[str, ArchiveJob] = {}
JOBS_LOCK = threading.RLock()
ACTIVE_JOB_ID: Optional[str] = None


def now_stamp() -> str:
    return time.strftime("%Y-%m-%d_%H%M")


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def quote_cmd(cmd: Iterable[str]) -> str:
    return " ".join(shlex.quote(str(x)) for x in cmd)


ARCHIVE_PROGRESS_PREFIX = "YOLEO_ARCHIVE_PROGRESS "


def archive_format_bytes_short(value: int | float | None) -> str:
    try:
        size = float(value or 0)
    except Exception:
        size = 0.0
    units = ("o", "K", "M", "G", "T", "P")
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    if size >= 100:
        return f"{size:.0f} {units[idx]}"
    if size >= 10:
        return f"{size:.1f} {units[idx]}"
    return f"{size:.2f} {units[idx]}"


def archive_parse_rsync_size_to_bytes(text: str | None) -> Optional[int]:
    if not text:
        return None
    raw = str(text).replace("\u202f", " ").replace("\xa0", " ").strip()
    raw = re.sub(r"(?i)bytes?|octets?", "", raw)
    raw = raw.replace("/s", " ").strip()
    m = re.search(r"([0-9][0-9\s,\.]*)\s*([kmgtpe]?i?b?|[kmgtpe])?", raw, re.IGNORECASE)
    if not m:
        return None
    num = (m.group(1) or "").strip().replace(" ", "")
    unit = (m.group(2) or "").strip().lower()
    if not num:
        return None
    if unit:
        # Avec une unité, une virgule unique est souvent décimale en sortie locale.
        if "," in num and "." not in num and num.count(",") == 1:
            num = num.replace(",", ".")
        else:
            num = num.replace(",", "")
    else:
        # Sans unité, rsync affiche souvent les milliers avec des virgules.
        num = num.replace(",", "")
    try:
        value = float(num)
    except ValueError:
        return None
    unit = unit.replace("ib", "").replace("b", "")
    multipliers = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4, "p": 1024**5, "e": 1024**6}
    return int(value * multipliers.get(unit, 1))


def archive_parse_rsync_progress_bytes(line: str) -> Optional[int]:
    clean = (line or "").strip().replace("\r", "")
    # Exemples rsync --info=progress2 :
    #   1,234,567  42% ...
    #   850.4M  42% ...
    m = re.search(r"^\s*([0-9][0-9\s,\.]*\s*(?:[kmgtpe]?i?b?|bytes?)?)\s+\d{1,3}%", clean, re.IGNORECASE)
    if not m:
        return None
    return archive_parse_rsync_size_to_bytes(m.group(1))


def archive_parse_rsync_line_percent(line: str) -> Optional[int]:
    m = re.search(r"\s(\d{1,3})%\s", " " + (line or "") + " ")
    if not m:
        return None
    try:
        return max(0, min(100, int(m.group(1))))
    except ValueError:
        return None


def archive_parse_rsync_stats_total(line: str) -> Optional[int]:
    clean = (line or "").strip()
    if not re.search(r"(?i)^total transferred file size\s*:", clean):
        return None
    return archive_parse_rsync_size_to_bytes(clean.split(":", 1)[1])


def archive_progress_clean_target(target: str | None) -> str:
    raw = clean_text(target or "")
    if raw in {"", "--all", "all"}:
        return "__all__"
    return raw


def archive_update_job_progress_attrs(job: ArchiveJob, payload: Dict[str, object]) -> None:
    target = archive_progress_clean_target(str(payload.get("target") or job.target or "__all__"))
    payload = dict(payload)
    payload["target"] = target
    if target == "__all__":
        job.progress_target = target
        job.progress_phase = str(payload.get("phase") or "")
        job.progress_label = str(payload.get("label") or "")
        pct = payload.get("percent")
        job.progress_percent = int(pct) if isinstance(pct, (int, float)) else None
    else:
        job.progress_rows[target] = payload


def archive_log_progress(job: ArchiveJob, target: str, phase: str, percent: Optional[int | float], label: str = "", *, state: str = "running", force: bool = False) -> None:
    clean_target = archive_progress_clean_target(target)
    pct: Optional[int]
    if percent is None:
        pct = None
    else:
        try:
            pct = int(max(0, min(100, round(float(percent)))))
        except Exception:
            pct = None
    payload: Dict[str, object] = {
        "target": clean_target,
        "phase": phase or "running",
        "percent": pct,
        "label": label or "",
        "state": state or "running",
        "time": now_text(),
    }
    archive_update_job_progress_attrs(job, payload)
    sig = (payload["phase"], payload["percent"], payload["label"], payload["state"])
    if not force and job._progress_last_emit.get(clean_target) == sig:
        return
    job._progress_last_emit[clean_target] = sig
    try:
        job.log(ARCHIVE_PROGRESS_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        pass


def archive_context_percent(context: EngineRuntimeContext, local_percent: Optional[int | float]) -> Optional[int]:
    total = max(0, int(getattr(context, "total_actions", 0) or 0))
    if total <= 0:
        if local_percent is None:
            return None
        return int(max(0, min(100, round(float(local_percent)))))
    completed = max(0, int(getattr(context, "completed_actions", 0) or 0))
    if local_percent is None:
        return int(max(0, min(99, round((completed * 100.0) / max(1, total)))))
    local = max(0.0, min(100.0, float(local_percent)))
    return int(max(0, min(99, round(((completed + (local / 100.0)) * 100.0) / max(1, total)))))


def archive_log_overall_progress(job: ArchiveJob, action: EngineAction, context: EngineRuntimeContext, phase: str, local_percent: Optional[int | float], label: str = "", *, state: str = "running") -> None:
    total = max(0, int(getattr(context, "total_actions", 0) or 0))
    index = int(getattr(context, "current_action_index", 0) or 0) + 1
    prefix = f"{action.title} ({index}/{total})" if total else action.title
    if label:
        text = f"{prefix} · {label}"
    elif local_percent is None:
        text = prefix
    else:
        text = f"{prefix} · {int(round(float(local_percent)))} %"
    archive_log_progress(job, "__all__", phase, archive_context_percent(context, local_percent), text, state=state)


def archive_parse_progress_from_log(log_text: str) -> Dict[str, object]:
    overall: Dict[str, object] = {}
    rows: Dict[str, Dict[str, object]] = {}
    for line in (log_text or "").splitlines():
        if not line.startswith(ARCHIVE_PROGRESS_PREFIX):
            continue
        try:
            payload = json.loads(line[len(ARCHIVE_PROGRESS_PREFIX):])
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        target = archive_progress_clean_target(str(payload.get("target") or ""))
        payload["target"] = target
        if target == "__all__":
            overall = payload
        else:
            rows[target] = payload
    return {"overall": overall, "rows": rows}


def engine_title(job: ArchiveJob, text: str) -> None:
    job.log("=" * 60)
    job.log(text)
    job.log("=" * 60)


def engine_split_csv(value: str) -> Tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.replace("\n", ",").split(",") if part.strip())


def engine_parse_float(value: Optional[str], default: float = 0.0) -> float:
    if value is None or str(value).strip() == "":
        return default
    try:
        return float(str(value).strip().replace(",", "."))
    except ValueError:
        return default


def engine_normalize_token(value: str) -> str:
    value = value.strip().lower().replace("_", "-")
    value = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def engine_base_from_conf(conf_file: Path) -> Path:
    parent = conf_file.resolve().parent
    if parent.name == "conf":
        return parent.parent
    return BASE_DIR


def engine_resolve_path(text: Optional[str], *, base_dir: Path, default: Optional[Path] = None) -> Optional[Path]:
    if text is None or not str(text).strip():
        return default
    raw = os.path.expandvars(os.path.expanduser(str(text).strip()))
    path = Path(raw)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def engine_command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def engine_find_7z() -> Optional[str]:
    for name in ("7z", "7zz", "7za"):
        found = shutil.which(name)
        if found:
            return found
    return None


def engine_load_config(path: Path) -> EngineConfig:
    # Utilise le même parser que l'édition Flask pour garantir le même langage INI.
    ensure_real_conf(path)
    parser = configparser.ConfigParser(interpolation=None, allow_no_value=False)
    parser.optionxform = str.lower
    with path.open("r", encoding="utf-8", errors="replace") as fp:
        parser.read_file(fp)

    base_dir = engine_base_from_conf(path)
    settings = parser["settings"] if parser.has_section("settings") else {}
    log_dir = engine_resolve_path(settings.get("log_dir", "/var/log/archive"), base_dir=base_dir, default=Path("/var/log/archive")) or Path("/var/log/archive")
    lock_file = engine_resolve_path(settings.get("lock_file", "/tmp/archive.py.lock"), base_dir=base_dir, default=Path("/tmp/archive.py.lock")) or Path("/tmp/archive.py.lock")

    actions: Dict[str, EngineAction] = {}
    aliases: Dict[str, str] = {}

    for section in parser.sections():
        if section.lower() == "settings" or section.startswith("group:"):
            continue
        sec = parser[section]
        key = section.strip()
        source_text = sec.get("source", "").strip()
        destination_text = sec.get("destination", "").strip()
        source = engine_resolve_path(source_text, base_dir=base_dir) if source_text else None
        destination = engine_resolve_path(destination_text, base_dir=base_dir) if destination_text else None
        if bool(source) != bool(destination):
            raise RuntimeError(f"Section incomplète [{section}] : source et destination doivent être remplies ensemble, ou vides ensemble.")

        command_start = first_non_empty(sec.get("command_start", ""), sec.get("before", ""), sec.get("command_before", ""), sec.get("stop", ""))
        command_end = first_non_empty(sec.get("command_end", ""), sec.get("after", ""), sec.get("command_after", ""), sec.get("start", ""))

        action = EngineAction(
            key=key,
            title=sec.get("title", key).strip() or key,
            source=source,
            destination=destination,
            command_start=command_start,
            command_end=command_end,
            aliases=engine_split_csv(sec.get("aliases", "")),
            enabled=parse_bool(sec.get("enabled", "true"), True),
            include_in_all=parse_bool(sec.get("include_in_all", "true"), True),
            archive=parse_bool(sec.get("archive", sec.get("tar", "false")), False),
            archive_format=sec.get("archive_format", sec.get("format", "tar.7z")).strip().lower() or "tar.7z",
            archive_children=parse_bool(sec.get("archive_children", "true"), True),
            archive_name=sec.get("archive_name", "").strip(),
            compression_level=sec.get("compression_level", "7").strip() or "7",
            replace_existing=parse_bool(sec.get("replace_existing", "true"), True),
            date_suffix=parse_bool(sec.get("date_suffix", "false"), False),
            delete_extra=parse_bool(sec.get("delete_extra", "false"), False),
            excludes=engine_split_csv(sec.get("excludes", "")),
            allow_dangerous_source=parse_bool(sec.get("allow_dangerous_source", "false"), False),
            allow_destination_inside_source=parse_bool(sec.get("allow_destination_inside_source", "false"), False),
            docker_exclude=engine_split_csv(sec.get("docker_exclude", "")),
            wait_start_docker_stopped=sec.get("wait_start_docker_stopped", "").strip(),
            wait_end_docker_running=sec.get("wait_end_docker_running", "").strip(),
            docker_wait_timeout=engine_parse_float(sec.get("docker_wait_timeout", "0"), 0.0),
            docker_wait_interval=engine_parse_float(sec.get("docker_wait_interval", "2"), 2.0),
        )
        if action.archive_format not in {"tar.gz", "tgz", "gz", "tar.7z", "7z"}:
            raise RuntimeError(f"Format archive inconnu dans [{section}] : {action.archive_format}")
        actions[key] = action
        aliases[engine_normalize_token(key)] = key
        for alias in action.aliases:
            aliases[engine_normalize_token(alias)] = key

    return EngineConfig(path, base_dir, actions, aliases, lock_file, log_dir)


def first_non_empty(*values: Optional[str]) -> str:
    for value in values:
        if value is not None and value.strip():
            return value.strip()
    return ""


def engine_select_by_prefix(name: str, config: EngineConfig) -> List[str]:
    token = engine_normalize_token(name)
    prefix = token.replace("-", "_") + "_"
    return [key for key in config.actions if key.startswith(prefix)]


def engine_resolve_names(name: str, config: EngineConfig) -> List[str]:
    token = engine_normalize_token(name)
    if token in config.aliases:
        return [config.aliases[token]]
    prefixed = engine_select_by_prefix(name, config)
    if prefixed:
        return prefixed
    raise KeyError(f"Nom inconnu dans archive.conf : {name}")


def engine_realpath_text(path: Path) -> str:
    return os.path.realpath(path)


def engine_is_under_or_equal(path: Path, root: Path) -> bool:
    path_s = engine_realpath_text(path)
    root_s = engine_realpath_text(root)
    return path_s == root_s or path_s.startswith(root_s.rstrip("/") + "/")


def engine_safe_path_checks(action: EngineAction) -> None:
    if action.source is None or action.destination is None:
        return
    src = action.source
    dst = action.destination
    src_real = engine_realpath_text(src)
    dst_real = engine_realpath_text(dst)
    if src_real == dst_real:
        raise RuntimeError(f"ERREUR [{action.key}] : source et destination identiques : {src}")
    if src_real in DANGEROUS_EXACT_PATHS and not action.allow_dangerous_source:
        raise RuntimeError(
            f"ERREUR [{action.key}] : source trop large/dangereuse refusée : {src}\n"
            "Ajoute allow_dangerous_source = 1 seulement si c'est volontaire."
        )
    if dst_real in DANGEROUS_EXACT_PATHS:
        raise RuntimeError(f"ERREUR [{action.key}] : destination trop large/dangereuse refusée : {dst}")
    if engine_is_under_or_equal(dst, src) and not action.allow_destination_inside_source:
        raise RuntimeError(
            f"ERREUR [{action.key}] : destination placée dans la source, risque de boucle archive :\n"
            f"  source      = {src}\n"
            f"  destination = {dst}"
        )


def engine_path_has_payload(path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        next(path.iterdir())
        return True
    except StopIteration:
        return False


def engine_should_exclude(path: Path, patterns: Tuple[str, ...]) -> bool:
    name = path.name
    for pattern in patterns:
        if name == pattern or path.match(pattern):
            return True
    return False


def engine_docker_names(job: ArchiveJob, *, all_containers: bool = False) -> List[str]:
    cmd = ["docker", "ps", "--format", "{{.Names}}"]
    if all_containers:
        cmd = ["docker", "ps", "-a", "--format", "{{.Names}}"]
    try:
        cp = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise RuntimeError("Commande Docker impossible : docker introuvable")
    if cp.returncode != 0:
        raise RuntimeError(f"Commande Docker impossible : {' '.join(cmd)} {(cp.stderr or '').strip()}")
    return [x.strip() for x in cp.stdout.splitlines() if x.strip()]


def engine_filter_docker_exclude(names: Iterable[str], excludes: Tuple[str, ...]) -> List[str]:
    excluded = {x.strip() for x in excludes if x.strip()}
    return [name for name in names if name not in excluded]


def engine_docker_stop(job: ArchiveJob, names: List[str]) -> bool:
    if not names:
        job.log("Aucun container à arrêter.")
        return True
    if job.dry_run:
        job.log("DRY-RUN :", quote_cmd(["docker", "stop", *names]))
        return True
    return job.run_process(["docker", "stop", *names]) == 0


def engine_docker_start(job: ArchiveJob, names: List[str]) -> bool:
    if not names:
        job.log("Aucun container à démarrer.")
        return True
    if job.dry_run:
        job.log("DRY-RUN :", quote_cmd(["docker", "start", *names]))
        return True
    return job.run_process(["docker", "start", *names]) == 0


def engine_docker_spec_names(job: ArchiveJob, spec: str, action: EngineAction, context: EngineRuntimeContext) -> Tuple[str, List[str]]:
    spec = (spec or "").strip()
    if not spec:
        return "none", []
    if spec == "stopped_by_backup":
        return "names", context.stopped_by_backup.get(action.key, [])
    running = engine_docker_names(job, all_containers=False)
    running = engine_filter_docker_exclude(running, action.docker_exclude)
    if spec == "all":
        return "all", running
    return "names", list(engine_split_csv(spec))


def engine_wait_docker_stopped(job: ArchiveJob, spec: str, action: EngineAction, context: EngineRuntimeContext) -> bool:
    spec = (spec or "").strip()
    if not spec:
        return True
    if job.dry_run:
        job.log(f"DRY-RUN : attente containers arrêtés ignorée : {spec}")
        return True
    timeout = action.docker_wait_timeout
    interval = max(action.docker_wait_interval, 0.5)
    start = time.monotonic()
    while True:
        if job.stop_requested:
            return False
        mode, names = engine_docker_spec_names(job, spec, action, context)
        running = set(engine_filter_docker_exclude(engine_docker_names(job, all_containers=False), action.docker_exclude))
        remaining = sorted(running) if mode == "all" else sorted(name for name in names if name in running)
        if not remaining:
            job.log(f"✅ Containers arrêtés : {spec}")
            return True
        job.log(f"⏳ Attente arrêt Docker ({spec}) : {', '.join(remaining)}")
        if timeout > 0 and (time.monotonic() - start) >= timeout:
            job.log(f"❌ Timeout attente Docker arrêtés : {', '.join(remaining)}")
            return False
        time.sleep(interval)


def engine_wait_docker_running(job: ArchiveJob, spec: str, action: EngineAction, context: EngineRuntimeContext) -> bool:
    spec = (spec or "").strip()
    if not spec:
        return True
    if job.dry_run:
        job.log(f"DRY-RUN : attente containers démarrés ignorée : {spec}")
        return True
    timeout = action.docker_wait_timeout
    interval = max(action.docker_wait_interval, 0.5)
    start = time.monotonic()
    while True:
        if job.stop_requested:
            return False
        _, names = engine_docker_spec_names(job, spec, action, context)
        running = set(engine_docker_names(job, all_containers=False))
        missing = sorted(name for name in names if name not in running)
        if not missing:
            job.log(f"✅ Containers démarrés : {spec}")
            return True
        job.log(f"⏳ Attente démarrage Docker ({spec}) : {', '.join(missing)}")
        if timeout > 0 and (time.monotonic() - start) >= timeout:
            job.log(f"❌ Timeout attente Docker démarrés : {', '.join(missing)}")
            return False
        time.sleep(interval)


def engine_shell_join(names: Iterable[str]) -> str:
    return " ".join(shlex.quote(name) for name in names)


def engine_expand_command(command: str, action: EngineAction, context: EngineRuntimeContext) -> str:
    running_before = context.running_before.get(action.key, [])
    stopped = context.stopped_by_backup.get(action.key, [])
    return (
        command.replace("{running_containers}", engine_shell_join(running_before))
        .replace("{stopped_by_backup}", engine_shell_join(stopped))
        .replace("{date}", shlex.quote(context.date_stamp))
    )


def engine_run_builtin_command(job: ArchiveJob, command: str, action: EngineAction, context: EngineRuntimeContext, *, label: str) -> bool:
    parts = shlex.split(command)
    if not parts:
        return True
    name = parts[0]
    if name == "@docker_stop_running":
        running = [] if job.dry_run else engine_docker_names(job, all_containers=False)
        running = engine_filter_docker_exclude(running, action.docker_exclude)
        context.running_before[action.key] = list(running)
        context.stopped_by_backup[action.key] = list(running)
        job.log(f"Containers à arrêter : {', '.join(running) if running else 'aucun'}")
        if not engine_docker_stop(job, running):
            return False
        return engine_wait_docker_stopped(job, "stopped_by_backup", action, context)
    if name == "@docker_start_stopped":
        names = context.stopped_by_backup.get(action.key, [])
        job.log(f"Containers à redémarrer : {', '.join(names) if names else 'aucun'}")
        if not engine_docker_start(job, names):
            return False
        return engine_wait_docker_running(job, "stopped_by_backup", action, context)
    if name == "@sleep":
        seconds = float(parts[1]) if len(parts) > 1 else 1.0
        if job.dry_run:
            job.log(f"DRY-RUN : sleep {seconds}s ignoré")
            return True
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if job.stop_requested:
                return False
            time.sleep(min(0.5, end - time.monotonic()))
        return True
    job.log(f"❌ Commande intégrée inconnue ({label}) : {name}")
    return False


def engine_run_config_command(job: ArchiveJob, command: str, action: EngineAction, context: EngineRuntimeContext, *, label: str) -> bool:
    command = command.strip()
    if not command:
        return True
    job.log("")
    job.log(f">>> {label}")
    job.log(command)
    if command.startswith("@"):
        return engine_run_builtin_command(job, command, action, context, label=label)
    if action.key not in context.running_before:
        try:
            context.running_before[action.key] = engine_docker_names(job, all_containers=False) if engine_command_exists("docker") and not job.dry_run else []
        except Exception:
            context.running_before[action.key] = []
    expanded = engine_expand_command(command, action, context)
    if job.dry_run:
        job.log("DRY-RUN : commande ignorée")
        job.log(expanded)
        return True
    return job.run_process(["bash", "-e", "-o", "pipefail", "-c", expanded]) == 0


def engine_ensure_tools_for_action(action: EngineAction, *, dry_run: bool) -> None:
    if dry_run or not action.has_backup:
        return
    if action.archive:
        if not engine_command_exists("tar"):
            raise RuntimeError("ERREUR : tar introuvable.")
        if action.archive_format in {"tar.7z", "7z"} and not engine_find_7z():
            raise RuntimeError("ERREUR : 7z/7zz/7za introuvable. Installe p7zip / 7zip.")
    else:
        if not engine_command_exists("rsync"):
            raise RuntimeError("ERREUR : rsync introuvable.")


def engine_prepare_destination(job: ArchiveJob, destination: Path) -> None:
    if job.dry_run:
        job.log(f"DRY-RUN : création destination ignorée : {destination}")
        return
    destination.mkdir(parents=True, exist_ok=True)


def engine_build_rsync_cmd(action: EngineAction, *, dry_run: bool) -> List[str]:
    assert action.source is not None and action.destination is not None
    cmd = ["rsync", "-aAXHhv", "--numeric-ids", "--info=progress2", "--stats"]
    if action.delete_extra:
        cmd.append("--delete")
    for pattern in action.excludes:
        cmd.extend(["--exclude", pattern])
    if dry_run:
        cmd.append("--dry-run")
    cmd.extend([str(action.source) + "/", str(action.destination) + "/"])
    return cmd


def engine_estimate_rsync_transfer_bytes(job: ArchiveJob, action: EngineAction) -> int:
    """Dry-run interne pour connaître le volume réel à transférer par rsync."""
    if job.dry_run:
        return 0
    cmd = engine_build_rsync_cmd(action, dry_run=True)
    job.log("Calcul rsync interne pour la progression :")
    job.log(">>>", quote_cmd(cmd))
    total = 0
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return 0
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            clean = line.rstrip("\n")
            job.log(clean)
            parsed = archive_parse_rsync_stats_total(clean)
            if parsed is not None:
                total = max(0, parsed)
            if job.stop_requested and proc.poll() is None:
                proc.terminate()
        proc.wait()
    finally:
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
    return total


def engine_stream_rsync_copy(job: ArchiveJob, action: EngineAction, context: EngineRuntimeContext, total_bytes: int) -> bool:
    cmd = engine_build_rsync_cmd(action, dry_run=job.dry_run)
    job.log(">>>", quote_cmd(cmd))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=0)
    except FileNotFoundError:
        job.log("❌ Commande introuvable : rsync")
        return False
    job._register_proc(proc)
    buffer = ""
    last_percent: Optional[int] = None
    try:
        assert proc.stdout is not None
        while True:
            ch = proc.stdout.read(1)
            if ch == "" and proc.poll() is not None:
                break
            if not ch:
                time.sleep(0.05)
                continue
            if ch in {"\r", "\n"}:
                clean = buffer.strip()
                buffer = ""
                if clean:
                    job.log(clean)
                    moved = archive_parse_rsync_progress_bytes(clean)
                    pct_from_line = archive_parse_rsync_line_percent(clean)
                    pct = pct_from_line
                    label = "Copie rsync…"
                    if moved is not None and total_bytes > 0:
                        pct = int(max(0, min(99, round((moved * 100.0) / max(1, total_bytes)))))
                        label = f"Copie : {archive_format_bytes_short(moved)} / {archive_format_bytes_short(total_bytes)}"
                    elif pct is not None:
                        label = f"Copie : {pct} %"
                    if pct is not None and pct != last_percent:
                        last_percent = pct
                        archive_log_progress(job, action.key, "copy", pct, label, state="running")
                        archive_log_overall_progress(job, action, context, "copy", pct, label, state="running")
                if job.stop_requested and proc.poll() is None:
                    proc.terminate()
            else:
                buffer += ch
        if buffer.strip():
            clean = buffer.strip()
            job.log(clean)
            moved = archive_parse_rsync_progress_bytes(clean)
            pct_from_line = archive_parse_rsync_line_percent(clean)
            pct = pct_from_line
            label = "Copie rsync…"
            if moved is not None and total_bytes > 0:
                pct = int(max(0, min(99, round((moved * 100.0) / max(1, total_bytes)))))
                label = f"Copie : {archive_format_bytes_short(moved)} / {archive_format_bytes_short(total_bytes)}"
            elif pct is not None:
                label = f"Copie : {pct} %"
            if pct is not None:
                archive_log_progress(job, action.key, "copy", pct, label, state="running")
                archive_log_overall_progress(job, action, context, "copy", pct, label, state="running")
        rc = proc.wait()
        if job.stop_requested and rc not in (0, 130, 143, -15):
            job.log(f"⏹️ Commande interrompue : code {rc}")
        return rc == 0
    finally:
        job._unregister_proc(proc)


def engine_copy_payload(job: ArchiveJob, action: EngineAction, context: EngineRuntimeContext) -> bool:
    assert action.source is not None and action.destination is not None
    job.log("")
    engine_title(job, f"COPIE ARCHIVE : {action.title}")
    job.log(f"SOURCE      : {action.source}/")
    job.log(f"DESTINATION : {action.destination}/")
    job.log(f"DELETE_EXTRA: {int(action.delete_extra)}")
    engine_prepare_destination(job, action.destination)
    archive_log_progress(job, action.key, "calcul", None, "Calcul du volume…", state="running")
    archive_log_overall_progress(job, action, context, "calcul", None, "Calcul du volume…", state="running")
    total_bytes = engine_estimate_rsync_transfer_bytes(job, action)
    if total_bytes > 0:
        archive_log_progress(job, action.key, "copy", 0, f"Copie : 0 / {archive_format_bytes_short(total_bytes)}", state="running")
        archive_log_overall_progress(job, action, context, "copy", 0, f"Copie : 0 / {archive_format_bytes_short(total_bytes)}", state="running")
    else:
        archive_log_progress(job, action.key, "copy", None, "Copie rsync…", state="running")
        archive_log_overall_progress(job, action, context, "copy", None, "Copie rsync…", state="running")
    ok = engine_stream_rsync_copy(job, action, context, total_bytes)
    if ok:
        archive_log_progress(job, action.key, "copy", 100, "Copie terminée", state="success")
        archive_log_overall_progress(job, action, context, "copy", 100, "Copie terminée", state="running")
    return ok


def engine_safe_archive_basename(name: str) -> str:
    cleaned = name.strip().replace("/", "_").replace("\0", "")
    return cleaned or "archive"


def engine_archive_output_path(action: EngineAction, item: Path, *, whole_source: bool, date_stamp: str) -> Path:
    assert action.destination is not None
    base = action.archive_name.strip() if whole_source and action.archive_name.strip() else item.name
    base = engine_safe_archive_basename(base)
    if action.date_suffix:
        base = f"{base}_{date_stamp}"
    suffix = ".tar.gz" if action.archive_format in {"tar.gz", "tgz", "gz"} else ".tar.7z"
    return action.destination / f"{base}{suffix}"


def engine_archive_item_tar_gz(job: ArchiveJob, item: Path, outfile: Path) -> bool:
    parent = item.parent
    tmp = outfile.with_name(outfile.name + ".tmp")
    cmd = ["tar", "--xattrs", "--acls", "--numeric-owner", "-czf", str(tmp), "-C", str(parent), item.name]
    if job.dry_run:
        job.log("DRY-RUN :", quote_cmd(cmd))
        job.log(f"DRY-RUN : mv {tmp} {outfile}")
        return True
    if tmp.exists():
        tmp.unlink()
    rc = job.run_process(cmd)
    if rc != 0:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        return False
    tmp.replace(outfile)
    return True


def engine_archive_item_tar_7z(job: ArchiveJob, item: Path, outfile: Path, *, level: str) -> bool:
    seven = engine_find_7z()
    if not seven:
        raise RuntimeError("ERREUR : 7z/7zz/7za introuvable.")
    parent = item.parent
    tmp = outfile.with_name(outfile.name + ".tmp")
    tar_cmd = ["tar", "--xattrs", "--acls", "--numeric-owner", "-C", str(parent), "-cf", "-", item.name]
    seven_cmd = [seven, "a", "-t7z", f"-mx={level}", f"-si{item.name}.tar", str(tmp)]
    if job.dry_run:
        job.log("DRY-RUN : " + quote_cmd(tar_cmd) + " | " + quote_cmd(seven_cmd))
        job.log(f"DRY-RUN : mv {tmp} {outfile}")
        return True
    if tmp.exists():
        tmp.unlink()
    job.log(f">>> {quote_cmd(tar_cmd)} | {quote_cmd(seven_cmd)}")
    tar_proc = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert tar_proc.stdout is not None
    seven_proc = subprocess.Popen(seven_cmd, stdin=tar_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    job._register_proc(tar_proc)
    job._register_proc(seven_proc)
    try:
        tar_proc.stdout.close()
        assert seven_proc.stdout is not None
        for line in seven_proc.stdout:
            job.log(line.rstrip("\n"))
            if job.stop_requested:
                try: seven_proc.terminate()
                except Exception: pass
                try: tar_proc.terminate()
                except Exception: pass
        seven_rc = seven_proc.wait()
        tar_err = tar_proc.stderr.read().decode("utf-8", errors="replace") if tar_proc.stderr else ""
        tar_rc = tar_proc.wait()
        if tar_err.strip():
            job.log(tar_err.strip())
    finally:
        job._unregister_proc(tar_proc)
        job._unregister_proc(seven_proc)
    if tar_rc != 0 or seven_rc != 0:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        job.log(f"❌ Erreur archive tar.7z : tar={tar_rc}, 7z={seven_rc}")
        return False
    tmp.replace(outfile)
    return True


def engine_archive_one_item(job: ArchiveJob, action: EngineAction, item: Path, *, whole_source: bool, date_stamp: str) -> bool:
    assert action.destination is not None
    outfile = engine_archive_output_path(action, item, whole_source=whole_source, date_stamp=date_stamp)
    if outfile.exists() and not action.replace_existing:
        job.log(f"⏭️  Archive déjà présente : {outfile}")
        return True
    job.log("")
    job.log(f"→ Archive : {item}")
    job.log(f"  Sortie  : {outfile}")
    engine_prepare_destination(job, action.destination)
    if action.archive_format in {"tar.gz", "tgz", "gz"}:
        ok = engine_archive_item_tar_gz(job, item, outfile)
    else:
        ok = engine_archive_item_tar_7z(job, item, outfile, level=action.compression_level)
    job.log(("✅ OK → " if ok else "❌ Échec archive → ") + str(outfile))
    return ok


def engine_archive_payload(job: ArchiveJob, action: EngineAction, context: EngineRuntimeContext) -> bool:
    assert action.source is not None and action.destination is not None
    job.log("")
    engine_title(job, f"ARCHIVE : {action.title}")
    job.log(f"SOURCE      : {action.source}/")
    job.log(f"DESTINATION : {action.destination}/")
    job.log(f"FORMAT      : {action.archive_format}")
    job.log(f"CHILDREN    : {int(action.archive_children)}")
    engine_prepare_destination(job, action.destination)
    archive_log_progress(job, action.key, "archive", None, "Préparation archive…", state="running")
    archive_log_overall_progress(job, action, context, "archive", None, "Préparation archive…", state="running")
    if action.archive_children:
        items = sorted([p for p in action.source.iterdir() if not engine_should_exclude(p, action.excludes)], key=lambda p: p.name.lower())
        if not items:
            job.log(f"⏭️  Source vide : {action.source}")
            archive_log_progress(job, action.key, "done", 100, "Source vide", state="success")
            return True
        ok = True
        total_items = max(1, len(items))
        for idx, item in enumerate(items, start=1):
            if job.stop_requested:
                return False
            pct_before = int(round(((idx - 1) * 100.0) / total_items))
            label_before = f"Archive {idx}/{total_items} : {item.name}"
            archive_log_progress(job, action.key, "archive", pct_before, label_before, state="running")
            archive_log_overall_progress(job, action, context, "archive", pct_before, label_before, state="running")
            if not engine_archive_one_item(job, action, item, whole_source=False, date_stamp=context.date_stamp):
                ok = False
            pct_after = int(round((idx * 100.0) / total_items))
            label_after = f"Archive {idx}/{total_items} terminée"
            archive_log_progress(job, action.key, "archive", pct_after, label_after, state="running" if idx < total_items else ("success" if ok else "error"))
            archive_log_overall_progress(job, action, context, "archive", pct_after, label_after, state="running")
        return ok
    ok = engine_archive_one_item(job, action, action.source, whole_source=True, date_stamp=context.date_stamp)
    archive_log_progress(job, action.key, "archive", 100 if ok else 100, "Archive terminée" if ok else "Erreur archive", state="success" if ok else "error")
    archive_log_overall_progress(job, action, context, "archive", 100, "Archive terminée" if ok else "Erreur archive", state="running")
    return ok


def engine_archive_payload_is_needed(job: ArchiveJob, action: EngineAction) -> bool:
    if not action.has_backup:
        return True
    assert action.source is not None
    engine_safe_path_checks(action)
    engine_ensure_tools_for_action(action, dry_run=job.dry_run)
    if not action.source.exists():
        job.log(f"⏭️  Source absente : {action.title}")
        job.log(f"    {action.source}")
        return False
    if not action.source.is_dir():
        raise RuntimeError(f"ERREUR [{action.key}] : la source doit être un dossier : {action.source}")
    if not engine_path_has_payload(action.source):
        job.log(f"⏭️  Source vide : {action.title}")
        job.log(f"    {action.source}")
        return False
    return True


def engine_execute_archive_payload(job: ArchiveJob, action: EngineAction, context: EngineRuntimeContext) -> bool:
    if not action.has_backup:
        return True
    engine_safe_path_checks(action)
    engine_ensure_tools_for_action(action, dry_run=job.dry_run)
    if action.archive:
        return engine_archive_payload(job, action, context)
    return engine_copy_payload(job, action, context)


def engine_execute_action(job: ArchiveJob, action: EngineAction, context: EngineRuntimeContext, *, explicit: bool = False) -> bool:
    if job.stop_requested:
        return False
    if not action.enabled:
        job.log(f"⏭️  Désactivé : {action.title}")
        return True
    if action.is_command_only and not explicit and not action.include_in_all:
        return True
    job.log("")
    engine_title(job, f"BLOC : {action.key} - {action.title}")
    job.log(f"MODE : {action.mode_label}")
    archive_log_progress(job, action.key, "start", 0, "Démarrage…", state="running")
    archive_log_overall_progress(job, action, context, "start", 0, "Démarrage…", state="running")
    if action.has_backup and not engine_archive_payload_is_needed(job, action):
        archive_log_progress(job, action.key, "done", 100, "Rien à faire", state="success")
        return True
    start_ok = True
    backup_ok = True
    end_ok = True
    if action.command_start.strip():
        archive_log_progress(job, action.key, "command", None, "Commande début…", state="running")
        archive_log_overall_progress(job, action, context, "command", None, "Commande début…", state="running")
        start_ok = engine_run_config_command(job, action.command_start, action, context, label=f"commande début {action.key}")
        if start_ok and not engine_wait_docker_stopped(job, action.wait_start_docker_stopped, action, context):
            start_ok = False
    if not start_ok:
        job.log(f"❌ Début échoué : le archive ne démarre pas pour {action.key}")
        if action.command_end.strip():
            engine_run_config_command(job, action.command_end, action, context, label=f"commande fin sécurité {action.key}")
        return False
    try:
        if action.has_backup:
            backup_ok = engine_execute_archive_payload(job, action, context)
        elif action.is_command_only:
            job.log("Bloc commande seule : aucune source/destination.")
        else:
            job.log(f"⏭️  Bloc sans action : {action.title}")
    finally:
        if action.command_end.strip():
            archive_log_progress(job, action.key, "command", None, "Commande fin…", state="running")
            archive_log_overall_progress(job, action, context, "command", None, "Commande fin…", state="running")
            end_ok = engine_run_config_command(job, action.command_end, action, context, label=f"commande fin {action.key}")
            if end_ok and not engine_wait_docker_running(job, action.wait_end_docker_running, action, context):
                end_ok = False
    return bool(backup_ok and end_ok and not job.stop_requested)


def engine_list_actions_text(config: EngineConfig) -> str:
    lines: List[str] = []
    lines.append("Configuration :")
    lines.append(f"  conf : {config.conf_file}")
    lines.append(f"  base : {config.base_dir}")
    lines.append(f"  logs : {config.log_dir}")
    lines.append(f"  lock : {config.lock_file}")
    lines.append("")
    lines.append("Commandes GUI/CLI compatibles :")
    lines.append("  --all                 Tous les blocs enabled + include_in_all")
    lines.append("  --NOM                 Lance un bloc par son nom/alias")
    lines.append("  --PREFIXE             Lance tous les blocs PREFIXE_*. Exemple : --appdata")
    lines.append("  --dry-run             Test sans copie/archive/commande réelle")
    lines.append("")
    lines.append("Blocs :")
    for key, action in config.actions.items():
        if not action.enabled:
            status = "désactivé"
        elif not action.include_in_all:
            status = "hors --all"
        else:
            status = "actif"
        aliases = f" | aliases: {', '.join(action.aliases)}" if action.aliases else ""
        lines.append(f"  --{key:<22} {action.title} ({status}) | {action.mode_label}{aliases}")
        if action.has_backup:
            lines.append(f"      {action.source}  ->  {action.destination}")
        elif action.is_command_only:
            lines.append("      commande seule")
    return "\n".join(lines)


def engine_build_selection(target: str, config: EngineConfig) -> Tuple[List[EngineAction], set]:
    selected_keys: List[str] = []
    explicit_keys = set()
    if target in {"__all__", "--all", "all"}:
        for key, action in config.actions.items():
            if action.enabled and action.include_in_all:
                selected_keys.append(key)
    else:
        for key in engine_resolve_names(target, config):
            selected_keys.append(key)
            explicit_keys.add(key)
    result: List[EngineAction] = []
    seen = set()
    for key in selected_keys:
        if key not in seen:
            result.append(config.actions[key])
            seen.add(key)
    return result, explicit_keys



# ------------------------------------------------------------
# Lancement robuste des archives dans tmux
# ------------------------------------------------------------
# Flask ne porte plus la longue archive dans son propre process/thread.
# Il démarre une session tmux stable par profil : Toto -> Toto-archive.
# Si l'utilisateur reclique 20 fois sur le même profil, on réutilise la
# session existante au lieu d'empiler des jobs.

TMUX_ARCHIVE_SUFFIX = "-archive"


def archive_tmux_safe_name(value: str) -> str:
    raw = clean_text(value).strip()
    if raw in {"", "__all__", "--all", "all"}:
        raw = "all"
    raw = raw.lstrip("-")
    # tmux accepte beaucoup de caractères, mais on reste volontairement simple
    # pour éviter les problèmes avec ':' (séparateur tmux) et les espaces.
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-._")
    if not safe:
        safe = "archive"
    if not safe.endswith(TMUX_ARCHIVE_SUFFIX):
        safe = f"{safe}{TMUX_ARCHIVE_SUFFIX}"
    return safe[:80]


def archive_tmux_has_session(session: str) -> bool:
    if not shutil.which("tmux"):
        return False
    res = subprocess.run(["tmux", "has-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return res.returncode == 0


def archive_tmux_log_file(config: EngineConfig, session: str) -> Path:
    return config.log_dir / f"{session}.log"


def archive_infer_job_status_from_log(log_text: str) -> Tuple[str, Optional[int]]:
    """Déduit l'état d'un job à partir du log, sans transformer un log incomplet en erreur.

    Point important pour les archives longues : certaines commandes restent longtemps
    silencieuses. Tant que le moteur n'a pas écrit son marqueur de fin, le suivi Web
    doit rester en cours et ne pas passer en rouge sur une simple absence de retour.
    """
    text = log_text or ""
    has_final_marker = "--- FIN LOG" in text

    if "✅ Archive terminé." in text:
        return "success", 0
    if "⏹️ Archive arrêté." in text or "Arrêt demandé depuis Flask" in text or "Session tmux fermée" in text:
        return "stopped", 130

    if has_final_marker and (
        "❌ Archive terminé avec erreur." in text
        or "Traceback" in text
        or "ERREUR" in text
        or "❌" in text
    ):
        return "error", 1

    # Log sans fin explicite : le job peut être encore dans une commande longue
    # (tar/7z/rsync silencieux). On garde l'état running au lieu d'inventer une erreur.
    if not has_final_marker:
        return "running", None

    if "Arrêt demandé" in text:
        return "stopped", 130
    return "stopped", 0


def archive_sync_tmux_job_state(job: ArchiveJob) -> None:
    if not job.tmux_session or job.status not in {"queued", "running", "stopping"}:
        return
    if archive_tmux_has_session(job.tmux_session):
        job.status = "running" if job.status != "stopping" else "stopping"
        job.returncode = None
        return

    status, returncode = archive_infer_job_status_from_log(job.read_log())
    job.status = status
    job.returncode = returncode
    if status != "running" and not job.ended_at:
        job.ended_at = now_text()


def archive_run_engine_to_log(*, target: str, dry_run: bool, log_file: Path, session: str = "") -> int:
    """Point d'entrée exécuté dans tmux.

    Ce code ne dépend pas du process Flask : si gunicorn/Flask redémarre,
    le archive continue dans la session tmux jusqu'à la fin.
    """
    label = "--all" if target in {"__all__", "--all", "all"} else target
    job = ArchiveJob(target=target, label=label, dry_run=dry_run, log_file=log_file)
    job.id = session or job.id
    job.tmux_session = session
    job.tmux_managed = True
    job.status = "running"
    ok = False
    try:
        conf = get_config()
        real_conf = module_real_conf_path(conf)
        config = engine_load_config(real_conf)
        actions, explicit_keys = engine_build_selection(job.target, config)
        if not actions:
            raise RuntimeError("Action manquante : aucun profil sélectionné.")
        job.log(f"--- LOG {now_text()} : {job.log_file} ---")
        if session:
            job.log(f"TMUX : {session}")
        with EngineLock(config.lock_file):
            engine_title(job, "ARCHIVE - moteur tmux piloté par archive.conf")
            job.log(f"Date : {now_text()}")
            job.log(f"CONF : {config.conf_file}")
            job.log(f"BASE : {config.base_dir}")
            if job.dry_run:
                job.log("MODE : DRY-RUN")
            ok = True
            context = EngineRuntimeContext(date_stamp=now_stamp(), stopped_by_backup={}, running_before={}, total_actions=len(actions), completed_actions=0, current_action_index=0)
            archive_log_progress(job, "__all__", "start", 0, f"Démarrage : {len(actions)} profil(s)", state="running", force=True)
            for index, action in enumerate(actions):
                context.current_action_index = index
                if job.stop_requested:
                    ok = False
                    break
                explicit = action.key in explicit_keys
                action_ok = engine_execute_action(job, action, context, explicit=explicit)
                if not action_ok:
                    ok = False
                context.completed_actions += 1
                archive_log_progress(job, action.key, "done" if action_ok else "error", 100, "Terminé" if action_ok else "Erreur", state="success" if action_ok else "error", force=True)
                overall_pct = int(round((context.completed_actions * 100.0) / max(1, context.total_actions)))
                archive_log_progress(job, "__all__", "copy" if action_ok else "error", min(99, overall_pct) if context.completed_actions < context.total_actions else overall_pct, f"{context.completed_actions}/{context.total_actions} profil(s)", state="running" if context.completed_actions < context.total_actions else ("success" if ok else "error"), force=True)
            if not job.dry_run:
                try:
                    os.sync()
                except Exception:
                    pass
        job.log("")
        if job.stop_requested:
            job.status = "stopped"
            job.returncode = 130
            archive_log_progress(job, "__all__", "stopped", 100, "Arrêté", state="error", force=True)
            job.log("⏹️ Archive arrêté.")
        elif ok:
            job.status = "success"
            job.returncode = 0
            archive_log_progress(job, "__all__", "done", 100, "Terminé", state="success", force=True)
            job.log("✅ Archive terminé.")
        else:
            job.status = "error"
            job.returncode = 1
            archive_log_progress(job, "__all__", "error", 100, "Erreur", state="error", force=True)
            job.log("❌ Archive terminé avec erreur.")
    except Exception as exc:
        job.error = str(exc)
        job.status = "stopped" if job.stop_requested else "error"
        job.returncode = 130 if job.stop_requested else 1
        archive_log_progress(job, "__all__", "stopped" if job.stop_requested else "error", 100, "Arrêté" if job.stop_requested else "Erreur", state="error", force=True)
        job.log(f"❌ {exc}")
    finally:
        job.ended_at = now_text()
        if session:
            job.log(f"TMUX : fermeture de la session {session}")
        job.log(f"--- FIN LOG {now_text()} ---")
    return int(job.returncode or (0 if ok else 1))


def archive_worker_cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Worker Archive lancé dans tmux par le module Services.")
    parser.add_argument("--archive-worker", action="store_true", dest="archive_worker")
    parser.add_argument("--target", default="__all__")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-file", required=True)
    parser.add_argument("--session", default="")
    args = parser.parse_args(argv)
    if not args.archive_worker:
        parser.error("mode worker archive manquant")
    return archive_run_engine_to_log(
        target=args.target,
        dry_run=bool(args.dry_run),
        log_file=Path(args.log_file),
        session=args.session,
    )

def archive_tmux_list_sessions() -> List[str]:
    """Liste les sessions tmux visibles. Retourne [] si tmux est absent."""
    tmux_bin = shutil.which("tmux")
    if not tmux_bin:
        return []
    res = subprocess.run([tmux_bin, "list-sessions", "-F", "#{session_name}"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
    if res.returncode != 0:
        return []
    return [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]


def archive_infer_finished_status_from_log(log_text: str) -> Tuple[str, Optional[int]]:
    """Compatibilité : conserve l'ancien nom, mais utilise l'inférence prudente."""
    return archive_infer_job_status_from_log(log_text)


def archive_job_from_external_log(session: str, log_file: Path, *, running: bool) -> ArchiveJob:
    """Reconstruit un objet Job depuis tmux/log quand Flask n'a plus l'état mémoire."""
    label = session
    if label.endswith(TMUX_ARCHIVE_SUFFIX):
        label = label[:-len(TMUX_ARCHIVE_SUFFIX)]
    target = "__all__" if label in {"all", "__all__", "--all"} else label
    human_label = "--all" if target == "__all__" else label
    job = ArchiveJob(target=target, label=human_label, dry_run=False, log_file=log_file)
    job.id = session
    job.tmux_session = session if session.endswith(TMUX_ARCHIVE_SUFFIX) else ""
    job.tmux_managed = bool(job.tmux_session)
    if running:
        job.status = "running"
        job.returncode = None
    else:
        job.status, job.returncode = archive_infer_finished_status_from_log(job.read_log())
        job.ended_at = now_text()
    return job


def archive_discover_jobs_locked() -> None:
    """Rattache les jobs tmux/log au process Flask courant.

    C'est le tuyau qui permet à l'onglet Logs de voir le même log que la popup,
    même après navigation, rechargement, ou si la requête arrive dans un autre worker.
    """
    global ACTIVE_JOB_ID
    try:
        config = engine_load_config(module_real_conf_path(get_config()))
    except Exception:
        return

    # 1) Jobs en cours : vérité tmux.
    for session in archive_tmux_list_sessions():
        if not session.endswith(TMUX_ARCHIVE_SUFFIX):
            continue
        log_file = archive_tmux_log_file(config, session)
        job = JOBS.get(session)
        if not job:
            job = archive_job_from_external_log(session, log_file, running=True)
            JOBS[session] = job
        else:
            job.log_file = log_file
            job.tmux_session = session
            job.tmux_managed = True
            archive_sync_tmux_job_state(job)
        if job.status in {"queued", "running", "stopping"}:
            ACTIVE_JOB_ID = job.id

    # 2) Dernier log terminé : utile quand on ouvre /archive/logs après fermeture de la popup.
    try:
        log_files = sorted(config.log_dir.glob(f"*{TMUX_ARCHIVE_SUFFIX}.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        log_files = []
    for log_file in log_files[:5]:
        session = log_file.stem
        if session not in JOBS:
            JOBS[session] = archive_job_from_external_log(session, log_file, running=archive_tmux_has_session(session))


def _latest_job_locked() -> Optional[ArchiveJob]:
    archive_discover_jobs_locked()
    latest: Optional[ArchiveJob] = None
    latest_mtime = -1.0
    for job in JOBS.values():
        archive_sync_tmux_job_state(job)
        try:
            mtime = job.log_file.stat().st_mtime
        except Exception:
            mtime = 0.0
        if latest is None or mtime >= latest_mtime:
            latest = job
            latest_mtime = mtime
    return latest


def _active_job_locked() -> Optional[ArchiveJob]:
    global ACTIVE_JOB_ID
    archive_discover_jobs_locked()
    if ACTIVE_JOB_ID and ACTIVE_JOB_ID in JOBS:
        job = JOBS[ACTIVE_JOB_ID]
        archive_sync_tmux_job_state(job)
        if job.status in {"queued", "running", "stopping"}:
            return job
    for job in JOBS.values():
        archive_sync_tmux_job_state(job)
        if job.status in {"queued", "running", "stopping"}:
            ACTIVE_JOB_ID = job.id
            return job
    ACTIVE_JOB_ID = None
    return None


def start_archive_job(*, target: str, dry_run: bool = False) -> ArchiveJob:
    """Démarre un archive dans tmux, avec une session stable par profil.

    Exemple : profil Toto -> session Toto-backup.
    Si la session existe déjà, on renvoie le même job/log au lieu de lancer un
    deuxième archive. Le vrai moteur tourne hors du process Flask.
    """
    global ACTIVE_JOB_ID
    tmux_bin = shutil.which("tmux")
    if not tmux_bin:
        raise RuntimeError("tmux introuvable. Installe tmux pour lancer les archives de façon détachée.")

    conf = get_config()
    real_conf = module_real_conf_path(conf)
    config = engine_load_config(real_conf)
    # Validation rapide avant de créer une session tmux.
    actions, _explicit_keys = engine_build_selection(target, config)
    if not actions:
        raise RuntimeError("Action manquante : aucun profil sélectionné.")

    label = "--all" if target in {"__all__", "--all", "all"} else target
    session = archive_tmux_safe_name(label)
    log_file = archive_tmux_log_file(config, session)

    with JOBS_LOCK:
        existing = JOBS.get(session)
        if existing and existing.status in {"queued", "running", "stopping"}:
            archive_sync_tmux_job_state(existing)
            if existing.status in {"queued", "running", "stopping"}:
                ACTIVE_JOB_ID = existing.id
                return existing

    if archive_tmux_has_session(session):
        job = ArchiveJob(target=target, label=label, dry_run=dry_run, log_file=log_file)
        job.id = session
        job.status = "running"
        job.tmux_session = session
        job.tmux_managed = True
        job.log(f"ℹ️ Session tmux déjà active : {session}. Nouveau lancement ignoré.")
        with JOBS_LOCK:
            JOBS[job.id] = job
            ACTIVE_JOB_ID = job.id
        return job

    log_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        log_file.write_text("", encoding="utf-8")
    except Exception:
        pass

    job = ArchiveJob(target=target, label=label, dry_run=dry_run, log_file=log_file)
    job.id = session
    job.status = "running"
    job.tmux_session = session
    job.tmux_managed = True
    job.log(f"🚀 Démarrage archive dans tmux : {session}")
    job.log(f"Cible : {label}")
    job.log(f"Log : {log_file}")
    archive_log_progress(job, "__all__", "start", 0, "Démarrage…", state="running", force=True)

    worker_cmd = [
        sys.executable or "/usr/bin/python3",
        str(Path(__file__).resolve()),
        "--archive-worker",
        "--target", target,
        "--log-file", str(log_file),
        "--session", session,
    ]
    if dry_run:
        worker_cmd.append("--dry-run")

    # La session se ferme quand le worker termine. Le kill-session final évite
    # de garder une fenêtre tmux morte si le shell reste ouvert.
    shell_cmd = " ".join(shlex.quote(x) for x in worker_cmd)
    shell_cmd = f"{shell_cmd}; tmux kill-session -t {shlex.quote(session)} >/dev/null 2>&1 || true"
    res = subprocess.run([tmux_bin, "new-session", "-d", "-s", session, shell_cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if res.returncode != 0:
        # Cas course : la session a été créée entre le test et new-session.
        if archive_tmux_has_session(session):
            job.log(f"ℹ️ Session tmux déjà active : {session}. Nouveau lancement ignoré.")
        else:
            err = (res.stderr or res.stdout or "Erreur tmux inconnue").strip()
            job.status = "error"
            job.error = err
            job.returncode = res.returncode
            job.log(f"❌ Impossible de créer la session tmux {session} : {err}")
            raise RuntimeError(err)

    with JOBS_LOCK:
        JOBS[job.id] = job
        ACTIVE_JOB_ID = job.id
        if len(JOBS) > 30:
            for old_id in list(JOBS.keys())[:-30]:
                if old_id != ACTIVE_JOB_ID:
                    JOBS.pop(old_id, None)
    return job

def _run_archive_job_thread(job_id: str) -> None:
    global ACTIVE_JOB_ID
    job = JOBS[job_id]
    job.status = "running"
    try:
        conf = get_config()
        real_conf = module_real_conf_path(conf)
        config = engine_load_config(real_conf)
        actions, explicit_keys = engine_build_selection(job.target, config)
        if not actions:
            raise RuntimeError("Action manquante : aucun profil sélectionné.")
        job.log(f"--- LOG {now_text()} : {job.log_file} ---")
        with EngineLock(config.lock_file):
            engine_title(job, "ARCHIVE - moteur Flask intégré piloté par archive.conf")
            job.log(f"Date : {now_text()}")
            job.log(f"CONF : {config.conf_file}")
            job.log(f"BASE : {config.base_dir}")
            if job.dry_run:
                job.log("MODE : DRY-RUN")
            ok = True
            context = EngineRuntimeContext(date_stamp=now_stamp(), stopped_by_backup={}, running_before={}, total_actions=len(actions), completed_actions=0, current_action_index=0)
            archive_log_progress(job, "__all__", "start", 0, f"Démarrage : {len(actions)} profil(s)", state="running", force=True)
            for index, action in enumerate(actions):
                context.current_action_index = index
                if job.stop_requested:
                    ok = False
                    break
                explicit = action.key in explicit_keys
                action_ok = engine_execute_action(job, action, context, explicit=explicit)
                if not action_ok:
                    ok = False
                context.completed_actions += 1
                archive_log_progress(job, action.key, "done" if action_ok else "error", 100, "Terminé" if action_ok else "Erreur", state="success" if action_ok else "error", force=True)
                overall_pct = int(round((context.completed_actions * 100.0) / max(1, context.total_actions)))
                archive_log_progress(job, "__all__", "copy" if action_ok else "error", min(99, overall_pct) if context.completed_actions < context.total_actions else overall_pct, f"{context.completed_actions}/{context.total_actions} profil(s)", state="running" if context.completed_actions < context.total_actions else ("success" if ok else "error"), force=True)
            if not job.dry_run:
                try:
                    os.sync()
                except Exception:
                    pass
        job.log("")
        if job.stop_requested:
            job.status = "stopped"
            job.returncode = 130
            archive_log_progress(job, "__all__", "stopped", 100, "Arrêté", state="error", force=True)
            job.log("⏹️ Archive arrêté.")
        elif ok:
            job.status = "success"
            job.returncode = 0
            archive_log_progress(job, "__all__", "done", 100, "Terminé", state="success", force=True)
            job.log("✅ Archive terminé.")
        else:
            job.status = "error"
            job.returncode = 1
            archive_log_progress(job, "__all__", "error", 100, "Erreur", state="error", force=True)
            job.log("❌ Archive terminé avec erreur.")
    except Exception as exc:
        job.error = str(exc)
        job.status = "stopped" if job.stop_requested else "error"
        job.returncode = 130 if job.stop_requested else 1
        archive_log_progress(job, "__all__", "stopped" if job.stop_requested else "error", 100, "Arrêté" if job.stop_requested else "Erreur", state="error", force=True)
        job.log(f"❌ {exc}")
    finally:
        job.ended_at = now_text()
        job.log(f"--- FIN LOG {now_text()} ---")
        with JOBS_LOCK:
            if ACTIVE_JOB_ID == job.id:
                ACTIVE_JOB_ID = None


def job_to_dict(job: ArchiveJob, *, include_log: bool = False) -> Dict[str, object]:
    archive_sync_tmux_job_state(job)
    log_text = job.read_log() if include_log else ""
    parsed_progress = archive_parse_progress_from_log(log_text) if log_text else {"overall": {}, "rows": {}}
    overall = parsed_progress.get("overall") if isinstance(parsed_progress, dict) else {}
    rows = parsed_progress.get("rows") if isinstance(parsed_progress, dict) else {}
    if not isinstance(overall, dict):
        overall = {}
    if not isinstance(rows, dict):
        rows = {}
    progress_percent = overall.get("percent", job.progress_percent)
    if progress_percent is not None:
        try:
            progress_percent = int(progress_percent)
        except Exception:
            progress_percent = None
    progress_phase = str(overall.get("phase", job.progress_phase or ""))
    progress_label = str(overall.get("label", job.progress_label or ""))
    progress_target = str(overall.get("target", job.progress_target or job.target or "__all__"))
    data: Dict[str, object] = {
        "id": job.id,
        "target": job.target,
        "label": job.label,
        "dry_run": job.dry_run,
        "status": job.status,
        "returncode": job.returncode,
        "error": job.error,
        "started_at": job.started_at,
        "ended_at": job.ended_at,
        "log_file": str(job.log_file),
        "tmux_session": job.tmux_session,
        "running": job.status in {"queued", "running", "stopping"},
        "progress_percent": progress_percent,
        "progress_label": progress_label,
        "progress_phase": progress_phase,
        "progress_target": progress_target,
        "progress_rows": rows or job.progress_rows,
    }
    if include_log:
        data["log"] = log_text
    return data

def run_integrated_check(conf: Dict[str, str]) -> Tuple[int, str]:
    """Vérification intégrée du vrai archive.conf, sans script externe."""
    try:
        real_conf = module_real_conf_path(conf)
        config = engine_load_config(real_conf)
        return 0, engine_list_actions_text(config)
    except Exception as exc:
        return 1, str(exc)




def _render_archive():
    conf = get_config()
    real_conf = module_real_conf_path(conf)
    error = ""
    settings = ArchiveSettings()
    blocks: List[ArchiveBlock] = []
    try:
        settings, blocks = read_real_conf(real_conf)
    except Exception as exc:
        error = str(exc)
    archive_active_subtab = services_requested_subtab({"general", "profiles", "system", "logs"}, "general")
    if archive_active_subtab == "profiles":
        return services_redirect("archive", subtab="general")
    return render_template(
        "services_archive.html",
        conf=conf,
        config_file=str(CONFIG_FILE),
        real_conf=str(real_conf),
        allowed_roots=allowed_roots(conf),
        error=error,
        settings=settings,
        blocks=blocks,
        active_count=sum(1 for b in blocks if b.is_enabled),
        all_count=sum(1 for b in blocks if b.is_enabled and b.is_all),
        archive_count=sum(1 for b in blocks if b.is_archive),
        active_subtab=archive_active_subtab,
        service_active="archive",
    )


@services_bp.route("/services/archive/config", methods=["POST"])
def archive_config_save():
    # Route conservée en compatibilité : l'UI ne propose plus d'édition des
    # chemins techniques Backup, mais un ancien bouton ou un appel manuel ne doit
    # pas pouvoir réinjecter une valeur différente.
    conf = archive_normalize_module_config(archive_DEFAULT_CONFIG)
    write_kv_file(CONFIG_FILE, conf)
    flash("Configuration technique Archive remise aux valeurs par defaut.", "success")
    return services_redirect("archive", subtab="system")


@services_bp.route("/services/archive/save", methods=["POST"])
def archive_save():
    conf = get_config()
    real_conf = module_real_conf_path(conf)
    try:
        existing_settings, _existing_blocks = read_real_conf(real_conf)
        settings = collect_settings_from_form(existing_settings)
        blocks = collect_blocks_from_form()
        errors = validate_blocks(settings, blocks)
        if errors:
            for err in errors:
                flash("❌ " + err, "error")
            return services_redirect("archive", subtab="general")
        backup_path = archive_real_conf(real_conf, module_archive_dir(conf))
        write_real_conf(real_conf, settings, blocks)
        flash("✅ archive.conf sauvegardé.", "success")
    except Exception as exc:
        flash(f"❌ Erreur sauvegarde Archive : {exc}", "error")
    return services_redirect("archive", subtab="general")


@services_bp.route("/services/archive/check", methods=["POST"])
def archive_check():
    conf = get_config()
    rc, output = run_integrated_check(conf)
    return jsonify({"ok": rc == 0, "code": rc, "output": output or "OK"})




@services_bp.route("/services/archive/api/run", methods=["POST"])
def archive_api_run():
    data = request.get_json(silent=True) or {}
    target = clean_text(data.get("target") or "__all__")
    action = clean_text(data.get("action") or "start").lower()
    dry_run = parse_bool(str(data.get("dry_run", "0")))

    if action == "stop":
        return archive_api_stop()

    if action == "restart":
        with JOBS_LOCK:
            active = _active_job_locked()
        if active:
            active.status = "stopping"
            active.request_stop()
            return jsonify({"ok": False, "message": "Arrêt demandé. Relance l'archive après arrêt complet.", "job": job_to_dict(active, include_log=True)})

    try:
        job = start_archive_job(target=target, dry_run=dry_run)
        return jsonify({"ok": True, "job": job_to_dict(job, include_log=True)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@services_bp.route("/services/archive/api/stop", methods=["POST"])
def archive_api_stop():
    data = request.get_json(silent=True) or {}
    job_id = clean_text(data.get("job_id") or "")
    stop_target = archive_progress_clean_target(clean_text(data.get("target") or ""))
    with JOBS_LOCK:
        job = JOBS.get(job_id) if job_id else _active_job_locked()
    if not job:
        return jsonify({"ok": False, "error": "Aucune archive en cours."}), 404
    if stop_target and stop_target != "__all__":
        archive_log_progress(job, stop_target, "stopping", None, "Arrêt demandé…", state="running", force=True)
    job.status = "stopping"
    job.request_stop()
    return jsonify({"ok": True, "job": job_to_dict(job, include_log=True)})


@services_bp.route("/services/archive/api/status", methods=["GET"])
def archive_api_status():
    job_id = clean_text(request.args.get("job_id") or "")
    with JOBS_LOCK:
        job = JOBS.get(job_id) if job_id else _active_job_locked()
        latest = job or _latest_job_locked()
        jobs = [job_to_dict(j) for j in JOBS.values()]
    return jsonify({
        "ok": True,
        "active": job_to_dict(job, include_log=True) if job else None,
        "latest": job_to_dict(latest, include_log=True) if latest else None,
        "jobs": jobs,
    })


@services_bp.route("/services/archive/api/log", methods=["GET"])
def archive_api_log():
    job_id = clean_text(request.args.get("job_id") or "")
    with JOBS_LOCK:
        if job_id and job_id not in JOBS:
            archive_discover_jobs_locked()
        job = JOBS.get(job_id) if job_id else _active_job_locked()
        if not job:
            job = _latest_job_locked()
    if not job:
        return jsonify({"ok": False, "error": "Job introuvable."}), 404
    return jsonify({"ok": True, "job": job_to_dict(job, include_log=True)})

@services_bp.route("/services/archive/api/browse", methods=["GET"])
def archive_browse():
    conf = get_config()
    roots = allowed_roots(conf)
    kind = request.args.get("kind", "dir").strip().lower()
    if kind not in {"dir", "file"}:
        kind = "dir"

    requested = request.args.get("path") or roots[0]
    requested = normalize_path(requested)
    if not requested:
        requested = roots[0]
    if not os.path.isabs(requested):
        requested = str(resolve_module_path(requested))
    if os.path.isfile(requested):
        requested = os.path.dirname(requested)
    if not is_under_allowed_root(requested, roots):
        requested = roots[0]

    real = os.path.realpath(requested)
    if not os.path.isdir(real):
        return jsonify({"ok": False, "path": real, "error": "Dossier introuvable.", "items": [], "roots": roots})

    items: List[Dict[str, str]] = []
    parent = os.path.realpath(os.path.dirname(real))
    if parent != real and is_under_allowed_root(parent, roots):
        items.append({"type": "parent", "name": "..", "path": parent})

    try:
        dirs = []
        files = []
        with os.scandir(real) as scan:
            for entry in scan:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append(entry)
                    elif kind == "file" and entry.is_file(follow_symlinks=False):
                        files.append(entry)
                except OSError:
                    continue
        dirs.sort(key=lambda e: e.name.lower())
        files.sort(key=lambda e: e.name.lower())
        for entry in dirs:
            path = os.path.realpath(entry.path)
            if is_under_allowed_root(path, roots):
                items.append({"type": "dir", "name": entry.name, "path": path})
        for entry in files:
            path = os.path.realpath(entry.path)
            if is_under_allowed_root(path, roots):
                items.append({"type": "file", "name": entry.name, "path": path})
        return jsonify({"ok": True, "path": real, "items": items, "roots": roots})
    except PermissionError:
        return jsonify({"ok": False, "path": real, "error": "Permission refusée.", "items": items, "roots": roots})
    except Exception as exc:
        return jsonify({"ok": False, "path": real, "error": str(exc), "items": items, "roots": roots})

# ============================================================
# ADAPTERS services.conf : on remplace les lectures/écritures
# des anciens fichiers minidnla.conf / proftpd.conf / sftp.conf.
# ============================================================
# ============================================================
# CACHE MERGED MODULE
# ============================================================

