CONFIG_FILE = nas_conf_file("dockers.conf")
LEGACY_CONFIG_FILE = nas_conf_file("snacks.conf")
LEGACY_IMAGES_DOCKER_CONFIG_FILE = nas_conf_file("images_docker.conf")
LEGACY_DOCKER_RUN_CONFIG_FILE = nas_conf_file("docker_run.conf")

GENERAL_CONFIG_KEYS = (
    "YML_FOLDER",
    "BROWSE_ROOTS",
)

STACKS_ENV_CONFIG_KEYS = (
    "STACKS_FILE",
    "ENV_FILE",
    "BACKUP_DIR",
)

# Alias gardé pour éviter de casser une ancienne référence interne éventuelle.
BASIC_CONFIG_KEYS = GENERAL_CONFIG_KEYS + STACKS_ENV_CONFIG_KEYS

IMAGE_DOCKER_CONFIG_KEYS = (
    "DOCKER_ROOT_DIR",
)

DOCKER_RUN_CONFIG_KEYS = (
    "DOCKER_RUN_DIR",
    "SHELL_BIN",
    "WORKDIR",
)

DOCKER_LAN_CONFIG_KEYS = (
    "DOCKER_LAN_STATE_FILE",
)

SYSTEM_CONFIG_KEYS = (
    "SYSTEM_STACKS_CONF_FILE",
    "SYSTEM_STACKS_UPDATE_NO_PULL",
    "SYSTEM_UP_NO_RECREATE",
    "SYSTEM_UP_REMOVE_ORPHANS",
    "SYSTEM_STACKS_EXTRA_ARGS",
    "SYSTEM_NETWORK_NAME",
    "SYSTEM_NETWORK_SUBNET",
    "SYSTEM_NETWORK_GATEWAY",
    "SYSTEM_NETWORK_BRIDGE",
    "SYSTEM_LOG_FILE",
    "SYSTEM_LOCK_FILE",
    "DOCKER_REGISTRY_LOGIN_FILE",
    "DOCKER_MODE_FILE",
)

# Valeurs visibles/modifiables dans l'onglet Système.
# Les autres clés restent dans dockers.conf, mais elles sont internes.
# But : éviter que l'UI mette le module Docker dans un état incohérent.
DOCKERS_EDITABLE_CONFIG_KEYS = (
    # Champs réellement modifiables dans l'UI Options.
    # Le reste reste dans dockers.conf, mais n'est plus exposé dans l'écran.
    # BROWSE_ROOTS, ENV_FILE et WORKDIR restent dérivés automatiquement depuis
    # YML_FOLDER / DOCKER_RUN_DIR.
    "YML_FOLDER",
    "DOCKER_RUN_DIR",
)

# Clés qui représentent des chemins et doivent être résolues depuis NAS_CONF_DIR.
# Exemple : NAS_CONF_DIR=/dockers/conf + ../yml -> /dockers/yml.
DOCKERS_PATH_CONFIG_KEYS = {
    "YML_FOLDER",
    "STACKS_FILE",
    "ENV_FILE",
    "BACKUP_DIR",
    "DOCKER_RUN_DIR",
    "DOCKER_LAN_STATE_FILE",
    "SYSTEM_STACKS_CONF_FILE",
    "SYSTEM_LOG_FILE",
    "DOCKER_REGISTRY_LOGIN_FILE",
    "DOCKER_MODE_FILE",
}

DOCKERS_CSV_PATH_CONFIG_KEYS = {
    "BROWSE_ROOTS",
}

DEFAULT_CONFIG = {
    # Configuration du module Flask Docker.
    # Ces valeurs reprennent le dockers.conf fourni.
    "YML_FOLDER": "../yml",
    "BROWSE_ROOTS": "../yml",

    # Onglet Stacks / Env.
    "STACKS_FILE": "../conf/stacks.conf",
    "ENV_FILE": "../yml/.env",
    "BACKUP_DIR": "../backups",

    # Onglet Images Docker.
    "DOCKER_ROOT_DIR": "/var/lib/docker",

    # Onglet Docker Run.
    "DOCKER_RUN_DIR": "../docker_run",
    "SHELL_BIN": "/bin/bash",
    "WORKDIR": "../docker_run",

    # Onglet LAN Docker.
    "DOCKER_LAN_STATE_FILE": "../conf/docker_lan.json",

    # Onglet Système / Compose intégré dockers.py.
    "SYSTEM_STACKS_CONF_FILE": "../conf/stacks.conf",
    "SYSTEM_STACKS_UPDATE_NO_PULL": "0",
    "SYSTEM_UP_NO_RECREATE": "0",
    "SYSTEM_UP_REMOVE_ORPHANS": "1",
    "SYSTEM_STACKS_EXTRA_ARGS": "",
    "SYSTEM_NETWORK_NAME": "ollama_lan",
    "SYSTEM_NETWORK_SUBNET": "172.20.0.0/16",
    "SYSTEM_NETWORK_GATEWAY": "172.20.0.1",
    "SYSTEM_NETWORK_BRIDGE": "ollama_lan",
    "SYSTEM_LOG_FILE": "/var/log/dockers.log",
    # Les pulls/updates Compose ne doivent pas partager le verrou des builds.
    # Ils restent exclusifs entre eux, mais peuvent tourner pendant un build.
    "SYSTEM_LOCK_FILE": "/tmp/flask_stacks_system.lock",
    "DOCKER_REGISTRY_LOGIN_FILE": "../conf/registre_login.conf",
    "DOCKER_MODE_FILE": "../conf/mode.conf",
}


def dockers_conf_exists() -> bool:
    return os.path.exists(CONFIG_FILE)


def dockers_default_ui_conf() -> Dict[str, str]:
    """Valeurs affichées quand dockers.conf n'existe pas encore.

    On ne préconfigure volontairement pas les deux dossiers importants :
    l'utilisateur doit choisir YML_FOLDER et DOCKER_RUN_DIR en premier démarrage.
    Les valeurs système non dangereuses restent proposées en relatif.
    """
    conf = DEFAULT_CONFIG.copy()
    conf["YML_FOLDER"] = ""
    conf["BROWSE_ROOTS"] = ""
    conf["ENV_FILE"] = ""
    conf["DOCKER_RUN_DIR"] = ""
    conf["WORKDIR"] = ""
    conf["BACKUP_DIR"] = DEFAULT_CONFIG["BACKUP_DIR"]
    conf["SYSTEM_LOG_FILE"] = DEFAULT_CONFIG["SYSTEM_LOG_FILE"]
    conf["STACKS_FILE"] = DEFAULT_CONFIG["STACKS_FILE"]
    conf["SYSTEM_STACKS_CONF_FILE"] = DEFAULT_CONFIG["SYSTEM_STACKS_CONF_FILE"]
    return conf


def _dockers_strip_quotes(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def dockers_conf_resolve_path(value: str, base_dir: Optional[str] = None) -> str:
    raw = _dockers_strip_quotes(str(value or "")).strip()
    if not raw:
        return ""
    raw = os.path.expanduser(os.path.expandvars(raw))
    if os.path.isabs(raw):
        return os.path.abspath(raw)
    root = base_dir or NAS_CONF_DIR
    return os.path.abspath(os.path.join(root, raw))


def dockers_conf_resolve_csv_paths(value: str, base_dir: Optional[str] = None) -> str:
    parts: List[str] = []
    for raw in str(value or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        parts.append(dockers_conf_resolve_path(raw, base_dir))
    return ",".join(parts)


def dockers_default_conf_text(conf: Optional[Dict[str, str]] = None) -> str:
    conf = {**DEFAULT_CONFIG, **(conf or {})}
    lines = [
        "# Configuration du module Flask Docker",
        "# Ce fichier règle seulement dockers.py / dockers.html.",
        "# Le vrai fichier de stacks édité par l'interface reste STACKS_FILE.",
        "# YML_FOLDER est le chemin YAML unique pour Stacks, Images Docker, Docker Run, YML et Compose intégré.",
        "",
        "# Paramètres généraux",
    ]
    for key in GENERAL_CONFIG_KEYS:
        lines.append(f"{key}={conf.get(key, DEFAULT_CONFIG.get(key, ''))}")
    lines.extend(["", "# Onglet Stacks / Env"])
    for key in STACKS_ENV_CONFIG_KEYS:
        lines.append(f"{key}={conf.get(key, DEFAULT_CONFIG.get(key, ''))}")
    lines.extend(["", "# Onglet Images Docker"])
    for key in IMAGE_DOCKER_CONFIG_KEYS:
        lines.append(f"{key}={conf.get(key, DEFAULT_CONFIG.get(key, ''))}")
    lines.extend(["", "# Onglet Docker Run"])
    for key in DOCKER_RUN_CONFIG_KEYS:
        lines.append(f"{key}={conf.get(key, DEFAULT_CONFIG.get(key, ''))}")
    lines.extend(["", "# Onglet LAN Docker"])
    for key in DOCKER_LAN_CONFIG_KEYS:
        lines.append(f"{key}={conf.get(key, DEFAULT_CONFIG.get(key, ''))}")
    lines.extend(["", "# Onglet Système / Compose intégré dockers.py"])
    for key in SYSTEM_CONFIG_KEYS:
        lines.append(f"{key}={conf.get(key, DEFAULT_CONFIG.get(key, ''))}")
    return "\n".join(lines).rstrip() + "\n"


def ensure_dockers_conf_file(path: str) -> bool:
    if os.path.exists(path):
        return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(dockers_default_conf_text())
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass
    return True


def write_module_conf(path: str, conf: Dict[str, str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(dockers_default_conf_text(conf))


def _resolve_dockers_runtime_conf(conf: Dict[str, str]) -> Dict[str, str]:
    out = dict(conf)
    for key in list(out.keys()):
        if key in DOCKERS_PATH_CONFIG_KEYS and out.get(key):
            out[key] = dockers_conf_resolve_path(out[key])
        elif key in DOCKERS_CSV_PATH_CONFIG_KEYS and out.get(key):
            out[key] = dockers_conf_resolve_csv_paths(out[key])

    # Clé unique : un seul dossier YAML pour Stacks, Images Docker, Docker Run,
    # YML et le moteur intégré. Les alias restent disponibles pour compat.
    yml_folder = (
        out.get("YML_FOLDER")
        or out.get("YML_DIR")
        or out.get("PATH_DOCKER_YML")
        or ""
    ).rstrip("/")
    out["YML_FOLDER"] = yml_folder
    out["YML_DIR"] = yml_folder
    out["PATH_DOCKER_YML"] = yml_folder

    out["DOCKER_ROOT_DIR"] = (out.get("DOCKER_ROOT_DIR") or "/var/lib/docker").rstrip("/")
    out["DOCKER_RUN_DIR"] = (out.get("DOCKER_RUN_DIR") or "").rstrip("/")
    out["SHELL_BIN"] = (out.get("SHELL_BIN") or "/bin/bash").strip() or "/bin/bash"
    out["WORKDIR"] = (out.get("WORKDIR") or out.get("DOCKER_RUN_DIR") or "").strip()
    out["DOCKER_LAN_STATE_FILE"] = (out.get("DOCKER_LAN_STATE_FILE") or dockers_conf_resolve_path(DEFAULT_CONFIG["DOCKER_LAN_STATE_FILE"])).strip() or dockers_conf_resolve_path(DEFAULT_CONFIG["DOCKER_LAN_STATE_FILE"])
    out["STACKS_FILE"] = out.get("STACKS_FILE") or dockers_conf_resolve_path(DEFAULT_CONFIG["STACKS_FILE"])
    out["SYSTEM_STACKS_CONF_FILE"] = out.get("SYSTEM_STACKS_CONF_FILE") or out["STACKS_FILE"]
    out["ENV_FILE"] = out.get("ENV_FILE") or os.path.join(yml_folder, ".env")
    out["BACKUP_DIR"] = out.get("BACKUP_DIR") or dockers_conf_resolve_path(DEFAULT_CONFIG["BACKUP_DIR"])
    out["SYSTEM_LOG_FILE"] = out.get("SYSTEM_LOG_FILE") or dockers_conf_resolve_path(DEFAULT_CONFIG["SYSTEM_LOG_FILE"])
    out["SYSTEM_LOCK_FILE"] = out.get("SYSTEM_LOCK_FILE") or DEFAULT_CONFIG["SYSTEM_LOCK_FILE"]
    out["DOCKER_REGISTRY_LOGIN_FILE"] = out.get("DOCKER_REGISTRY_LOGIN_FILE") or dockers_conf_resolve_path(DEFAULT_CONFIG["DOCKER_REGISTRY_LOGIN_FILE"])
    out["DOCKER_MODE_FILE"] = out.get("DOCKER_MODE_FILE") or dockers_conf_resolve_path(DEFAULT_CONFIG["DOCKER_MODE_FILE"])

    try:
        os.makedirs(out["DOCKER_RUN_DIR"], exist_ok=True)
    except OSError:
        pass
    try:
        docker_lan_state_dir = os.path.dirname(out["DOCKER_LAN_STATE_FILE"])
        if docker_lan_state_dir:
            os.makedirs(docker_lan_state_dir, exist_ok=True)
    except OSError:
        pass
    return out


def get_config() -> Dict[str, str]:
    ensure_dockers_conf_file(CONFIG_FILE)

    raw_conf = DEFAULT_CONFIG.copy()

    # Migration douce : si l'ancien snacks.conf existe et que le nouveau fichier
    # dockers.conf n'existe pas encore, on reprend ses chemins.
    legacy_data = read_kv_file(LEGACY_CONFIG_FILE) if not os.path.exists(CONFIG_FILE) else {}
    module_data = read_kv_file(CONFIG_FILE)
    legacy_images_data = read_kv_file(LEGACY_IMAGES_DOCKER_CONFIG_FILE)
    legacy_docker_run_data = read_kv_file(LEGACY_DOCKER_RUN_CONFIG_FILE)
    explicit_keys = set(legacy_data) | set(module_data)

    raw_conf.update(legacy_data)
    raw_conf.update(module_data)

    # Migration douce de l'ancien conf/images_docker.conf vers conf/dockers.conf.
    # Si dockers.conf contient déjà ces clés, il reste prioritaire.
    for key in IMAGE_DOCKER_CONFIG_KEYS:
        if key not in explicit_keys and legacy_images_data.get(key):
            raw_conf[key] = legacy_images_data[key]

    # Migration douce de l'ancien conf/docker_run.conf vers conf/dockers.conf.
    # dockers.conf reste prioritaire dès que la clé y existe.
    for key in DOCKER_RUN_CONFIG_KEYS:
        if key not in explicit_keys and legacy_docker_run_data.get(key):
            raw_conf[key] = legacy_docker_run_data[key]

    return _resolve_dockers_runtime_conf(raw_conf)


@dataclass
class StackBlock:
    index: int
    name: str
    ymls: List[str]


@dataclass
class EnvRow:
    row_type: str
    key: str = ""
    value: str = ""
    raw: str = ""


def strip_quotes(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def read_kv_file(path: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not path or not os.path.exists(path):
        return data
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key:
                data[key] = strip_quotes(value)
    return data


def _dockers_join_file(parent: str, name: str) -> str:
    parent = str(parent or "").strip().rstrip("/") or "/"
    return os.path.join(parent, name)


def _dockers_apply_ui_defaults(conf: Dict[str, str]) -> Dict[str, str]:
    """Ajoute aux templates les valeurs par défaut portées par Python.

    Les HTML ne doivent pas inventer de chemins avec des `or '../...'`.
    Ils affichent seulement ces clés fournies par dockers.py.
    """
    conf["DEFAULT_BACKUP_DIR"] = DEFAULT_CONFIG["BACKUP_DIR"]
    conf["DEFAULT_SYSTEM_LOG_FILE"] = DEFAULT_CONFIG["SYSTEM_LOG_FILE"]
    conf["DEFAULT_STACKS_FILE"] = DEFAULT_CONFIG["STACKS_FILE"]
    conf["DEFAULT_SYSTEM_STACKS_CONF_FILE"] = DEFAULT_CONFIG["SYSTEM_STACKS_CONF_FILE"]
    return conf


def _dockers_derive_hidden_config_values(conf: Dict[str, str]) -> Dict[str, str]:
    """Garde les clés internes cohérentes sans les exposer dans l'UI.

    Décision produit : l'utilisateur choisit seulement le dossier YML et le
    dossier Docker Run. Le reste suit :
      - ENV_FILE = <YML_FOLDER>/.env
      - BROWSE_ROOTS = <YML_FOLDER> pour que les browse YAML repartent au bon endroit
      - WORKDIR = <DOCKER_RUN_DIR> pour les commandes Docker Run classiques
    """
    out = dict(conf)
    yml_folder = str(out.get("YML_FOLDER", "")).strip()
    docker_run_dir = str(out.get("DOCKER_RUN_DIR", "")).strip()

    out["YML_FOLDER"] = yml_folder.rstrip("/") if yml_folder else ""
    out["ENV_FILE"] = _dockers_join_file(out["YML_FOLDER"], ".env") if out["YML_FOLDER"] else ""
    out["BROWSE_ROOTS"] = out["YML_FOLDER"]

    out["DOCKER_RUN_DIR"] = docker_run_dir.rstrip("/") if docker_run_dir else ""
    out["WORKDIR"] = out["DOCKER_RUN_DIR"]

    # Valeurs internes non modifiables depuis l'UI.
    out.setdefault("STACKS_FILE", DEFAULT_CONFIG["STACKS_FILE"])
    out.setdefault("DOCKER_ROOT_DIR", DEFAULT_CONFIG["DOCKER_ROOT_DIR"])
    out.setdefault("SHELL_BIN", DEFAULT_CONFIG["SHELL_BIN"])
    out.setdefault("DOCKER_LAN_STATE_FILE", DEFAULT_CONFIG["DOCKER_LAN_STATE_FILE"])
    out.setdefault("SYSTEM_STACKS_CONF_FILE", out.get("STACKS_FILE", DEFAULT_CONFIG["STACKS_FILE"]))
    out.setdefault("SYSTEM_LOCK_FILE", DEFAULT_CONFIG["SYSTEM_LOCK_FILE"])
    out.setdefault("DOCKER_REGISTRY_LOGIN_FILE", DEFAULT_CONFIG["DOCKER_REGISTRY_LOGIN_FILE"])
    out.setdefault("DOCKER_MODE_FILE", DEFAULT_CONFIG["DOCKER_MODE_FILE"])
    return out


def _dockers_migrate_hidden_legacy_values(conf: Dict[str, str]) -> Dict[str, str]:
    """Nettoie les anciennes valeurs cachées sans écraser les choix manuels.

    La page Options n'expose plus BACKUP_DIR, SYSTEM_LOG_FILE ni le réseau
    système. On garde ces clés dans dockers.conf, mais on corrige l'ancien
    chemin historique /yoleo/backups vers le chemin relatif standard.
    """
    out = dict(conf)
    backup_dir = str(out.get("BACKUP_DIR", "") or "").strip().rstrip("/")
    if backup_dir == "/yoleo/backups":
        out["BACKUP_DIR"] = DEFAULT_CONFIG["BACKUP_DIR"]
    return out


def write_module_conf(path: str, conf: Dict[str, str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conf = _dockers_derive_hidden_config_values({**DEFAULT_CONFIG, **conf})
    lines = [
        "# Configuration du module Flask Docker",
        "# Ce fichier règle seulement dockers.py / dockers.html.",
        "# Le vrai fichier de stacks édité par l'interface reste STACKS_FILE.",
        "# YML_FOLDER est le chemin YAML unique pour Stacks, Images Docker, Docker Run, YML et Compose intégré.",
        "# ENV_FILE, BROWSE_ROOTS et WORKDIR sont dérivés automatiquement depuis les dossiers choisis dans l'UI.",
        "",
        "# Paramètres généraux",
    ]
    for key in GENERAL_CONFIG_KEYS:
        lines.append(f"{key}={conf.get(key, DEFAULT_CONFIG.get(key, ''))}")
    lines.extend(["", "# Onglet Stacks / Env"])
    for key in STACKS_ENV_CONFIG_KEYS:
        lines.append(f"{key}={conf.get(key, DEFAULT_CONFIG.get(key, ''))}")
    lines.extend(["", "# Onglet Images Docker"])
    for key in IMAGE_DOCKER_CONFIG_KEYS:
        lines.append(f"{key}={conf.get(key, DEFAULT_CONFIG.get(key, ''))}")
    lines.extend(["", "# Onglet Docker Run"])
    for key in DOCKER_RUN_CONFIG_KEYS:
        lines.append(f"{key}={conf.get(key, DEFAULT_CONFIG.get(key, ''))}")
    lines.extend(["", "# Onglet LAN Docker"])
    for key in DOCKER_LAN_CONFIG_KEYS:
        lines.append(f"{key}={conf.get(key, DEFAULT_CONFIG.get(key, ''))}")
    lines.extend(["", "# Onglet Système / Compose intégré dockers.py"])
    for key in SYSTEM_CONFIG_KEYS:
        lines.append(f"{key}={conf.get(key, DEFAULT_CONFIG.get(key, ''))}")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines).rstrip() + "\n")


def dockers_setup_status(conf: Dict[str, str], *, config_exists: Optional[bool] = None) -> Dict[str, object]:
    reasons: List[str] = []
    exists = dockers_conf_exists() if config_exists is None else bool(config_exists)
    yml_folder = str(conf.get("YML_FOLDER", "") or "").strip()
    docker_run_dir = str(conf.get("DOCKER_RUN_DIR", "") or "").strip()

    if not exists:
        reasons.append("dockers.conf n'existe pas encore.")
    if not yml_folder:
        reasons.append("Choisis le dossier où seront stockés les fichiers YML et le .env.")
    if not docker_run_dir:
        reasons.append("Choisis le dossier où seront stockés les scripts Docker Run classiques.")

    return {
        "required": bool(reasons),
        "reasons": reasons,
        "config_exists": exists,
        "yml_folder": yml_folder,
        "docker_run_dir": docker_run_dir,
    }


def _dockers_make_runtime_dirs(conf: Dict[str, str]) -> None:
    for key in ("YML_FOLDER", "DOCKER_RUN_DIR", "BACKUP_DIR"):
        path = str(conf.get(key, "") or "").strip()
        if not path:
            continue
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass

    env_file = str(conf.get("ENV_FILE", "") or "").strip()
    if env_file:
        try:
            os.makedirs(os.path.dirname(env_file) or ".", exist_ok=True)
            if not os.path.exists(env_file):
                rows = _default_env_rows(env_file)
                text, _errors = serialize_env_rows(rows)
                with open(env_file, "w", encoding="utf-8") as handle:
                    handle.write(text)
        except Exception:
            pass

    try:
        docker_lan_state_dir = os.path.dirname(conf.get("DOCKER_LAN_STATE_FILE", ""))
        if docker_lan_state_dir:
            os.makedirs(docker_lan_state_dir, exist_ok=True)
    except OSError:
        pass


def _dockers_migrate_system_lock_file(module_data: Dict[str, str]) -> Dict[str, str]:
    """Migre automatiquement l'ancien verrou Docker partagé.

    Les anciennes installations ont `LOCK_FILE` dans `dockers.conf`, avec le
    même chemin que le module Build. Au premier chargement, on le remplace
    atomiquement par le verrou Compose dédié. Cette migration préserve le reste
    du fichier et est sans effet dès que `SYSTEM_LOCK_FILE` est déjà présent.
    """
    if "SYSTEM_LOCK_FILE" in module_data or not os.path.isfile(CONFIG_FILE):
        return module_data

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8", errors="replace") as handle:
            source_lines = handle.readlines()

        lock_line = f"SYSTEM_LOCK_FILE={DEFAULT_CONFIG['SYSTEM_LOCK_FILE']}\n"
        migrated_lines: List[str] = []
        inserted = False
        for line in source_lines:
            key = line.strip().split("=", 1)[0].strip() if "=" in line else ""
            if key == "LOCK_FILE":
                # Ancienne clé propre au module Docker : elle partageait par
                # erreur le verrou des builds. On la remplace, sans la garder.
                if not inserted:
                    migrated_lines.append(lock_line)
                    inserted = True
                continue
            migrated_lines.append(line)
            if key == "SYSTEM_LOG_FILE" and not inserted:
                migrated_lines.append(lock_line)
                inserted = True

        if not inserted:
            if migrated_lines and not migrated_lines[-1].endswith("\n"):
                migrated_lines[-1] += "\n"
            migrated_lines.append(lock_line)

        tmp_path = f"{CONFIG_FILE}.{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.writelines(migrated_lines)
        os.replace(tmp_path, CONFIG_FILE)
        try:
            os.chmod(CONFIG_FILE, 0o644)
        except OSError:
            pass
        return read_kv_file(CONFIG_FILE)
    except OSError:
        # Le verrou dédié reste disponible via DEFAULT_CONFIG, même si le
        # fichier n'est pas inscriptible (installation en lecture seule).
        return module_data


def get_config() -> Dict[str, str]:
    config_exists = dockers_conf_exists()

    if not config_exists:
        raw_conf = dockers_default_ui_conf()
        conf = dict(raw_conf)
        conf["DOCKERS_CONFIG_EXISTS"] = "0"
        conf["DOCKERS_SETUP_REQUIRED"] = "1"
        conf["YML_DIR"] = conf.get("YML_FOLDER", "")
        conf["PATH_DOCKER_YML"] = conf.get("YML_FOLDER", "")
        conf["SYSTEM_STACKS_CONF_FILE"] = conf.get("STACKS_FILE", DEFAULT_CONFIG["STACKS_FILE"])
        conf["UI_YML_FOLDER"] = conf.get("YML_FOLDER", "")
        conf["UI_DOCKER_RUN_DIR"] = conf.get("DOCKER_RUN_DIR", "")
        conf["UI_BACKUP_DIR"] = conf.get("BACKUP_DIR", DEFAULT_CONFIG["BACKUP_DIR"])
        conf["UI_SYSTEM_LOG_FILE"] = conf.get("SYSTEM_LOG_FILE", DEFAULT_CONFIG["SYSTEM_LOG_FILE"])
        return _dockers_apply_ui_defaults(conf)

    raw_conf = DEFAULT_CONFIG.copy()

    legacy_data = {}
    module_data = _dockers_migrate_system_lock_file(read_kv_file(CONFIG_FILE))
    legacy_images_data = read_kv_file(LEGACY_IMAGES_DOCKER_CONFIG_FILE)
    legacy_docker_run_data = read_kv_file(LEGACY_DOCKER_RUN_CONFIG_FILE)
    explicit_keys = set(module_data)

    raw_conf.update(module_data)

    for key in IMAGE_DOCKER_CONFIG_KEYS:
        if key not in explicit_keys and legacy_images_data.get(key):
            raw_conf[key] = legacy_images_data[key]
    for key in DOCKER_RUN_CONFIG_KEYS:
        if key not in explicit_keys and legacy_docker_run_data.get(key):
            raw_conf[key] = legacy_docker_run_data[key]

    raw_conf = _dockers_derive_hidden_config_values(raw_conf)
    editable_raw_conf = dict(raw_conf)
    conf = _resolve_dockers_runtime_conf(raw_conf)

    # Après résolution, on force aussi les chemins dérivés en absolu.
    conf["ENV_FILE"] = os.path.join(conf["YML_FOLDER"], ".env") if conf.get("YML_FOLDER") else ""
    conf["BROWSE_ROOTS"] = conf.get("YML_FOLDER", "")
    conf["WORKDIR"] = conf.get("DOCKER_RUN_DIR", "")
    conf["YML_DIR"] = conf.get("YML_FOLDER", "")
    conf["PATH_DOCKER_YML"] = conf.get("YML_FOLDER", "")
    conf["SYSTEM_STACKS_CONF_FILE"] = conf.get("SYSTEM_STACKS_CONF_FILE") or conf.get("STACKS_FILE")
    # Valeurs brutes pour l'écran Options : on affiche les chemins relatifs
    # du fichier conf au lieu de réinjecter partout les chemins absolus runtime.
    conf["UI_YML_FOLDER"] = editable_raw_conf.get("YML_FOLDER", "")
    conf["UI_DOCKER_RUN_DIR"] = editable_raw_conf.get("DOCKER_RUN_DIR", "")
    conf["UI_BACKUP_DIR"] = editable_raw_conf.get("BACKUP_DIR", DEFAULT_CONFIG["BACKUP_DIR"])
    conf["UI_SYSTEM_LOG_FILE"] = editable_raw_conf.get("SYSTEM_LOG_FILE", DEFAULT_CONFIG["SYSTEM_LOG_FILE"])
    conf["DOCKERS_CONFIG_EXISTS"] = "1"
    _dockers_apply_ui_defaults(conf)

    setup = dockers_setup_status(conf, config_exists=True)
    conf["DOCKERS_SETUP_REQUIRED"] = "1" if setup.get("required") else "0"

    if not setup.get("required"):
        _dockers_make_runtime_dirs(conf)
    return conf


def conf_bool(conf: Dict[str, str], key: str, default: str = "0") -> bool:
    return str(conf.get(key, default)).strip().lower() in {"1", "true", "yes", "on"}

def normalize_path(value: str) -> str:
    value = (value or "").strip().replace("\\", "/")
    if not value:
        return ""
    return os.path.normpath(value)


def allowed_roots(conf: Dict[str, str]) -> List[str]:
    roots: List[str] = []
    yml_folder = normalize_path(conf.get("YML_FOLDER", ""))
    if yml_folder:
        roots.append(os.path.realpath(yml_folder))
    for raw in conf.get("BROWSE_ROOTS", "").split(","):
        raw = normalize_path(raw)
        if raw:
            roots.append(os.path.realpath(raw))
    return list(dict.fromkeys(roots)) or ["/dockers/yml"]


def is_under_allowed(path: str, roots: List[str]) -> bool:
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


def backup_file(path: str, backup_dir: str) -> str:
    os.makedirs(backup_dir, exist_ok=True)
    base = os.path.basename(path.rstrip("/")) or "file.conf"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(backup_dir, f"{base}.{stamp}.bak")
    if os.path.exists(path):
        shutil.copy2(path, dest)
    else:
        with open(dest, "w", encoding="utf-8") as handle:
            handle.write("")
    return dest


def read_text(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def parse_stacks_conf(path: str) -> List[StackBlock]:
    text = read_text(path)
    stacks: List[StackBlock] = []
    current: Optional[StackBlock] = None
    auto_index = 1
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = strip_quotes(value)
        if re.fullmatch(r"STACK\d+", key, flags=re.IGNORECASE) or key.upper() == "STACK":
            if current is not None:
                stacks.append(current)
            current = StackBlock(index=auto_index, name=value.strip(), ymls=[])
            auto_index += 1
            continue
        if re.fullmatch(r"YML\d+", key, flags=re.IGNORECASE) or re.fullmatch(r"YAML\d+", key, flags=re.IGNORECASE):
            if current is None:
                current = StackBlock(index=auto_index, name="base", ymls=[])
                auto_index += 1
            if value.strip():
                current.ymls.append(value.strip())
    if current is not None:
        stacks.append(current)
    stacks = normalize_stack_blocks(stacks)
    if not stacks:
        stacks.append(StackBlock(index=1, name="base", ymls=[]))
    return stacks


def normalize_stack_blocks(stacks: List[StackBlock]) -> List[StackBlock]:
    """Nettoie les vieux imports stacks.conf sans rendre tout le module bloquant.

    Certains imports historiques ont laisse des lignes YAML vides, doublees ou
    avec des caracteres de controle. On garde les stacks lisibles et on retire
    uniquement les entrees qui ne peuvent pas etre sauvees proprement.
    """
    cleaned: List[StackBlock] = []
    for stack in stacks or []:
        name = re.sub(r"[\r\n\0]+", " ", strip_quotes(stack.name or "")).strip()
        ymls: List[str] = []
        seen: set[str] = set()
        for raw_yml in stack.ymls or []:
            yml = re.sub(r"[\r\n\0]+", " ", strip_quotes(str(raw_yml or ""))).strip()
            if not yml or yml in seen:
                continue
            seen.add(yml)
            ymls.append(yml)
        if name or ymls:
            cleaned.append(StackBlock(index=len(cleaned) + 1, name=name or f"stack_{len(cleaned) + 1}", ymls=ymls))
    return cleaned


def serialize_stacks(stacks: List[StackBlock]) -> str:
    lines: List[str] = [
        "# stacks.conf",
        "# Format simple lu par le gestionnaire de stacks.",
        "# Exemple :",
        "#   STACK1=base",
        "#   YML1=system.yml",
        "#   YML2=samba.yml",
        "",
    ]
    stack_no = 1
    for stack in stacks:
        name = (stack.name or "").strip()
        if not name:
            continue
        lines.append(f"STACK{stack_no}={name}")
        yml_no = 1
        for yml in stack.ymls:
            yml = (yml or "").strip()
            if not yml:
                continue
            lines.append(f"YML{yml_no}={yml}")
            yml_no += 1
        lines.append("")
        stack_no += 1
    return "\n".join(lines).rstrip() + "\n"


def _normalize_registry_env_value(value: str) -> str:
    """Normalise une valeur de registre Docker pour les images Compose.

    Compose attend un registre sous forme ``host:port/image``. Les valeurs
    ``http://host:port`` sont donc transformées en ``host:port``. On corrige
    aussi l'erreur fréquente ``192.168.1.140.7777`` en ``192.168.1.140:7777``.
    """
    value = strip_quotes(value or "").strip()
    if not value:
        return ""

    # On accepte REGISTRY_URL=http://host:port, https://host:port ou host:port.
    value = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", value)
    value = value.split("/", 1)[0].strip()

    # Correction sûre : uniquement IPv4 + dernier séparateur en point + port.
    # Exemple cassé observé : 192.168.1.140.7777 -> 192.168.1.140:7777
    match = re.fullmatch(r"((?:\d{1,3}\.){3}\d{1,3})\.(\d{2,5})", value)
    if match:
        ip, port = match.groups()
        try:
            ipaddress.ip_address(ip)
            port_i = int(port)
            if 1 <= port_i <= 65535:
                return f"{ip}:{port_i}"
        except Exception:
            pass

    return value


def _default_env_tz() -> str:
    env_tz = os.environ.get("TZ", "").strip()
    if env_tz:
        return env_tz
    try:
        tz = Path("/etc/timezone").read_text(encoding="utf-8", errors="replace").strip()
        if tz:
            return tz
    except Exception:
        pass
    return "Europe/Paris"


def _env_default_conf_candidates(env_path: str) -> List[Path]:
    """Fichiers possibles pour retrouver le registre quand .env n'existe plus."""
    candidates: List[Path] = []

    def add(path: Any) -> None:
        try:
            p = Path(str(path)).expanduser()
            if not p.is_absolute():
                p = Path.cwd() / p
            p = p.resolve()
            if p not in candidates:
                candidates.append(p)
        except Exception:
            pass

    # Cas normal actuel : /dockers/yml/.env => /dockers/conf et /dockers/system/conf.
    try:
        env_file = Path(env_path).expanduser()
        if env_file:
            if not env_file.is_absolute():
                env_file = (Path.cwd() / env_file).resolve()
            roots = [env_file.parent, *env_file.parents]
            for root in roots[:6]:
                add(root / "conf" / "builds.conf")
                add(root / "system" / "conf" / "builds.conf")
                add(root / "conf" / "registry.conf")
                add(root / "system" / "conf" / "registry.conf")
    except Exception:
        pass

    # Cas gunicorn lancé depuis /dockers/system/dockers.py.
    try:
        module_dir = Path(__file__).resolve().parent
        add(module_dir / "conf" / "builds.conf")
        add(module_dir.parent / "conf" / "builds.conf")
        add(module_dir / "conf" / "registry.conf")
        add(module_dir.parent / "conf" / "registry.conf")
    except Exception:
        pass

    # Fallbacks relatifs classiques.
    for raw in (
        nas_conf_file("builds.conf"),
        nas_conf_file("registry.conf"),
    ):
        add(raw)

    return candidates


def _read_registry_from_build_or_registry_conf(env_path: str) -> str:
    for conf_path in _env_default_conf_candidates(env_path):
        if not conf_path.exists():
            continue

        data = read_kv_file(str(conf_path))
        if not data:
            continue

        lower = {str(k).strip().lower(): str(v).strip() for k, v in data.items()}

        # Priorité : builds.conf / registry.conf avec REGISTRY_URL=http://IP:PORT
        for key in ("registry_url", "registry"):
            value = _normalize_registry_env_value(lower.get(key, ""))
            if value:
                return value

        # Compatibilité : REGISTRY_HOST + REGISTRY_PORT.
        host = strip_quotes(lower.get("registry_host", "")).strip()
        port = strip_quotes(lower.get("registry_port", "")).strip()
        if host and port:
            host = _normalize_registry_env_value(host)
            return f"{host}:{port}"

    return ""


def _default_env_rows(path: str) -> List[EnvRow]:
    registry = _read_registry_from_build_or_registry_conf(path)
    if not registry:
        registry = f"{get_host_lan_ip()}:7777"

    return [
        EnvRow(row_type="raw", raw="# .env"),
        EnvRow(row_type="raw", raw="# Généré automatiquement depuis builds.conf/registry.conf si possible"),
        EnvRow(row_type="kv", key="REGISTRY", value=registry),
        EnvRow(row_type="kv", key="TZ", value=_default_env_tz()),
    ]


def parse_env_file(path: str) -> List[EnvRow]:
    text = read_text(path)
    rows: List[EnvRow] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw:
            rows.append(EnvRow(row_type="raw", raw=raw))
            continue
        key, value = raw.split("=", 1)
        rows.append(EnvRow(row_type="kv", key=key.strip(), value=strip_quotes(value)))
    if not rows:
        rows = _default_env_rows(path)
    return rows


def serialize_env_rows(rows: List[EnvRow]) -> Tuple[str, List[str]]:
    errors: List[str] = []
    seen: set[str] = set()
    lines: List[str] = []
    key_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    for row in rows:
        if row.row_type == "raw":
            lines.append(row.raw)
            continue
        key = (row.key or "").strip()
        value = (row.value or "").strip()
        if not key and not value:
            continue
        if not key:
            errors.append("Une ligne .env a une clé vide.")
            continue
        if not key_re.fullmatch(key):
            errors.append(f"Clé .env invalide : {key}")
            continue
        if key in seen:
            errors.append(f"Clé .env en double : {key}")
            continue
        if key.upper() in {"REGISTRY", "REGISTRY_HOST", "REGISTRY_URL", "DOCKER_REGISTRY", "DOCKER_REGISTRY_HOST"}:
            normalized = _normalize_registry_env_value(value)
            if value and normalized != value:
                value = normalized
        seen.add(key)
        lines.append(f"{key}={value}")
    return "\n".join(lines).rstrip() + "\n", errors


def collect_stacks_from_form() -> List[StackBlock]:
    names = request.form.getlist("stack_name[]")
    yml_stack = request.form.getlist("yml_stack[]")
    yml_files = request.form.getlist("yml_file[]")
    stacks: List[StackBlock] = []
    for i, name in enumerate(names):
        stacks.append(StackBlock(index=i + 1, name=name.strip(), ymls=[]))
    for idx, yml in zip(yml_stack, yml_files):
        try:
            pos = int(idx)
        except ValueError:
            continue
        if 0 <= pos < len(stacks):
            clean = yml.strip()
            if clean:
                stacks[pos].ymls.append(clean)
    return [s for s in stacks if s.name or s.ymls]


def collect_env_rows_from_form() -> List[EnvRow]:
    row_types = request.form.getlist("env_row_type[]")
    raws = request.form.getlist("env_raw[]")
    keys = request.form.getlist("env_key[]")
    values = request.form.getlist("env_value[]")
    rows: List[EnvRow] = []
    max_len = max(len(row_types), len(raws), len(keys), len(values), 0)
    for i in range(max_len):
        row_type = row_types[i] if i < len(row_types) else "kv"
        if row_type == "raw":
            raw = raws[i] if i < len(raws) else ""
            rows.append(EnvRow(row_type="raw", raw=raw))
        else:
            key = keys[i] if i < len(keys) else ""
            value = values[i] if i < len(values) else ""
            rows.append(EnvRow(row_type="kv", key=key.strip(), value=value.strip()))
    return rows


def stacks_summary(stacks: List[StackBlock]) -> Dict[str, Any]:
    return {"stacks": len([s for s in stacks if s.name]), "ymls": sum(len(s.ymls) for s in stacks)}


def env_summary(rows: List[EnvRow]) -> Dict[str, Any]:
    return {"keys": sum(1 for r in rows if r.row_type == "kv" and r.key), "raw": sum(1 for r in rows if r.row_type == "raw")}


def q(value: str) -> str:
    return shlex.quote(str(value))


def shjoin(cmd: List[str]) -> str:
    return " ".join(q(part) for part in cmd)


