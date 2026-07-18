MODULE_DIR = Path(__file__).resolve().parent
BASE_DIR = MODULE_DIR.parent
CONFIG_FILE = Path(SERVICES_CONFIG_FILE)

DEFAULT_MODULE_CONFIG: Dict[str, str] = {
    # Seule dépendance volontaire : le vrai archive.conf commun au CLI et au Flask.
    # Si le module Flask est dans system/, ../conf/archive.conf pointe vers le même
    # fichier que le moteur archive utilise par défaut côté terminal.
    "PATH_CONF": nas_conf_file("archive.conf"),
    "BROWSE_ROOTS": "/",
    "BACKUP_DIR": "../backups/archive",
}

DEFAULT_REAL_ARCHIVE_CONF = """# ============================================================
# archive.conf - Configuration commune Archive CLI/Flask
# ============================================================
# Fichier éditable par le module Flask Archive.
# Le même fichier est utilisable depuis le terminal et depuis le Flask.
# Aucun profil n'est créé par défaut : ajoute tes profils depuis l'interface.
# ============================================================

[settings]
log_dir = /var/log/archive
lock_file = /tmp/archive.py.lock
"""

SECTION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
BOOL_TRUE = {"1", "true", "yes", "y", "on", "oui"}
BOOL_FALSE = {"0", "false", "no", "n", "off", "non", ""}

MAIN_ORDER = [
    "title",
    "command_start",
    "source",
    "destination",
    "archive",
    "archive_format",
    "archive_children",
    "archive_name",
    "compression_level",
    "replace_existing",
    "date_suffix",
    "delete_extra",
    "excludes",
    "command_end",
    "docker_exclude",
    "wait_start_docker_stopped",
    "wait_end_docker_running",
    "docker_wait_timeout",
    "docker_wait_interval",
    "enabled",
    "include_in_all",
    "aliases",
    "allow_dangerous_source",
    "allow_destination_inside_source",
]


@dataclass
class ArchiveSettings:
    log_dir: str = "/var/log/archive"
    lock_file: str = "/tmp/archive.py.lock"


@dataclass
class ArchiveBlock:
    key: str
    title: str = ""
    command_start: str = ""
    source: str = ""
    destination: str = ""
    archive: str = "0"
    archive_format: str = "tar.7z"
    archive_children: str = "1"
    archive_name: str = ""
    compression_level: str = "7"
    replace_existing: str = "1"
    date_suffix: str = "0"
    delete_extra: str = "0"
    excludes: str = ""
    command_end: str = ""
    docker_exclude: str = ""
    wait_start_docker_stopped: str = ""
    wait_end_docker_running: str = ""
    docker_wait_timeout: str = "0"
    docker_wait_interval: str = "2"
    enabled: str = "true"
    include_in_all: str = "true"
    aliases: str = ""
    allow_dangerous_source: str = "0"
    allow_destination_inside_source: str = "0"

    @property
    def is_archive(self) -> bool:
        return parse_bool(self.archive)

    @property
    def is_enabled(self) -> bool:
        return parse_bool(self.enabled)

    @property
    def is_all(self) -> bool:
        return parse_bool(self.include_in_all)

    @property
    def mode_label(self) -> str:
        if not self.source and not self.destination:
            return "commande"
        if self.is_archive:
            scope = "par dossier" if parse_bool(self.archive_children) else "source complète"
            return f"archive {self.archive_format} / {scope}"
        return "copie rsync"


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    raw = str(value).strip().lower()
    if raw in BOOL_TRUE:
        return True
    if raw in BOOL_FALSE:
        return False
    return default


def bool_text(value: str | None, default: str = "0") -> str:
    return "1" if parse_bool(value, parse_bool(default)) else "0"


def clean_text(value: str | None) -> str:
    return (value or "").replace("\r", " ").replace("\n", " ").strip()


def clean_multiline(value: str | None) -> str:
    return (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def read_kv_file(path: Path) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not path.exists():
        return data
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().upper()
            if key:
                data[key] = value.strip().strip('"').strip("'")
    return data


def write_kv_file(path: Path, data: Dict[str, str]) -> None:
    """Écrit la configuration Archive dans services.conf avec le préfixe ARCHIVE_."""
    values, multi = _svc_read_values()
    if not values and not multi:
        values = _svc_default_values()
        multi = _svc_default_multi()
    for key in archive_CONFIG_ORDER:
        values["ARCHIVE_" + key] = str(data.get(key, archive_DEFAULT_CONFIG.get(key, "")))
    _svc_write_all(values, multi)


def archive_normalize_module_config(conf: Dict[str, str]) -> Dict[str, str]:
    """Normalise les chemins techniques Archive cachés dans l'interface.

    Ces valeurs restent dans services.conf pour que le moteur sache où écrire
    et où ranger les copies de sécurité du archive.conf, mais elles ne sont plus
    modifiables depuis la page Info. Un ancien services.conf est donc remis aux
    valeurs officielles dès le premier accès au module Archive.
    """
    out = dict(conf or {})
    out["PATH_CONF"] = archive_DEFAULT_CONFIG["PATH_CONF"]
    out["BROWSE_ROOTS"] = archive_DEFAULT_CONFIG["BROWSE_ROOTS"]
    out["BACKUP_DIR"] = archive_DEFAULT_CONFIG["BACKUP_DIR"]
    return out


def get_config() -> Dict[str, str]:
    """Lit la configuration Archive depuis services.conf et force les defaults cachés."""
    _svc_ensure_file()
    values, _multi = _svc_read_values()
    conf = archive_DEFAULT_CONFIG.copy()
    for key in archive_CONFIG_ORDER:
        full = "ARCHIVE_" + key
        legacy = "BACKUP_" + key
        if full in values:
            conf[key] = values[full]
        elif legacy in values:
            conf[key] = values[legacy]
    conf = archive_normalize_module_config(conf)

    # Si services.conf existait déjà sans les clés Archive cachées, ou avec une
    # ancienne valeur, on persiste automatiquement les defaults au premier accès.
    values, _multi = _svc_read_values()
    needs_write = any(values.get("ARCHIVE_" + key) != str(conf.get(key, "")) for key in archive_CONFIG_ORDER)
    if needs_write:
        write_kv_file(CONFIG_FILE, conf)

    return conf

def resolve_module_path(value: str | None, *, default: Optional[Path] = None) -> Path:
    raw = clean_text(value)
    if not raw:
        if default is not None:
            return default
        raw = "."
    raw = os.path.expandvars(os.path.expanduser(raw))
    path = Path(raw)
    if path.is_absolute():
        return path
    return (MODULE_DIR / path).resolve()


def display_path(path: Path) -> str:
    try:
        rel = path.resolve().relative_to(MODULE_DIR)
        return str(Path(".") / rel)
    except Exception:
        try:
            rel = path.resolve().relative_to(BASE_DIR)
            return str(Path("..") / rel)
        except Exception:
            return str(path)


def module_real_conf_path(conf: Dict[str, str]) -> Path:
    return resolve_module_path(conf.get("PATH_CONF"), default=BASE_DIR / "conf" / "archive.conf")



def module_archive_dir(conf: Dict[str, str]) -> Path:
    return resolve_module_path(conf.get("BACKUP_DIR"), default=BASE_DIR / "backups" / "archive")


def normalize_path(value: str) -> str:
    value = clean_text(value)
    if not value:
        return ""
    return os.path.normpath(value.replace("\\", "/"))


def allowed_roots(conf: Dict[str, str]) -> List[str]:
    roots: List[str] = []
    for raw in conf.get("BROWSE_ROOTS", "/").split(","):
        raw = normalize_path(raw)
        if raw:
            roots.append(os.path.realpath(str(resolve_module_path(raw)) if not raw.startswith("/") else raw))
    return roots or ["/"]


def is_under_allowed_root(path: str, roots: List[str]) -> bool:
    if not path:
        return False
    real = os.path.realpath(path)
    for root in roots:
        root = os.path.realpath(root)
        if root == "/":
            return True
        if real == root or real.startswith(root.rstrip("/") + "/"):
            return True
    return False


def ensure_real_conf(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_REAL_ARCHIVE_CONF, encoding="utf-8")


def read_real_conf(path: Path) -> Tuple[ArchiveSettings, List[ArchiveBlock]]:
    ensure_real_conf(path)
    parser = configparser.ConfigParser(interpolation=None, allow_no_value=False)
    parser.optionxform = str.lower
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        parser.read_file(handle)

    settings_section = parser["settings"] if parser.has_section("settings") else {}
    settings = ArchiveSettings(
        log_dir=settings_section.get("log_dir", "/var/log/archive"),
        lock_file=settings_section.get("lock_file", "/tmp/archive.py.lock"),
    )

    blocks: List[ArchiveBlock] = []
    for section in parser.sections():
        if section.lower() == "settings" or section.startswith("group:"):
            continue
        sec = parser[section]
        block = ArchiveBlock(
            key=section,
            title=sec.get("title", section),
            command_start=sec.get("command_start", sec.get("before", sec.get("command_before", sec.get("stop", "")))),
            source=sec.get("source", ""),
            destination=sec.get("destination", ""),
            archive=bool_text(sec.get("archive", sec.get("tar", "0"))),
            archive_format=sec.get("archive_format", sec.get("format", "tar.7z")),
            archive_children=bool_text(sec.get("archive_children", "1"), "1"),
            archive_name=sec.get("archive_name", ""),
            compression_level=sec.get("compression_level", "7"),
            replace_existing=bool_text(sec.get("replace_existing", "1"), "1"),
            date_suffix=bool_text(sec.get("date_suffix", "0")),
            delete_extra=bool_text(sec.get("delete_extra", "0")),
            excludes=sec.get("excludes", ""),
            command_end=sec.get("command_end", sec.get("after", sec.get("command_after", sec.get("start", "")))),
            docker_exclude=sec.get("docker_exclude", ""),
            wait_start_docker_stopped=sec.get("wait_start_docker_stopped", ""),
            wait_end_docker_running=sec.get("wait_end_docker_running", ""),
            docker_wait_timeout=sec.get("docker_wait_timeout", "0"),
            docker_wait_interval=sec.get("docker_wait_interval", "2"),
            enabled="true" if parse_bool(sec.get("enabled", "true"), True) else "false",
            include_in_all="true" if parse_bool(sec.get("include_in_all", "true"), True) else "false",
            aliases=sec.get("aliases", ""),
            allow_dangerous_source=bool_text(sec.get("allow_dangerous_source", "0")),
            allow_destination_inside_source=bool_text(sec.get("allow_destination_inside_source", "0")),
        )
        blocks.append(block)
    return settings, blocks


def archive_real_conf(path: Path, backup_dir: Path) -> str:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"{path.name}.{stamp}.bak"
    if path.exists():
        shutil.copy2(path, dest)
    else:
        dest.write_text("", encoding="utf-8")
    return str(dest)


def write_real_conf(path: Path, settings: ArchiveSettings, blocks: List[ArchiveBlock]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = [
        "# ============================================================",
        "# archive.conf - Configuration du moteur archive",
        "# Généré/édité par le module Flask Backup.",
        "# Le moteur reste neutre : chemins et options vivent ici.",
        "# ============================================================",
        "",
        "[settings]",
        f"log_dir = {settings.log_dir}",
        f"lock_file = {settings.lock_file}",
        "",
    ]

    for block in blocks:
        lines.append("# ------------------------------------------------------------")
        lines.append(f"# {block.title or block.key}")
        lines.append("# ------------------------------------------------------------")
        lines.append(f"[{block.key}]")
        values = block.__dict__.copy()
        values.pop("key", None)
        for key in MAIN_ORDER:
            value = str(values.get(key, "")).strip()
            lines.append(f"{key} = {value}")
        lines.append("")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def validate_number(value: str, label: str, *, allow_float: bool = True) -> Optional[str]:
    value = clean_text(value)
    if value == "":
        return None
    try:
        if allow_float:
            float(value.replace(",", "."))
        else:
            int(value)
        return None
    except ValueError:
        return f"{label} doit être un nombre : {value}"


def validate_blocks(settings: ArchiveSettings, blocks: List[ArchiveBlock]) -> List[str]:
    errors: List[str] = []
    if not settings.log_dir:
        errors.append("log_dir est obligatoire dans [settings].")
    if not settings.lock_file:
        errors.append("lock_file est obligatoire dans [settings].")

    seen: set[str] = set()
    for block in blocks:
        if not block.key:
            errors.append("Un bloc a un nom vide.")
            continue
        if block.key.lower() == "settings" or block.key.startswith("group:"):
            errors.append(f"Nom de bloc réservé : {block.key}")
        if not SECTION_RE.fullmatch(block.key):
            errors.append(f"Nom de bloc invalide : {block.key}. Utilise lettres, chiffres, tiret, point ou underscore.")
        if block.key.lower() in seen:
            errors.append(f"Bloc en double : {block.key}")
        seen.add(block.key.lower())
        if not block.title:
            errors.append(f"Titre manquant pour [{block.key}].")
        if bool(block.source) != bool(block.destination):
            errors.append(f"[{block.key}] source et destination doivent être remplies ensemble, ou vides ensemble.")
        if block.archive_format.strip().lower() not in {"tar.gz", "tgz", "gz", "tar.7z", "7z"}:
            errors.append(f"[{block.key}] archive_format invalide : {block.archive_format}")
        err = validate_number(block.docker_wait_timeout, f"[{block.key}] docker_wait_timeout")
        if err:
            errors.append(err)
        err = validate_number(block.docker_wait_interval, f"[{block.key}] docker_wait_interval")
        if err:
            errors.append(err)
        if block.compression_level:
            err = validate_number(block.compression_level, f"[{block.key}] compression_level", allow_float=False)
            if err:
                errors.append(err)
    return errors


def collect_settings_from_form(existing: Optional[ArchiveSettings] = None) -> ArchiveSettings:
    """Collecte [settings] depuis le formulaire Backup.

    L'onglet Info affiche maintenant ces chemins en lecture seule. Quand les
    champs ne sont pas postés, on conserve les valeurs existantes du vrai
    archive.conf, ou les defaults propres du moteur.
    """
    base = existing or ArchiveSettings()
    return ArchiveSettings(
        log_dir=clean_text(request.form.get("setting_log_dir", base.log_dir)) or base.log_dir,
        lock_file=clean_text(request.form.get("setting_lock_file", base.lock_file)) or base.lock_file,
    )


def _form_list(name: str) -> List[str]:
    return request.form.getlist(name)


def collect_blocks_from_form() -> List[ArchiveBlock]:
    originals = _form_list("block_original[]")
    delete_set = set(_form_list("block_delete[]"))
    keys = _form_list("block_key[]")
    count = max(len(keys), len(originals))

    def get(field: str, i: int, default: str = "") -> str:
        values = _form_list(field)
        if i < len(values):
            return clean_multiline(values[i])
        return default

    blocks: List[ArchiveBlock] = []
    for i in range(count):
        original = originals[i] if i < len(originals) else ""
        if original and original in delete_set:
            continue
        key = get("block_key[]", i)
        title = get("block_title[]", i)
        if not key and not title:
            continue
        block = ArchiveBlock(
            key=key,
            title=title,
            command_start=get("block_command_start[]", i),
            source=normalize_path(get("block_source[]", i)),
            destination=normalize_path(get("block_destination[]", i)),
            archive=get("block_archive[]", i, "0"),
            archive_format=get("block_archive_format[]", i, "tar.7z").lower() or "tar.7z",
            archive_children=get("block_archive_children[]", i, "1"),
            archive_name=get("block_archive_name[]", i),
            compression_level=get("block_compression_level[]", i, "7"),
            replace_existing=get("block_replace_existing[]", i, "1"),
            date_suffix=get("block_date_suffix[]", i, "0"),
            delete_extra=get("block_delete_extra[]", i, "0"),
            excludes=get("block_excludes[]", i),
            command_end=get("block_command_end[]", i),
            docker_exclude=get("block_docker_exclude[]", i),
            wait_start_docker_stopped=get("block_wait_start_docker_stopped[]", i),
            wait_end_docker_running=get("block_wait_end_docker_running[]", i),
            docker_wait_timeout=get("block_docker_wait_timeout[]", i, "0"),
            docker_wait_interval=get("block_docker_wait_interval[]", i, "2"),
            enabled="true" if parse_bool(get("block_enabled[]", i, "true"), True) else "false",
            include_in_all="true" if parse_bool(get("block_include_in_all[]", i, "true"), True) else "false",
            aliases=get("block_aliases[]", i),
            allow_dangerous_source=get("block_allow_dangerous_source[]", i, "0"),
            allow_destination_inside_source=get("block_allow_destination_inside_source[]", i, "0"),
        )
        blocks.append(block)
    return blocks



# ============================================================
# Moteur intégré Flask
# ============================================================
# Le moteur ci-dessous est intégré directement dans Services.
# Il ne dépend pas d’un fichier backup.py séparé. Le contrat reste le même :
# un seul archive.conf partagé entre CLI et GUI.

