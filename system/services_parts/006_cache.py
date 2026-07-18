CACHE_ORDER = [
    "title",
    "command_start",
    "source",
    "destination",
    "command_end",
    "aliases",
    "enabled",
    "include_in_all",
]


DEFAULT_REAL_CACHE_CONF = """# ============================================================
# cache.conf - Configuration complète du mover Cache intégré
# ============================================================
# Aucun profil n'est créé par défaut.
# Ajoute les profils depuis l'interface Cache > Profils cache.
#
# Format d'un profil, à titre d'information seulement :
# nom_de_profil
# title = Nom lisible
# command_start =
# source = /mnt/cache/Dossier
# destination = /mnt/user0/Dossier
# command_end =
# aliases =
# enabled = true
# include_in_all = true
# ============================================================
"""


@dataclass
class CacheBlock:
    key: str
    title: str = ""
    command_start: str = ""
    source: str = ""
    destination: str = ""
    command_end: str = ""
    aliases: str = ""
    enabled: str = "true"
    include_in_all: str = "true"

    @property
    def is_enabled(self) -> bool:
        return parse_bool(self.enabled, True)

    @property
    def is_all(self) -> bool:
        return parse_bool(self.include_in_all, True)

    @property
    def has_move(self) -> bool:
        return bool(self.source and self.destination)

    @property
    def mode_label(self) -> str:
        if self.has_move:
            return "deplacement rsync"
        if self.command_start or self.command_end:
            return "commande"
        return "vide"

    @property
    def command_label(self) -> str:
        parts = []
        if self.command_start:
            parts.append("debut")
        if self.command_end:
            parts.append("fin")
        return " + ".join(parts) if parts else "aucune"

    def source_has_payload(self) -> bool:
        # Pour l'affichage du tableau : si la source n'existe pas, ou si elle ne
        # contient aucun fichier/lien, le cache est considéré vide. On s'arrête
        # dès qu'un vrai contenu est trouvé pour éviter de scanner inutilement.
        if not self.source or not os.path.isdir(self.source):
            return False
        try:
            for _root, _dirs, files in os.walk(self.source):
                if files:
                    return True
        except Exception:
            # Si on ne peut pas lire la source, on préfère signaler qu'il reste
            # quelque chose à vérifier/vider plutôt que d'afficher un faux OK.
            return True
        return False

    def source_free_bytes(self) -> int:
        if not self.source or not os.path.isdir(self.source):
            return -1
        try:
            st = os.statvfs(self.source)
            return int(st.f_bavail) * int(st.f_frsize)
        except Exception:
            return -1

    @property
    def cache_status_empty(self) -> bool:
        return not self.source_has_payload()

    @property
    def cache_status_label(self) -> str:
        return "vide" if self.cache_status_empty else "à vider"

    @property
    def cache_status_class(self) -> str:
        if self.cache_status_empty:
            return "badge-ok"
        # Non vide n'est pas une erreur en soi : on ne passe en rouge que si
        # l'espace libre du volume source est bas. Seuil volontairement simple.
        free_bytes = self.source_free_bytes()
        if free_bytes >= 0 and free_bytes < (50 * 1024 * 1024 * 1024):
            return "badge-no"
        return "badge-info"

    @property
    def cache_status_hint(self) -> str:
        if self.cache_status_empty:
            if not self.source:
                return "Aucune source configurée."
            if not os.path.isdir(self.source):
                return "Dossier source absent : cache considéré vide."
            return "Dossier source vide."
        free_bytes = self.source_free_bytes()
        if free_bytes >= 0 and free_bytes < (50 * 1024 * 1024 * 1024):
            return "Source non vide et espace libre inférieur à 50 Go : à vider en priorité."
        return "Source non vide : à vider quand tu veux."

    @property
    def source_exists(self) -> bool:
        return bool(self.source and os.path.isdir(self.source))

    @property
    def destination_exists(self) -> bool:
        return bool(self.destination and os.path.isdir(self.destination))


@dataclass
class CacheJob:
    target: str
    label: str
    dry_run: bool
    log_file: Path
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "queued"
    returncode: Optional[int] = None
    error: str = ""
    started_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))
    ended_at: str = ""
    process: Optional[subprocess.Popen] = None
    progress_percent: Optional[int] = None
    progress_label: str = ""
    progress_phase: str = "idle"
    progress_target: str = ""
    progress_total_bytes: int = 0
    progress_done_bytes: int = 0
    progress_current_bytes: int = 0
    progress_current_total_bytes: int = 0
    progress_expected_by_action: Dict[str, int] = field(default_factory=dict)
    progress_rows: Dict[str, Dict[str, object]] = field(default_factory=dict)

    def log(self, text: str) -> None:
        clean = text.rstrip("\n")
        # Les lignes rsync --info=progress2 contiennent un pourcentage. On le
        # garde dans le job pour que l'interface puisse afficher une barre sans
        # obliger l'utilisateur à ouvrir l'onglet Logs.
        try:
            matches = re.findall(r"(?<!\d)(\d{1,3})%", clean)
            if matches:
                percent = max(0, min(100, int(matches[-1])))
                moved = cache_parse_rsync_progress_bytes(clean)
                if moved is not None:
                    self.progress_phase = "dry_run" if self.dry_run else "copy"
                    self.progress_current_bytes = max(0, moved)
                # Si une simulation interne a estimé le total à déplacer, la
                # progression globale est calculée en octets transférés plutôt
                # qu'avec le pourcentage local de rsync. Cela évite une barre
                # fantaisiste quand plusieurs profils sont lancés avec --all.
                if self.progress_total_bytes > 0:
                    cache_update_job_progress_from_line(self, clean)
                else:
                    self.progress_percent = percent
                    if moved is not None:
                        prefix = "Simulation" if self.dry_run else "Copie"
                        self.progress_label = f"{prefix} : {percent}% · {cache_format_bytes_short(moved)}"
                    else:
                        self.progress_label = clean.strip()[:180]
                    cache_set_row_progress(self, self.progress_target, self.progress_phase, percent, self.progress_label, "running")
        except Exception:
            pass
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(clean + "\n")

    def read_log(self) -> str:
        if not self.log_file.exists():
            return ""
        return self.log_file.read_text(encoding="utf-8", errors="replace")

    def request_stop(self) -> None:
        self.status = "stopping"
        proc = self.process
        if not proc or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                proc.terminate()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass


CACHE_JOBS: Dict[str, CacheJob] = {}
CACHE_JOBS_LOCK = threading.Lock()
ACTIVE_CACHE_JOB_ID: Optional[str] = None


def cache_normalize_module_config(conf: Dict[str, str]) -> Dict[str, str]:
    """Normalise la configuration Cache lue depuis services.conf.

    Les chemins techniques du module Cache sont conservés dans services.conf pour
    compatibilité et pour les sauvegardes internes, mais ils ne sont plus éditables
    depuis l'interface. On force donc les valeurs officielles ici, y compris le
    dossier de backup du module, pour éviter qu'un ancien services.conf réinjecte
    des chemins obsolètes.
    """
    out = dict(conf or {})
    out["PATH_CONF"] = cache_DEFAULT_CONFIG["PATH_CONF"]
    out["BROWSE_ROOTS"] = cache_DEFAULT_CONFIG["BROWSE_ROOTS"]
    out["BACKUP_DIR"] = cache_DEFAULT_CONFIG["BACKUP_DIR"]
    out["LOG_DIR"] = cache_DEFAULT_CONFIG["LOG_DIR"]
    return out


def cache_get_config() -> Dict[str, str]:
    conf = _svc_prefixed_config("CACHE_", cache_DEFAULT_CONFIG, cache_CONFIG_ORDER)
    conf = cache_normalize_module_config(conf)

    # Si services.conf existait déjà sans les clés Cache cachées, ou avec une
    # ancienne valeur, on persiste automatiquement les defaults au premier accès.
    values, _multi = _svc_read_values()
    needs_write = any(values.get("CACHE_" + key) != str(conf.get(key, "")) for key in cache_CONFIG_ORDER)
    if needs_write:
        cache_write_config(conf)

    return conf


def cache_write_config(data: Dict[str, str]) -> None:
    _svc_update_prefixed("CACHE_", data, cache_CONFIG_ORDER)


def cache_real_conf_path(conf: Dict[str, str]) -> Path:
    return resolve_module_path(conf.get("PATH_CONF"), default=BASE_DIR / "conf" / "cache.conf")



def cache_backup_dir(conf: Dict[str, str]) -> Path:
    return resolve_module_path(conf.get("BACKUP_DIR"), default=BASE_DIR / "backups" / "cache")


def cache_log_dir(conf: Dict[str, str]) -> Path:
    return resolve_module_path(conf.get("LOG_DIR"), default=Path("/var/log/cache"))


def cache_ensure_real_conf(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_REAL_CACHE_CONF, encoding="utf-8")


def cache_read_real_conf(path: Path) -> List[CacheBlock]:
    cache_ensure_real_conf(path)
    parser = configparser.ConfigParser(interpolation=None, allow_no_value=False)
    parser.optionxform = str.lower
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        parser.read_file(handle)
    blocks: List[CacheBlock] = []
    for section in parser.sections():
        if section.startswith("group:") or section.lower() == "settings":
            continue
        sec = parser[section]
        blocks.append(
            CacheBlock(
                key=section,
                title=sec.get("title", section),
                command_start=sec.get("command_start", sec.get("before", sec.get("command_before", sec.get("stop", "")))),
                source=normalize_path(sec.get("source", "")),
                destination=normalize_path(sec.get("destination", "")),
                command_end=sec.get("command_end", sec.get("after", sec.get("command_after", sec.get("start", "")))),
                aliases=sec.get("aliases", ""),
                enabled="true" if parse_bool(sec.get("enabled", "true"), True) else "false",
                include_in_all="true" if parse_bool(sec.get("include_in_all", "true"), True) else "false",
            )
        )
    return blocks


def cache_conf_backup(path: Path, backup_dir: Path) -> str:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"{path.name}.{stamp}.bak"
    if path.exists():
        shutil.copy2(path, dest)
    else:
        dest.write_text("", encoding="utf-8")
    return str(dest)


def cache_write_real_conf(path: Path, blocks: List[CacheBlock]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = [
        "# ============================================================",
        "# cache.conf - Configuration du mover cache intégré",
        "# Genere/edite par le module Flask Cache.",
        "# Le fichier reste compatible avec l'ancien format cache.conf.",
        "# ============================================================",
        "",
    ]
    for block in blocks:
        lines.append("# ------------------------------------------------------------")
        lines.append(f"# {block.title or block.key}")
        lines.append("# ------------------------------------------------------------")
        lines.append(f"[{block.key}]")
        values = block.__dict__.copy()
        values.pop("key", None)
        for key in CACHE_ORDER:
            lines.append(f"{key} = {str(values.get(key, '')).strip()}")
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def cache_unique_key(base: str, used: set[str]) -> str:
    """Retourne un nom de profil cache valide et non déjà utilisé.

    Le bouton Ajouter crée d'abord un profil temporaire. Si l'utilisateur a déjà
    un ancien `nouveau_cache_1`, il ne faut pas que l'ajout suivant produise
    immédiatement un faux doublon. On garde cette logique côté serveur aussi,
    au cas où le navigateur aurait une vieille page ou un JS non rafraîchi.
    """
    raw = clean_text(base) or "cache"
    raw = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._-") or "cache"
    raw = raw[:60]
    candidate = raw
    n = 2
    while candidate.lower() in used:
        candidate = f"{raw}_{n}"
        n += 1
    return candidate


def cache_collect_blocks_from_form() -> List[CacheBlock]:
    originals = [clean_text(v) for v in request.form.getlist("cache_original[]")]
    delete_set = {clean_text(v) for v in request.form.getlist("cache_delete[]") if clean_text(v)}
    keys = request.form.getlist("cache_key[]")
    count = max(len(keys), len(originals))

    def get(field: str, i: int, default: str = "") -> str:
        values = request.form.getlist(field)
        if i < len(values):
            return clean_multiline(values[i])
        return default

    blocks: List[CacheBlock] = []
    used: set[str] = set()
    for i in range(count):
        original = originals[i] if i < len(originals) else ""
        if original and original in delete_set:
            continue
        key = clean_text(get("cache_key[]", i))
        title = get("cache_title[]", i)
        if not key and not title:
            continue

        # Pour les nouveaux profils uniquement, éviter les faux doublons créés
        # par les clés temporaires `nouveau_cache_1`, `nouveau_cache_2`, etc.
        if not original:
            key = cache_unique_key(key or title or "cache", used)

        block = CacheBlock(
            key=key,
            title=title,
            command_start=get("cache_command_start[]", i),
            source=normalize_path(get("cache_source[]", i)),
            destination=normalize_path(get("cache_destination[]", i)),
            command_end=get("cache_command_end[]", i),
            aliases=get("cache_aliases[]", i),
            enabled="true" if parse_bool(get("cache_enabled[]", i, "true"), True) else "false",
            include_in_all="true" if parse_bool(get("cache_include_in_all[]", i, "true"), True) else "false",
        )
        blocks.append(block)
        used.add(block.key.lower())
    return blocks


def cache_validate_blocks(blocks: List[CacheBlock]) -> List[str]:
    errors: List[str] = []
    seen: set[str] = set()
    for block in blocks:
        if not block.key:
            errors.append("Un profil cache a un nom vide.")
            continue
        if block.key.lower() == "settings" or block.key.startswith("group:"):
            errors.append(f"Nom de profil reserve : {block.key}")
        if not SECTION_RE.fullmatch(block.key):
            errors.append(f"Nom de profil invalide : {block.key}. Utilise lettres, chiffres, tiret, point ou underscore.")
        if block.key.lower() in seen:
            errors.append(f"Profil cache en double : {block.key}")
        seen.add(block.key.lower())
        if not block.title:
            errors.append(f"Titre manquant pour [{block.key}].")
        if bool(block.source) != bool(block.destination):
            errors.append(f"[{block.key}] source et destination doivent etre remplies ensemble, ou vides ensemble.")
        if block.source and block.destination:
            path_error = cache_profile_path_error(block.source, block.destination)
            if path_error:
                errors.append(f"[{block.key}] {path_error}")
    return errors


# ------------------------------------------------------------
# Moteur Cache intégré
# ------------------------------------------------------------
# Cette partie reprend l'ancienne logique du mover Cache directement dans
# Services : lecture de cache.conf, alias/préfixes, lock fcntl,
# command_start/command_end, contrôles de sécurité et déplacement rsync.

CACHE_ALLOWED_MOVE_ROOTS = ("/",)
CACHE_ALLOWED_SOURCE_ROOTS = tuple(Path(root) for root in CACHE_ALLOWED_MOVE_ROOTS)
CACHE_ALLOWED_DESTINATION_ROOTS = CACHE_ALLOWED_MOVE_ROOTS
CACHE_DEFAULT_LOCK_FILE = Path(os.environ.get("CACHE_LOCK_FILE", "/tmp/cache_mover.lock"))


@dataclass(frozen=True)
class CacheAction:
    key: str
    title: str
    source: Optional[Path]
    destination: Optional[Path]
    command_start: str = ""
    command_end: str = ""
    aliases: Tuple[str, ...] = ()
    enabled: bool = True
    include_in_all: bool = True

    @property
    def has_move(self) -> bool:
        return self.source is not None and self.destination is not None

    @property
    def is_command_only(self) -> bool:
        return not self.has_move and bool(self.command_start.strip() or self.command_end.strip())


@dataclass(frozen=True)
class CacheEngineConfig:
    conf_file: Path
    actions: Dict[str, CacheAction]
    aliases: Dict[str, str]
    lock_file: Path


class CacheNonBlockingLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fp = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = self.path.open("w")
        try:
            fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"ERREUR : moteur Cache déjà en cours d'exécution : {self.path}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fp:
            fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
            self.fp.close()


def cache_split_csv(value: str) -> Tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.replace("\n", ",").split(",") if part.strip())


def cache_first_non_empty(*values: str) -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def cache_normalize_token(value: str) -> str:
    value = str(value or "").strip().lower().replace("_", "-")
    value = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def cache_command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def cache_load_engine_config(path: Path) -> CacheEngineConfig:
    blocks = cache_read_real_conf(path)
    actions: Dict[str, CacheAction] = {}
    aliases: Dict[str, str] = {}

    for block in blocks:
        key = clean_text(block.key)
        source_text = clean_text(block.source)
        destination_text = clean_text(block.destination)
        if bool(source_text) != bool(destination_text):
            raise RuntimeError(f"Section incomplète [{key}] : source et destination doivent être remplis ensemble, ou vides ensemble.")

        action = CacheAction(
            key=key,
            title=clean_text(block.title) or key,
            source=Path(source_text) if source_text else None,
            destination=Path(destination_text) if destination_text else None,
            command_start=cache_first_non_empty(block.command_start),
            command_end=cache_first_non_empty(block.command_end),
            aliases=cache_split_csv(block.aliases),
            enabled=parse_bool(block.enabled, True),
            include_in_all=parse_bool(block.include_in_all, True),
        )
        actions[key] = action
        aliases[cache_normalize_token(key)] = key
        for alias in action.aliases:
            aliases[cache_normalize_token(alias)] = key

    return CacheEngineConfig(
        conf_file=path,
        actions=actions,
        aliases=aliases,
        lock_file=CACHE_DEFAULT_LOCK_FILE,
    )


def cache_select_by_prefix(name: str, config: CacheEngineConfig) -> List[str]:
    token = cache_normalize_token(name)
    prefix = token.replace("-", "_") + "_"
    return [key for key in config.actions if key.startswith(prefix)]


def cache_resolve_names(name: str, config: CacheEngineConfig) -> List[str]:
    cleaned = clean_text(name)
    if cleaned.startswith("--"):
        cleaned = cleaned[2:]
    token = cache_normalize_token(cleaned)
    if token in config.aliases:
        return [config.aliases[token]]

    prefixed = cache_select_by_prefix(cleaned, config)
    if prefixed:
        return prefixed

    raise KeyError(f"Nom inconnu dans cache.conf : {name}")


def cache_build_selection(target: str, config: CacheEngineConfig) -> Tuple[List[CacheAction], set]:
    selected_keys: List[str] = []
    explicit_keys: set = set()
    target = clean_text(target) or "__all__"

    if target in {"__all__", "--all", "all"}:
        for key, action in config.actions.items():
            if action.enabled and action.include_in_all:
                selected_keys.append(key)
    else:
        for key in cache_resolve_names(target, config):
            selected_keys.append(key)
            explicit_keys.add(key)

    result: List[CacheAction] = []
    seen: set = set()
    for key in selected_keys:
        if key not in seen:
            result.append(config.actions[key])
            seen.add(key)
    return result, explicit_keys


def cache_realpath_clean(path: Path | str) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    real = os.path.normpath(os.path.realpath(raw))
    return real.rstrip("/") or "/"


def cache_path_matches_move_root(path: Path, root: Path | str) -> bool:
    path_s = cache_realpath_clean(path)
    root_s = cache_realpath_clean(root)
    if root_s == "/":
        return path_s.startswith("/")
    if root_s == "/mnt/disk":
        return bool(re.fullmatch(r"/mnt/disk(?:\d+)?(?:/.*)?", path_s))
    return path_s == root_s or path_s.startswith(root_s + "/")


def cache_is_dangerous_move_endpoint(path: Path) -> bool:
    path_s = cache_realpath_clean(path)
    return path_s in {"", "/", "/mnt"}


def cache_fstab_unescape(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        try:
            return chr(int(match.group(1), 8))
        except Exception:
            return match.group(0)
    return re.sub(r"\\([0-7]{3})", repl, str(value or ""))


def cache_clean_mergerfs_branch(value: str) -> str:
    branch = cache_fstab_unescape(value).strip()
    if "=" in branch:
        candidate, _mode = branch.rsplit("=", 1)
        if candidate.startswith("/"):
            branch = candidate
    return branch


def cache_expand_mergerfs_branches(source: str) -> List[str]:
    branches: List[str] = []
    for raw_branch in cache_fstab_unescape(source).split(":"):
        branch = cache_clean_mergerfs_branch(raw_branch)
        if not branch or not branch.startswith("/"):
            continue
        expanded = glob.glob(branch) if any(ch in branch for ch in "*?[") else []
        values = expanded or [branch]
        for item in values:
            clean = cache_realpath_clean(item)
            if clean and clean not in branches:
                branches.append(clean)
    return branches


def cache_mergerfs_source_spec(source: str, options: str = "") -> str:
    clean_source = cache_fstab_unescape(source).strip()
    if clean_source and clean_source != "mergerfs":
        return clean_source
    for item in cache_fstab_unescape(options).split(","):
        item = item.strip()
        if item.startswith("branches="):
            return item.split("=", 1)[1].strip()
    return clean_source


def cache_add_mergerfs_view(views: List[Dict[str, object]], seen: set, target: str, source: str, origin: str) -> None:
    clean_target = cache_realpath_clean(cache_fstab_unescape(target))
    branches = cache_expand_mergerfs_branches(source)
    if not clean_target or not branches:
        return
    key = (clean_target, tuple(branches))
    if key in seen:
        return
    seen.add(key)
    views.append({"target": clean_target, "branches": branches, "origin": origin})


def cache_mergerfs_views_from_fstab() -> List[Dict[str, object]]:
    path = Path(os.environ.get("CACHE_FSTAB_FILE", "/etc/fstab"))
    views: List[Dict[str, object]] = []
    seen: set = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return views
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 3:
            continue
        source, target, fstype = parts[:3]
        options = parts[3] if len(parts) > 3 else ""
        blob = " ".join([source, target, fstype, options]).lower()
        if "mergerfs" not in blob:
            continue
        cache_add_mergerfs_view(views, seen, target, cache_mergerfs_source_spec(source, options), "fstab")
    return views


def cache_mergerfs_views_from_mountinfo() -> List[Dict[str, object]]:
    path = Path(os.environ.get("CACHE_MOUNTINFO_FILE", "/proc/self/mountinfo"))
    views: List[Dict[str, object]] = []
    seen: set = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return views
    for raw in lines:
        parts = raw.strip().split()
        if len(parts) < 10 or "-" not in parts:
            continue
        try:
            sep = parts.index("-")
            target = cache_fstab_unescape(parts[4])
            fstype = parts[sep + 1] if len(parts) > sep + 1 else ""
            source = cache_fstab_unescape(parts[sep + 2]) if len(parts) > sep + 2 else ""
            options = cache_fstab_unescape(parts[sep + 3]) if len(parts) > sep + 3 else ""
        except Exception:
            continue
        source_spec = cache_mergerfs_source_spec(source, options)
        if "mergerfs" not in fstype.lower() or ":" not in source_spec:
            continue
        cache_add_mergerfs_view(views, seen, target, source_spec, "mountinfo")
    return views


def cache_collect_mergerfs_views() -> List[Dict[str, object]]:
    views: List[Dict[str, object]] = []
    seen: set = set()
    for row in cache_mergerfs_views_from_fstab() + cache_mergerfs_views_from_mountinfo():
        target = str(row.get("target") or "")
        branches = [str(item) for item in (row.get("branches") or []) if str(item or "")]
        key = (target, tuple(branches))
        if not target or not branches or key in seen:
            continue
        seen.add(key)
        views.append({"target": target, "branches": branches, "origin": row.get("origin") or ""})
    return views


def cache_relative_under(path: str, root: str) -> Optional[str]:
    path_s = cache_realpath_clean(path)
    root_s = cache_realpath_clean(root)
    if not path_s or not root_s:
        return None
    if path_s == root_s:
        return ""
    prefix = root_s + "/"
    if path_s.startswith(prefix):
        return path_s[len(prefix):]
    return None


def cache_join_under(root: str, rel: str) -> str:
    if not rel:
        return cache_realpath_clean(root)
    return cache_realpath_clean(os.path.join(root, rel))


def cache_paths_overlap(left: str, right: str) -> bool:
    left_s = cache_realpath_clean(left)
    right_s = cache_realpath_clean(right)
    if not left_s or not right_s:
        return False
    return left_s == right_s or left_s.startswith(right_s + "/") or right_s.startswith(left_s + "/")


def cache_physical_candidates(path: Path, views: List[Dict[str, object]]) -> List[str]:
    clean_path = cache_realpath_clean(path)
    candidates: List[str] = []

    def add(value: str) -> None:
        clean = cache_realpath_clean(value)
        if clean and clean not in candidates:
            candidates.append(clean)

    add(clean_path)
    for view in views:
        rel = cache_relative_under(clean_path, str(view.get("target") or ""))
        if rel is None:
            continue
        for branch in view.get("branches") or []:
            add(cache_join_under(str(branch), rel))
    return candidates


def cache_find_incompatible_overlap(source: Path, destination: Path) -> Optional[Tuple[str, str]]:
    views = cache_collect_mergerfs_views()
    source_candidates = cache_physical_candidates(source, views)
    destination_candidates = cache_physical_candidates(destination, views)
    for src in source_candidates:
        for dst in destination_candidates:
            if cache_paths_overlap(src, dst):
                return src, dst
    return None


def cache_check_move_coherence(source: Path, destination: Path) -> None:
    source_s = cache_realpath_clean(source)
    destination_s = cache_realpath_clean(destination)
    if not source_s.startswith("/") or not destination_s.startswith("/"):
        raise RuntimeError("ERREUR : source incompatible avec cible : les chemins doivent etre absolus.")
    if cache_is_dangerous_move_endpoint(source) or cache_is_dangerous_move_endpoint(destination):
        raise RuntimeError("ERREUR : source incompatible avec cible : chemin trop large.")
    conflict = cache_find_incompatible_overlap(source, destination)
    if conflict:
        left, right = conflict
        raise RuntimeError(
            "ERREUR : source incompatible avec cible : les deux chemins se voient ou se chevauchent "
            f"({left} <-> {right})."
        )


def cache_profile_path_error(source: str, destination: str) -> str:
    try:
        cache_check_move_coherence(Path(source), Path(destination))
    except RuntimeError as exc:
        message = str(exc)
        if message.startswith("ERREUR : "):
            message = message[len("ERREUR : "):]
        return message
    except Exception as exc:
        return f"verification coherence impossible : {exc}"
    return ""


def cache_safe_path_checks(action: CacheAction) -> None:
    if action.source is None or action.destination is None:
        return

    source = action.source
    destination = action.destination

    if not any(cache_path_matches_move_root(source, root) for root in CACHE_ALLOWED_SOURCE_ROOTS):
        roots = ", ".join(str(p) for p in CACHE_ALLOWED_SOURCE_ROOTS)
        raise RuntimeError(f"ERREUR : source refusée hors racines autorisées ({roots}) : {source}")

    if cache_is_dangerous_move_endpoint(source):
        raise RuntimeError(f"ERREUR : source trop dangereuse, racine directe refusÃ©e : {source}")

    for root in CACHE_ALLOWED_SOURCE_ROOTS:
        if os.path.realpath(source) == os.path.realpath(root):
            raise RuntimeError(f"ERREUR : source trop dangereuse, racine directe refusée : {source}")

    if not any(cache_path_matches_move_root(destination, root) for root in CACHE_ALLOWED_DESTINATION_ROOTS):
        roots = ", ".join(CACHE_ALLOWED_DESTINATION_ROOTS)
        raise RuntimeError(f"ERREUR : destination refusée hors racines autorisées ({roots}) : {destination}")

    if cache_is_dangerous_move_endpoint(destination):
        raise RuntimeError(f"ERREUR : destination trop dangereuse : {destination}")

    dangerous = {"/", "/mnt"}
    if os.path.realpath(destination) in dangerous:
        raise RuntimeError(f"ERREUR : destination trop dangereuse : {destination}")

    src_real = os.path.realpath(source)
    dst_real = os.path.realpath(destination)
    if src_real == dst_real:
        raise RuntimeError("ERREUR : source et destination identiques.")

    cache_check_move_coherence(source, destination)


def cache_run_quiet(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def cache_format_bytes_short(value: int) -> str:
    try:
        size = float(max(0, int(value)))
    except Exception:
        size = 0.0
    units = ["o", "Ko", "Mo", "Go", "To"]
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    return f"{size:.1f} {units[idx]}"


def cache_parse_size_to_bytes(value: str) -> Optional[int]:
    """Convertit les tailles rsync en octets.

    Accepte les formats du --stats et du --info=progress2 :
    123456789, 1,234,567, 127 666 bytes, 15.26M, 850.4MB, 1.2G bytes.
    """
    raw = str(value or "").strip().replace("\xa0", " ")
    if not raw:
        return None
    raw = re.sub(r"\s+(?:bytes?|octets?)$", "", raw, flags=re.I).strip()
    match = re.match(r"^([0-9][0-9., ]*)\s*([KMGTPE]?i?B?|bytes?|octets?)?$", raw, flags=re.I)
    if not match:
        return None
    number = (match.group(1) or "").strip().replace(" ", "")
    unit = (match.group(2) or "").strip().upper()
    if not number:
        return None

    if not unit:
        # Sans unité, rsync affiche souvent les milliers avec des virgules.
        digits = re.sub(r"[^0-9]", "", number)
        return int(digits) if digits else None

    if "," in number and "." not in number:
        number = number.replace(",", ".")
    else:
        number = number.replace(",", "")
    try:
        amount = float(number)
    except Exception:
        return None

    if unit.startswith("BYTE") or unit.startswith("OCTET") or unit == "B":
        factor = 1
    else:
        # Cohérent avec l'affichage interne Ko/Mo/Go du module Cache.
        factor = 1024 ** {"K": 1, "M": 2, "G": 3, "T": 4, "P": 5, "E": 6}.get(unit[:1], 0)
    return int(amount * factor)


def cache_parse_stat_bytes(text: str) -> int:
    parsed = cache_parse_size_to_bytes(str(text or ""))
    return int(parsed or 0)


def cache_parse_rsync_progress_bytes(line: str) -> Optional[int]:
    # Exemples rsync --info=progress2 :
    #   "15.26M   0%    2.76MB/s ..."
    #   "127,666 100%   12.34MB/s ..."
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", str(line or "")).strip()
    if "%" not in text:
        return None
    m = re.match(
        r"^\s*(?P<amount>[0-9][0-9., ]*\s*(?:[KMGTPE]?i?B?|bytes?|octets?)?)\s+"
        r"(?P<percent>100|[0-9]{1,2})%",
        text,
        re.I,
    )
    if not m:
        return None
    return cache_parse_size_to_bytes(m.group("amount") or "")


def cache_row_percent(percent: Optional[object]) -> Optional[int]:
    if percent is None or percent == "":
        return None
    try:
        return int(min(100, max(0, round(float(percent)))))
    except (TypeError, ValueError):
        return None


def cache_set_row_progress(
    job: CacheJob,
    action_key: str,
    phase: str,
    percent: Optional[object],
    label: str = "",
    state: str = "running",
) -> None:
    key = str(action_key or "").strip()
    if not key or key in {"__all__", "--all", "all"}:
        return
    job.progress_rows[key] = {
        "state": state or "running",
        "phase": phase or "idle",
        "percent": cache_row_percent(percent),
        "label": str(label or "")[:140],
    }


def cache_finish_known_rows(job: CacheJob, state: str, label: str, percent: int = 100) -> None:
    for key, row in list(job.progress_rows.items()):
        if row.get("state") == "error":
            continue
        cache_set_row_progress(job, key, "done" if state == "success" else state, percent, label, state)


def cache_mark_stopped_rows(job: CacheJob) -> None:
    """Marque en rouge les lignes non terminées quand l'utilisateur arrête le mover."""
    marked = False
    for key, row in list(job.progress_rows.items()):
        if row.get("state") == "success":
            continue
        cache_set_row_progress(job, key, "stopped", 100, "Arrêté", "error")
        marked = True
    target = str(job.progress_target or job.target or "").strip()
    if target and target not in {"__all__", "--all", "all"}:
        current = job.progress_rows.get(target) or {}
        if current.get("state") != "success":
            cache_set_row_progress(job, target, "stopped", 100, "Arrêté", "error")
            marked = True
    if not marked and target and target not in {"__all__", "--all", "all"}:
        cache_set_row_progress(job, target, "stopped", 100, "Arrêté", "error")


def cache_update_job_progress_from_line(job: CacheJob, line: str) -> None:
    moved = cache_parse_rsync_progress_bytes(line)
    if moved is not None:
        job.progress_current_bytes = max(0, moved)
        job.progress_phase = "dry_run" if job.dry_run else "copy"
    if job.progress_total_bytes <= 0:
        return
    current_cap = job.progress_current_total_bytes or job.progress_current_bytes
    current = min(job.progress_current_bytes, current_cap) if current_cap else job.progress_current_bytes
    done = max(0, job.progress_done_bytes) + max(0, current)
    pct = int(min(99, max(0, round((done * 100.0) / max(1, job.progress_total_bytes)))))
    job.progress_percent = pct
    prefix = "Simulation" if job.dry_run else "Copie"
    job.progress_label = f"{prefix} : {cache_format_bytes_short(done)} / {cache_format_bytes_short(job.progress_total_bytes)}"
    row_pct = None
    if job.progress_current_total_bytes > 0:
        row_pct = int(min(99, max(0, round((current * 100.0) / max(1, job.progress_current_total_bytes)))))
    cache_set_row_progress(job, job.progress_target, job.progress_phase, row_pct, f"{prefix} : {row_pct} %" if row_pct is not None else f"{prefix}...", "running")


def cache_mark_action_done(job: CacheJob, action: CacheAction) -> None:
    expected = int(job.progress_expected_by_action.get(action.key, 0) or 0)
    # Si le dry-run n'a pas su estimer ce profil mais que rsync a tout de même
    # publié des octets, on reprend le compteur courant pour éviter une barre
    # finale bloquée à 0 %.
    if expected <= 0 and job.progress_current_bytes > 0:
        expected = job.progress_current_bytes
    if expected > 0:
        job.progress_done_bytes += expected
    job.progress_current_bytes = 0
    job.progress_current_total_bytes = 0
    if job.progress_total_bytes > 0:
        job.progress_done_bytes = min(job.progress_done_bytes, job.progress_total_bytes)
        pct = int(min(99, max(0, round((job.progress_done_bytes * 100.0) / max(1, job.progress_total_bytes)))))
        job.progress_percent = pct
        job.progress_phase = "copy"
        job.progress_label = f"Copie : {cache_format_bytes_short(job.progress_done_bytes)} / {cache_format_bytes_short(job.progress_total_bytes)}"
    cache_set_row_progress(job, action.key, "done", 100, "Termine", "success")


def cache_estimate_action_bytes(job: CacheJob, action: CacheAction) -> int:
    if not action.has_move or action.source is None or action.destination is None:
        return 0
    if not action.enabled or not action.source.exists() or not cache_has_payload(action.source):
        return 0
    try:
        cache_safe_path_checks(action)
    except Exception as exc:
        job.log(f"Pré-simulation ignorée pour {action.title} : {exc}")
        return 0

    cmd = [
        "rsync",
        "-aAXHhv",
        "--dry-run",
        "--stats",
        "--outbuf=L",
        str(action.source) + "/",
        str(action.destination) + "/",
    ]
    cp = cache_run_quiet(cmd)
    output = (cp.stdout or "") + "\n" + (cp.stderr or "")
    if cp.returncode != 0:
        tail = "\n".join(output.strip().splitlines()[-4:])
        job.log(f"Pré-simulation indisponible pour {action.title}. Progression rsync classique utilisée.")
        if tail:
            job.log(tail)
        return 0

    transferred: Optional[int] = None
    total: Optional[int] = None
    for raw in output.splitlines():
        line = raw.strip()
        low = line.lower()
        if low.startswith("total transferred file size"):
            transferred = cache_parse_stat_bytes(line.split(":", 1)[-1])
        elif low.startswith("total file size"):
            total = cache_parse_stat_bytes(line.split(":", 1)[-1])
    if transferred is not None:
        return max(0, transferred)
    return max(0, total or 0)


def cache_prescan_actions(job: CacheJob, actions: List[CacheAction]) -> None:
    for action in actions:
        cache_set_row_progress(job, action.key, "idle", 0, "En attente", "idle")
    if job.dry_run:
        return
    # Phase volontairement indéterminée : on calcule d'abord le volume réel
    # à déplacer avec rsync --dry-run. Tant que ce total n'est pas connu, une
    # barre chiffrée à 0 % donne une fausse impression de blocage.
    job.progress_target = "__all__" if job.target in {"__all__", "--all", "all"} else job.target
    job.progress_phase = "calcul"
    job.progress_percent = None
    job.progress_label = "Calcul du volume…"
    job.log("")
    job.log("Calcul interne des transferts rsync pour une progression réelle...")
    expected: Dict[str, int] = {}
    total = 0
    for action in actions:
        size = cache_estimate_action_bytes(job, action)
        if size > 0:
            expected[action.key] = size
            total += size
            job.log(f"  - {action.title} : {cache_format_bytes_short(size)} à déplacer")
    job.progress_expected_by_action = expected
    job.progress_total_bytes = total
    job.progress_done_bytes = 0
    job.progress_current_bytes = 0
    job.progress_current_total_bytes = 0
    if total > 0:
        job.progress_phase = "copy"
        job.progress_percent = 0
        job.progress_label = f"Copie : 0 / {cache_format_bytes_short(total)}"
        job.log(f"Total estimé : {cache_format_bytes_short(total)}")
    else:
        # Si l'estimation échoue, on repasse en progression rsync classique
        # plutôt que de garder une barre de calcul bloquée.
        job.progress_phase = "copy"
        job.progress_percent = None
        job.progress_label = "Copie rsync…"
        job.log("Total estimé : 0 octet à déplacer ou estimation indisponible.")


def cache_stream_command(job: CacheJob, cmd: List[str], *, cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> int:
    creationflags = 0
    preexec_fn = None
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        preexec_fn = os.setsid

    with subprocess.Popen(
        cmd,
        cwd=str(cwd or BASE_DIR),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        creationflags=creationflags,
        preexec_fn=preexec_fn,
    ) as proc:
        job.process = proc
        try:
            assert proc.stdout is not None
            buf: List[str] = []

            def flush_buffer() -> None:
                if not buf:
                    return
                line = "".join(buf).strip("\r\n")
                buf.clear()
                if line.strip():
                    job.log(line.strip())

            # rsync écrit la progression avec des retours chariot. Lire caractère
            # par caractère permet de mettre à jour la progression en direct.
            while True:
                ch = proc.stdout.read(1)
                if ch == "":
                    break
                if ch in ("\r", "\n"):
                    flush_buffer()
                else:
                    buf.append(ch)
            flush_buffer()
            return proc.wait()
        finally:
            job.process = None


def cache_has_payload(path: Path) -> bool:
    if not path.is_dir():
        return False
    cp = cache_run_quiet(["find", str(path), "(", "-type", "f", "-o", "-type", "l", ")", "-print", "-quit"])
    return bool(cp.stdout.strip())


def cache_cleanup_empty_dirs(job: CacheJob, source: Path, dry_run: bool = False) -> None:
    if dry_run:
        job.log(f"DRY-RUN : nettoyage dossiers vides ignoré : {source}")
        return
    if not source.exists():
        return

    cache_run_quiet(["find", str(source), "-mindepth", "1", "-type", "d", "-empty", "-delete"])
    try:
        source.rmdir()
        job.log(f"🧹 Dossier source vide supprimé : {source}")
    except OSError:
        pass


def cache_prepare_destination(job: CacheJob, source: Path, destination: Path, dry_run: bool = False) -> None:
    if dry_run:
        job.log(f"DRY-RUN : création destination ignorée : {destination}")
        return

    destination.mkdir(parents=True, exist_ok=True)
    cache_run_quiet(["chmod", f"--reference={source}", str(destination)])
    cache_run_quiet(["chown", f"--reference={source}", str(destination)])


def cache_run_shell_command(job: CacheJob, command: str, *, label: str, dry_run: bool = False) -> bool:
    command = clean_multiline(command)
    if not command:
        return True

    job.log("")
    job.log(f">>> {label}")
    job.log(command)
    if dry_run:
        job.log("DRY-RUN : commande ignorée")
        return True

    rc = cache_stream_command(job, ["bash", "-lc", command])
    if rc != 0:
        job.log(f"❌ Commande échouée ({rc}) : {label}")
        return False
    return True


def cache_move_payload(job: CacheJob, action: CacheAction, *, dry_run: bool = False) -> bool:
    assert action.source is not None
    assert action.destination is not None

    job.log("")
    job.log("============================================================")
    job.log(f"Déplacement : {action.title}")
    job.log(f"SOURCE      : {action.source}/")
    job.log(f"DESTINATION : {action.destination}/")
    job.log("============================================================")

    cache_prepare_destination(job, action.source, action.destination, dry_run=dry_run)

    if dry_run:
        job.progress_phase = "dry_run"
        job.progress_label = f"Simulation : {action.title}"
    else:
        job.progress_phase = "copy"
        cache_run_quiet(["sync"])
        if job.progress_total_bytes > 0:
            job.progress_current_total_bytes = int(job.progress_expected_by_action.get(action.key, 0) or 0)
            job.progress_current_bytes = 0
            cache_update_job_progress_from_line(job, "0 0%")
        else:
            job.progress_label = f"Copie : {action.title}"

    rsync_cmd = [
        "rsync",
        "-aAXHhv",
        "--outbuf=L",
        "--info=progress2",
        "--stats",
        "--remove-source-files",
    ]
    if dry_run:
        rsync_cmd.append("--dry-run")
    rsync_cmd.extend([str(action.source) + "/", str(action.destination) + "/"])

    job.log(">>> " + " ".join(shlex.quote(x) for x in rsync_cmd))
    rc = cache_stream_command(job, rsync_cmd)
    if rc != 0:
        cache_set_row_progress(job, action.key, "error", 100, "Erreur", "error")
        job.log(f"❌ Erreur rsync : {action.title}")
        return False

    cache_cleanup_empty_dirs(job, action.source, dry_run=dry_run)
    if not dry_run and job.progress_total_bytes > 0:
        cache_mark_action_done(job, action)
    else:
        job.progress_percent = 100
        cache_set_row_progress(job, action.key, "done", 100, "Termine", "success")
    job.log(f"✅ Terminé : {action.title}")
    return True


def cache_execute_action(job: CacheJob, action: CacheAction, *, dry_run: bool = False, explicit: bool = False) -> bool:
    job.progress_target = action.key
    if dry_run:
        job.progress_phase = "dry_run"
        job.progress_label = f"Simulation : {action.title}"
    elif action.has_move:
        job.progress_phase = "copy"
        if not job.progress_label or job.progress_phase == "calcul":
            job.progress_label = f"Copie : {action.title}"
    else:
        job.progress_phase = "command"
        job.progress_label = action.title
    cache_set_row_progress(job, action.key, job.progress_phase, 0, job.progress_label, "running")
    # Avec un total global pré-calculé, ne remets pas la barre à 0 à chaque
    # profil : elle doit représenter l'avancement global du mover.
    if not (job.progress_total_bytes > 0 and not dry_run):
        job.progress_percent = 0

    if job.status == "stopping":
        return False

    if not action.enabled:
        job.progress_percent = 100
        cache_set_row_progress(job, action.key, "done", 100, "Ignore", "success")
        job.log(f"⏭️  Désactivé : {action.title}")
        return True

    if action.has_move:
        cache_safe_path_checks(action)
        assert action.source is not None

        if not action.source.exists():
            job.progress_percent = 100
            cache_set_row_progress(job, action.key, "done", 100, "Source absente", "success")
            job.log(f"⏭️  Source absente : {action.title}")
            job.log(f"    {action.source}")
            return True

        if not cache_has_payload(action.source):
            job.log(f"⏭️  Aucun fichier à déplacer : {action.title}")
            job.log(f"    Nettoyage éventuel des dossiers vides : {action.source}")
            cache_cleanup_empty_dirs(job, action.source, dry_run=dry_run)
            job.progress_percent = 100
            cache_set_row_progress(job, action.key, "done", 100, "Rien a deplacer", "success")
            return True

        ok = True
        if not cache_run_shell_command(job, action.command_start, label=f"commande début {action.key}", dry_run=dry_run):
            cache_set_row_progress(job, action.key, "error", 100, "Erreur", "error")
            return False
        try:
            ok = cache_move_payload(job, action, dry_run=dry_run)
        finally:
            if not cache_run_shell_command(job, action.command_end, label=f"commande fin {action.key}", dry_run=dry_run):
                ok = False
        if not ok:
            cache_set_row_progress(job, action.key, "error", 100, "Erreur", "error")
        return ok

    if action.is_command_only:
        if not explicit and not action.include_in_all:
            cache_set_row_progress(job, action.key, "done", 100, "Ignore", "success")
            return True
        ok = cache_run_shell_command(job, action.command_start, label=f"commande début {action.key}", dry_run=dry_run)
        if not cache_run_shell_command(job, action.command_end, label=f"commande fin {action.key}", dry_run=dry_run):
            ok = False
        if ok:
            job.progress_percent = 100
            cache_set_row_progress(job, action.key, "done", 100, "Termine", "success")
        else:
            cache_set_row_progress(job, action.key, "error", 100, "Erreur", "error")
        return ok

    job.progress_percent = 100
    cache_set_row_progress(job, action.key, "done", 100, "Ignore", "success")
    job.log(f"⏭️  Bloc sans action : {action.title}")
    return True


def cache_run_integrated_engine(job: CacheJob, real_conf: Path) -> int:
    config = cache_load_engine_config(real_conf)

    if not cache_command_exists("rsync"):
        job.log("ERREUR : rsync introuvable.")
        return 1

    actions, explicit_keys = cache_build_selection(job.target, config)
    if not actions:
        job.log("❌ Action manquante.")
        job.log("Exemples :")
        job.log("  --all")
        job.log("  media")
        job.log("  --media")
        job.log("  --immich")
        job.log("  --nextcloud")
        return 1

    ok = True
    with CacheNonBlockingLock(config.lock_file):
        job.log("============================================================")
        job.log("CACHE MOVER - moteur intégré Services piloté par cache.conf")
        job.log(f"CONF : {config.conf_file}")
        job.log(f"LOCK : {config.lock_file}")
        if job.dry_run:
            job.log("MODE : DRY-RUN")
        job.log("============================================================")

        cache_prescan_actions(job, actions)

        for action in actions:
            if job.status == "stopping":
                ok = False
                break
            explicit = action.key in explicit_keys
            if not cache_execute_action(job, action, dry_run=job.dry_run, explicit=explicit):
                cache_set_row_progress(job, action.key, "error", 100, "Erreur", "error")
                ok = False

    job.log("")
    if ok:
        job.log("✅ Cache mover terminé.")
        return 0
    if job.status == "stopping":
        job.log("Cache mover arrêté.")
        return 130
    job.log("❌ Cache mover terminé avec erreur.")
    return 1



def cache_summary_text(conf: Dict[str, str], blocks: List[CacheBlock]) -> str:
    lines = [
        "Configuration Cache",
        f"  conf       : {cache_real_conf_path(conf)}",
        "  moteur     : intégré dans services.py",
        f"  logs       : {cache_log_dir(conf)}",
        f"  backups    : {cache_backup_dir(conf)}",
        "",
        "Commandes Flask",
        "  Tout vider : --all via moteur intégré",
        "  Un profil  : nom / alias / préfixe",
        "",
        "Profils",
    ]
    for block in blocks:
        state = "actif" if block.is_enabled else "desactive"
        all_state = "--all" if block.is_all else "manuel"
        lines.append(f"  --{block.key:<22} {block.title} ({state}, {all_state})")
        if block.has_move:
            lines.append(f"      {block.source} -> {block.destination}")
        if block.command_start:
            lines.append(f"      debut : {block.command_start}")
        if block.command_end:
            lines.append(f"      fin   : {block.command_end}")
    return "\n".join(lines)


def cache_blocks_status_payload(blocks: List[CacheBlock]) -> List[Dict[str, object]]:
    """Retourne l'état actuel des sources cache pour rafraîchir le tableau sans F5."""
    rows: List[Dict[str, object]] = []
    for block in blocks:
        rows.append({
            "key": block.key,
            "cache_status_label": block.cache_status_label,
            "cache_status_class": block.cache_status_class,
            "cache_status_hint": block.cache_status_hint,
            "cache_status_empty": block.cache_status_empty,
        })
    return rows


def cache_job_to_dict(job: CacheJob, *, include_log: bool = False) -> Dict[str, object]:
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
        "running": job.status in {"queued", "running", "stopping"},
        "progress_percent": job.progress_percent,
        "progress_label": job.progress_label,
        "progress_phase": job.progress_phase,
        "progress_target": job.progress_target,
        "progress_total_bytes": job.progress_total_bytes,
        "progress_done_bytes": job.progress_done_bytes,
        "progress_rows": job.progress_rows,
    }
    if include_log:
        data["log"] = job.read_log()
    return data


def _active_cache_job_locked() -> Optional[CacheJob]:
    if ACTIVE_CACHE_JOB_ID and ACTIVE_CACHE_JOB_ID in CACHE_JOBS:
        job = CACHE_JOBS[ACTIVE_CACHE_JOB_ID]
        if job.status in {"queued", "running", "stopping"}:
            return job
    return None


def start_cache_job(*, target: str, dry_run: bool = False) -> CacheJob:
    global ACTIVE_CACHE_JOB_ID
    conf = cache_get_config()
    real_conf = cache_real_conf_path(conf)
    log_name = f"flask_cache_{now_stamp()}_{uuid.uuid4().hex[:6]}.log"
    log_file = cache_log_dir(conf) / log_name
    label = "--all" if target in {"__all__", "--all", "all"} else target
    job = CacheJob(target=target, label=label, dry_run=dry_run, log_file=log_file)
    with CACHE_JOBS_LOCK:
        active = _active_cache_job_locked()
        if active:
            raise RuntimeError(f"Un cache mover est deja en cours : {active.label}")
        CACHE_JOBS[job.id] = job
        ACTIVE_CACHE_JOB_ID = job.id
        if len(CACHE_JOBS) > 30:
            for old_id in list(CACHE_JOBS.keys())[:-30]:
                if old_id != ACTIVE_CACHE_JOB_ID:
                    CACHE_JOBS.pop(old_id, None)
    thread = threading.Thread(target=_run_cache_job_thread, args=(job.id, str(real_conf)), daemon=True)
    thread.start()
    return job


def _run_cache_job_thread(job_id: str, real_conf: str) -> None:
    global ACTIVE_CACHE_JOB_ID
    job = CACHE_JOBS[job_id]
    job.status = "running"
    try:
        real_conf_path = Path(real_conf)
        env = os.environ.copy()
        env["CACHE_CONF"] = str(real_conf_path)
        job.log(f"--- LOG {now_text()} : {job.log_file} ---")
        job.log("CACHE - lancement du moteur intégré Services")
        job.log(f"Conf : {real_conf_path}")
        job.returncode = cache_run_integrated_engine(job, real_conf_path)

        if job.status == "stopping":
            job.progress_phase = "stopped"
            job.progress_percent = 100
            job.progress_label = "Arrêté"
            cache_mark_stopped_rows(job)
            job.status = "stopped"
            job.log("Cache mover arrete.")
        elif job.returncode == 0:
            job.progress_phase = "done"
            job.progress_percent = 100
            cache_finish_known_rows(job, "success", "Termine", 100)
            if job.progress_total_bytes > 0:
                job.progress_done_bytes = job.progress_total_bytes
                job.progress_label = f"Terminé : {cache_format_bytes_short(job.progress_total_bytes)} / {cache_format_bytes_short(job.progress_total_bytes)}"
            else:
                job.progress_label = "Terminé"
            job.status = "success"
            job.log("Cache mover termine.")
        else:
            job.progress_phase = "error"
            cache_set_row_progress(job, job.progress_target, "error", 100, "Erreur", "error")
            job.status = "error"
            job.log(f"Cache mover termine avec erreur, code {job.returncode}.")
    except Exception as exc:
        job.error = str(exc)
        job.progress_phase = "error" if job.status != "stopping" else "stopped"
        job.status = "error" if job.status != "stopping" else "stopped"
        job.returncode = 1 if job.status == "error" else 130
        if job.status == "stopped":
            job.progress_percent = 100
            job.progress_label = "Arrêté"
            cache_mark_stopped_rows(job)
        else:
            cache_set_row_progress(job, job.progress_target, job.progress_phase, 100, "Erreur", "error")
        job.log(f"Erreur Cache : {exc}")
    finally:
        job.ended_at = now_text()
        job.log(f"--- FIN LOG {now_text()} ---")
        with CACHE_JOBS_LOCK:
            if ACTIVE_CACHE_JOB_ID == job.id:
                ACTIVE_CACHE_JOB_ID = None




def _render_cache():
    conf = cache_get_config()
    real_conf = cache_real_conf_path(conf)
    error = ""
    blocks: List[CacheBlock] = []
    try:
        blocks = cache_read_real_conf(real_conf)
    except Exception as exc:
        error = str(exc)
    cache_active_subtab = services_requested_subtab({"general", "profiles", "option", "info", "logs", "log"}, "general")
    if cache_active_subtab in {"profiles", "option"}:
        return services_redirect("cache", subtab="general")
    elif cache_active_subtab == "log":
        cache_active_subtab = "logs"
    return render_template(
        "services_cache.html",
        conf=conf,
        config_file=str(CONFIG_FILE),
        real_conf=str(real_conf),
        log_dir=str(cache_log_dir(conf)),
        backup_dir=str(cache_backup_dir(conf)),
        allowed_roots=allowed_roots(conf),
        error=error,
        cache_blocks=blocks,
        active_count=sum(1 for b in blocks if b.is_enabled),
        all_count=sum(1 for b in blocks if b.is_enabled and b.is_all),
        command_count=sum(1 for b in blocks if b.command_start or b.command_end),
        active_subtab=cache_active_subtab,
        service_active="cache",
    )


@services_bp.route("/services/cache/config", methods=["POST"])
def cache_config_save():
    # Route conservée en compatibilité : l'UI ne propose plus d'édition des
    # chemins techniques Cache, mais un ancien bouton ou un appel manuel ne doit
    # pas pouvoir réinjecter une valeur différente.
    conf = cache_normalize_module_config(cache_DEFAULT_CONFIG)
    cache_write_config(conf)
    flash("Configuration technique Cache remise aux valeurs par defaut.", "success")
    return services_redirect("cache", subtab="info")


@services_bp.route("/services/cache/save", methods=["POST"])
def cache_save():
    conf = cache_get_config()
    real_conf = cache_real_conf_path(conf)
    try:
        blocks = cache_collect_blocks_from_form()
        errors = cache_validate_blocks(blocks)
        if errors:
            for err in errors:
                flash("Erreur Cache : " + err, "error")
            return services_redirect("cache", subtab="general")
        backup_path = cache_conf_backup(real_conf, cache_backup_dir(conf))
        cache_write_real_conf(real_conf, blocks)
        flash("✅ cache.conf sauvegardé.", "success")
    except Exception as exc:
        flash(f"Erreur sauvegarde Cache : {exc}", "error")
    return services_redirect("cache", subtab="general")


@services_bp.route("/services/cache/check", methods=["POST"])
def cache_check():
    conf = cache_get_config()
    try:
        blocks = cache_read_real_conf(cache_real_conf_path(conf))
        return jsonify({"ok": True, "code": 0, "output": cache_summary_text(conf, blocks)})
    except Exception as exc:
        return jsonify({"ok": False, "code": 1, "output": str(exc)})


@services_bp.route("/services/cache/api/validate-profile", methods=["POST"])
def cache_api_validate_profile():
    data = request.get_json(silent=True) or {}
    source = normalize_path(str(data.get("source") or ""))
    destination = normalize_path(str(data.get("destination") or ""))
    if bool(source) != bool(destination):
        return jsonify({"ok": False, "error": "source et destination doivent etre remplies ensemble, ou vides ensemble."}), 400
    if source and destination:
        path_error = cache_profile_path_error(source, destination)
        if path_error:
            return jsonify({"ok": False, "error": path_error}), 400
    return jsonify({"ok": True})


@services_bp.route("/services/cache/api/run", methods=["POST"])
def cache_api_run():
    data = request.get_json(silent=True) or {}
    target = clean_text(data.get("target") or "__all__")
    action = clean_text(data.get("action") or "start").lower()
    dry_run = parse_bool(str(data.get("dry_run", "0")))

    if action == "stop":
        return cache_api_stop()
    if action == "restart":
        with CACHE_JOBS_LOCK:
            active = _active_cache_job_locked()
        if active:
            active.request_stop()
            return jsonify({"ok": False, "message": "Arret demande. Relance le cache apres arret complet.", "job": cache_job_to_dict(active, include_log=True)})
    try:
        job = start_cache_job(target=target, dry_run=dry_run)
        return jsonify({"ok": True, "job": cache_job_to_dict(job, include_log=True)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@services_bp.route("/services/cache/api/stop", methods=["POST"])
def cache_api_stop():
    data = request.get_json(silent=True) or {}
    job_id = clean_text(data.get("job_id") or "")
    with CACHE_JOBS_LOCK:
        job = CACHE_JOBS.get(job_id) if job_id else _active_cache_job_locked()
    if not job:
        return jsonify({"ok": False, "error": "Aucun cache mover en cours."}), 404
    job.request_stop()
    return jsonify({"ok": True, "job": cache_job_to_dict(job, include_log=True)})


@services_bp.route("/services/cache/api/log", methods=["GET"])
def cache_api_log():
    job_id = clean_text(request.args.get("job_id") or "")
    with CACHE_JOBS_LOCK:
        job = CACHE_JOBS.get(job_id) if job_id else _active_cache_job_locked()
    if not job:
        return jsonify({"ok": False, "error": "Job cache introuvable."}), 404
    return jsonify({"ok": True, "job": cache_job_to_dict(job, include_log=True)})


@services_bp.route("/services/cache/api/status", methods=["GET"])
def cache_api_status():
    """État à jour des profils cache pour rafraîchir la colonne Statut sans recharger la page."""
    conf = cache_get_config()
    try:
        blocks = cache_read_real_conf(cache_real_conf_path(conf))
        return jsonify({"ok": True, "rows": cache_blocks_status_payload(blocks)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@services_bp.route("/services/cache/api/browse", methods=["GET"])
def cache_browse():
    conf = cache_get_config()
    roots = allowed_roots(conf)
    requested = normalize_path(request.args.get("path") or roots[0])
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
        with os.scandir(real) as scan:
            for entry in scan:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append(entry)
                except OSError:
                    continue
        dirs.sort(key=lambda e: e.name.lower())
        for entry in dirs:
            path = os.path.realpath(entry.path)
            if is_under_allowed_root(path, roots):
                items.append({"type": "dir", "name": entry.name, "path": path})
        return jsonify({"ok": True, "path": real, "items": items, "roots": roots})
    except PermissionError:
        return jsonify({"ok": False, "path": real, "error": "Permission refusee.", "items": items, "roots": roots})
    except Exception as exc:
        return jsonify({"ok": False, "path": real, "error": str(exc), "items": items, "roots": roots})
