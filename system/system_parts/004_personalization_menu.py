import copy
import threading

PERSONALIZATION_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

# La personnalisation est maintenant stockée dans system.conf.
# L'ancien fichier d'accueil ne doit plus être lu ni recréé par cet onglet.
PERSONALIZATION_DEFAULT_CONFIG = {
    'base_dir': '../tabs',
    'titre_tab': 'System Manager',
    'titre_logo': 'System Manager',
    'nav_icons': '/static/logo.png',
}

PERSONALIZATION_CONFIG_CANONICAL_KEYS = {
    'titre_tab': 'titre_tab',
    'titre_logo': 'titre_logo',
    'nav_icons': 'nav_icons',
}

PERSONALIZATION_EDITABLE_CONFIG_KEYS = {'titre_tab', 'titre_logo', 'nav_icons'}

# Le nom réseau est volontairement séparé des libellés de l'interface
# (titre_logo / titre_tab). Il s'agit du vrai hostname Linux, utilisé par
# mDNS, Samba et les autres services qui publient le nom de la machine.
PERSONALIZATION_HOSTNAME_RE = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')


def personalization_get_hostname() -> str:
    """Retourne le hostname courant, sans dépendre du contenu de system.conf."""
    try:
        result = subprocess.run(
            ['hostname'],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        hostname = (result.stdout or '').strip()
        if result.returncode == 0 and hostname:
            return hostname
    except (OSError, subprocess.TimeoutExpired):
        pass
    return (platform.node() or 'nas').strip()


def personalization_validate_hostname(value: str) -> str:
    """Valide un nom d'hôte court, compatible DNS/mDNS et Linux."""
    hostname = str(value or '').strip().lower()
    if not PERSONALIZATION_HOSTNAME_RE.fullmatch(hostname):
        raise ValueError(
            'Hostname invalide : utilise 1 à 63 caractères minuscules, chiffres ou tirets, '
            'sans tiret au début ni à la fin.'
        )
    return hostname


def personalization_set_hostname(value: str) -> str:
    """Applique durablement le hostname Linux, avec un repli pour les systèmes sans systemd."""
    hostname = personalization_validate_hostname(value)
    errors = []

    hostnamectl = shutil.which('hostnamectl')
    if hostnamectl:
        try:
            result = subprocess.run(
                [hostnamectl, 'set-hostname', hostname],
                capture_output=True,
                text=True,
                timeout=12,
                check=False,
            )
            if result.returncode == 0:
                return hostname
            details = (result.stderr or result.stdout or '').strip()
            errors.append(details or f'hostnamectl a retourné le code {result.returncode}')
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(str(exc))

    # Repli utile sur une installation Linux sans systemd : /etc/hostname rend
    # la modification persistante après redémarrage, puis `hostname` l'applique
    # immédiatement à la machine en cours.
    try:
        with open('/etc/hostname', 'w', encoding='utf-8') as handle:
            handle.write(f'{hostname}\n')
        result = subprocess.run(
            ['hostname', hostname],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            return hostname
        details = (result.stderr or result.stdout or '').strip()
        errors.append(details or f'hostname a retourné le code {result.returncode}')
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(str(exc))

    detail = ' ; '.join(part for part in errors if part)
    raise RuntimeError(f'Impossible de modifier le hostname.{" " + detail if detail else ""}')

# Extensions acceptées par le navigateur d'icônes /static.
# Sans cette constante, la route /system/personnalisation/static-browse renvoie
# une erreur NameError au clic sur « Parcourir ».
PERSONALIZATION_STATIC_IMAGE_EXTENSIONS = {
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.ico', '.bmp', '.avif'
}


def personalization_normalize_config_key(key: str) -> str:
    return key.strip().lower()


def personalization_read_key_value_file(path: str) -> dict:
    data = {}
    if not path or not os.path.exists(path):
        return data

    with open(path, 'r', encoding='utf-8') as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            data[personalization_normalize_config_key(key)] = value.strip()
    return data


def personalization_get_config_path() -> str:
    """Retourne le system.conf officiel, pas /system/conf/system.conf."""
    if loaded_config and not _is_bad_system_subconf(loaded_config):
        return os.path.abspath(loaded_config)

    for candidate in _build_config_candidates():
        if candidate and os.path.exists(candidate) and not _is_bad_system_subconf(candidate):
            return os.path.abspath(candidate)

    roots = _project_root_candidates()
    root = roots[0] if roots else os.path.dirname(PERSONALIZATION_MODULE_DIR)
    return os.path.abspath(os.path.join(root, 'conf', 'system.conf'))


def personalization_load_module_config() -> dict:
    config_path = personalization_get_config_path()
    file_config = personalization_read_key_value_file(config_path)
    merged = PERSONALIZATION_DEFAULT_CONFIG.copy()

    # Compatibilité : on accepte les clés écrites en majuscules ou minuscules,
    # mais la source officielle reste uniquement system.conf.
    for key in PERSONALIZATION_DEFAULT_CONFIG:
        value = file_config.get(key)
        if value is not None and str(value).strip():
            merged[key] = str(value).strip()

    merged['_config_path'] = config_path
    merged['_config_dir'] = os.path.dirname(os.path.abspath(config_path))
    return merged


def personalization_update_system_conf_values(config_path: str, updates: dict) -> None:
    """Met à jour quelques clés plates dans system.conf sans détruire le reste du fichier."""
    os.makedirs(os.path.dirname(config_path), exist_ok=True)

    normalized_updates = {
        personalization_normalize_config_key(key): str(value).strip()
        for key, value in updates.items()
        if personalization_normalize_config_key(key) in PERSONALIZATION_CONFIG_CANONICAL_KEYS
    }

    lines = []
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as handle:
            lines = handle.readlines()

    final_lines = []
    found = set()

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith('#') or stripped.startswith(';') or '=' not in stripped:
            final_lines.append(raw_line)
            continue

        key, _value = stripped.split('=', 1)
        normalized_key = personalization_normalize_config_key(key)

        if normalized_key in normalized_updates:
            canonical_key = PERSONALIZATION_CONFIG_CANONICAL_KEYS[normalized_key]
            final_lines.append(f"{canonical_key}={normalized_updates[normalized_key]}\n")
            found.add(normalized_key)
        else:
            final_lines.append(raw_line)

    missing = [key for key in PERSONALIZATION_CONFIG_CANONICAL_KEYS if key in normalized_updates and key not in found]
    if missing:
        if final_lines and final_lines[-1].strip():
            final_lines.append('\n')
        final_lines.extend([
            '# ============================================================\n',
            '# Personnalisation de l’accueil / onglet Système > Personnalisation\n',
            '# Ancienne configuration de la page d’accueil fusionnée ici.\n',
            '# Le futur fichier d’accueil peut donc servir à autre chose sans casser cet onglet.\n',
            '# ============================================================\n',
        ])
        for key in PERSONALIZATION_CONFIG_CANONICAL_KEYS:
            if key in normalized_updates and key not in found:
                canonical_key = PERSONALIZATION_CONFIG_CANONICAL_KEYS[key]
                final_lines.append(f"{canonical_key}={normalized_updates[key]}\n")

    with open(config_path, 'w', encoding='utf-8') as handle:
        handle.writelines(final_lines)


def personalization_save_module_config(config: dict) -> None:
    config_path = personalization_get_config_path()
    current = personalization_load_module_config()

    updates = {}
    for key in PERSONALIZATION_CONFIG_CANONICAL_KEYS:
        default_value = PERSONALIZATION_DEFAULT_CONFIG.get(key, '')
        value = config.get(key, current.get(key, default_value))
        updates[key] = personalization_sanitize_conf_value(str(value), default_value)

    personalization_update_system_conf_values(config_path, updates)

    # Met à jour le cache CONF du process Flask courant, sans attendre le prochain redémarrage.
    for key, value in updates.items():
        CONF[PERSONALIZATION_CONFIG_CANONICAL_KEYS[key]] = value



# --------------------------------------------------
# CONFIGURATION DE LA PAGE D'ACCUEIL : index.conf
# --------------------------------------------------
HOME_CONFIG_DEFAULTS = {
    "SHOW_TIME": "1",
    "SHOW_CPU": "1",
    "SHOW_RAM": "1",
    "SHOW_DOCKER_TOTAL": "1",
    "SHOW_DOCKER_RUNNING": "1",
    "SHOW_BUILD": "1",
    "SHOW_STORAGE": "1",
    "SHOW_SERVICES": "1",
    "SHOW_UPTIME": "1",
    "SHOW_NVIDIA_LOCAL": "1",
    "SHOW_NVIDIA_SSH": "1",
    "SHOW_INTEL_GPU": "1",
    "SHOW_NETWORK": "1",
    "SHOW_HOST": "1",
    "SHOW_LOCAL_RESOLUTION": "1",
    "SHOW_DISK_MOUNTS": "0",
    "SHOW_FANS": "0",
}

HOME_CONFIG_LABELS = {
    "SHOW_TIME": "Heure",
    "SHOW_CPU": "Processeur",
    "SHOW_RAM": "Mémoire RAM",
    "SHOW_DOCKER_TOTAL": "Docker",
    "SHOW_DOCKER_RUNNING": "VM",
    "SHOW_BUILD": "Build",
    "SHOW_STORAGE": "Stockage",
    "SHOW_SERVICES": "Services",
    "SHOW_UPTIME": "Uptime",
    "SHOW_NVIDIA_LOCAL": "NVIDIA GPU (local)",
    "SHOW_NVIDIA_SSH": "NVIDIA GPU (SSH)",
    "SHOW_INTEL_GPU": "Intel GPU",
    "SHOW_NETWORK": "Résolution locale / réseau",
    "SHOW_HOST": "Hôte",
    "SHOW_LOCAL_RESOLUTION": "Résolution locale",
    "SHOW_DISK_MOUNTS": "Vérifier montages disque",
    "SHOW_FANS": "Ventilateurs",
}

HOME_SSH_CONFIG_KEYS = {
    "SSH_GPU_HOST",
    "SSH_GPU_PORT",
    "SSH_GPU_USER",
    "SSH_GPU_KEY_PATH",
    "REMOTE_NVIDIA_SMI",
    "SSH_GPU_CONNECT_TIMEOUT",
    "SSH_GPU_COMMAND_TIMEOUT",
    "SSH_GPU_CACHE_SECONDS",
}


def home_config_normalize_key(key: str) -> str:
    return str(key or "").strip().upper()


def home_config_bool(value: Any) -> bool:
    return str(value if value is not None else "").strip().lower() in {"1", "true", "yes", "oui", "on", "checked"}


def home_config_get_path() -> str:
    env_path = os.environ.get("INDEX_CONF", "").strip()
    if env_path:
        return os.path.abspath(os.path.expanduser(os.path.expandvars(env_path)))

    roots = _project_root_candidates()
    candidates = []
    for root in roots:
        candidates.append(os.path.join(root, "conf", "index.conf"))
    candidates.extend([
        nas_conf_file("index.conf"),
        nas_conf_file("index.conf"),
        "index.conf",
    ])

    for candidate in _unique_existing_order(candidates):
        if os.path.exists(candidate):
            return os.path.abspath(candidate)

    root = roots[0] if roots else os.path.dirname(PERSONALIZATION_MODULE_DIR)
    return os.path.abspath(os.path.join(root, "conf", "index.conf"))


def home_config_read(path: str = "") -> Dict[str, str]:
    path = path or home_config_get_path()
    data = {}
    if not path or not os.path.exists(path):
        return data

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or line.startswith(";") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = home_config_normalize_key(key)
                if key in HOME_CONFIG_DEFAULTS:
                    data[key] = "1" if home_config_bool(value) else "0"
    except Exception as exc:
        print(f"❌ Erreur lecture index.conf : {exc}")
    return data


def home_config_write(updates: Dict[str, Any]) -> str:
    path = home_config_get_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)

    current = HOME_CONFIG_DEFAULTS.copy()
    current.update(home_config_read(path))
    for key, value in (updates or {}).items():
        key = home_config_normalize_key(key)
        if key in HOME_CONFIG_DEFAULTS:
            current[key] = "1" if home_config_bool(value) else "0"

    lines = [
        "# ============================================================\n",
        "# Page d'accueil NAS - index.py / index.html\n",
        "# 1 = affiche le bloc ; 0 = masque le bloc.\n",
        "# Les futurs déplacements de blocs pourront réutiliser ce fichier.\n",
        "# ============================================================\n",
        "\n",
    ]
    for key in HOME_CONFIG_DEFAULTS:
        label = HOME_CONFIG_LABELS.get(key, key)
        lines.append(f"# {label}\n")
        lines.append(f"{key}={current.get(key, HOME_CONFIG_DEFAULTS[key])}\n")
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    return path


def home_config_load() -> Dict[str, Any]:
    path = home_config_get_path()
    merged = HOME_CONFIG_DEFAULTS.copy()
    if os.path.exists(path):
        merged.update(home_config_read(path))
    else:
        home_config_write(merged)

    out = {key.lower(): "1" if home_config_bool(value) else "0" for key, value in merged.items()}
    out["_config_path"] = path
    return out


# ==========================================================
# 🧩 Ordre des blocs de la page d'accueil
# ==========================================================
# Fichier officiel : ../conf/index_top.conf.
def index_top_get_path() -> str:
    env_path = os.environ.get("INDEX_TOP_CONF", "").strip()
    if env_path:
        return os.path.abspath(os.path.expanduser(os.path.expandvars(env_path)))

    roots = _project_root_candidates()
    candidates = [os.path.join(root, "conf", "index_top.conf") for root in roots]
    candidates.extend([nas_conf_file("index_top.conf"), "index_top.conf"])
    for candidate in _unique_existing_order(candidates):
        if os.path.exists(candidate):
            return os.path.abspath(candidate)

    root = roots[0] if roots else os.path.dirname(PERSONALIZATION_MODULE_DIR)
    return os.path.abspath(os.path.join(root, "conf", "index_top.conf"))


def index_top_split_order(value: str) -> List[str]:
    raw = str(value or "").replace(";", ",").replace("|", ",").replace("\n", ",")
    return [part.strip() for part in raw.split(",") if part.strip()]


def index_top_normalize_order(order: List[str]) -> List[str]:
    known = list(HOME_CONFIG_DEFAULTS.keys())
    out: List[str] = []
    for raw in order or []:
        key = home_config_normalize_key(raw)
        if key in known and key not in out:
            out.append(key)
    for key in known:
        if key not in out:
            out.append(key)
    return out


def index_top_read_order(path: str = "") -> List[str]:
    path = path or index_top_get_path()
    if not path or not os.path.exists(path):
        return []

    values: List[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or line.startswith(";"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    if key.strip().upper() in {"ORDER", "INDEX_ORDER", "INDEX_TOP_ORDER"}:
                        values.extend(index_top_split_order(value))
                else:
                    values.extend(index_top_split_order(line))
    except Exception as exc:
        print(f"❌ Erreur lecture index_top.conf : {exc}")
        return []
    return index_top_normalize_order(values)


def index_top_write_order(order: List[str]) -> str:
    path = index_top_get_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    normalized = index_top_normalize_order(order)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("# ============================================================\n")
        handle.write("# Ordre des étiquettes/blocs de la page d'accueil Yoleo\n")
        handle.write("# index.conf garde afficher/masquer ; ce fichier garde seulement l’ordre.\n")
        handle.write("# ============================================================\n")
        handle.write("ORDER=" + ",".join(normalized) + "\n")
    return path


def index_top_load_order() -> List[str]:
    path = index_top_get_path()
    if os.path.exists(path):
        return index_top_read_order(path)
    index_top_write_order(list(HOME_CONFIG_DEFAULTS.keys()))
    return index_top_normalize_order(list(HOME_CONFIG_DEFAULTS.keys()))


def index_top_load_items() -> List[Dict[str, str]]:
    return [
        {
            "key": key,
            "key_lower": key.lower(),
            "label": HOME_CONFIG_LABELS.get(key, key),
        }
        for key in index_top_load_order()
    ]


# ==========================================================
# 🧭 Bandeau haut : ../conf/top.conf
# ==========================================================
TOP_CONFIG_ITEMS = {
    "DATE": {"label": "Date", "icon": "📅", "default": "1", "hint": "Affiche la date dans le bandeau haut."},
    "TIME": {"label": "Heure", "icon": "🕘", "default": "1", "hint": "Affiche l’heure dans le bandeau haut."},
    "METEO": {"label": "Météo", "icon": "🌤️", "default": "1", "hint": "Affiche la météo choisie dans l’onglet Météo."},
    "LAN": {"label": "LAN", "icon": "🌐", "default": "0", "hint": "Affiche les débits réseau montant/descendant."},
}

TOP_MENU_ACTIONS = {
    "RESTART_FLASK": {"label": "Redémarrer Flask", "default": "1"},
    "RESTART_FLASK_UNRAID": {"label": "Redémarrer Flask Unraid", "default": "1"},
    "POWEROFF_SERVER": {"label": "Arrêter le serveur", "default": "1"},
    "REBOOT_SERVER": {"label": "Redémarrer le serveur", "default": "1"},
}

TOP_CONFIG_CACHE_LOCK = threading.RLock()
TOP_CONFIG_CACHE: Optional[Dict[str, Any]] = None


def top_config_clone(config: Dict[str, Any] | None) -> Dict[str, Any]:
    return copy.deepcopy(config or {})


def top_config_clear_cache() -> None:
    global TOP_CONFIG_CACHE
    with TOP_CONFIG_CACHE_LOCK:
        TOP_CONFIG_CACHE = None


def top_config_set_cache(config: Dict[str, Any]) -> None:
    global TOP_CONFIG_CACHE
    with TOP_CONFIG_CACHE_LOCK:
        TOP_CONFIG_CACHE = top_config_clone(config)


def top_config_bool(value: Any) -> bool:
    return str(value if value is not None else "").strip().lower() in {"1", "true", "yes", "oui", "on", "checked"}


def top_config_get_path() -> str:
    env_path = os.environ.get("TOP_CONF", "").strip()
    if env_path:
        return os.path.abspath(os.path.expanduser(os.path.expandvars(env_path)))

    roots = _project_root_candidates()
    candidates = [os.path.join(root, "conf", "top.conf") for root in roots]
    candidates.extend([nas_conf_file("top.conf"), "top.conf"])
    for candidate in _unique_existing_order(candidates):
        if os.path.exists(candidate):
            return os.path.abspath(candidate)

    root = roots[0] if roots else os.path.dirname(PERSONALIZATION_MODULE_DIR)
    return os.path.abspath(os.path.join(root, "conf", "top.conf"))


def top_config_split_order(value: str) -> List[str]:
    raw = str(value or "").replace(";", ",").replace("|", ",").replace("\n", ",")
    return [part.strip().upper() for part in raw.split(",") if part.strip()]


def top_config_normalize_order(order: List[str]) -> List[str]:
    known = list(TOP_CONFIG_ITEMS.keys())
    out: List[str] = []
    for raw in order or []:
        key = str(raw or "").strip().upper()
        if key in known and key not in out:
            out.append(key)
    for key in known:
        if key not in out:
            out.append(key)
    return out


def top_config_read(path: str = "") -> Dict[str, str]:
    path = path or top_config_get_path()
    data: Dict[str, str] = {}
    if not path or not os.path.exists(path):
        return data

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or line.startswith(";") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                data[key.strip().upper()] = value.strip()
    except Exception as exc:
        print(f"❌ Erreur lecture top.conf : {exc}")
    return data


def top_config_load(force_reload: bool = False) -> Dict[str, Any]:
    global TOP_CONFIG_CACHE
    with TOP_CONFIG_CACHE_LOCK:
        if TOP_CONFIG_CACHE is not None and not force_reload:
            return top_config_clone(TOP_CONFIG_CACHE)

        path = top_config_get_path()
        raw = top_config_read(path)
        merged: Dict[str, Any] = {"_config_path": path}

        for key, meta in TOP_CONFIG_ITEMS.items():
            conf_key = f"SHOW_{key}"
            merged[conf_key] = "1" if top_config_bool(raw.get(conf_key, meta.get("default", "0"))) else "0"

        for key, meta in TOP_MENU_ACTIONS.items():
            conf_key = f"MENU_{key}"
            merged[conf_key] = "1" if top_config_bool(raw.get(conf_key, meta.get("default", "0"))) else "0"

        order_value = raw.get("ORDER") or raw.get("TOP_ORDER") or raw.get("BANNER_ORDER") or ""
        merged["ORDER"] = ",".join(top_config_normalize_order(top_config_split_order(order_value))) if order_value else ",".join(top_config_normalize_order([]))

        if not os.path.exists(path):
            top_config_write(merged)
            if TOP_CONFIG_CACHE is not None:
                return top_config_clone(TOP_CONFIG_CACHE)

        TOP_CONFIG_CACHE = top_config_clone(merged)
        return top_config_clone(TOP_CONFIG_CACHE)


def top_config_write(values: Dict[str, Any]) -> str:
    path = top_config_get_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)

    current = top_config_load() if os.path.exists(path) else {"ORDER": ",".join(top_config_normalize_order([]))}
    current.update(values or {})

    order = top_config_normalize_order(top_config_split_order(str(current.get("ORDER", ""))))

    lines = [
        "# ============================================================\n",
        "# Bandeau haut Yoleo - menu.html\n",
        "# 1 = visible ; 0 = masqué. L’ordre ne concerne que les éléments du bandeau.\n",
        "# ============================================================\n",
        "\n",
        "# Ordre d’affichage : DATE, TIME, METEO, LAN\n",
        "ORDER=" + ",".join(order) + "\n",
        "\n",
    ]

    for key, meta in TOP_CONFIG_ITEMS.items():
        conf_key = f"SHOW_{key}"
        lines.append(f"# {meta.get('label', key)}\n")
        lines.append(f"{conf_key}={'1' if top_config_bool(current.get(conf_key, meta.get('default', '0'))) else '0'}\n")

    lines.append("\n# Menu déroulant système\n")
    for key, meta in TOP_MENU_ACTIONS.items():
        conf_key = f"MENU_{key}"
        lines.append(f"# {meta.get('label', key)}\n")
        lines.append(f"{conf_key}={'1' if top_config_bool(current.get(conf_key, meta.get('default', '0'))) else '0'}\n")

    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    current["_config_path"] = path
    top_config_set_cache(current)
    return path


def top_config_load_items(config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    config = config or top_config_load()
    order = top_config_normalize_order(top_config_split_order(str(config.get("ORDER", ""))))
    return [
        {
            "key": key,
            "key_lower": key.lower(),
            "label": TOP_CONFIG_ITEMS[key].get("label", key),
            "icon": TOP_CONFIG_ITEMS[key].get("icon", "•"),
            "hint": TOP_CONFIG_ITEMS[key].get("hint", ""),
            "enabled": top_config_bool(config.get(f"SHOW_{key}", TOP_CONFIG_ITEMS[key].get("default", "0"))),
            "field": f"SHOW_{key}",
        }
        for key in order
    ]


def top_config_load_actions(config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    config = config or top_config_load()
    return [
        {
            "key": key,
            "key_lower": key.lower(),
            "label": meta.get("label", key),
            "enabled": top_config_bool(config.get(f"MENU_{key}", meta.get("default", "0"))),
            "field": f"MENU_{key}",
        }
        for key, meta in TOP_MENU_ACTIONS.items()
    ]


def top_config_action_map(actions: Optional[List[Dict[str, Any]]] = None) -> Dict[str, bool]:
    rows = actions if actions is not None else top_config_load_actions()
    return {item["key"]: bool(item.get("enabled")) for item in rows}


# ==========================================================
# Session Flask : ../conf/session.conf
# ==========================================================
SESSION_CONFIG_DEFAULT_MINUTES = 20
SESSION_CONFIG_MIN_MINUTES = 1
SESSION_CONFIG_MAX_MINUTES = 10080


def session_config_get_path() -> str:
    env_path = os.environ.get("SESSION_CONF", "").strip()
    if env_path:
        return os.path.abspath(os.path.expanduser(os.path.expandvars(env_path)))

    roots = _project_root_candidates()
    candidates = [os.path.join(root, "conf", "session.conf") for root in roots]
    candidates.extend([nas_conf_file("session.conf"), "session.conf"])
    for candidate in _unique_existing_order(candidates):
        if os.path.exists(candidate):
            return os.path.abspath(candidate)

    root = roots[0] if roots else os.path.dirname(PERSONALIZATION_MODULE_DIR)
    return os.path.abspath(os.path.join(root, "conf", "session.conf"))


def session_config_read(path: str = "") -> Dict[str, str]:
    path = path or session_config_get_path()
    data: Dict[str, str] = {}
    if not path or not os.path.exists(path):
        return data
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or line.startswith(";") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                data[key.strip().upper()] = value.strip()
    except Exception as exc:
        print(f"Erreur lecture session.conf : {exc}")
    return data


def session_config_clean_minutes(value: Any) -> int:
    try:
        minutes = int(float(str(value if value is not None else "").strip()))
    except Exception:
        minutes = SESSION_CONFIG_DEFAULT_MINUTES
    return max(SESSION_CONFIG_MIN_MINUTES, min(SESSION_CONFIG_MAX_MINUTES, minutes))


def session_config_minutes(data: Optional[Dict[str, str]] = None) -> int:
    data = data if data is not None else session_config_read()
    for key in ("SESSION_MINUTES", "DUREE_MINUTES", "DUREE", "DURATION_MINUTES", "TIME", "MINUTES"):
        raw = str((data or {}).get(key, "")).strip()
        if raw:
            return session_config_clean_minutes(raw)
    return SESSION_CONFIG_DEFAULT_MINUTES


def session_config_write(minutes: Any) -> str:
    path = session_config_get_path()
    clean_minutes = session_config_clean_minutes(minutes)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [
        "# Configuration session Flask Yoleo\n",
        "# Duree de session en minutes. Exemple : 720 = 12 heures.\n",
        f"SESSION_MINUTES={clean_minutes}\n",
    ]
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass
    return path


def session_config_load() -> Dict[str, Any]:
    path = session_config_get_path()
    if not os.path.exists(path):
        session_config_write(SESSION_CONFIG_DEFAULT_MINUTES)
    minutes = session_config_minutes(session_config_read(path))
    return {
        "path": path,
        "minutes": minutes,
        "min_minutes": SESSION_CONFIG_MIN_MINUTES,
        "max_minutes": SESSION_CONFIG_MAX_MINUTES,
    }


# ==========================================================
# Menu crante du bandeau haut : ../conf/menu_top/
# ==========================================================
MENU_TOP_DIR_NAME = "menu_top"
# Nom historique conservé pour limiter les changements internes : il désigne
# maintenant le dossier conf/menu_top/, plus l'ancien fichier menu_top.conf.
MENU_TOP_CONF_NAME = MENU_TOP_DIR_NAME
MENU_TOP_LEGACY_CONF_NAMES = ()
MENU_TOP_LIST_URL = "/system/personnalisation/menu-top"
MENU_TOP_EDITOR_URL = "/system/personnalisation/top-menu"
MENU_TOP_CACHE_LOCK = threading.RLock()
MENU_TOP_SECTIONS_CACHE: Optional[List[Dict[str, Any]]] = None


def menu_top_clone_sections(sections: List[Dict[str, Any]] | None) -> List[Dict[str, Any]]:
    return copy.deepcopy(sections or [])


def menu_top_clear_cache() -> None:
    global MENU_TOP_SECTIONS_CACHE
    with MENU_TOP_CACHE_LOCK:
        MENU_TOP_SECTIONS_CACHE = None
    clear_system_menu = globals().get("system_menu_clear_cache")
    if callable(clear_system_menu):
        clear_system_menu()


def menu_top_set_cache(sections: List[Dict[str, Any]]) -> None:
    global MENU_TOP_SECTIONS_CACHE
    with MENU_TOP_CACHE_LOCK:
        MENU_TOP_SECTIONS_CACHE = menu_top_clone_sections(sections)
    clear_system_menu = globals().get("system_menu_clear_cache")
    if callable(clear_system_menu):
        clear_system_menu()


def menu_top_cached_sections(force_reload: bool = False) -> List[Dict[str, Any]]:
    global MENU_TOP_SECTIONS_CACHE
    with MENU_TOP_CACHE_LOCK:
        if MENU_TOP_SECTIONS_CACHE is None or force_reload:
            MENU_TOP_SECTIONS_CACHE = menu_top_clone_sections(menu_top_load_sections_from_disk())
        return MENU_TOP_SECTIONS_CACHE


def menu_top_get_path() -> str:
    """Retourne le dossier officiel conf/menu_top/.

    L'ancien conf/menu_top.conf n'est plus lu ici. La seule configuration
    active du menu cranté est l'arborescence conf/menu_top/.
    """
    env_path = os.environ.get("MENU_TOP_DIR", "").strip()
    if env_path:
        return os.path.abspath(os.path.expanduser(os.path.expandvars(env_path)))

    roots = _project_root_candidates()
    candidates = [os.path.join(root, "conf", MENU_TOP_DIR_NAME) for root in roots]
    candidates.extend([nas_conf_file(MENU_TOP_DIR_NAME), MENU_TOP_DIR_NAME])
    for candidate in _unique_existing_order(candidates):
        if os.path.isdir(candidate):
            return os.path.abspath(candidate)

    root = roots[0] if roots else os.path.dirname(PERSONALIZATION_MODULE_DIR)
    return os.path.abspath(os.path.join(root, "conf", MENU_TOP_DIR_NAME))


# ----------------------------------------------------------
# Editeur du menu haut en dossiers : ../conf/menu_top/
# ----------------------------------------------------------
MENU_TOP_EDITOR_DIR_NAME = MENU_TOP_DIR_NAME


def menu_top_editor_get_dir(create: bool = False) -> str:
    path = menu_top_get_path()
    if create:
        os.makedirs(path, exist_ok=True)
        return path

    # L'éditeur utilise la même initialisation que le menu réel : si
    # conf/menu_top/ n'existe pas encore, on tente de le créer depuis
    # default_menu_top/. Si le défaut est absent, le dossier reste absent.
    return menu_top_ensure_config()


def menu_top_editor_safe_segment(value: str, fallback: str = "general") -> str:
    name = str(value or fallback or "general").strip()
    name = re.sub(r"[\\/\x00\r\n]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or fallback or "general"


def menu_top_editor_safe_filename(label: str, fallback: str = "Menu top") -> str:
    name = menu_top_editor_safe_segment(label, fallback)
    if not name.lower().endswith(".conf"):
        name += ".conf"
    return name


def menu_top_editor_folder_label(folder: str) -> str:
    raw = str(folder or "").strip().replace("_", " ").replace("-", " ")
    if not raw:
        return "Menu top"
    parts = [part for part in raw.split() if part]
    return " ".join(part[:1].upper() + part[1:] for part in parts) or raw


def menu_top_editor_guess_folder(match: str, fallback: str = "general") -> str:
    first = (menu_top_split_match(match) or [""])[0]
    first = str(first or "").strip()
    if first.startswith(("http://", "https://")):
        return fallback
    parts = [part for part in first.strip("/").split("/") if part]
    return parts[0] if parts else fallback


def menu_top_editor_parse_conf(path: str, folder: str, section_id: str) -> Optional[Dict[str, Any]]:
    file_name = os.path.basename(path)
    stem = os.path.splitext(file_name)[0]
    section: Dict[str, Any] = {
        "id": section_id,
        "label": stem,
        "icon": "⚙️",
        "match": "",
        "enabled": True,
        "items": [],
        "group": folder,
        "folder": folder,
        "group_label": menu_top_editor_folder_label(folder),
        "file_name": file_name,
        "storage": "dir",
        "source_path": path,
    }
    item_rows: Dict[str, str] = {}
    saw_value = False

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or line.startswith(";"):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    # Nouveau format : le fichier .conf sépare déjà le bloc.
                    # Un ancien [bloc] oublié est donc ignoré.
                    continue
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                low = key.lower()
                saw_value = True
                if low.startswith("item_"):
                    item_rows[low] = value
                elif low == "enabled":
                    section["enabled"] = menu_top_bool(value)
                elif low in {"label", "title", "nom"}:
                    section["label"] = value or section.get("label") or stem
                elif low in {"icon", "icone", "icons"}:
                    section["icon"] = value or "⚙️"
                elif low in {"match", "matches", "prefix", "route"}:
                    section["match"] = value
    except Exception as exc:
        print(f"⚠️ Fichier menu_top illisible {path} : {exc}")
        return None

    if not saw_value:
        return None

    items: List[Dict[str, Any]] = []
    def item_sort_key(name: str) -> Tuple[int, str]:
        found = re.search(r"(\d+)", name)
        return (int(found.group(1)) if found else 9999, name)

    for idx, key in enumerate(sorted(item_rows, key=item_sort_key), start=1):
        item = menu_top_parse_item_value(item_rows[key], idx)
        if menu_top_is_management_url(item.get("url", "")):
            continue
        item["id"] = f"item_{idx:03d}"
        item["order"] = idx
        items.append(item)
    section["items"] = items
    section["match_list"] = menu_top_split_match(section.get("match", ""))
    return section


def menu_top_editor_load_sections() -> List[Dict[str, Any]]:
    base_dir = menu_top_editor_get_dir(create=False)
    if not base_dir or not os.path.isdir(base_dir):
        return []

    sections: List[Dict[str, Any]] = []
    used: set[str] = set()
    try:
        folder_names = [
            name for name in os.listdir(base_dir)
            if os.path.isdir(os.path.join(base_dir, name)) and not name.startswith(".")
        ]
    except Exception as exc:
        print(f"❌ Erreur lecture dossier menu_top : {exc}")
        return []

    for folder in sorted(folder_names, key=lambda value: value.lower()):
        folder_path = os.path.join(base_dir, folder)
        try:
            file_names = [
                name for name in os.listdir(folder_path)
                if name.lower().endswith(".conf") and os.path.isfile(os.path.join(folder_path, name))
            ]
        except Exception as exc:
            print(f"⚠️ Dossier menu_top illisible {folder_path} : {exc}")
            continue

        for file_name in sorted(file_names, key=lambda value: value.lower()):
            stem = os.path.splitext(file_name)[0]
            base_id = f"{system_menu_slug(folder, 'folder')}__{system_menu_slug(stem, 'menu')}"
            section_id = base_id
            suffix = 2
            while section_id in used:
                section_id = f"{base_id}_{suffix}"
                suffix += 1
            used.add(section_id)
            section = menu_top_editor_parse_conf(os.path.join(folder_path, file_name), folder, section_id)
            if section:
                section["id"] = section_id
                sections.append(section)

    return menu_top_editor_normalize_sections(sections)


def menu_top_editor_load_groups(sections: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    base_dir = menu_top_editor_get_dir(create=False)
    rows = sections if sections is not None else menu_top_editor_load_sections()
    by_folder: Dict[str, Dict[str, Any]] = {}

    if base_dir and os.path.isdir(base_dir):
        try:
            for folder in sorted(
                [name for name in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, name)) and not name.startswith(".")],
                key=lambda value: value.lower(),
            ):
                by_folder[folder] = {
                    "id": system_menu_slug(folder, "menu_top"),
                    "folder": folder,
                    "label": menu_top_editor_folder_label(folder),
                    "route": "/" + folder.strip("/"),
                    "icon": "📁",
                    "sections": 0,
                    "items": 0,
                    "enabled": False,
                }
        except Exception as exc:
            print(f"⚠️ Dossiers menu_top illisibles : {exc}")

    for section in rows or []:
        folder = str(section.get("group") or section.get("folder") or "").strip() or menu_top_editor_guess_folder(section.get("match") or "", "general")
        row = by_folder.setdefault(folder, {
            "id": system_menu_slug(folder, "menu_top"),
            "folder": folder,
            "label": menu_top_editor_folder_label(folder),
            "route": "/" + folder.strip("/"),
            "icon": "📁",
            "sections": 0,
            "items": 0,
            "enabled": False,
        })
        row["sections"] += 1
        row["items"] += len(section.get("items") or [])
        row["enabled"] = bool(row.get("enabled") or section.get("enabled"))

    return [by_folder[key] for key in sorted(by_folder, key=lambda value: value.lower())]


def menu_top_editor_normalize_sections(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    used: set[str] = set()
    for index, raw in enumerate(rows or [], start=1):
        match = str(raw.get("match") or "").strip()
        group = str(raw.get("group") or raw.get("folder") or "").strip() or menu_top_editor_guess_folder(match, "general")
        group = menu_top_editor_safe_segment(group, "general")
        label = personalization_sanitize_conf_value(raw.get("label") or raw.get("id") or f"Menu {index}", f"Menu {index}")
        base_id = system_menu_slug(raw.get("id") or f"{group}__{label}", f"menu_top_{index:02d}")
        section_id = base_id
        suffix = 2
        while section_id in used:
            section_id = f"{base_id}_{suffix}"
            suffix += 1
        used.add(section_id)

        items: List[Dict[str, Any]] = []
        for item_index, item in enumerate(raw.get("items") or [], start=1):
            item_label = personalization_sanitize_conf_value(item.get("label") or f"Entree {item_index}", f"Entree {item_index}")
            item_url = personalization_sanitize_conf_value(item.get("url") or "#", "#") or "#"
            if menu_top_is_management_url(item_url):
                continue
            item_icon = personalization_sanitize_conf_value(item.get("icon") or "•", "•") or "•"
            item_onclick = personalization_sanitize_conf_value(item.get("onclick") or "", "")
            order = len(items) + 1
            items.append({
                "id": f"item_{order:03d}",
                "label": item_label,
                "url": item_url,
                "icon": item_icon,
                "onclick": item_onclick,
                "order": order,
            })

        file_name = os.path.basename(str(raw.get("file_name") or raw.get("filename") or "").strip())
        out.append({
            "id": section_id,
            "label": label,
            "icon": personalization_sanitize_conf_value(raw.get("icon") or "⚙️", "⚙️") or "⚙️",
            "match": match,
            "match_list": menu_top_split_match(match),
            "enabled": menu_top_bool(raw.get("enabled", "1")),
            "items": items,
            "group": group,
            "folder": group,
            "group_label": menu_top_editor_folder_label(group),
            "file_name": file_name,
            "storage": "dir",
        })
    return out


def menu_top_editor_write_section_file(path: str, section: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"enabled={'1' if section.get('enabled') else '0'}\n")
        handle.write(f"label={section.get('label') or section.get('id') or 'Menu top'}\n")
        handle.write(f"icon={section.get('icon') or '⚙️'}\n")
        handle.write(f"match={section.get('match') or ''}\n")
        for idx, item in enumerate(section.get("items") or [], start=1):
            parts = [item.get("icon") or "•", item.get("label") or f"Entree {idx}", item.get("url") or "#"]
            if item.get("onclick"):
                parts.append(item.get("onclick") or "")
            handle.write(f"item_{idx:03d}={'|'.join(parts)}\n")


def menu_top_editor_save_sections(sections: List[Dict[str, Any]]) -> str:
    base_dir = menu_top_editor_get_dir(create=True)
    normalized = menu_top_editor_normalize_sections(sections)

    # L'éditeur travaille uniquement sur conf/menu_top/. Il ne lit ni n'écrit
    # l'ancien menu_top.conf.
    if os.path.isdir(base_dir):
        for folder in list(os.listdir(base_dir)):
            folder_path = os.path.join(base_dir, folder)
            if not os.path.isdir(folder_path) or folder.startswith("."):
                continue
            for name in list(os.listdir(folder_path)):
                if name.lower().endswith(".conf"):
                    try:
                        os.remove(os.path.join(folder_path, name))
                    except FileNotFoundError:
                        pass
            try:
                if not os.listdir(folder_path):
                    os.rmdir(folder_path)
            except Exception:
                pass

    used_by_folder: Dict[str, set[str]] = {}
    for section in normalized:
        folder = menu_top_editor_safe_segment(section.get("group") or section.get("folder") or "general", "general")
        folder_path = os.path.join(base_dir, folder)
        used = used_by_folder.setdefault(folder, set())
        requested_name = os.path.basename(str(section.get("file_name") or "").strip())
        file_name = menu_top_editor_safe_filename(requested_name or section.get("label") or section.get("id") or "Menu top")
        base_name, ext = os.path.splitext(file_name)
        candidate = file_name
        suffix = 2
        while candidate.lower() in used:
            candidate = f"{base_name} - {suffix}{ext or '.conf'}"
            suffix += 1
        used.add(candidate.lower())
        section["group"] = folder
        section["folder"] = folder
        section["file_name"] = candidate
        menu_top_editor_write_section_file(os.path.join(folder_path, candidate), section)

    menu_top_set_cache(normalized)
    return base_dir


def menu_top_legacy_paths() -> List[str]:
    roots = _project_root_candidates()
    candidates: List[str] = []
    for name in MENU_TOP_LEGACY_CONF_NAMES:
        candidates.extend([os.path.join(root, "conf", name) for root in roots])
        candidates.extend([nas_conf_file(name), name])
    return [os.path.abspath(path) for path in _unique_existing_order(candidates) if os.path.exists(path)]


def menu_top_bool(value: Any) -> bool:
    return str(value if value is not None else "").strip().lower() in {"1", "true", "yes", "oui", "on", "checked"}


def menu_top_split_match(value: str) -> List[str]:
    raw = str(value or "").replace("\n", "||")
    out: List[str] = []
    for part in raw.split("||"):
        item = part.strip()
        if item and item not in out:
            out.append(item)
    return out


def menu_top_match_path(path: str, match_value: str) -> bool:
    current = "/" + str(path or "/").strip().lstrip("/")
    for prefix in menu_top_split_match(match_value):
        if not prefix:
            continue
        if prefix.startswith(("http://", "https://")):
            if current == prefix:
                return True
            continue
        prefix = "/" + prefix.strip().lstrip("/")
        prefix = prefix.rstrip("/") or "/"
        if current == prefix or (prefix != "/" and current.startswith(prefix + "/")):
            return True
    return False


def menu_top_parse_item_value(value: str, index: int = 1) -> Dict[str, Any]:
    parts = str(value or "").split("|", 3)
    icon = parts[0].strip() if len(parts) > 0 else ""
    label = parts[1].strip() if len(parts) > 1 else ""
    url = parts[2].strip() if len(parts) > 2 else ""
    onclick = parts[3].strip() if len(parts) > 3 else ""
    return {
        "id": f"item_{index:03d}",
        "icon": icon or "•",
        "label": label or f"Entree {index}",
        "url": url or "#",
        "onclick": onclick,
        "order": index,
    }


def menu_top_is_management_url(url: str) -> bool:
    """Route interne de configuration : elle reste accessible mais ne doit pas
    apparaître dans les menus crantés utilisateur par défaut.
    """
    clean = (str(url or "").split("?", 1)[0]).rstrip("/")
    return clean in {
        "/system/personnalisation/top-menu",
    }


def menu_top_packaged_default_path() -> str:
    """Dossier default_menu_top/ embarqué avec l'application."""
    return os.path.join(PERSONALIZATION_MODULE_DIR, "default_menu_top")


def menu_top_clear_directory(path: str) -> None:
    """Vide prudemment le dossier cible avant copie du menu haut par défaut."""
    import shutil

    path = os.path.abspath(path or "")
    if not path or path in {"/", os.path.expanduser("~")}:
        raise RuntimeError(f"Chemin menu_top dangereux : {path}")
    os.makedirs(path, exist_ok=True)
    for name in os.listdir(path):
        target = os.path.join(path, name)
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target)
        else:
            os.unlink(target)


def menu_top_copy_packaged_default_tree(path: str) -> bool:
    """Copie default_menu_top/ vers conf/menu_top/.

    Il n'y a pas de secours depuis l'ancien menu_top.conf et pas de menu
    intégré en dur dans Python. Si default_menu_top/ est absent, le menu haut
    reste vide.
    """
    import shutil

    source = os.path.abspath(menu_top_packaged_default_path())
    target = os.path.abspath(path or "")
    if not os.path.isdir(source):
        return False
    if target == source or target.startswith(source + os.sep):
        raise RuntimeError("Le dossier menu_top cible ne peut pas être dans default_menu_top.")

    os.makedirs(os.path.dirname(target), exist_ok=True)
    menu_top_clear_directory(target)
    shutil.copytree(source, target, dirs_exist_ok=True)
    return True


def menu_top_write_default_config(path: str) -> bool:
    """Initialise conf/menu_top/ uniquement depuis default_menu_top/."""
    return menu_top_copy_packaged_default_tree(path)


def menu_top_ensure_config() -> str:
    path = menu_top_get_path()
    if os.path.isdir(path):
        return path

    # Une seule source de vérité par défaut : default_menu_top/.
    # S'il est absent, on ne recrée plus rien en dur dans Python.
    menu_top_write_default_config(path)
    return path


def menu_top_clean_url(value: str) -> str:
    return (str(value or "").split("?", 1)[0]).rstrip("/") or "/"


def menu_top_ensure_personalization_list_item(sections: List[Dict[str, Any]]) -> bool:
    """Ancienne migration neutralisée.

    L’accès au catalogue des menus crantés est maintenant une entrée fixe de la
    page Menu personnalisé. Il ne faut plus recréer automatiquement une commande
    "Menu top" dans menu_top.conf, sinon l’utilisateur ne peut pas nettoyer le
    fichier proprement.
    """
    return False

def menu_top_load_sections_from_disk() -> List[Dict[str, Any]]:
    base_dir = menu_top_ensure_config()
    if not base_dir or not os.path.isdir(base_dir):
        return []

    # Le menu réel lit maintenant la même arborescence que l'éditeur :
    # conf/menu_top/<route>/<label humain>.conf.
    return menu_top_editor_load_sections()


def menu_top_load_sections(force_reload: bool = False) -> List[Dict[str, Any]]:
    return menu_top_clone_sections(menu_top_cached_sections(force_reload=force_reload))


def menu_top_normalize_sections(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    used: set[str] = set()
    for index, raw in enumerate(rows or [], start=1):
        label = personalization_sanitize_conf_value(raw.get("label") or raw.get("id") or f"Menu {index}", f"Menu {index}")
        section_id = system_menu_slug(raw.get("id") or label, f"menu_top_{index:02d}")
        base_id = section_id
        suffix = 2
        while section_id in used:
            section_id = f"{base_id}_{suffix}"
            suffix += 1
        used.add(section_id)
        match = str(raw.get("match") or "").strip()
        items: List[Dict[str, Any]] = []
        for item_index, item in enumerate(raw.get("items") or [], start=1):
            item_label = personalization_sanitize_conf_value(item.get("label") or f"Entree {item_index}", f"Entree {item_index}")
            item_url = personalization_sanitize_conf_value(item.get("url") or "#", "#") or "#"
            if menu_top_is_management_url(item_url):
                continue
            item_icon = personalization_sanitize_conf_value(item.get("icon") or "•", "•") or "•"
            item_onclick = personalization_sanitize_conf_value(item.get("onclick") or "", "")
            order = len(items) + 1
            items.append({
                "id": f"item_{order:03d}",
                "label": item_label,
                "url": item_url,
                "icon": item_icon,
                "onclick": item_onclick,
                "order": order,
            })
        out.append({
            "id": section_id,
            "label": label,
            "icon": personalization_sanitize_conf_value(raw.get("icon") or "⚙️", "⚙️") or "⚙️",
            "match": match,
            "match_list": menu_top_split_match(match),
            "enabled": menu_top_bool(raw.get("enabled", "1")),
            "items": items,
        })
    return out


def menu_top_save_sections(sections: List[Dict[str, Any]]) -> str:
    """Enregistre le menu haut dans conf/menu_top/.

    Chaque section devient un fichier .conf séparé dans son dossier de route
    de base. L'ancien format en un seul menu_top.conf n'est plus écrit.
    """
    return menu_top_editor_save_sections(sections)


def menu_top_find_section_for_path(path: str, include_disabled: bool = False) -> Optional[Dict[str, Any]]:
    matches: List[Tuple[int, Dict[str, Any]]] = []
    for section in menu_top_cached_sections():
        if not include_disabled and not section.get("enabled"):
            continue
        match_value = section.get("match") or ""
        if not match_value or not menu_top_match_path(path, match_value):
            continue
        longest = max([len(item) for item in menu_top_split_match(match_value)] or [0])
        matches.append((longest, section))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    return copy.deepcopy(matches[0][1])


def menu_top_is_url_enabled(url: str) -> bool:
    url = str(url or "").strip()
    if not url or url == "#":
        return False
    section = menu_top_find_section_for_path(url, include_disabled=True)
    return bool(section and section.get("enabled"))


def menu_top_set_for_menu_item(label: str, url: str, icon: str, enabled: Any) -> Tuple[bool, str]:
    url = str(url or "").strip()
    if not url or url == "#":
        return False, "Top menu ignoré : aucune route sur cette entrée."

    sections = menu_top_load_sections()
    section_index = -1
    for idx, section in enumerate(sections):
        if menu_top_match_path(url, section.get("match") or ""):
            section_index = idx
            break

    if section_index < 0:
        section_id = system_menu_slug(label or url, f"menu_top_{len(sections) + 1:02d}")
        sections.append({
            "id": section_id,
            "label": f"Menu {label or section_id}",
            "icon": icon or "⚙️",
            "match": url,
            "enabled": menu_top_bool(enabled),
            "items": [{
                "icon": icon or "•",
                "label": label or "Page",
                "url": url,
                "onclick": "",
            }],
        })
    else:
        sections[section_index]["enabled"] = menu_top_bool(enabled)
        if not sections[section_index].get("match"):
            sections[section_index]["match"] = url

    menu_top_save_sections(sections)
    return True, "Top menu mis à jour."


def home_ssh_config_load() -> Dict[str, str]:
    return {key: get_conf_str(key, "") for key in sorted(HOME_SSH_CONFIG_KEYS)}


def home_ssh_required_missing(values: Dict[str, Any]) -> list:
    required = [
        "SSH_GPU_HOST",
        "SSH_GPU_PORT",
        "SSH_GPU_USER",
        "SSH_GPU_KEY_PATH",
    ]
    return [key for key in required if not str(values.get(key, "") or "").strip()]


def home_update_system_conf_values(updates: Dict[str, Any]) -> None:
    config_path = personalization_get_config_path()
    os.makedirs(os.path.dirname(config_path), exist_ok=True)

    normalized_updates = {}
    for key, value in (updates or {}).items():
        key = str(key or "").strip().upper()
        if key in HOME_SSH_CONFIG_KEYS:
            normalized_updates[key] = personalization_sanitize_conf_value(str(value), "")

    if not normalized_updates:
        return

    lines = []
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()

    found = set()
    final_lines = []
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";") or "=" not in stripped:
            final_lines.append(raw_line)
            continue

        key, _value = stripped.split("=", 1)
        clean_key = key.strip().upper()
        if clean_key in normalized_updates:
            final_lines.append(f"{clean_key}={normalized_updates[clean_key]}\n")
            found.add(clean_key)
        else:
            final_lines.append(raw_line)

    missing = [key for key in sorted(normalized_updates) if key not in found]
    if missing:
        if final_lines and final_lines[-1].strip():
            final_lines.append("\n")
        final_lines.extend([
            "# ============================================================\n",
            "# NVIDIA distante pour la page d'accueil\n",
            "# Modifiable depuis Système > Personnalisation > Accueil.\n",
            "# ============================================================\n",
        ])
        for key in missing:
            final_lines.append(f"{key}={normalized_updates[key]}\n")

    with open(config_path, "w", encoding="utf-8") as handle:
        handle.writelines(final_lines)

    for key, value in normalized_updates.items():
        CONF[key] = value



def personalization_resolve_base_dir(base_dir: str, config_dir: str = '', create: bool = False) -> str:
    """Résout BASE_DIR sans créer de faux /dockers/system/tabs.

    BASE_DIR=../tabs dans /dockers/conf/system.conf doit donner /dockers/tabs.
    L'onglet personnalisation et le menu haut doivent donc pointer le même dossier.
    """
    base_dir = (base_dir or '').strip() or PERSONALIZATION_DEFAULT_CONFIG['base_dir']
    if os.path.isabs(base_dir):
        resolved = os.path.abspath(base_dir)
        if create:
            os.makedirs(resolved, exist_ok=True)
        return resolved

    config_dir = os.path.abspath(config_dir or os.path.dirname(personalization_get_config_path()))
    project_root = os.path.dirname(config_dir) if os.path.basename(config_dir).lower() == 'conf' else (_project_root_candidates()[0] if _project_root_candidates() else os.path.dirname(PERSONALIZATION_MODULE_DIR))

    candidates = []
    # Règle officielle : relatif au dossier du system.conf.
    candidates.append(os.path.join(config_dir, base_dir))
    # Compat : relatif à la racine projet si BASE_DIR=tabs.
    candidates.append(os.path.join(project_root, base_dir))

    tail = os.path.basename(base_dir.rstrip('/\\'))
    if tail.lower() in {'tab', 'tabs'}:
        candidates.append(os.path.join(project_root, tail))
        candidates.append(os.path.join(project_root, 'tabs'))

    for root in _project_root_candidates():
        candidates.append(os.path.join(root, base_dir))
    candidates.extend([
        os.path.join(PERSONALIZATION_MODULE_DIR, base_dir),
        os.path.join(os.getcwd(), base_dir),
    ])
    candidates = _unique_existing_order(candidates)

    for candidate in candidates:
        if os.path.isdir(os.path.join(candidate, 'Menu')):
            return os.path.abspath(candidate)
    for candidate in candidates:
        normalized = os.path.abspath(candidate).replace('\\', '/')
        if os.path.isdir(candidate) and not normalized.endswith('/system/tabs'):
            return os.path.abspath(candidate)

    resolved = os.path.abspath(candidates[0] if candidates else base_dir)
    if create:
        os.makedirs(resolved, exist_ok=True)
    return resolved


def personalization_get_base_dir(create: bool = False) -> str:
    config = personalization_load_module_config()
    base_dir = config.get('base_dir', PERSONALIZATION_DEFAULT_CONFIG['base_dir']).strip()
    return personalization_resolve_base_dir(base_dir, config.get('_config_dir') or os.path.dirname(personalization_get_config_path()), create=create)


def personalization_sanitize_conf_value(value: str, default: str = '') -> str:
    cleaned = (value or '').replace('\r', ' ').replace('\n', ' ').strip()
    return cleaned or default


def personalization_sanitize_segment(value: str, default: str) -> str:
    cleaned = personalization_sanitize_conf_value(value, default)
    cleaned = cleaned.replace('/', '-').replace('\\', '-')
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' .')
    if cleaned in {'', '.', '..'}:
        return default
    return cleaned[:120]


def personalization_safe_join(base_dir: str, *parts: str) -> str:
    base_dir = os.path.abspath(base_dir)
    candidate = os.path.abspath(os.path.join(base_dir, *parts))
    if os.path.commonpath([base_dir, candidate]) != base_dir:
        raise ValueError('Invalid path')
    return candidate


def personalization_read_service_file(filepath: str, base_dir: str) -> dict:
    data = {
        'id': '',
        'nom': 'Inconnu',
        'url': '#',
        'icone': '❓',
        'categorie': 'Autre',
    }

    if not os.path.isfile(filepath):
        return data

    data['categorie'] = os.path.basename(os.path.dirname(filepath))
    data['id'] = os.path.relpath(filepath, base_dir)

    with open(filepath, 'r', encoding='utf-8') as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip().lower()
            value = value.strip()

            if key in {'name', 'nom'}:
                data['nom'] = value
            elif key in {'icones', 'icone', 'icon', 'src', 'logo'}:
                data['icone'] = value
            elif key in {'url', 'lien'}:
                data['url'] = value

    return data


def personalization_write_service_file(
    category: str,
    name: str,
    url: str,
    icon: str,
    *,
    previous_rel_path: Optional[str] = None,
) -> str:
    base_dir = personalization_get_base_dir(create=True)
    safe_category = personalization_sanitize_segment(category, 'Autre')
    safe_name = personalization_sanitize_segment(name, 'Service')
    filename = safe_name if safe_name.endswith('.conf') else f'{safe_name}.conf'

    folder = personalization_safe_join(base_dir, safe_category)
    os.makedirs(folder, exist_ok=True)
    filepath = personalization_safe_join(folder, filename)

    if previous_rel_path:
        old_path = personalization_safe_join(base_dir, previous_rel_path)
        if os.path.isfile(old_path) and os.path.abspath(old_path) != os.path.abspath(filepath):
            os.remove(old_path)
            old_folder = os.path.dirname(old_path)
            if os.path.isdir(old_folder) and not os.listdir(old_folder):
                os.rmdir(old_folder)

    with open(filepath, 'w', encoding='utf-8') as handle:
        handle.write(
            f"name={personalization_sanitize_conf_value(name, 'Service')}\n"
            f"icone={personalization_sanitize_conf_value(icon, '🌐')}\n"
            f"url={personalization_sanitize_conf_value(url, '#')}\n"
        )

    return filepath


def personalization_get_config() -> dict:
    config = personalization_load_module_config()
    return {
        'base_dir': config.get('base_dir', PERSONALIZATION_DEFAULT_CONFIG['base_dir']),
        'titre_tab': config.get('titre_tab', PERSONALIZATION_DEFAULT_CONFIG['titre_tab']),
        'titre_logo': config.get('titre_logo', PERSONALIZATION_DEFAULT_CONFIG['titre_logo']),
        'nav_icons': config.get('nav_icons', PERSONALIZATION_DEFAULT_CONFIG['nav_icons']),
        'hostname': personalization_get_hostname(),
    }




# --------------------------------------------------
# MENU GLOBAL : source fichier/dossiers dans ../conf/menu
# --------------------------------------------------
# Nouveau moteur : system.conf garde seulement path_menu_root.
# La sidebar garde le même rendu, mais les entrées viennent de :
#   conf/menu/menu.conf                 -> catégories principales
#   conf/menu/<categorie>/*.conf        -> entrées du groupe
# Format catégorie dans conf/menu/menu.conf :
#   NomDossier=emoji-ou-chemin-png
#   NomDossier_url=/route-optionnelle
# Format entrée dans conf/menu/<categorie>/*.conf : nom=... / url=... / icons=...
SYSTEM_MENU_CONF_NAME = 'menu.conf'
SYSTEM_MENU_ENTRY_EXT = '.conf'
SYSTEM_MENU_ICON_KEYS = ('icons', 'icone', 'icon')
SYSTEM_MENU_DEFAULT_CATEGORY_OVERRIDES = {}
# Ancien menu intégré Python supprimé : la seule source par défaut est
# le dossier default_menu_sidebar. Si ce dossier est absent ou incomplet,
# aucun ancien fallback Python ne complète le menu.
SYSTEM_MENU_CACHE_LOCK = threading.RLock()
SYSTEM_MENU_ITEMS_CACHE: Optional[List[Dict[str, Any]]] = None


def system_menu_clone_items(items: List[Dict[str, Any]] | None) -> List[Dict[str, Any]]:
    return copy.deepcopy(items or [])


def system_menu_clear_cache() -> None:
    global SYSTEM_MENU_ITEMS_CACHE
    with SYSTEM_MENU_CACHE_LOCK:
        SYSTEM_MENU_ITEMS_CACHE = None


def system_menu_set_cache(items: List[Dict[str, Any]]) -> None:
    global SYSTEM_MENU_ITEMS_CACHE
    with SYSTEM_MENU_CACHE_LOCK:
        SYSTEM_MENU_ITEMS_CACHE = system_menu_clone_items(items)

# Accueil est déjà une entrée fixe dans templates/menu.html.
# On ne la stocke pas dans conf/menu, sinon elle apparaît deux fois
# ou peut empêcher la régénération propre du menu par défaut.
SYSTEM_MENU_INTERNAL_CATEGORY_SLUGS = {'accueil'}

# Anciennes entrées VM retirées du menu par défaut.
# On les filtre aussi à la lecture pour nettoyer les menus déjà générés
# sans dépendre d'une suppression manuelle dans system_parts/default_menu.
SYSTEM_MENU_VM_PARENT_SLUGS = {'vm', 'virtuel_machine', 'virtual_machine'}
SYSTEM_MENU_VM_REMOVED_CHILD_SLUGS = {'console', 'peripheriques', 'xml', 'settings', 'reglages'}
SYSTEM_MENU_VM_REMOVED_CHILD_URLS = {'/vm/console', '/vms/details', '/vms/xml', '/vm/settings', '/vms/settings'}


def system_menu_slug(value: str, fallback: str = 'item') -> str:
    value = str(value or '').strip().lower()
    value = value.replace('é', 'e').replace('è', 'e').replace('ê', 'e').replace('ë', 'e')
    value = value.replace('à', 'a').replace('â', 'a').replace('ä', 'a')
    value = value.replace('î', 'i').replace('ï', 'i')
    value = value.replace('ô', 'o').replace('ö', 'o')
    value = value.replace('ù', 'u').replace('û', 'u').replace('ü', 'u')
    value = value.replace('ç', 'c')
    value = re.sub(r'[^a-z0-9]+', '_', value).strip('_')
    return value or fallback


def system_menu_fs_name(value: str, fallback: str = 'menu') -> str:
    """Nom de dossier/fichier lisible mais sûr pour la configuration menu."""
    raw = str(value or '').strip() or fallback
    raw = raw.replace('/', ' ').replace('\\', ' ')
    raw = re.sub(r'\s+', ' ', raw).strip()
    raw = re.sub(r'[^A-Za-z0-9À-ÖØ-öø-ÿ_.() -]+', '_', raw).strip(' ._')
    return raw or fallback


def system_menu_is_internal_category(folder: str) -> bool:
    return system_menu_slug(folder, '') in SYSTEM_MENU_INTERNAL_CATEGORY_SLUGS


def system_menu_strip_file_prefix(filename: str) -> str:
    base = os.path.splitext(os.path.basename(str(filename or '')))[0]
    return re.sub(r'^\d+[_ -]+', '', base).strip() or base


def system_menu_dom_id(parent_id: str, item_id: str = '') -> str:
    base = f"{parent_id}__{item_id}" if item_id else str(parent_id or '')
    return re.sub(r'[^A-Za-z0-9_-]+', '_', base)


def system_menu_split_category_value(value: str) -> Tuple[str, str, str]:
    parts = [part.strip() for part in str(value or '').split('|')]
    icon = parts[0] if len(parts) >= 1 and parts[0] else '🌐'
    url = parts[1] if len(parts) >= 2 and parts[1] else '#'
    label = parts[2] if len(parts) >= 3 and parts[2] else ''
    return icon, url, label


def system_menu_format_category_value(icon: str, url: str = '#', label: str = '') -> str:
    icon = personalization_sanitize_conf_value(icon, '🌐')
    url = personalization_sanitize_conf_value(url, '#') or '#'
    label = personalization_sanitize_conf_value(label, '')
    if label:
        return f'{icon}|{url}|{label}'
    if url and url != '#':
        return f'{icon}|{url}'
    return icon


def system_menu_normalize_item(raw: Dict[str, Any], index: int = 0) -> Dict[str, Any]:
    label = str(raw.get('label') or raw.get('nom') or raw.get('name') or 'Menu').strip()
    item_id = system_menu_slug(str(raw.get('id') or label), f'menu_{index:02d}')
    icon_value = str(raw.get('icon') or raw.get('icone') or raw.get('icons') or '🌐').strip() or '🌐'
    url_value = str(raw.get('url') or raw.get('lien') or '#').strip() or '#'
    item = {
        'id': item_id,
        'safe_id': system_menu_dom_id(item_id),
        'label': label,
        'nom': label,
        'icon': icon_value,
        'icone': icon_value,
        'icons': icon_value,
        'url': url_value,
        'lien': url_value,
        'order': int(raw.get('order') or index * 10),
        'children': [],
        '_folder': raw.get('_folder') or raw.get('folder') or system_menu_fs_name(label, item_id),
        'top_menu_enabled': menu_top_is_url_enabled(url_value),
    }
    for child_index, child in enumerate(raw.get('children') or [], start=1):
        child_label = str(child.get('label') or child.get('nom') or child.get('name') or 'Sous-menu').strip()
        child_id = system_menu_slug(str(child.get('id') or child_label), f'{item_id}_child_{child_index:02d}')
        child_icon = str(child.get('icon') or child.get('icone') or child.get('icons') or '↳').strip() or '↳'
        child_url = str(child.get('url') or child.get('lien') or '#').strip() or '#'
        child_item = {
            'id': child_id,
            'safe_id': system_menu_dom_id(item_id, child_id),
            'parent_id': item_id,
            'label': child_label,
            'nom': child_label,
            'icon': child_icon,
            'icone': child_icon,
            'icons': child_icon,
            'url': child_url,
            'lien': child_url,
            'order': int(child.get('order') or child_index * 10),
            '_file': child.get('_file') or child.get('file') or '',
            'kind': 'command',
            'is_direct': False,
            '_is_direct': False,
            'top_menu_enabled': menu_top_is_url_enabled(child_url),
        }
        item['children'].append(child_item)
    item['children'].sort(key=lambda x: (int(x.get('order') or 0), str(x.get('label') or '').lower()))

    raw_kind = str(raw.get('kind') or raw.get('type') or raw.get('item_type') or '').strip().lower()
    has_direct_url = bool(url_value and url_value != '#')
    is_direct = (
        raw_kind in {'command', 'direct', 'shortcut', 'raccourci'}
        or bool(raw.get('is_direct') or raw.get('_is_direct'))
        or (has_direct_url and not item['children'])
    )
    item['kind'] = 'command' if is_direct else 'folder'
    item['is_direct'] = is_direct
    item['_is_direct'] = is_direct
    return item


def system_menu_default_items() -> List[Dict[str, Any]]:
    """Ancien menu latéral intégré supprimé.

    La source de vérité du menu par défaut est uniquement le dossier
    default_menu_sidebar. Si ce dossier n’existe pas, on retourne un menu vide
    au lieu de reconstruire un ancien menu codé en dur.
    """
    return []

def system_menu_is_removed_vm_child(parent_id: str, child: Dict[str, Any]) -> bool:
    """True si l'entrée fait partie des anciennes lignes VM retirées."""
    parent_slug = system_menu_slug(parent_id, '')
    if parent_slug not in SYSTEM_MENU_VM_PARENT_SLUGS:
        return False

    raw_file = str(child.get('_file') or child.get('file') or '').strip()
    candidates = {
        system_menu_slug(child.get('id') or '', ''),
        system_menu_slug(child.get('label') or child.get('nom') or child.get('name') or '', ''),
        system_menu_slug(system_menu_strip_file_prefix(raw_file), ''),
    }
    child_url = str(child.get('url') or child.get('lien') or '').strip()
    return bool(candidates & SYSTEM_MENU_VM_REMOVED_CHILD_SLUGS) or child_url in SYSTEM_MENU_VM_REMOVED_CHILD_URLS


def system_menu_cleanup_removed_vm_entries(root: str) -> bool:
    """Nettoie les anciens fichiers VM dans un menu déjà généré.

    La source par défaut peut venir du dossier embarqué system_parts/default_menu ;
    ce nettoyage garde donc la règle dans le Python générateur et évite de devoir
    supprimer les .conf à la main.
    """
    changed = False
    root = os.path.abspath(str(root or ''))
    if not root or not os.path.isdir(root):
        return False

    try:
        folders = [name for name in os.listdir(root) if os.path.isdir(os.path.join(root, name))]
    except Exception:
        return False

    for folder in folders:
        folder_slug = system_menu_slug(folder, '')
        if folder_slug not in SYSTEM_MENU_VM_PARENT_SLUGS:
            continue
        folder_path = os.path.join(root, folder)
        try:
            filenames = [name for name in os.listdir(folder_path) if name.lower().endswith(SYSTEM_MENU_ENTRY_EXT)]
        except Exception:
            continue
        for filename in filenames:
            path = os.path.join(folder_path, filename)
            data = system_menu_read_kv_file(path)
            child = {
                'id': system_menu_slug(system_menu_strip_file_prefix(filename), ''),
                'label': data.get('nom') or data.get('name') or system_menu_strip_file_prefix(filename),
                'url': data.get('url') or data.get('lien') or '',
                '_file': filename,
            }
            if not system_menu_is_removed_vm_child(folder_slug, child):
                continue
            try:
                os.unlink(path)
                changed = True
            except Exception:
                pass
    return changed


def system_menu_prune_removed_routes(menu_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    removed_urls = {'/system/system', '/disk/mount', '/build/scripts'}
    cleaned: List[Dict[str, Any]] = []
    for raw_item in menu_items or []:
        item = dict(raw_item)
        item_id = str(item.get('id') or '').strip().lower()
        if str(item.get('url') or '').strip() == '/system/system':
            item['url'] = '/system/info'
            item['lien'] = '/system/info'
        if str(item.get('url') or '').strip() == '/disk/mount':
            item['url'] = '/disk/maintenance'
            item['lien'] = '/disk/maintenance'
        if item_id == 'storage' and str(item.get('url') or item.get('lien') or '').strip() == '/disk':
            item['url'] = '/disk/main'
            item['lien'] = '/disk/main'

        children = []
        for raw_child in item.get('children') or []:
            child = dict(raw_child)
            child_id = str(child.get('id') or '').strip().lower()
            child_url = str(child.get('url') or child.get('lien') or '').strip()
            if child_url in removed_urls:
                continue
            if item_id == 'system' and child_id in {'general', 'system'}:
                continue
            if item_id == 'storage' and child_id in {'mount', 'format', 'formatage'}:
                continue
            if item_id == 'storage' and child_id in {'disk', 'general', 'disks', 'main'} and child_url == '/disk':
                child['url'] = '/disk/main'
                child['lien'] = '/disk/main'
            if item_id == 'build' and child_id in {'scripts'}:
                continue
            # Ne plus filtrer les pages Docker qui étaient auparavant déportées
            # dans le menu cranté fixe. Le menu cranté est maintenant
            # personnalisable, donc Stacks / ENV / Images / LAN Docker peuvent
            # revenir librement dans le menu latéral si leurs fichiers .conf
            # existent dans conf/menu/Docker/.
            if system_menu_is_removed_vm_child(item_id, child):
                continue
            children.append(child)
        item['children'] = sorted(children, key=lambda c: int(c.get('order') or 999))
        cleaned.append(item)
    return cleaned


def system_menu_config_path() -> str:
    return personalization_get_config_path()


def system_menu_root_path() -> str:
    raw = str(CONF.get('path_menu_root') or CONF.get('PATH_MENU_ROOT') or '').strip()
    if not raw:
        raw = '../conf/menu'
    raw = os.path.expanduser(os.path.expandvars(raw))
    if os.path.isabs(raw):
        return os.path.abspath(raw)
    conf_dir = loaded_config_dir or os.path.dirname(os.path.abspath(system_menu_config_path()))
    return os.path.abspath(os.path.join(conf_dir, raw))


def system_menu_ensure_conf_key() -> None:
    config_path = system_menu_config_path()
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    key = 'path_menu_root'
    if not os.path.exists(config_path):
        ensure_system_conf_file(config_path)
    try:
        with open(config_path, 'r', encoding='utf-8', errors='replace') as handle:
            lines = handle.readlines()
    except Exception:
        lines = []
    changed = False

    # Nettoyage automatique des anciens gros blocs MENU_xx dans system.conf.
    # La source officielle est maintenant le dossier path_menu_root.
    start_marker = '# BEGIN_SYSTEM_GLOBAL_MENU'
    end_marker = '# END_SYSTEM_GLOBAL_MENU'
    start_idx = None
    end_idx = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped == start_marker:
            start_idx = idx
        elif stripped == end_marker and start_idx is not None:
            end_idx = idx
            break
    if start_idx is not None and end_idx is not None and end_idx >= start_idx:
        # On retire aussi le titre/commentaire juste au-dessus quand il correspond au vieux bloc.
        remove_start = start_idx
        probe = start_idx - 1
        while probe >= 0 and (not lines[probe].strip() or lines[probe].lstrip().startswith('#')):
            remove_start = probe
            if lines[probe].strip() == '# END_SYSTEM_PERSONALIZATION_MENU':
                remove_start = probe + 1
                break
            if 'Personnalisation générale' in lines[probe]:
                remove_start = probe + 1
                break
            probe -= 1
        replacement = [
            '\n',
            '# Menu latéral : source détaillée dans path_menu_root.\n',
            '# Le dossier ../conf/menu est généré automatiquement si absent.\n',
        ]
        lines = lines[:remove_start] + replacement + lines[end_idx + 1:]
        changed = True

    has_key = any(line.strip().lower().startswith(key + '=') for line in lines if '=' in line)
    if not has_key:
        insert_line = f'{key}=../conf/menu\n'
        end_marker = '# END_SYSTEM_PERSONALIZATION_MENU'
        for idx, line in enumerate(lines):
            if line.strip() == end_marker:
                lines.insert(idx, insert_line)
                break
        else:
            if lines and lines[-1].strip():
                lines.append('\n')
            lines.append('# Menu latéral\n')
            lines.append(insert_line)
        CONF[key] = '../conf/menu'
        changed = True

    if changed:
        with open(config_path, 'w', encoding='utf-8') as handle:
            handle.writelines(lines)


def system_menu_read_kv_file(path: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                data[key.strip().lower()] = value.strip()
    except Exception:
        pass
    return data


def system_menu_write_kv_file(path: str, data: Dict[str, str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write('# Entrée du menu Yoleo\n')
        handle.write(f"nom={personalization_sanitize_conf_value(data.get('nom') or data.get('label') or 'Menu', 'Menu')}\n")
        handle.write(f"url={personalization_sanitize_conf_value(data.get('url') or '#', '#')}\n")
        handle.write(f"icons={personalization_sanitize_conf_value(data.get('icons') or data.get('icone') or data.get('icon') or '🌐', '🌐')}\n")
    system_menu_clear_cache()


def system_menu_value_looks_like_url(value: str) -> bool:
    value = str(value or '').strip()
    return value.startswith(('/', '#', 'http://', 'https://'))


def system_menu_category_lines() -> List[Tuple[str, str]]:
    """Lit conf/menu/menu.conf.

    Format officiel simple :
        Système=🖥️
        Système_url=

    Tolérance : si une ancienne valeur pipe existe encore
    (Système=🖥️|/system), elle est relue sans casser le menu.
    Tolérance aussi pour la coquille Fichiers=/file_manager quand
    Fichiers=📂 existe déjà : la deuxième ligne devient Fichiers_url.
    """
    root = system_menu_root_path()
    menu_conf = os.path.join(root, SYSTEM_MENU_CONF_NAME)
    order: List[str] = []
    icons: Dict[str, str] = {}
    urls: Dict[str, str] = {}
    labels: Dict[str, str] = {}
    if not os.path.exists(menu_conf):
        return []
    try:
        with open(menu_conf, 'r', encoding='utf-8', errors='replace') as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip()
                if not key:
                    continue

                is_url_key = key.lower().endswith('_url')
                folder = key[:-4].strip() if is_url_key else key
                if not folder or system_menu_is_internal_category(folder):
                    continue
                if folder not in order:
                    order.append(folder)

                if is_url_key:
                    urls[folder] = value
                    continue

                # Correction douce d'une faute de frappe du type :
                # Fichiers=📂 puis Fichiers=/file_manager.
                if folder in icons and system_menu_value_looks_like_url(value) and folder not in urls:
                    urls[folder] = value
                    continue

                icon, old_url, old_label = system_menu_split_category_value(value)
                icons[folder] = icon
                if old_url and old_url != '#' and folder not in urls:
                    urls[folder] = old_url
                if old_label:
                    labels[folder] = old_label
    except Exception:
        return []

    rows: List[Tuple[str, str]] = []
    for folder in order:
        if system_menu_is_internal_category(folder):
            continue
        icon = icons.get(folder, '🌐') or '🌐'
        url = urls.get(folder, '')
        label = labels.get(folder, '')
        rows.append((folder, system_menu_format_category_value(icon, url, label)))
    return rows


def system_menu_write_category_lines(rows: List[Tuple[str, str]]) -> None:
    root = system_menu_root_path()
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, SYSTEM_MENU_CONF_NAME)
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write('# Catégories principales du menu Yoleo\n')
        handle.write('# Format : Nom=emoji-ou-chemin-png\n')
        handle.write('#          Nom_url=/route-optionnelle\n')
        handle.write('# Accueil est codé en dur dans menu.html : ne pas l’ajouter ici.\n')
        for folder, value in rows:
            folder = system_menu_fs_name(folder, 'Menu')
            if not folder or system_menu_is_internal_category(folder):
                continue
            icon, url, _label = system_menu_split_category_value(value)
            if url == '#':
                url = ''
            handle.write(f'{folder}={icon}\n')
            handle.write(f'{folder}_url={url}\n')
    system_menu_clear_cache()


def system_menu_cleanup_internal_categories(root: str) -> bool:
    """Retire les catégories fixes du fichier menu.conf.

    Exemple : Accueil est codé en dur dans menu.html. Il ne doit donc pas
    exister dans conf/menu/menu.conf, sinon il peut être affiché deux fois
    ou bloquer la régénération propre du menu par défaut.
    """
    changed = False
    menu_conf = os.path.join(root, SYSTEM_MENU_CONF_NAME)
    if os.path.exists(menu_conf):
        kept_lines: List[str] = []
        try:
            with open(menu_conf, 'r', encoding='utf-8', errors='replace') as handle:
                for raw in handle:
                    stripped = raw.strip()
                    if stripped and not stripped.startswith('#') and '=' in stripped:
                        key = stripped.split('=', 1)[0].strip()
                        base_key = key[:-4].strip() if key.lower().endswith('_url') else key
                        if system_menu_is_internal_category(base_key):
                            changed = True
                            continue
                    kept_lines.append(raw)
        except Exception:
            kept_lines = []
        if changed:
            with open(menu_conf, 'w', encoding='utf-8') as handle:
                handle.writelines(kept_lines)

    # Accueil ne doit jamais exister dans conf/menu : il est fixe dans menu.html.
    try:
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if os.path.isdir(path) and system_menu_is_internal_category(name):
                shutil.rmtree(path)
                changed = True
    except Exception:
        pass
    return changed



def system_menu_cleanup_empty_direct_category_folders(root: str, rows: Optional[List[Tuple[str, str]]] = None) -> bool:
    """Supprime les dossiers vides des catégories qui sont de simples liens directs.

    Exemple : Fichiers pointe directement vers /file_manager. Le dossier
    conf/menu/Fichiers n'apporte rien tant qu'il ne contient aucune entrée.
    Si l'utilisateur y ajoute plus tard des .conf, on le conserve.
    """
    changed = False
    if rows is None:
        rows = system_menu_category_lines()
    for folder, raw_value in rows or []:
        _icon, url, _label = system_menu_split_category_value(raw_value)
        if not str(url or '').strip() or str(url or '').strip() == '#':
            continue
        folder_path = os.path.join(root, folder)
        if not os.path.isdir(folder_path):
            continue
        try:
            entries = [name for name in os.listdir(folder_path) if not name.startswith('.')]
        except Exception:
            continue
        if entries:
            continue
        try:
            os.rmdir(folder_path)
            changed = True
        except Exception:
            pass
    return changed

def system_menu_packaged_default_root() -> str:
    """Arborescence menu par défaut embarquée avec l'application.

    Cette source remplace l'ancien gros défaut Python : elle correspond au
    menu.zip fourni le 2026-06-08. Elle n'est utilisée que lorsque le menu
    utilisateur n'existe pas encore ou qu'il est inutilisable.
    """
    return os.path.join(PERSONALIZATION_MODULE_DIR, 'default_menu_sidebar')


def system_menu_clear_directory(path: str) -> None:
    """Vide prudemment un dossier avant d'y copier le menu par défaut."""
    path = os.path.abspath(path or '')
    if not path or path in {'/', os.path.expanduser('~')}:
        raise RuntimeError(f'Chemin menu dangereux : {path}')
    os.makedirs(path, exist_ok=True)
    for name in os.listdir(path):
        target = os.path.join(path, name)
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target)
        else:
            os.unlink(target)


def system_menu_copy_packaged_default_tree(root: str) -> bool:
    """Copie le menu par défaut depuis default_menu_sidebar.

    Retourne True si la copie a été faite. Retourne False si la source
    default_menu_sidebar n’est pas disponible. Dans ce cas, il n’y a plus
    d’ancien fallback Python : le menu reste vide.
    """
    source = system_menu_packaged_default_root()
    source_conf = os.path.join(source, SYSTEM_MENU_CONF_NAME)
    if not os.path.isdir(source) or not os.path.exists(source_conf):
        return False

    root = os.path.abspath(root)
    source = os.path.abspath(source)
    if root == source or root.startswith(source + os.sep):
        raise RuntimeError('Le dossier menu cible ne peut pas être dans la source par défaut.')

    # Copie brute du menu embarqué : ne pas renommer, ne pas corriger,
    # ne pas nettoyer les entrées. Le menu par défaut doit rester exactement
    # identique au menu.zip fourni par l'utilisateur.
    system_menu_clear_directory(root)
    shutil.copytree(source, root, dirs_exist_ok=True)
    return True


def system_menu_seed_default_tree(root: str) -> None:
    """Écrit l’arborescence menu par défaut depuis default_menu_sidebar uniquement."""
    os.makedirs(root, exist_ok=True)

    # Une seule source de vérité pour le menu par défaut :
    # le dossier default_menu_sidebar. Aucun tableau Python ne complète
    # les entrées manquantes.
    system_menu_copy_packaged_default_tree(root)

def system_menu_init_tree_if_missing() -> bool:
    """Initialise ../conf/menu uniquement depuis default_menu_sidebar si nécessaire."""
    system_menu_ensure_conf_key()
    root = system_menu_root_path()
    root_existed = os.path.isdir(root)
    os.makedirs(root, exist_ok=True)

    menu_conf = os.path.join(root, SYSTEM_MENU_CONF_NAME)
    rows = system_menu_category_lines()

    # Cas normal : menu déjà personnalisé, on ne le complète plus et on ne
    # rajoute plus les anciennes entrées Python.
    if root_existed and os.path.exists(menu_conf) and rows:
        return False

    # Initialisation uniquement depuis default_menu_sidebar. Si ce dossier
    # n’existe pas, le menu reste vide.
    before = os.path.exists(menu_conf)
    system_menu_seed_default_tree(root)
    created = (not before) and os.path.exists(menu_conf)
    if created:
        system_menu_clear_cache()
    return created

def system_menu_load_items_from_tree() -> List[Dict[str, Any]]:
    root = system_menu_root_path()
    rows = system_menu_category_lines()
    if not rows:
        return []
    items: List[Dict[str, Any]] = []
    for index, (folder, raw_value) in enumerate(rows, start=1):
        icon, url, display_label = system_menu_split_category_value(raw_value)
        label = display_label or folder
        folder_path = os.path.join(root, folder)
        children: List[Dict[str, Any]] = []
        if os.path.isdir(folder_path):
            files = [name for name in os.listdir(folder_path) if name.lower().endswith(SYSTEM_MENU_ENTRY_EXT)]
            files.sort(key=lambda name: (int(re.match(r'^(\d+)', name).group(1)) if re.match(r'^(\d+)', name) else 9999, name.lower()))
            for child_index, filename in enumerate(files, start=1):
                path = os.path.join(folder_path, filename)
                data = system_menu_read_kv_file(path)
                child_label = data.get('nom') or data.get('name') or system_menu_strip_file_prefix(filename)
                child_icon = ''
                for key in SYSTEM_MENU_ICON_KEYS:
                    if data.get(key):
                        child_icon = data[key]
                        break
                child_url = data.get('url') or data.get('lien') or '#'
                children.append({
                    'id': system_menu_slug(system_menu_strip_file_prefix(filename), f'child_{child_index:02d}'),
                    'label': child_label,
                    'icon': child_icon or '↳',
                    'url': child_url,
                    'order': child_index * 10,
                    '_file': filename,
                })
        items.append(system_menu_normalize_item({
            'id': system_menu_slug(folder, f'menu_{index:02d}'),
            'label': label,
            'icon': icon,
            'url': url,
            'order': index * 10,
            'children': children,
            '_folder': folder,
        }, index))
    return items


def system_menu_load_items(force_reload: bool = False) -> List[Dict[str, Any]]:
    global SYSTEM_MENU_ITEMS_CACHE
    try:
        with SYSTEM_MENU_CACHE_LOCK:
            if SYSTEM_MENU_ITEMS_CACHE is not None and not force_reload:
                return system_menu_clone_items(SYSTEM_MENU_ITEMS_CACHE)

            system_menu_init_tree_if_missing()
            items = system_menu_load_items_from_tree()
            SYSTEM_MENU_ITEMS_CACHE = system_menu_clone_items(items)
            return system_menu_clone_items(SYSTEM_MENU_ITEMS_CACHE)
    except Exception as exc:
        print(f"⚠️ Lecture du menu dossier impossible : {exc}")
    return []


def system_menu_find_parent(menu_items: List[Dict[str, Any]], parent_id: str) -> Optional[Dict[str, Any]]:
    parent_id = system_menu_slug(parent_id, '')
    for item in menu_items:
        if item.get('id') == parent_id:
            return item
    return None


def system_menu_find_child(parent: Dict[str, Any], child_id: str) -> Optional[Dict[str, Any]]:
    child_id = system_menu_slug(child_id, '')
    for child in parent.get('children') or []:
        if child.get('id') == child_id:
            return child
    return None


def system_menu_next_child_filename(folder_path: str, label: str) -> str:
    existing = [name for name in os.listdir(folder_path) if name.lower().endswith(SYSTEM_MENU_ENTRY_EXT)] if os.path.isdir(folder_path) else []
    nums = []
    for name in existing:
        m = re.match(r'^(\d+)', name)
        if m:
            try:
                nums.append(int(m.group(1)))
            except Exception:
                pass
    next_num = (max(nums) + 1) if nums else (len(existing) + 1)
    return f'{next_num:02d}_{system_menu_fs_name(label, f"entree_{next_num}")}.conf'


def system_menu_upsert_item(
    *,
    item_id: str,
    parent_id: str,
    label: str,
    url: str,
    icon: str,
    order: str = '',
    item_type: str = '',
    top_menu: Any = None,
) -> Tuple[bool, str]:
    del order  # Le nouvel arbre menu utilise l'ordre naturel des dossiers/fichiers.
    system_menu_init_tree_if_missing()
    root = system_menu_root_path()
    label = personalization_sanitize_conf_value(label, 'Menu')
    url = personalization_sanitize_conf_value(url, '#') or '#'
    icon = personalization_sanitize_conf_value(icon, '🌐') or '🌐'
    item_id = system_menu_slug(item_id or label, 'menu')
    parent_id = system_menu_slug(parent_id, '')
    item_type = str(item_type or '').strip().lower()
    is_root_command = (not parent_id) and item_type in {'command', 'direct', 'shortcut', 'raccourci'}

    menu_items = system_menu_load_items_from_tree()
    if parent_id:
        parent = system_menu_find_parent(menu_items, parent_id)
        if not parent:
            return False, 'Catégorie parent introuvable.'
        folder = parent.get('_folder') or system_menu_fs_name(parent.get('label') or parent_id, parent_id)
        folder_path = os.path.join(root, folder)
        os.makedirs(folder_path, exist_ok=True)
        current = system_menu_find_child(parent, item_id)
        old_file = current.get('_file') if current else ''
        old_path = os.path.join(folder_path, old_file) if old_file else ''
        if old_file:
            m = re.match(r'^(\d+[_ -]+)', old_file)
            prefix = m.group(1) if m else ''
            filename = f'{prefix}{system_menu_fs_name(label, item_id)}.conf'
        else:
            filename = system_menu_next_child_filename(folder_path, label)
        new_path = os.path.join(folder_path, filename)
        if old_path and old_path != new_path and os.path.exists(old_path):
            try:
                os.replace(old_path, new_path)
            except OSError:
                new_path = old_path
        system_menu_write_kv_file(new_path, {'nom': label, 'url': url, 'icons': icon})
        if top_menu is not None:
            menu_top_set_for_menu_item(label, url, icon, top_menu)
        return True, 'Entrée sauvegardée.'

    # Entrée directe à la racine du menu.
    # Elle vit uniquement dans conf/menu/menu.conf avec Nom=icône + Nom_url=/route,
    # sans dossier conf/menu/<Nom>. C'est le cas typique de Fichiers -> /file_manager.
    current = system_menu_find_parent(menu_items, item_id)
    rows = system_menu_category_lines()
    old_folder = current.get('_folder') if current else ''
    new_folder_base = system_menu_fs_name(label, item_id)
    new_folder = new_folder_base
    used = {folder.lower() for folder, _ in rows if folder != old_folder}
    n = 2
    while new_folder.lower() in used:
        new_folder = f'{new_folder_base}_{n}'
        n += 1

    if is_root_command:
        found = False
        new_rows: List[Tuple[str, str]] = []
        for folder, value in rows:
            if folder == old_folder or (not old_folder and system_menu_slug(folder) == item_id):
                new_rows.append((new_folder, system_menu_format_category_value(icon, url, label if label != new_folder else '')))
                found = True
            else:
                new_rows.append((folder, value))
        if not found:
            new_rows.append((new_folder, system_menu_format_category_value(icon, url, label if label != new_folder else '')))
        system_menu_write_category_lines(new_rows)
        system_menu_cleanup_empty_direct_category_folders(root, new_rows)
        if top_menu is not None:
            menu_top_set_for_menu_item(label, url, icon, top_menu)
        return True, 'Commande racine sauvegardée.'

    # Catégorie principale / dossier.
    if old_folder and old_folder != new_folder:
        old_path = os.path.join(root, old_folder)
        new_path = os.path.join(root, new_folder)
        if os.path.isdir(old_path) and not os.path.exists(new_path):
            os.rename(old_path, new_path)
    else:
        os.makedirs(os.path.join(root, new_folder), exist_ok=True)
    found = False
    new_rows: List[Tuple[str, str]] = []
    for folder, value in rows:
        if folder == old_folder or (not old_folder and system_menu_slug(folder) == item_id):
            new_rows.append((new_folder, system_menu_format_category_value(icon, url, label if label != new_folder else '')))
            found = True
        else:
            new_rows.append((folder, value))
    if not found:
        new_rows.append((new_folder, system_menu_format_category_value(icon, url, label if label != new_folder else '')))
    system_menu_write_category_lines(new_rows)
    if top_menu is not None:
        menu_top_set_for_menu_item(label, url, icon, top_menu)
    return True, 'Catégorie sauvegardée.'


def system_menu_delete_item(item_id: str, parent_id: str = '') -> Tuple[bool, str]:
    system_menu_init_tree_if_missing()
    root = system_menu_root_path()
    menu_items = system_menu_load_items_from_tree()
    item_id = system_menu_slug(item_id, '')
    parent_id = system_menu_slug(parent_id, '')
    if not item_id:
        return False, 'Entrée manquante.'
    if parent_id:
        parent = system_menu_find_parent(menu_items, parent_id)
        if not parent:
            return False, 'Catégorie parent introuvable.'
        child = system_menu_find_child(parent, item_id)
        if not child:
            return False, 'Sous-catégorie introuvable.'
        folder = parent.get('_folder') or system_menu_fs_name(parent.get('label') or parent_id, parent_id)
        file_name = child.get('_file') or ''
        if file_name:
            try:
                os.remove(os.path.join(root, folder, file_name))
                system_menu_clear_cache()
            except FileNotFoundError:
                pass
        return True, 'Entrée supprimée.'

    parent = system_menu_find_parent(menu_items, item_id)
    if not parent:
        return False, 'Catégorie introuvable.'
    folder = parent.get('_folder') or system_menu_fs_name(parent.get('label') or item_id, item_id)
    rows = [(f, v) for f, v in system_menu_category_lines() if f != folder]
    system_menu_write_category_lines(rows)
    folder_path = os.path.join(root, folder)
    if os.path.isdir(folder_path):
        shutil.rmtree(folder_path)
        system_menu_clear_cache()
    return True, 'Catégorie supprimée.'


def system_menu_sorted_entry_files(folder_path: str) -> List[str]:
    if not os.path.isdir(folder_path):
        return []
    files = [name for name in os.listdir(folder_path) if name.lower().endswith(SYSTEM_MENU_ENTRY_EXT)]
    files.sort(key=lambda name: (int(re.match(r'^(\d+)', name).group(1)) if re.match(r'^(\d+)', name) else 9999, name.lower()))
    return files


def system_menu_reindex_entry_files(folder_path: str, ordered_files: List[str]) -> None:
    os.makedirs(folder_path, exist_ok=True)
    temp_names: List[Tuple[str, str]] = []
    token = str(int(time.time() * 1000))

    for idx, filename in enumerate(ordered_files, start=1):
        old_path = os.path.join(folder_path, filename)
        if not os.path.exists(old_path):
            continue
        tmp_name = f'.yoleo-order-{token}-{idx:03d}.tmp'
        tmp_path = os.path.join(folder_path, tmp_name)
        os.replace(old_path, tmp_path)
        temp_names.append((tmp_name, filename))

    used: set[str] = set()
    for idx, (tmp_name, original_name) in enumerate(temp_names, start=1):
        base = system_menu_fs_name(system_menu_strip_file_prefix(original_name), f'entree_{idx}')
        final_name = f'{idx:02d}_{base}{SYSTEM_MENU_ENTRY_EXT}'
        suffix = 2
        while final_name.lower() in used:
            final_name = f'{idx:02d}_{base}_{suffix}{SYSTEM_MENU_ENTRY_EXT}'
            suffix += 1
        used.add(final_name.lower())
        os.replace(os.path.join(folder_path, tmp_name), os.path.join(folder_path, final_name))
    system_menu_clear_cache()


def system_menu_move_item(*, item_id: str, parent_id: str = '', direction: str = '') -> Tuple[bool, str]:
    system_menu_init_tree_if_missing()
    root = system_menu_root_path()
    item_id = system_menu_slug(item_id, '')
    parent_id = system_menu_slug(parent_id, '')
    step = -1 if str(direction).strip().lower() in ('-1', 'up', 'haut', 'monter') else 1

    if not item_id:
        return False, 'Entrée manquante.'

    if parent_id:
        parent = system_menu_find_parent(system_menu_load_items_from_tree(), parent_id)
        if not parent:
            return False, 'Catégorie parent introuvable.'
        folder = parent.get('_folder') or system_menu_fs_name(parent.get('label') or parent_id, parent_id)
        folder_path = os.path.join(root, folder)
        files = system_menu_sorted_entry_files(folder_path)
        current_index = -1
        for idx, filename in enumerate(files):
            if system_menu_slug(system_menu_strip_file_prefix(filename), '') == item_id:
                current_index = idx
                break
        if current_index < 0:
            return False, 'Sous-catégorie introuvable.'
        new_index = current_index + step
        if new_index < 0 or new_index >= len(files):
            return True, 'Ordre déjà en limite.'
        files[current_index], files[new_index] = files[new_index], files[current_index]
        system_menu_reindex_entry_files(folder_path, files)
        return True, 'Ordre du sous-menu mis à jour.'

    rows = system_menu_category_lines()
    current_index = -1
    for idx, (folder, _value) in enumerate(rows):
        if system_menu_slug(folder, '') == item_id:
            current_index = idx
            break
    if current_index < 0:
        return False, 'Catégorie introuvable.'
    new_index = current_index + step
    if new_index < 0 or new_index >= len(rows):
        return True, 'Ordre déjà en limite.'
    rows[current_index], rows[new_index] = rows[new_index], rows[current_index]
    system_menu_write_category_lines(rows)
    return True, 'Ordre des catégories mis à jour.'


@system_bp.app_context_processor
def system_inject_navigation_context():
    """Alimente menu.html avec le menu global stocké dans conf/menu."""
    config = personalization_get_config()
    menu_items = system_menu_load_items()
    topbar_config = top_config_load()
    topbar_actions = top_config_load_actions(topbar_config)
    menu_top_current = menu_top_find_section_for_path(request.path, include_disabled=True)
    return {
        'system_dashboard_config': config,
        'system_global_menu': menu_items,
        'topbar_config': topbar_config,
        'topbar_items': top_config_load_items(topbar_config),
        'topbar_actions': topbar_actions,
        'topbar_action_map': top_config_action_map(topbar_actions),
        'menu_top_config_path': menu_top_get_path(),
        'menu_top_sections': menu_top_load_sections(),
        'menu_top_current': menu_top_current,
    }


try:
    if system_menu_init_tree_if_missing():
        print(f"✅ Menu Yoleo créé dans : {system_menu_root_path()}")
except Exception as exc:
    print(f"⚠️ Initialisation du menu dossier impossible : {exc}")


try:
    top_config_load(force_reload=True)
    menu_top_load_sections(force_reload=True)
    system_menu_load_items(force_reload=True)
    print("✅ Caches menus chargés en mémoire (topbar, menu haut, menu gauche).")
except Exception as exc:
    print(f"⚠️ Préchargement des caches menus impossible : {exc}")


# --------------------------------------------------
# DISK TOP : montages affichés sur la page d'accueil
# --------------------------------------------------
def disk_top_get_path() -> str:
    env_path = os.environ.get("DISK_TOP_CONF", "").strip()
    if env_path:
        return os.path.abspath(os.path.expanduser(os.path.expandvars(env_path)))

    roots = _project_root_candidates()
    candidates = [os.path.join(root, "conf", "disk_top.conf") for root in roots]
    candidates.extend([nas_conf_file("disk_top.conf"), "disk_top.conf"])
    for candidate in _unique_existing_order(candidates):
        if os.path.exists(candidate):
            return os.path.abspath(candidate)

    root = roots[0] if roots else os.path.dirname(PERSONALIZATION_MODULE_DIR)
    return os.path.abspath(os.path.join(root, "conf", "disk_top.conf"))


def disk_top_normalize_path(path: str) -> str:
    value = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path or "").strip())))
    if value == ".":
        value = ""
    return value


def disk_top_load_config() -> Dict[str, Any]:
    path = disk_top_get_path()
    mounts: List[str] = []
    usages: List[str] = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or line.startswith(";") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip().lower()
                    normalized = disk_top_normalize_path(value.strip().strip('"'))
                    if not normalized:
                        continue
                    if key.startswith("mount") or key.startswith("path"):
                        if normalized not in mounts:
                            mounts.append(normalized)
                    elif key.startswith(("usage", "space", "watch", "monitor")):
                        if normalized not in usages:
                            usages.append(normalized)
        except Exception as exc:
            print(f"❌ Erreur lecture disk_top.conf : {exc}")
    return {"path": path, "mounts": mounts, "usages": usages}


def disk_top_save_config(paths: List[str], usage_paths: List[str] | None = None) -> str:
    config_path = disk_top_get_path()
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    clean: List[str] = []
    for raw in paths or []:
        value = disk_top_normalize_path(raw)
        if not value or value in clean:
            continue
        clean.append(value)
    clean_usage: List[str] = []
    if usage_paths is None:
        usage_paths = disk_top_load_config().get("usages") or []
    for raw in usage_paths or []:
        value = disk_top_normalize_path(raw)
        if not value or value in clean_usage:
            continue
        clean_usage.append(value)

    lines = [
        "# ============================================================\n",
        "# Montages affichés sur l'accueil Yoleo\n",
        "# Le contrôle vérifie que chaque chemin est un vrai point de montage.\n",
        "# Ce fichier ne monte rien et ne modifie pas fstab.\n",
        "# ============================================================\n",
        "\n",
    ]
    for idx, path in enumerate(clean, start=1):
        lines.append(f"Mount.{idx}={path}\n")
    if clean_usage:
        lines.append("\n")
    for idx, path in enumerate(clean_usage, start=1):
        lines.append(f"Usage.{idx}={path}\n")
    with open(config_path, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    return config_path


def disk_top_findmnt_map() -> Dict[str, Dict[str, str]]:
    mounts: Dict[str, Dict[str, str]] = {}
    code, output = run_cmd(["findmnt", "-rn", "-o", "TARGET,SOURCE,FSTYPE"], timeout=8)
    if code != 0:
        return mounts
    for raw in (output or "").splitlines():
        parts = raw.split(None, 2)
        if not parts:
            continue
        target = disk_top_normalize_path(parts[0])
        if not target:
            continue
        mounts[target] = {
            "path": target,
            "source": parts[1] if len(parts) > 1 else "—",
            "fstype": parts[2] if len(parts) > 2 else "—",
        }
    return mounts


def disk_top_collect_candidates() -> Dict[str, Any]:
    config = disk_top_load_config()
    selected = set(config.get("mounts") or [])
    usage_selected = set(config.get("usages") or [])
    findmnt = disk_top_findmnt_map()
    paths = set(selected) | set(usage_selected)

    for target in findmnt:
        if target.startswith(("/mnt", "/srv", "/media")):
            paths.add(target)

    # Dossiers NAS classiques, même non montés : utile pour détecter /mnt/cache devenu simple dossier.
    for base in ("/mnt", "/media", "/srv"):
        if os.path.isdir(base):
            try:
                for name in sorted(os.listdir(base)):
                    child = os.path.join(base, name)
                    if os.path.isdir(child):
                        paths.add(os.path.abspath(child))
            except Exception:
                pass
    for common in ("/mnt/cache", "/mnt/user", "/mnt/disk1", "/mnt/disk2", "/mnt/disk3", "/mnt/disk4"):
        if os.path.exists(common) or common in selected:
            paths.add(common)

    rows = []
    for path in sorted(paths):
        info = findmnt.get(path) or {}
        exists = os.path.exists(path)
        is_mount = bool(info) or os.path.ismount(path)
        try:
            has_local_entries = exists and os.path.isdir(path) and (not is_mount) and bool(os.listdir(path))
        except Exception:
            has_local_entries = False
        if is_mount:
            status = "ok"
            label = "OK"
        elif exists:
            status = "folder_with_data" if has_local_entries else "folder"
            label = "Dossier local" if has_local_entries else "Dossier"
        else:
            status = "missing"
            label = "Absent"
        rows.append({
            "path": path,
            "selected": path in selected,
            "usage_selected": path in usage_selected,
            "exists": exists,
            "is_mount": is_mount,
            "source": info.get("source") or "—",
            "fstype": info.get("fstype") or "—",
            "status": status,
            "status_label": label,
        })
    return {"path": disk_top_get_path(), "rows": rows, "selected": sorted(selected), "usage_selected": sorted(usage_selected)}


# --------------------------------------------------
# CONFIGURATION MOBILE / PWA : mobile.conf
# --------------------------------------------------
MOBILE_CONFIG_DEFAULTS = {
    "SHORT_NAME": "Yoleo",
    "NAME": "Yoleo Nas OS",
    "DESCRIPTION": "Interface NAS Debian",
    "START_URL": "/index",
    "DISPLAY": "standalone",
    "BACKGROUND_COLOR": "#000000",
    "THEME_COLOR": "#000000",
    "ICON_SRC": "/static/logo.png",
}

MOBILE_CONFIG_KEYS = [
    "SHORT_NAME",
    "NAME",
    "DESCRIPTION",
    "START_URL",
    "DISPLAY",
    "BACKGROUND_COLOR",
    "THEME_COLOR",
    "ICON_SRC",
]


def mobile_config_get_path() -> str:
    env_path = os.environ.get("MOBILE_CONF", "").strip()
    if env_path:
        return os.path.abspath(os.path.expanduser(os.path.expandvars(env_path)))

    roots = _project_root_candidates()
    candidates = [os.path.join(root, "conf", "mobile.conf") for root in roots]
    candidates.extend([nas_conf_file("mobile.conf"), "mobile.conf"])
    for candidate in _unique_existing_order(candidates):
        if os.path.exists(candidate):
            return os.path.abspath(candidate)

    root = roots[0] if roots else os.path.dirname(PERSONALIZATION_MODULE_DIR)
    return os.path.abspath(os.path.join(root, "conf", "mobile.conf"))


def mobile_config_clean_text(value: Any, default: str = "") -> str:
    value = str(value if value is not None else "").strip().replace("\r", " ").replace("\n", " ")
    return value or default


def mobile_config_clean_color(value: Any, default: str = "#000000") -> str:
    value = mobile_config_clean_text(value, default)
    if len(value) == 7 and value.startswith("#") and all(ch in "0123456789abcdefABCDEF" for ch in value[1:]):
        return value.upper()
    return default


def mobile_config_clean_icon(value: Any, default: str = "/static/logo.png") -> str:
    value = mobile_config_clean_text(value, default).replace("\\", "/")
    if value.startswith("static/"):
        value = "/" + value
    if not value.startswith("/static/"):
        return default
    if not value.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico")):
        return default
    return value


def mobile_config_normalize(data: Dict[str, Any] | None = None) -> Dict[str, str]:
    cfg = dict(MOBILE_CONFIG_DEFAULTS)
    for key, value in (data or {}).items():
        clean_key = str(key or "").strip().upper()
        if clean_key in cfg:
            cfg[clean_key] = str(value).strip()

    cfg["SHORT_NAME"] = mobile_config_clean_text(cfg.get("SHORT_NAME"), MOBILE_CONFIG_DEFAULTS["SHORT_NAME"])
    cfg["NAME"] = mobile_config_clean_text(cfg.get("NAME"), MOBILE_CONFIG_DEFAULTS["NAME"])
    cfg["DESCRIPTION"] = mobile_config_clean_text(cfg.get("DESCRIPTION"), MOBILE_CONFIG_DEFAULTS["DESCRIPTION"])
    cfg["START_URL"] = mobile_config_clean_text(cfg.get("START_URL"), MOBILE_CONFIG_DEFAULTS["START_URL"])
    if not cfg["START_URL"].startswith("/"):
        cfg["START_URL"] = MOBILE_CONFIG_DEFAULTS["START_URL"]
    cfg["DISPLAY"] = mobile_config_clean_text(cfg.get("DISPLAY"), MOBILE_CONFIG_DEFAULTS["DISPLAY"]).lower()
    if cfg["DISPLAY"] not in {"standalone", "fullscreen", "minimal-ui", "browser"}:
        cfg["DISPLAY"] = MOBILE_CONFIG_DEFAULTS["DISPLAY"]
    cfg["BACKGROUND_COLOR"] = mobile_config_clean_color(cfg.get("BACKGROUND_COLOR"), MOBILE_CONFIG_DEFAULTS["BACKGROUND_COLOR"])
    cfg["THEME_COLOR"] = mobile_config_clean_color(cfg.get("THEME_COLOR"), MOBILE_CONFIG_DEFAULTS["THEME_COLOR"])
    cfg["ICON_SRC"] = mobile_config_clean_icon(cfg.get("ICON_SRC"), MOBILE_CONFIG_DEFAULTS["ICON_SRC"])
    return cfg


def mobile_config_read(path: str = "") -> Dict[str, str]:
    path = path or mobile_config_get_path()
    data: Dict[str, str] = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or line.startswith(";") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    data[key.strip().upper()] = value.strip()
        except Exception as exc:
            print(f"❌ Erreur lecture mobile.conf : {exc}")
    return mobile_config_normalize(data)


def mobile_config_write(values: Dict[str, Any]) -> str:
    path = mobile_config_get_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cfg = mobile_config_normalize(values)
    lines = [
        "# ============================================================\n",
        "# Configuration mobile / PWA - manifest.json\n",
        "# Créé automatiquement par app.py si absent.\n",
        "# Modifiable depuis Système > Personnalisation > Mobile.\n",
        "# ============================================================\n",
        "\n",
    ]
    for key in MOBILE_CONFIG_KEYS:
        lines.append(f"{key}={cfg[key]}\n")
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    return path


def mobile_config_load() -> Dict[str, str]:
    path = mobile_config_get_path()
    if not os.path.exists(path):
        mobile_config_write(MOBILE_CONFIG_DEFAULTS)
    return mobile_config_read(path)


def mobile_manifest_preview(config: Dict[str, Any] | None = None) -> str:
    cfg = mobile_config_normalize(config or mobile_config_load())
    data = {
        "short_name": cfg["SHORT_NAME"],
        "name": cfg["NAME"],
        "description": cfg["DESCRIPTION"],
        "start_url": cfg["START_URL"],
        "display": cfg["DISPLAY"],
        "background_color": cfg["BACKGROUND_COLOR"],
        "theme_color": cfg["THEME_COLOR"],
        "icons": [
            {"src": cfg["ICON_SRC"], "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": cfg["ICON_SRC"], "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


# --------------------------------------------------
# MENU CLI : editeur de ../conf/menu.conf pour scripts/menu.sh
# --------------------------------------------------
CLI_MENU_CONF_NAME = "menu.conf"
CLI_MENU_ROW_TYPES = {"section", "submenu", "command", "separator", "raw"}


def cli_menu_config_path() -> str:
    return nas_conf_file(CLI_MENU_CONF_NAME)


def cli_menu_sanitize_line(value: Any) -> str:
    return str(value if value is not None else "").replace("\r", " ").replace("\n", " ").strip()


CLI_MENU_DEFAULT_TEXT = """--- Stacks YML ---
Install Stacks = stacks.py --update
,Option Stacks...
,Démarrer les Dockers = stacks.py --start
,Affioche les Stacks = stacks.py --list
,install LAN Ollama = stacks.py --ollama
,Remove Ollama LAN = stacks.py --remove-ollama
-
--- Docker ---
Docker - SAVE tous = docker.py --save
Docker - LOAD tous = docker.py --load
,Option Dockers...
,Docker - SAVE choisir = docker.py --select --save
,Docker - LOAD choisir = docker.py --select --load
,Docker - Liste complète = docker.py --list all
-
--- Backup ---
-
--- Registre ---
Netoyer de Registre = registry.py
-
--- Cache ---
Vider le Cache = cache.py --all
"""


def cli_menu_packaged_default_path() -> str:
    return os.path.join(PERSONALIZATION_MODULE_DIR, "default_menu_cli", CLI_MENU_CONF_NAME)


def cli_menu_default_text() -> str:
    source = cli_menu_packaged_default_path()
    if os.path.exists(source):
        with open(source, "r", encoding="utf-8-sig", errors="replace") as handle:
            return handle.read().rstrip() + "\n"
    return CLI_MENU_DEFAULT_TEXT.rstrip() + "\n"


def cli_menu_ensure_file() -> str:
    path = cli_menu_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(cli_menu_default_text())
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
    return path


def cli_menu_parse_line(raw_line: str, index: int) -> Dict[str, Any]:
    raw = raw_line.rstrip("\n")
    stripped = raw.strip()
    row: Dict[str, Any] = {
        "id": f"cli_{index:04d}",
        "type": "raw",
        "raw": raw,
        "title": "",
        "label": "",
        "command": "",
        "depth": 0,
    }

    if not stripped:
        return row
    if stripped == "-":
        row["type"] = "separator"
        row["raw"] = "-"
        return row
    if stripped.startswith("---") and stripped.endswith("---") and len(stripped) >= 6:
        row["type"] = "section"
        row["title"] = stripped.strip("-").strip()
        return row
    if stripped.startswith(",") and "=" not in raw:
        prefix = re.match(r"^(,+)", raw)
        depth = len(prefix.group(1)) if prefix else 1
        row["type"] = "submenu"
        row["depth"] = min(max(depth, 1), 6)
        row["title"] = raw[depth:].strip()
        return row
    if "=" in raw and not stripped.startswith("#"):
        label, command = raw.split("=", 1)
        prefix = re.match(r"^(,+)", label)
        depth = len(prefix.group(1)) if prefix else 0
        row["type"] = "command"
        row["depth"] = min(max(depth, 0), 6)
        row["label"] = label[depth:].strip() if depth else label.strip()
        row["command"] = command.strip()
        return row
    return row


def cli_menu_format_row(row: Dict[str, Any]) -> str:
    row_type = str(row.get("type") or "raw").strip().lower()
    if row_type not in CLI_MENU_ROW_TYPES:
        row_type = "raw"
    if row_type == "section":
        title = cli_menu_sanitize_line(row.get("title") or row.get("raw") or "Section")
        return f"--- {title or 'Section'} ---"
    if row_type == "submenu":
        depth = max(1, min(6, int(row.get("depth") or 1)))
        title = cli_menu_sanitize_line(row.get("title") or row.get("raw") or "Sous-menu")
        return f"{',' * depth}{title or 'Sous-menu'}"
    if row_type == "command":
        depth = max(0, min(6, int(row.get("depth") or 0)))
        label = cli_menu_sanitize_line(row.get("label") or "Commande")
        command = cli_menu_sanitize_line(row.get("command") or "")
        return f"{',' * depth}{label or 'Commande'} = {command}"
    if row_type == "separator":
        return "-"
    return cli_menu_sanitize_line(row.get("raw") or "")


def cli_menu_load() -> Dict[str, Any]:
    path = cli_menu_ensure_file()
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for index, raw_line in enumerate(handle, start=1):
            rows.append(cli_menu_parse_line(raw_line, index))
    return {
        "path": path,
        "rows": rows,
        "raw_text": "".join(cli_menu_format_row(row) + "\n" for row in rows).rstrip() + ("\n" if rows else ""),
    }


def cli_menu_save_rows(rows: List[Dict[str, Any]]) -> str:
    path = cli_menu_ensure_file()
    ok, message = cli_menu_validate_rows(rows)
    if not ok:
        raise ValueError(message)

    cleaned: List[str] = []
    for raw_row in rows or []:
        if not isinstance(raw_row, dict):
            continue
        line = cli_menu_format_row(raw_row)
        if line.strip() or str(raw_row.get("type") or "").strip().lower() == "raw":
            cleaned.append(line)

    if not cleaned:
        cleaned = cli_menu_default_text().splitlines()

    if os.path.exists(path):
        backup = f"{path}.bak-{time.strftime('%Y%m%d_%H%M%S')}"
        try:
            shutil.copy2(path, backup)
        except Exception:
            pass

    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(cleaned).rstrip() + "\n")
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass
    return path


def cli_menu_validate_rows(rows: List[Dict[str, Any]]) -> Tuple[bool, str]:
    heading_seen: Dict[int, bool] = {0: True}
    visible_index = 0

    for raw_row in rows or []:
        if not isinstance(raw_row, dict):
            continue
        row_type = str(raw_row.get("type") or "raw").strip().lower()
        if row_type not in CLI_MENU_ROW_TYPES:
            row_type = "raw"
        line = cli_menu_format_row(raw_row).strip()
        if not line:
            continue
        visible_index += 1

        if row_type == "section":
            heading_seen = {0: True}
            continue
        if row_type == "submenu":
            depth = max(1, min(6, int(raw_row.get("depth") or 1)))
            if not heading_seen.get(depth - 1):
                return False, f"Ligne {visible_index} : le sous-menu niveau {depth} doit avoir un titre parent au-dessus."
            for key in list(heading_seen):
                if key >= depth:
                    heading_seen.pop(key, None)
            heading_seen[depth] = True
            continue
        if row_type == "command":
            depth = max(0, min(6, int(raw_row.get("depth") or 0)))
            if depth > 0 and not heading_seen.get(depth):
                return False, f"Ligne {visible_index} : la commande niveau {depth} doit être placée sous un sous-menu niveau {depth}."
            continue

    return True, "OK"


def personalization_normalize_subtab(value: str = "") -> str:
    value = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "accueil": "home",
        "index": "home",
        "home": "home",
        "top": "top",
        "bandeau": "top",
        "bandeau_haut": "top",
        "topbar": "top",
        "top_menu": "menu_top",
        "menu_top": "menu_top",
        "menus_top": "menu_top",
        "top_menus": "menu_top",
        "haut_menu": "menu_top",
        "menu_haut": "menu_top",
        "top_menu_editor": "top_menu_editor",
        "menu_top_editor": "top_menu_editor",
        "menu": "menu",
        "menus": "menu",
        "main": "menu",
        "menu_cli": "menu_cli",
        "cli_menu": "menu_cli",
        "cli": "menu_cli",
        "terminal_menu": "menu_cli",
        "meteo": "meteo",
        "météo": "meteo",
        "weather": "meteo",
        "disque": "disk",
        "disk": "disk",
        "montage": "disk",
        "montages": "disk",
        "mount": "disk",
        "mounts": "disk",
        "mobile": "mobile",
        "pwa": "mobile",
        "telephone": "mobile",
        "téléphone": "mobile",
        "app": "mobile",
        "application": "mobile",
        "info": "info",
        "infos": "info",
        "information": "info",
        "informations": "info",
    }
    return aliases.get(value, "menu")


def mdns_normalize_subtab(value: str = "") -> str:
    value = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "": "dashboard",
        "main": "dashboard",
        "mdns": "dashboard",
        "dashboard": "dashboard",
        "overview": "dashboard",
        "status": "dashboard",
        "noms": "config",
        "names": "config",
        "local": "config",
        "locals": "config",
        "config": "config",
        "log": "logs",
        "logs": "logs",
        "journal": "logs",
        "settings": "info",
        "setting": "info",
        "chemins": "info",
        "paths": "info",
        "infos": "info",
        "info": "info",
    }
    return aliases.get(value, "dashboard")


def lan_normalize_subtab(value: str = "") -> str:
    value = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "": "overview",
        "main": "overview",
        "lan": "overview",
        "network": "overview",
        "reseau": "overview",
        "réseau": "overview",
        "overview": "overview",
        "interfaces": "overview",
        "plans": "plans",
        "bridges": "plans",
        "bridge": "plans",
        "apply": "apply",
        "appliquer": "apply",
        "info": "overview",
        "infos": "overview",
        "config": "overview",
        "conf": "overview",
        "settings": "overview",
    }
    return aliases.get(value, "overview")


def personalization_build_dashboard_context(mode: str = "edit", active_subtab: str = "menu") -> Dict[str, Any]:
    active_subtab = personalization_normalize_subtab(active_subtab)
    menu_items = system_menu_load_items()
    top_config = top_config_load()
    return {
        'menu_items': menu_items,
        'config': personalization_get_config(),
        'mode': mode,
        'active_personalization_subtab': active_subtab,
        'home_config': home_config_load(),
        'home_config_path': home_config_get_path(),
        'home_config_labels': HOME_CONFIG_LABELS,
        'home_ssh_config': home_ssh_config_load(),
        'index_top_items': index_top_load_items(),
        'index_top_config_path': index_top_get_path(),
        'top_config': top_config,
        'top_config_path': top_config_get_path(),
        'top_items': top_config_load_items(top_config),
        'top_menu_actions': top_config_load_actions(top_config),
        'mobile_config': mobile_config_load(),
        'mobile_config_path': mobile_config_get_path(),
        'mobile_manifest_preview': mobile_manifest_preview(),
    }


def _render_system_personalization_section(section: str = "menu"):
    """Rendu de la personnalisation dans le template dedie a la sous-route."""
    section = personalization_normalize_subtab(section)
    templates = {
        "menu": "system_personnalisation_menu.html",
        "menu_cli": "system_personnalisation_cli_menu.html",
        "home": "system_personnalisation_home.html",
        "top": "system_personnalisation_top.html",
        "menu_top": "system_personnalisation_menu_top.html",
        "top_menu_editor": "system_personnalisation_top_menu.html",
        "disk": "system_personnalisation_disk.html",
        "mobile": "system_personnalisation_mobile.html",
        "info": "system_personnalisation_info.html",
    }

    context: Dict[str, Any] = {
        "active_personalization_subtab": section,
        "personalization_config": personalization_get_config(),
        "menu_items": [],
        "home_config": {},
        "home_config_path": home_config_get_path(),
        "home_config_labels": HOME_CONFIG_LABELS,
        "home_ssh_config": {},
        "index_top_items": [],
        "index_top_config_path": index_top_get_path(),
        "top_config": {},
        "top_config_path": top_config_get_path(),
        "top_items": [],
        "top_menu_actions": [],
        "session_config": session_config_load(),
        "disk_top_config": {"path": disk_top_get_path(), "mounts": [], "usages": []},
        "disk_top_candidates": {"rows": [], "selected": [], "usage_selected": [], "selected_set": []},
        "mobile_config": {},
        "mobile_config_path": mobile_config_get_path(),
        "mobile_manifest_preview": "",
        "cli_menu": {"rows": [], "path": cli_menu_config_path()},
        "menu_top_config_path": menu_top_editor_get_dir(),
        "menu_top_sections": [],
        "menu_top_groups": [],
        "menu_top_active_folder": request.args.get("folder", "").strip(),
    }

    if section == "top":
        top_config = top_config_load()
        context.update({
            "top_config": top_config,
            "top_items": top_config_load_items(top_config),
            "top_menu_actions": top_config_load_actions(top_config),
            "session_config": session_config_load(),
        })
    elif section in {"menu_top", "top_menu_editor"}:
        sections = menu_top_editor_load_sections()
        context.update({
            "menu_top_sections": sections,
            "menu_top_groups": menu_top_editor_load_groups(sections),
        })
    elif section == "menu_cli":
        context["cli_menu"] = cli_menu_load()
    elif section == "mobile":
        mobile_config = mobile_config_load()
        context.update({
            "mobile_config": mobile_config,
            "mobile_manifest_preview": mobile_manifest_preview(mobile_config),
        })
    elif section in {"home", "disk"}:
        context.update({
            "menu_items": system_menu_load_items(),
            "home_config": home_config_load(),
            "home_ssh_config": home_ssh_config_load(),
            "disk_top_config": disk_top_load_config(),
            "disk_top_candidates": disk_top_collect_candidates(),
        })
        if section == "home":
            context.update({
                "index_top_items": index_top_load_items(),
            })
    elif section == "menu":
        context.update({
            "menu_items": system_menu_load_items(),
            "home_config": home_config_load(),
            "home_ssh_config": home_ssh_config_load(),
        })
    elif section == "info":
        context.update({
            "menu_items": system_menu_load_items(),
            "home_config": home_config_load(),
            "home_ssh_config": home_ssh_config_load(),
        })

    return render_system_tab_template(
        "personalization",
        template_name=templates.get(section, "system_personnalisation.html"),
        **context,
    )


@system_bp.route("/system/personnalisation", methods=['GET', 'POST'])
def system_personalization_page():
    requested_subtab = request.args.get('subtab', '').strip()
    if requested_subtab:
        section = personalization_normalize_subtab(requested_subtab)
        if section != 'menu':
            if section == 'home':
                return redirect(url_for('system_bp.system_personalization_home_route'))
            if section == 'top':
                return redirect(url_for('system_bp.system_personalization_top_route'))
            if section == 'menu_top':
                return redirect(url_for('system_bp.system_personalization_menu_top_route'))
            if section == 'top_menu_editor':
                return redirect(url_for('system_bp.system_personalization_top_menu_route'))
            if section == 'info':
                return redirect(url_for('system_bp.system_personalization_info_route'))
            if section == 'disk':
                return redirect(url_for('system_bp.system_personalization_disk_route'))
            if section == 'mobile':
                return redirect(url_for('system_bp.system_personalization_mobile_route'))
            if section == 'menu_cli':
                return redirect(url_for('system_bp.system_personalization_cli_menu_route'))
            if section == 'meteo':
                return redirect(url_for('meteo_bp.meteo_home'))
    return _render_system_personalization_section('menu')


@system_bp.route("/system/personnalisation/menu")
def system_personalization_menu_route():
    return _render_system_personalization_section('menu')


@system_bp.route("/system/personnalisation/menu-cli")
@system_bp.route("/system/personnalisation/cli-menu")
def system_personalization_cli_menu_route():
    return _render_system_personalization_section('menu_cli')


@system_bp.route("/system/personnalisation/menu-top")
@system_bp.route("/system/personnalisation/menus-top")
@system_bp.route("/system/personnalisation/top-menus")
def system_personalization_menu_top_route():
    return _render_system_personalization_section('menu_top')


@system_bp.route("/system/personnalisation/top-menu", methods=["GET", "POST"])
def system_personalization_top_menu_route():
    if request.method == "POST":
        wants_json = (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or "application/json" in str(request.headers.get("Accept", "")).lower()
        )
        try:
            payload = request.get_json(silent=True) if request.is_json else None
            if payload is None:
                raw_sections = request.form.get("sections", "[]")
                payload = {"sections": json.loads(raw_sections)}
            sections = payload.get("sections") if isinstance(payload, dict) else payload
            if not isinstance(sections, list):
                raise ValueError("Format invalide : liste de sections attendue.")
            path = menu_top_editor_save_sections(sections)
        except Exception as exc:
            message = f"Erreur enregistrement menu haut : {exc}"
            if wants_json:
                return jsonify({"ok": False, "message": message}), 500
            flash(message, "error")
            return redirect(url_for("system_bp.system_personalization_top_menu_route"))

        if wants_json:
            return jsonify({
                "ok": True,
                "message": "Menu haut enregistré.",
                "path": path,
                "sections": menu_top_editor_load_sections(),
                "groups": menu_top_editor_load_groups(),
            })
        flash(f"Menu haut enregistré ({path}).", "success")
        return redirect(url_for("system_bp.system_personalization_top_menu_route"))
    return _render_system_personalization_section('top_menu_editor')


@system_bp.route("/system/personnalisation/menu-cli/demo-url", methods=["POST"])
def system_personalization_cli_menu_demo_url():
    try:
        from terminal import get_config as terminal_get_config
        from terminal import ensure_terminal_url_args, ttyd_url_with_args

        conf = terminal_get_config()
        ok, message = ensure_terminal_url_args(conf)
        conf = terminal_get_config()
        if not ok:
            return jsonify({"ok": False, "message": message}), 500
        return jsonify({"ok": True, "message": message, "url": ttyd_url_with_args(conf, ["menu-demo"])})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@system_bp.route("/system/personnalisation/home")
@system_bp.route("/system/personnalisation/accueil")
def system_personalization_home_route():
    return _render_system_personalization_section('home')


@system_bp.route("/system/personnalisation/top", methods=["GET", "POST"])
@system_bp.route("/system/personnalisation/bandeau", methods=["GET", "POST"])
def system_personalization_top_route():
    if request.method == "POST":
        wants_json = (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or "application/json" in str(request.headers.get("Accept", "")).lower()
        )
        updates: Dict[str, Any] = {}
        for key in TOP_CONFIG_ITEMS:
            updates[f"SHOW_{key}"] = request.form.get(f"SHOW_{key}", "0")
        for key in TOP_MENU_ACTIONS:
            updates[f"MENU_{key}"] = request.form.get(f"MENU_{key}", "0")
        updates["ORDER"] = request.form.get("TOP_ORDER", "")

        try:
            path = top_config_write(updates)
            session_path = ""
            session_minutes = session_config_load().get("minutes", SESSION_CONFIG_DEFAULT_MINUTES)
            if "SESSION_MINUTES" in request.form:
                session_minutes = session_config_clean_minutes(request.form.get("SESSION_MINUTES"))
                session_path = session_config_write(session_minutes)
        except Exception as exc:
            message = f"Erreur enregistrement du bandeau haut : {exc}"
            if wants_json:
                return jsonify({"ok": False, "message": message}), 500
            flash(message, "error")
            return redirect(url_for("system_bp.system_personalization_top_route"))

        if wants_json:
            return jsonify({
                "ok": True,
                "message": "Configuration du bandeau haut enregistrée.",
                "path": path,
                "order": top_config_load().get("ORDER", ""),
                "session_path": session_path,
                "session_minutes": session_minutes,
            })

        flash(f"Configuration du bandeau haut enregistrée ({path}).", "success")
        return redirect(url_for("system_bp.system_personalization_top_route"))
    return _render_system_personalization_section('top')



def _system_personalization_disk_save_response(wants_json: bool = True):
    try:
        paths = request.form.getlist("mount_path")
        usage_paths = request.form.getlist("usage_path")
        custom = request.form.get("custom_mount", "")
        if custom:
            paths.append(custom)
        config_path = disk_top_save_config(paths, usage_paths)
        saved_config = disk_top_load_config()
        selected = saved_config.get("mounts") or []
        usage_selected = saved_config.get("usages") or []
        if wants_json:
            return jsonify({
                "ok": True,
                "message": "Enregistré",
                "path": config_path,
                "selected": selected,
                "usage_selected": usage_selected,
            }), 200
        return redirect(url_for("system_bp.system_personalization_disk_route"))
    except Exception as exc:
        if wants_json:
            return jsonify({
                "ok": False,
                "message": f"Enregistrement impossible : {exc}",
            }), 500
        flash(f"Enregistrement impossible : {exc}", "error")
        return redirect(url_for("system_bp.system_personalization_disk_route"))


@system_bp.route("/system/personnalisation/disque", methods=["GET", "POST"])
@system_bp.route("/system/personnalisation/disk", methods=["GET", "POST"])
def system_personalization_disk_route():
    if request.method == "POST":
        wants_json = (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or "application/json" in (request.headers.get("Accept") or "")
        )
        return _system_personalization_disk_save_response(wants_json=wants_json)
    return _render_system_personalization_section('disk')


@system_bp.route("/system/personnalisation/disque/save", methods=["POST"])
@system_bp.route("/system/personnalisation/disk/save", methods=["POST"])
def system_personalization_disk_save_route():
    return _system_personalization_disk_save_response(wants_json=True)



@system_bp.route("/system/personnalisation/mobile", methods=["GET", "POST"])
@system_bp.route("/system/personnalisation/pwa", methods=["GET", "POST"])
def system_personalization_mobile_route():
    if request.method == "POST":
        values = {key: request.form.get(key, "") for key in MOBILE_CONFIG_KEYS}
        mobile_config_write(values)
        return redirect(url_for("system_bp.system_personalization_mobile_route"))
    return _render_system_personalization_section('mobile')

@system_bp.route("/system/personnalisation/info")
@system_bp.route("/system/personnalisation/infos")
def system_personalization_info_route():
    return _render_system_personalization_section('info')

@system_bp.route('/system/personnalisation/debug_menu', methods=['GET'])
def system_personalization_debug_menu():
    config = personalization_load_module_config()
    menu_items = system_menu_load_items()
    return jsonify({
        'config_path': config.get('_config_path'),
        'loaded_config': loaded_config,
        'menu_count': len(menu_items),
        'menu': menu_items,
    })




def system_personalization_menu_payload(message: str = "OK", ok: bool = True, status: int = 200):
    """Réponse JSON canonique pour l'éditeur du menu personnalisé.

    L'interface ne doit pas deviner l'état du menu après une écriture :
    le serveur renvoie l'arbre relu depuis conf/menu et l'état réel du menu
    cranté de la page courante. Ça évite les doublons et les icônes crantées
    qui restent désynchronisées jusqu'à F5.
    """
    current_path = (
        request.form.get("current_path", "")
        or request.headers.get("X-Current-Path", "")
        or request.referrer
        or ""
    )
    try:
        if current_path.startswith("http://") or current_path.startswith("https://"):
            from urllib.parse import urlparse
            current_path = urlparse(current_path).path or request.path
    except Exception:
        current_path = request.path
    if not str(current_path).startswith("/"):
        current_path = request.path

    return jsonify({
        "ok": bool(ok),
        "message": str(message or ("OK" if ok else "Erreur")),
        "menu_items": system_menu_load_items(),
        "menu_top_sections": menu_top_load_sections(),
        "menu_top_current": menu_top_find_section_for_path(str(current_path), include_disabled=True),
    }), status


def system_personalization_wants_json() -> bool:
    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in str(request.headers.get("Accept", "")).lower()
        or request.form.get("response") == "json"
    )

@system_bp.route('/system/personnalisation/api', methods=['POST'])
def system_personalization_api():
    action = request.form.get('action', '').strip()

    if action == 'update_hostname':
        try:
            hostname = personalization_set_hostname(request.form.get('valeur', ''))
            return jsonify({'ok': True, 'hostname': hostname}), 200
        except ValueError as exc:
            return str(exc), 400
        except RuntimeError as exc:
            return str(exc), 500

    if action == 'update_config':
        key = personalization_normalize_config_key(request.form.get('cle', ''))
        value = personalization_sanitize_conf_value(request.form.get('valeur', ''))
        if key not in PERSONALIZATION_EDITABLE_CONFIG_KEYS:
            return 'Invalid config key', 400
        if not value:
            return 'Empty value', 400

        config = personalization_get_config()
        config[key] = value
        personalization_save_module_config(config)
        return 'OK', 200

    if action == 'update_disk_top_config':
        return _system_personalization_disk_save_response(wants_json=True)

    if action == 'update_mobile_config':
        values = {key: request.form.get(key, "") for key in MOBILE_CONFIG_KEYS}
        try:
            config_path = mobile_config_write(values)
            config = mobile_config_load()
            return jsonify({
                "ok": True,
                "message": "Enregistré",
                "path": config_path,
                "config": config,
                "manifest_preview": mobile_manifest_preview(config),
            }), 200
        except Exception as exc:
            return jsonify({"ok": False, "message": f"Erreur : {exc}"}), 500

    if action == 'update_home_config':
        updates = {}
        for key in HOME_CONFIG_DEFAULTS:
            # En FormData, une checkbox absente veut dire 0.
            updates[key] = request.form.get(key, "0")

        ssh_updates = {}
        current_ssh = home_ssh_config_load()
        for key in HOME_SSH_CONFIG_KEYS:
            if key in request.form:
                ssh_updates[key] = request.form.get(key, "")

        effective_ssh = current_ssh.copy()
        effective_ssh.update(ssh_updates)
        if home_config_bool(updates.get("SHOW_NVIDIA_SSH", "0")):
            missing = home_ssh_required_missing(effective_ssh)
            if missing:
                return "Erreur : vous devez renseigner les réglages SSH GPU avant d’activer GPU SSH.", 400

        if ssh_updates:
            home_update_system_conf_values(ssh_updates)
        path = home_config_write(updates)

        order_value = request.form.get("INDEX_TOP_ORDER", "")
        top_path = ""
        if order_value.strip():
            top_path = index_top_write_order(index_top_split_order(order_value))

        if top_path:
            return f"OK : index.conf mis à jour ({path}) ; index_top.conf mis à jour ({top_path})", 200
        return f"OK : index.conf mis à jour ({path})", 200

    if action == 'update_home_ssh_config':
        updates = {}
        for key in HOME_SSH_CONFIG_KEYS:
            if key in request.form:
                updates[key] = request.form.get(key, "")
        home_update_system_conf_values(updates)
        return "OK : réglages SSH GPU mis à jour dans system.conf", 200

    if action == 'cli_menu_save':
        payload = request.form.get("rows", "[]")
        try:
            rows = json.loads(payload)
        except Exception as exc:
            return f"JSON invalide : {exc}", 400
        if not isinstance(rows, list):
            return "Format invalide : liste attendue.", 400
        try:
            path = cli_menu_save_rows(rows)
        except ValueError as exc:
            return str(exc), 400
        return f"OK : menu CLI sauvegardé ({path})", 200

    if action == 'menu_top_save':
        payload = request.form.get("sections", "[]")
        try:
            sections = json.loads(payload)
        except Exception as exc:
            return f"JSON invalide : {exc}", 400
        if not isinstance(sections, list):
            return "Format invalide : liste attendue.", 400
        path = menu_top_editor_save_sections(sections)
        return f"OK : menu haut sauvegardé ({path})", 200

    if action == 'menu_upsert':
        ok, message = system_menu_upsert_item(
            item_id=request.form.get('item_id', ''),
            parent_id=request.form.get('parent_id', ''),
            label=request.form.get('nom', ''),
            url=request.form.get('url', ''),
            icon=request.form.get('icone', ''),
            order=request.form.get('order', ''),
            item_type=request.form.get('item_type', ''),
            top_menu=request.form.get('top_menu') if 'top_menu' in request.form else None,
        )
        if system_personalization_wants_json():
            return system_personalization_menu_payload(message, ok=ok, status=200 if ok else 400)
        return (message, 200) if ok else (message, 400)

    if action == 'menu_move':
        ok, message = system_menu_move_item(
            item_id=request.form.get('item_id', ''),
            parent_id=request.form.get('parent_id', ''),
            direction=request.form.get('direction', ''),
        )
        if system_personalization_wants_json():
            return system_personalization_menu_payload(message, ok=ok, status=200 if ok else 400)
        return (message, 200) if ok else (message, 400)

    if action == 'menu_delete':
        ok, message = system_menu_delete_item(
            item_id=request.form.get('item_id', ''),
            parent_id=request.form.get('parent_id', ''),
        )
        if system_personalization_wants_json():
            return system_personalization_menu_payload(message, ok=ok, status=200 if ok else 400)
        return (message, 200) if ok else (message, 400)

    return 'Action refusée', 400

def personalization_static_root_candidates() -> List[str]:
    """Dossiers possibles derrière l'URL /static.

    On privilégie la racine du projet (/dockers/static) parce que l'ancien
    system.conf utilisait ../static depuis /dockers/conf/system.conf.
    """
    roots: List[str] = []

    def add(path: str) -> None:
        if not path:
            return
        path = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path))))
        if path not in roots:
            roots.append(path)

    for root in _project_root_candidates():
        add(os.path.join(root, 'static'))
    add(os.path.join(PERSONALIZATION_MODULE_DIR, 'static'))
    add(os.path.join(os.getcwd(), 'static'))
    add('/static')
    return roots


def personalization_get_static_root() -> str:
    candidates = personalization_static_root_candidates()
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return candidates[0] if candidates else os.path.join(PERSONALIZATION_MODULE_DIR, 'static')


@system_bp.route('/system/personnalisation/static-browse', methods=['POST'])
def system_personalization_static_browse():
    root = os.path.realpath(personalization_get_static_root())
    requested = (request.form.get('path', '') or '/static').strip() or '/static'

    rel = ''
    if requested.startswith('/static'):
        rel = requested[len('/static'):].lstrip('/\\')
    elif requested.startswith('/'):
        rel = requested.lstrip('/\\')
    else:
        rel = requested.lstrip('/\\')

    current = os.path.realpath(os.path.join(root, rel))
    try:
        if os.path.commonpath([root, current]) != root:
            current = root
            rel = ''
    except Exception:
        current = root
        rel = ''

    if os.path.isfile(current):
        current = os.path.dirname(current)
        rel = os.path.relpath(current, root)
        if rel == '.':
            rel = ''

    if not os.path.isdir(current):
        current = root
        rel = ''

    folders = []
    files = []
    try:
        for name in os.listdir(current):
            if name.startswith('.'):
                continue
            full = os.path.join(current, name)
            child_rel = os.path.relpath(full, root).replace('\\', '/')
            web_path = '/static/' + child_rel if child_rel != '.' else '/static'
            if os.path.isdir(full):
                folders.append({'name': name, 'path': web_path})
            elif os.path.isfile(full):
                ext = os.path.splitext(name)[1].lower()
                if ext in PERSONALIZATION_STATIC_IMAGE_EXTENSIONS:
                    files.append({'name': name, 'path': web_path})
        folders.sort(key=lambda x: x['name'].lower())
        files.sort(key=lambda x: x['name'].lower())
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc), 'root': root}), 500

    current_rel = os.path.relpath(current, root).replace('\\', '/')
    current_web = '/static' if current_rel == '.' else '/static/' + current_rel
    parent_web = '/static'
    if current_web != '/static':
        parent_rel = os.path.dirname(current_rel)
        parent_web = '/static' if not parent_rel or parent_rel == '.' else '/static/' + parent_rel

    return jsonify({
        'ok': True,
        'root': root,
        'current_path': current_web,
        'parent_path': parent_web,
        'folders': folders,
        'files': files,
    })


@system_bp.route('/system/personnalisation/add', methods=['POST'])
def system_personalization_add():
    item_type = request.form.get('item_type', 'main').strip()
    parent_id = request.form.get('parent_id', '').strip() if item_type == 'child' else ''
    label = request.form.get('nom', 'Menu')
    url = request.form.get('url', '#')
    icon = request.form.get('icone', '🌐')
    order = request.form.get('order', '')
    item_id = request.form.get('item_id', '').strip()

    system_menu_upsert_item(
        item_id=item_id,
        parent_id=parent_id,
        label=label,
        url=url,
        icon=icon,
        order=order,
        item_type=item_type,
    )
    return redirect(url_for('system_bp.system_personalization_page'))
