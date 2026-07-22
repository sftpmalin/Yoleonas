def builds_browse_normalize_path(raw: str) -> str:
    raw = strip_quotes(str(raw or "")).strip()
    if not raw:
        raw = "/"
    raw = os.path.expanduser(os.path.expandvars(raw))
    if not os.path.isabs(raw):
        raw = build_conf_resolve_path(raw)
    return os.path.abspath(os.path.normpath(raw))


def builds_browse_nearest_existing_dir(path: str) -> Tuple[str, str]:
    """Retourne le dossier existant le plus proche.

    Pourquoi : au premier démarrage, les champs peuvent contenir un futur chemin
    comme /dockers/docker_buils ou /dockers/tar qui n'existe pas encore.
    Le navigateur doit alors ouvrir le parent existant au lieu de rester bloqué.
    """
    requested = builds_browse_normalize_path(path)
    current = requested
    while current and current != "/" and not os.path.exists(current):
        parent = os.path.dirname(current.rstrip("/")) or "/"
        if parent == current:
            break
        current = parent
    if not current or not os.path.exists(current) or not os.path.isdir(current):
        current = "/"
    return os.path.abspath(current), requested


def builds_browse_list_dirs(path: str) -> Tuple[bool, Dict[str, object], int]:
    path, requested_path = builds_browse_nearest_existing_dir(path)
    warning = ""
    if requested_path != path:
        warning = f"Le dossier demandé n'existe pas encore : {requested_path}\nAffichage du parent existant : {path}"
    if not os.path.isdir(path):
        return False, {"ok": False, "message": f"Ce chemin n'est pas un dossier : {path}", "path": path}, 400

    dirs: List[Dict[str, str]] = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    name = entry.name
                    if name in {".", ".."}:
                        continue
                    dirs.append({"name": name, "path": os.path.join(path, name)})
                except OSError:
                    continue
    except PermissionError:
        return False, {"ok": False, "message": f"Permission refusée : {path}", "path": path}, 403
    except OSError as exc:
        return False, {"ok": False, "message": f"Impossible de lire le dossier : {path}\n{exc}", "path": path}, 500

    dirs.sort(key=lambda item: item["name"].lower())
    parent = os.path.dirname(path.rstrip("/")) or "/"
    payload = {"ok": True, "path": path, "parent": parent, "dirs": dirs, "requested_path": requested_path}
    if warning:
        payload["message"] = warning
    return True, payload, 200


def builds_safe_new_dir_name(name: str) -> Tuple[bool, str]:
    name = strip_quotes(str(name or "")).strip()
    if not name:
        return False, "Nom de dossier vide."
    if name in {".", ".."}:
        return False, "Nom de dossier interdit."
    if "/" in name or "\\" in name or "\x00" in name:
        return False, "Le nom ne doit pas contenir de slash."
    # On garde les espaces possibles, mais on bloque les noms absurdes.
    if len(name) > 120:
        return False, "Nom de dossier trop long."
    return True, name

def get_config() -> Dict[str, str]:
    created = ensure_builds_conf_file(CONFIG_FILE)
    conf = DEFAULT_CONFIG.copy()
    file_conf = read_config_file(CONFIG_FILE)
    conf.update(file_conf)
    conf["BUILD_CONFIG_PATH"] = CONFIG_FILE
    conf["BUILD_CONFIG_CREATED"] = "1" if created else "0"

    # Les chemins relatifs de builds.conf sont toujours relatifs au dossier conf officiel.
    for key in list(conf.keys()):
        if key in BUILD_CONFIG_PATH_KEYS and conf.get(key):
            conf[key] = build_conf_resolve_path(conf[key])
        elif key in BUILD_CONFIG_CSV_PATH_KEYS and conf.get(key):
            conf[key] = build_conf_resolve_csv_paths(conf[key])

    # Config Registre intégrée : registry.conf n'est plus obligatoire.
    for registry_key in ("REGISTRY_URL", "REGISTRY_USER", "REGISTRY_PASSWORD", "YML_DIR"):
        conf[registry_key] = strip_quotes(conf.get(registry_key, "")).strip()
    if not conf.get("YML_DIR"):
        conf["YML_DIR"] = nas_root_path("yml")
    else:
        conf["YML_DIR"] = build_conf_resolve_path(conf["YML_DIR"])

    # Compat anciennes clés éventuelles.
    if conf.get("BUILDS_DIR") and not file_conf.get("HOST_BUILDS_DIR"):
        conf["HOST_BUILDS_DIR"] = build_conf_resolve_path(conf["BUILDS_DIR"])
    if conf.get("TAR_DIR") and not file_conf.get("HOST_TAR_DIR"):
        conf["HOST_TAR_DIR"] = build_conf_resolve_path(conf["TAR_DIR"])
    if conf.get("REGISTRY_FILE") and not file_conf.get("HOST_REGISTRY_FILE"):
        conf["HOST_REGISTRY_FILE"] = build_conf_resolve_path(conf["REGISTRY_FILE"])
    if conf.get("MODE_FILE") and not file_conf.get("HOST_MODE_FILE"):
        conf["HOST_MODE_FILE"] = build_conf_resolve_path(conf["MODE_FILE"])
    if conf.get("PLATFORMS_FILE") and not file_conf.get("HOST_PLATFORMS_FILE"):
        conf["HOST_PLATFORMS_FILE"] = build_conf_resolve_path(conf["PLATFORMS_FILE"])
    # Cache interne Build : chemin configurable dans builds.conf, compat aliases éventuels.
    if conf.get("CACHE_BUILD_FILE") and not file_conf.get("BUILD_CACHE_FILE"):
        conf["BUILD_CACHE_FILE"] = build_conf_resolve_path(conf["CACHE_BUILD_FILE"])
    if conf.get("CACHE_BUILD") and not file_conf.get("BUILD_CACHE_FILE"):
        conf["BUILD_CACHE_FILE"] = build_conf_resolve_path(conf["CACHE_BUILD"])

    host_builds = build_conf_resolve_path(conf.get("HOST_BUILDS_DIR", DEFAULT_CONFIG["HOST_BUILDS_DIR"])).rstrip("/")
    host_tar = build_conf_resolve_path(conf.get("HOST_TAR_DIR", DEFAULT_CONFIG["HOST_TAR_DIR"])).rstrip("/")
    host_conf = build_conf_resolve_path(conf.get("HOST_CONF_DIR", os.path.dirname(conf.get("HOST_REGISTRY_FILE", "")) or NAS_CONF_DIR)).rstrip("/")
    host_log = build_conf_resolve_path(conf.get("HOST_LOG_DIR", "/var/log/builds")).rstrip("/")

    conf["HOST_BUILDS_DIR"] = host_builds
    conf["HOST_TAR_DIR"] = host_tar
    conf["HOST_CONF_DIR"] = host_conf
    conf["HOST_LOG_DIR"] = host_log
    conf["HOST_REGISTRY_FILE"] = build_conf_resolve_path(conf.get("HOST_REGISTRY_FILE") or os.path.join(host_conf, "registre.conf"))
    conf["HOST_MODE_FILE"] = build_conf_resolve_path(conf.get("HOST_MODE_FILE") or os.path.join(host_conf, "mode.conf"))
    conf["HOST_PLATFORMS_FILE"] = build_conf_resolve_path(conf.get("HOST_PLATFORMS_FILE") or os.path.join(host_conf, "platforms.conf"))
    conf["HOST_REGISTRY_LOGIN_FILE"] = build_conf_resolve_path(conf.get("HOST_REGISTRY_LOGIN_FILE") or os.path.join(host_conf, "registre_login.conf"))
    conf["HOST_REGISTRY_CONFIG_FILE"] = build_conf_resolve_path(conf.get("HOST_REGISTRY_CONFIG_FILE") or os.path.join(host_conf, "builds.conf"))
    conf["BUILD_CACHE_FILE"] = build_conf_resolve_path(conf.get("BUILD_CACHE_FILE") or os.path.join(host_conf, "build.jdom"))

    unified = conf.get("UNIFIED_PATHS", "1").strip().lower() in {"1", "true", "yes", "on"}
    if unified:
        conf["DOCKER_BUILDS_DIR"] = conf["HOST_BUILDS_DIR"]
        conf["DOCKER_TAR_DIR"] = conf["HOST_TAR_DIR"]
        conf["DOCKER_CONF_DIR"] = conf["HOST_CONF_DIR"]
        conf["DOCKER_LOG_DIR"] = conf["HOST_LOG_DIR"]
        conf["DOCKER_REGISTRY_FILE"] = conf["HOST_REGISTRY_FILE"]
        conf["DOCKER_MODE_FILE"] = conf["HOST_MODE_FILE"]
        conf["DOCKER_PLATFORMS_FILE"] = conf["HOST_PLATFORMS_FILE"]
        conf["DOCKER_REGISTRY_LOGIN_FILE"] = conf["HOST_REGISTRY_LOGIN_FILE"]
        conf["DOCKER_REGISTRY_CONFIG_FILE"] = conf["HOST_REGISTRY_CONFIG_FILE"]
    else:
        docker_conf = build_conf_resolve_path(conf.get("DOCKER_CONF_DIR", NAS_CONF_DIR)).rstrip("/")
        docker_tar = build_conf_resolve_path(conf.get("DOCKER_TAR_DIR", nas_root_path("tar"))).rstrip("/")
        conf["DOCKER_BUILDS_DIR"] = build_conf_resolve_path(conf.get("DOCKER_BUILDS_DIR", nas_root_path("docker_buils"))).rstrip("/")
        conf["DOCKER_TAR_DIR"] = docker_tar
        conf["DOCKER_CONF_DIR"] = docker_conf
        conf["DOCKER_LOG_DIR"] = build_conf_resolve_path(conf.get("DOCKER_LOG_DIR", "/var/log/builds")).rstrip("/")
        conf["DOCKER_REGISTRY_FILE"] = build_conf_resolve_path(conf.get("DOCKER_REGISTRY_FILE") or os.path.join(docker_conf, "registre.conf"))
        conf["DOCKER_MODE_FILE"] = build_conf_resolve_path(conf.get("DOCKER_MODE_FILE") or os.path.join(docker_conf, "mode.conf"))
        conf["DOCKER_PLATFORMS_FILE"] = build_conf_resolve_path(conf.get("DOCKER_PLATFORMS_FILE") or os.path.join(docker_conf, "platforms.conf"))
        conf["DOCKER_REGISTRY_LOGIN_FILE"] = build_conf_resolve_path(conf.get("DOCKER_REGISTRY_LOGIN_FILE") or os.path.join(docker_conf, "registre_login.conf"))
        conf["DOCKER_REGISTRY_CONFIG_FILE"] = build_conf_resolve_path(conf.get("DOCKER_REGISTRY_CONFIG_FILE") or os.path.join(docker_conf, "builds.conf"))

    # Adresse du registre : en priorité on lit REGISTRY_URL fusionné dans builds.conf.
    # Un placeholder 192.168.1.xxx:xxxx signifie "configuration initiale requise".
    inline_registry_prefix = "" if is_placeholder_value(conf.get("REGISTRY_URL", "")) else normalize_registry_prefix(conf.get("REGISTRY_URL", ""))
    if inline_registry_prefix:
        conf["REGISTRY_PREFIX"] = inline_registry_prefix
        conf["REGISTRY_PREFIX_SOURCE"] = CONFIG_FILE
    else:
        registry_conf_explicit = bool(
            file_conf.get("DOCKER_REGISTRY_CONFIG_FILE")
            or file_conf.get("HOST_REGISTRY_CONFIG_FILE")
            or file_conf.get("REGISTRY_CONFIG_FILE")
        )
        registry_conf_candidates: List[str] = []
        for candidate in (
            conf.get("DOCKER_REGISTRY_CONFIG_FILE", ""),
            conf.get("HOST_REGISTRY_CONFIG_FILE", ""),
            conf.get("REGISTRY_CONFIG_FILE", ""),
            os.path.join(conf.get("DOCKER_CONF_DIR", NAS_CONF_DIR), "registry.conf"),
            os.path.join(conf.get("HOST_CONF_DIR", NAS_CONF_DIR), "registry.conf"),
            nas_conf_file("registry.conf"),
        ):
            if candidate:
                registry_conf_candidates.append(candidate)
        for candidate in dict.fromkeys(registry_conf_candidates):
            if os.path.exists(candidate):
                prefix = registry_prefix_from_conf_file(candidate)
                if prefix and not is_placeholder_value(prefix):
                    conf["REGISTRY_PREFIX"] = prefix
                    conf["REGISTRY_PREFIX_SOURCE"] = candidate
                    break
        else:
            conf["REGISTRY_PREFIX"] = normalize_registry_prefix(conf.get("REGISTRY_PREFIX", "registry.sftpmalin.com"))
            if not registry_conf_explicit:
                conf.setdefault("REGISTRY_PREFIX_SOURCE", "builds.conf/default")

    if not conf.get("STATE_DIR"):
        conf["STATE_DIR"] = os.path.join(conf["DOCKER_CONF_DIR"], ".save_state")
    else:
        conf["STATE_DIR"] = build_conf_resolve_path(conf["STATE_DIR"])
    if not conf.get("LOCK_FILE"):
        conf["LOCK_FILE"] = "/tmp/flask_builds_python.lock"

    conf["BUILD_SETUP"] = build_setup_status(conf)
    return conf


def q(value: str) -> str:
    return shlex.quote(str(value))


def shjoin(cmd: List[str]) -> str:
    return " ".join(q(part) for part in cmd)


def normalize_item_name(name: Optional[str]) -> str:
    # Compat ancienne base : certaines lignes pouvaient être enregistrées en name.tar=...
    # mais les dossiers de build et les noms logiques doivent rester name.
    name = (name or "").strip()
    if name.endswith(".tar"):
        name = name[:-4]
    return name


def is_valid_name(name: Optional[str]) -> bool:
    return bool(name and VALID_NAME_RE.fullmatch(name))


def same_item_key(left: str, right: str) -> bool:
    return normalize_item_name(left) == normalize_item_name(right)


def normalize_named_map(data: Dict[str, str]) -> Dict[str, str]:
    # Normalise uniquement les clés applicatives. _default reste intact.
    out: Dict[str, str] = {}
    for key, value in data.items():
        if key == "_default":
            out[key] = value
            continue
        clean_key = normalize_item_name(key)
        if clean_key and is_valid_name(clean_key):
            out[clean_key] = value
    return out


def local_read_text(path: str) -> str:
    try:
        if not path or not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except Exception:
        return ""


def local_write_text(path: str, content: str) -> Tuple[bool, str]:
    try:
        parent = os.path.dirname(path.rstrip("/")) or "."
        os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
        return True, ""
    except Exception as exc:
        return False, str(exc)


def write_kv_file_preserve(path: str, updates: Dict[str, str]) -> Tuple[bool, str]:
    current = local_read_text(path)
    lines = current.splitlines()
    seen = set()
    out: List[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw:
            out.append(raw)
            continue
        key = raw.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(raw)
    missing = [key for key in updates if key not in seen]
    if missing and out and out[-1].strip():
        out.append("")
    for key in missing:
        out.append(f"{key}={updates[key]}")
    return local_write_text(path, "\n".join(out).rstrip() + "\n")


def quote_env_value(value: str) -> str:
    value = str(value or "")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def parse_kv(text: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = strip_quotes(value.strip())
        if key:
            result[key] = value
    return result


def update_kv_text(original: str, key: str, value: str) -> str:
    key = normalize_item_name(key)
    value = value.strip()
    lines = (original or "").splitlines()
    output: List[str] = []
    replaced = False

    for raw_line in lines:
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            existing_key = line.split("=", 1)[0].strip()
            # Remplace aussi une ancienne clé name.tar=... par la clé propre name=...
            if same_item_key(existing_key, key):
                if value:
                    output.append(f"{key}={value}")
                replaced = True
                continue
        output.append(raw_line)

    if not replaced and value:
        if output and output[-1].strip():
            output.append("")
        output.append(f"{key}={value}")

    return "\n".join(output).rstrip() + "\n"


def normalize_platforms(value: str) -> str:
    value = strip_quotes(value).strip()
    lower = value.lower().replace(" ", "")
    mapping = {
        "1": "linux/amd64",
        "amd64": "linux/amd64",
        "linux/amd64": "linux/amd64",
        "2": "linux/amd64,linux/arm64",
        "multi": "linux/amd64,linux/arm64",
        "multiarch": "linux/amd64,linux/arm64",
        "linux/amd64,linux/arm64": "linux/amd64,linux/arm64",
        "arm64": "linux/arm64",
        "linux/arm64": "linux/arm64",
    }
    return mapping.get(lower, value or "linux/amd64")


def platform_flags(platforms: str) -> Tuple[bool, bool]:
    normalized = normalize_platforms(platforms).lower()
    return "linux/amd64" in normalized, "linux/arm64" in normalized


def get_platforms_for(conf: Dict[str, str], name: str, platforms: Dict[str, str]) -> str:
    if name in platforms and platforms[name].strip():
        return normalize_platforms(platforms[name])
    if "_default" in platforms and platforms["_default"].strip():
        return normalize_platforms(platforms["_default"])
    return normalize_platforms(conf.get("DEFAULT_PLATFORMS", "linux/amd64"))


def suggested_registry(conf: Dict[str, str], name: str) -> str:
    prefix = normalize_registry_prefix(conf.get("REGISTRY_PREFIX", "registry.sftpmalin.com"))
    return f"{prefix}/{name}:latest" if prefix else ""



def human_size(size: Optional[int]) -> str:
    if size is None or size < 0:
        return "—"
    units = ["o", "Ko", "Mo", "Go", "To"]
    value = float(size)
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024.0
    if unit == "o":
        return f"{int(value)} {unit}"
    return f"{value:.1f} {unit}"


def close_key(name: str, data: Dict[str, str], cutoff: float = 0.78) -> str:
    keys = [k for k in data.keys() if k != "_default"]
    if not name or not keys:
        return ""
    matches = __import__("difflib").get_close_matches(name, keys, n=1, cutoff=cutoff)
    return matches[0] if matches else ""


def list_project_names(builds_dir: str) -> List[str]:
    names: List[str] = []
    if os.path.isdir(builds_dir):
        for name in sorted(os.listdir(builds_dir), key=str.lower):
            path = os.path.join(builds_dir, name)
            if os.path.isdir(path) and is_valid_name(name):
                names.append(name)
    return names


def list_tar_names(tar_dir: str) -> List[str]:
    names: List[str] = []
    if os.path.isdir(tar_dir):
        for filename in sorted(os.listdir(tar_dir), key=str.lower):
            if filename.endswith(".tar") and is_valid_name(filename[:-4]):
                names.append(filename[:-4])
    return names


def inventory_state_value(conf: Dict[str, str], name: str, suffix: str) -> str:
    """Lit l'état local sans dépendre d'un ancien chemin absolu."""
    state_dir = (conf.get("STATE_DIR") or os.path.join(conf.get("DOCKER_CONF_DIR", NAS_CONF_DIR), ".save_state")).rstrip("/")
    tar_dir = conf.get("DOCKER_TAR_DIR", "").rstrip("/")
    candidates = []
    if state_dir:
        candidates.append(os.path.join(state_dir, f"{name}{suffix}"))
    if tar_dir:
        candidates.append(os.path.join(tar_dir, f"{name}{suffix}"))
        candidates.append(os.path.join(tar_dir, f"{name}.tar{suffix}"))
    for path in dict.fromkeys(candidates):
        value = local_read_text(path).strip()
        if value:
            return value.split()[0]
    return ""


def inventory_build_current(conf: Dict[str, str], name: str, context_dir: str, dockerfile_ok: bool, tar_path: str, platforms: str) -> bool:
    """Vrai état Build -> TAR pour ne pas proposer Build quand le TAR est déjà à jour."""
    if not os.path.isdir(context_dir) or not dockerfile_ok:
        return False
    sha_file = f"{tar_path}.sha256"
    if not tar_sha_ok(tar_path, sha_file):
        return False
    try:
        context_hash = hash_context(context_dir)
    except Exception:
        return False
    old_context_hash = inventory_state_value(conf, name, ".context.sha256")
    old_platforms = inventory_state_value(conf, name, ".platforms")

    # Même si l'état plateformes a été invalidé par une modification de base,
    # un contexte modifié ne doit jamais être masqué par un vieux TAR qui a les
    # bonnes architectures.
    if old_context_hash and context_hash != old_context_hash:
        return False
    if old_platforms and platforms != old_platforms:
        return False

    if old_context_hash and old_platforms:
        # Sécurité cache : même si les petits fichiers .save_state disent OK,
        # on vérifie que le TAR contient réellement les plateformes demandées.
        # Sinon un changement de plateforme en base peut laisser un vieux TAR
        # mono-arch affiché comme "À jour".
        try:
            return tar_matches_platforms(tar_path, platforms)
        except Exception:
            return False
    try:
        return tar_matches_platforms(tar_path, platforms)
    except Exception:
        return False


def registry_state_paths(conf: Dict[str, str], name: str) -> Tuple[str, str]:
    state_dir = (conf.get("STATE_DIR") or os.path.join(conf.get("DOCKER_CONF_DIR", NAS_CONF_DIR), ".save_state")).rstrip("/")
    return (
        os.path.join(state_dir, f"{name}.registry.tar.sha256"),
        os.path.join(state_dir, f"{name}.registry.target"),
    )


def registry_import_state_current(conf: Dict[str, str], name: str, target: str, tar_path: str) -> bool:
    """État local après import TAR -> Registre.

    But : après un envoi réussi, le bouton Envoyer ne doit pas réapparaître
    tant que le TAR et la cible registre n'ont pas changé.
    """
    if not target or not os.path.isfile(tar_path):
        return False
    saved_tar_hash = saved_sha_hash(f"{tar_path}.sha256")
    if not saved_tar_hash:
        return False
    state_hash_path, state_target_path = registry_state_paths(conf, name)
    return (
        local_read_text(state_hash_path).strip().split()[0:1] == [saved_tar_hash]
        and local_read_text(state_target_path).strip() == target
    )


def mark_registry_import_state(conf: Dict[str, str], name: str, target: str, tar_path: str) -> None:
    saved_tar_hash = saved_sha_hash(f"{tar_path}.sha256")
    if not saved_tar_hash:
        return
    state_hash_path, state_target_path = registry_state_paths(conf, name)
    local_write_text(state_hash_path, saved_tar_hash + "\n")
    local_write_text(state_target_path, target + "\n")


def clear_registry_import_state(conf: Dict[str, str], names: Optional[Iterable[str]] = None) -> int:
    """Invalide le cache local TAR -> Registre.

    Pourquoi : l'onglet TAR -> Registre masque le bouton Envoyer quand le
    dernier import local correspond au TAR et a la cible registre. Quand on
    vide les tags ou le stockage du registre, ces petits fichiers d'etat ne
    representent plus la realite du registre. On les supprime donc pour forcer
    le prochain refresh a reproposer l'envoi.
    """
    state_dir = (conf.get("STATE_DIR") or os.path.join(conf.get("DOCKER_CONF_DIR", NAS_CONF_DIR), ".save_state")).rstrip("/")
    paths: List[str] = []

    if names is None:
        for pattern in ("*.registry.tar.sha256", "*.registry.target"):
            paths.extend(glob.glob(os.path.join(state_dir, pattern)))
    else:
        for raw_name in names:
            name = normalize_item_name(str(raw_name or ""))
            if not is_valid_name(name):
                continue
            paths.extend(registry_state_paths(conf, name))

    removed = 0
    for path in dict.fromkeys(paths):
        try:
            if path and os.path.isfile(path):
                os.unlink(path)
                removed += 1
        except OSError:
            pass
    return removed



def empty_build_summary(conf: Dict[str, str]) -> Dict[str, object]:
    return {
        "total": 0,
        "projects": 0,
        "dockerfiles": 0,
        "tars": 0,
        "registry": 0,
        "registry_missing": 0,
        "platforms_missing": 0,
        "meta_missing": 0,
        "amd64": 0,
        "arm64": 0,
        "default_platforms": normalize_platforms(conf.get("DEFAULT_PLATFORMS", "linux/amd64")),
    }


def build_inventory_sources(conf: Dict[str, str]) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str], List[str]]:
    registry = normalize_named_map(parse_kv(local_read_text(conf["DOCKER_REGISTRY_FILE"])))
    platforms = normalize_named_map(parse_kv(local_read_text(conf["DOCKER_PLATFORMS_FILE"])))
    modes = normalize_named_map(parse_kv(local_read_text(effective_mode_file(conf))))

    project_names = list_project_names(conf["DOCKER_BUILDS_DIR"])
    tar_names = list_tar_names(conf["DOCKER_TAR_DIR"])
    all_names = (
        set(project_names)
        | set(tar_names)
        | {k for k in registry.keys() if k != "_default" and is_valid_name(k)}
        | {k for k in platforms.keys() if k != "_default" and is_valid_name(k)}
        | {k for k in modes.keys() if k != "_default" and is_valid_name(k)}
    )
    names = []
    used = set()
    for key in registry.keys():
        if key != "_default" and key in all_names and key not in used and is_valid_name(key):
            names.append(key)
            used.add(key)
    names.extend(sorted((name for name in all_names if name not in used), key=str.lower))
    return registry, platforms, modes, names


def build_inventory_item(
    conf: Dict[str, str],
    name: str,
    registry: Dict[str, str],
    platforms: Dict[str, str],
    modes: Dict[str, str],
    check_registry: bool = False,
) -> Dict[str, object]:
    context_dir = os.path.join(conf["DOCKER_BUILDS_DIR"], name)
    has_context = os.path.isdir(context_dir)
    dockerfile = os.path.isfile(os.path.join(context_dir, "Dockerfile")) or os.path.isfile(os.path.join(context_dir, "dockerfile"))
    tar_path = os.path.join(conf["DOCKER_TAR_DIR"], f"{name}.tar")
    sha_path = f"{tar_path}.sha256"
    tar_exists = os.path.isfile(tar_path)
    tar_size = os.path.getsize(tar_path) if tar_exists else None
    tar_mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(tar_path))) if tar_exists else ""
    sha = os.path.isfile(sha_path)

    registry_value = registry.get(name, "").strip()
    registry_suggest = suggested_registry(conf, name)
    raw_platform = platforms.get(name, "").strip()
    effective_platform = get_platforms_for(conf, name, platforms)
    display_platform = raw_platform if raw_platform else (
        effective_platform if conf.get("SHOW_DEFAULT_PLATFORMS_IN_TABLE", "0").strip() in {"1", "true", "yes", "on"} else ""
    )
    amd64, arm64 = platform_flags(effective_platform)

    raw_mode = modes.get(name, "").strip()
    selected_mode = raw_mode if raw_mode else modes.get("_default", "0")
    mode_value = registry_mode_db_value(selected_mode)
    mode_label = registry_mode_label_from_value(mode_value)

    registry_missing = not bool(registry_value)
    platform_missing = not bool(raw_platform)
    meta_missing = registry_missing or platform_missing

    build_current = inventory_build_current(conf, name, context_dir, dockerfile, tar_path, effective_platform)
    registry_current = False
    registry_status_label = "Envoyer"
    can_import = tar_exists and bool(registry_value)
    registry_status_message = ""

    if check_registry and tar_exists and registry_value:
        try:
            status = registry_status_for(conf, name)
            registry_state = str(status.get("state", ""))
            registry_current = registry_state == "current" and not bool(status.get("needs_action"))
            registry_status_label = str(status.get("label") or ("À jour" if registry_current else "Envoyer"))
            can_import = bool(status.get("can_run", True)) and bool(status.get("needs_action", not registry_current))
            registry_status_message = str(status.get("message") or "")
        except Exception as exc:
            registry_current = False
            registry_status_label = "Envoyer"
            can_import = True
            registry_status_message = f"Statut registre non vérifié : {exc}"

    return {
        "name": name,
        "has_context": has_context,
        "dockerfile": dockerfile,
        "context_dir": context_dir,
        "tar_exists": tar_exists,
        "tar_size": tar_size,
        "tar_size_h": human_size(tar_size),
        "tar_mtime": tar_mtime,
        "sha": sha,
        "registry": registry_value,
        "registry_display": registry_value or registry_suggest,
        "registry_suggested": registry_suggest,
        "registry_missing": registry_missing,
        "registry_close_key": close_key(name, registry),
        "platforms": display_platform,
        "platform_effective": effective_platform,
        "platform_suggested": effective_platform,
        "platform_missing": platform_missing,
        "platform_close_key": close_key(name, platforms),
        "amd64": amd64,
        "arm64": arm64,
        "mode_raw": raw_mode,
        "mode_value": mode_value,
        "mode_label": mode_label,
        "mode_close_key": close_key(name, modes),
        "meta_missing": meta_missing,
        "build_current": build_current,
        "registry_current": registry_current,
        "registry_status_label": registry_status_label,
        "registry_status_message": registry_status_message,
        "build_status_label": "À jour" if build_current else "Build",
        "can_build": has_context and dockerfile and not build_current,
        "can_import": can_import and not registry_current,
    }


def summarize_build_inventory(conf: Dict[str, str], builds: List[Dict[str, object]]) -> Dict[str, object]:
    summary = empty_build_summary(conf)
    summary.update({
        "total": len(builds),
        "projects": sum(1 for item in builds if item.get("has_context")),
        "dockerfiles": sum(1 for item in builds if item.get("dockerfile")),
        "tars": sum(1 for item in builds if item.get("tar_exists")),
        "registry": sum(1 for item in builds if item.get("registry")),
        "registry_missing": sum(1 for item in builds if item.get("registry_missing")),
        "platforms_missing": sum(1 for item in builds if item.get("platform_missing")),
        "meta_missing": sum(1 for item in builds if item.get("meta_missing")),
        "amd64": sum(1 for item in builds if item.get("amd64")),
        "arm64": sum(1 for item in builds if item.get("arm64")),
    })
    return summary


def build_inventory_warning(conf: Dict[str, str]) -> Optional[str]:
    if not os.path.isdir(conf["DOCKER_BUILDS_DIR"]):
        return f"Dossier builds introuvable : {conf['DOCKER_BUILDS_DIR']}"
    return None


def build_inventory_scan(
    conf: Dict[str, str],
    check_registry: bool = False,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> Tuple[List[Dict[str, object]], Dict[str, object], Optional[str]]:
    registry, platforms, modes, names = build_inventory_sources(conf)
    builds: List[Dict[str, object]] = []
    total = len(names)
    for idx, name in enumerate(names, start=1):
        if progress_callback:
            progress_callback(idx - 1, total, name)
        builds.append(build_inventory_item(conf, name, registry, platforms, modes, check_registry=check_registry))
        if progress_callback:
            progress_callback(idx, total, name)
    summary = summarize_build_inventory(conf, builds)
    warning = build_inventory_warning(conf)
    return builds, summary, warning


def build_cache_path(conf: Dict[str, str]) -> str:
    return build_conf_resolve_path(conf.get("BUILD_CACHE_FILE") or nas_conf_file("build.jdom"))


def build_cache_now_label() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def build_cache_source_snapshot(conf: Dict[str, str]) -> Dict[str, Dict[str, object]]:
    """Empreinte légère des mini-bases qui alimentent le cache Build.

    Le cache doit rester stable quand on ajoute/supprime des dossiers à la main
    avec FileZilla : ça reste volontairement manuel. Par contre, quand l'UI
    modifie registre/platforms/mode, le cache doit se considérer obsolète et
    se reconstruire automatiquement au prochain affichage.
    """
    snapshot: Dict[str, Dict[str, object]] = {}
    for key in ("DOCKER_REGISTRY_FILE", "DOCKER_PLATFORMS_FILE", "DOCKER_MODE_FILE"):
        path = str(conf.get(key) or "")
        item: Dict[str, object] = {"path": path, "exists": False, "mtime_ns": 0, "size": 0}
        try:
            st = os.stat(path)
            item.update({"exists": True, "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))), "size": int(st.st_size)})
        except OSError:
            pass
        snapshot[key] = item
    return snapshot


def build_cache_sources_changed(conf: Dict[str, str]) -> bool:
    """Dit si les mini-bases ont changé depuis l'écriture de build.jdom."""
    path = build_cache_path(conf)
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    old = data.get("source_snapshot")
    if not isinstance(old, dict):
        # Ancien build.jdom : on rafraîchit une seule fois pour y inscrire
        # l'empreinte des fichiers base.
        return True
    current = build_cache_source_snapshot(conf)
    for key, item in current.items():
        prev = old.get(key) if isinstance(old.get(key), dict) else {}
        if str(prev.get("path") or "") != str(item.get("path") or ""):
            return True
        if bool(prev.get("exists")) != bool(item.get("exists")):
            return True
        if int(prev.get("mtime_ns") or 0) != int(item.get("mtime_ns") or 0):
            return True
        if int(prev.get("size") or 0) != int(item.get("size") or 0):
            return True
    return False


def build_cache_info(conf: Dict[str, str]) -> Dict[str, object]:
    path = build_cache_path(conf)
    info: Dict[str, object] = {
        "path": path,
        "exists": os.path.isfile(path),
        "updated_at": "",
        "updated_at_label": "vide",
        "source": "",
        "total": 0,
    }
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            info["updated_at"] = str(data.get("updated_at") or "")
            info["updated_at_label"] = str(data.get("updated_at") or "vide")
            info["source"] = str(data.get("source") or "")
            info["total"] = int((data.get("summary") or {}).get("total") or 0)
    except Exception:
        if info["exists"]:
            info["updated_at_label"] = "illisible"
    return info


def write_build_inventory_cache(
    conf: Dict[str, str],
    builds: List[Dict[str, object]],
    summary: Dict[str, object],
    warning: Optional[str],
    source: str = "scan",
) -> Tuple[bool, str]:
    path = build_cache_path(conf)
    payload = {
        "version": 1,
        "updated_at": build_cache_now_label(),
        "source": source,
        "conf": {
            "builds_dir": conf.get("DOCKER_BUILDS_DIR", ""),
            "tar_dir": conf.get("DOCKER_TAR_DIR", ""),
            "registry_file": conf.get("DOCKER_REGISTRY_FILE", ""),
            "platforms_file": conf.get("DOCKER_PLATFORMS_FILE", ""),
            "mode_file": effective_mode_file(conf),
        },
        "source_snapshot": build_cache_source_snapshot(conf),
        "builds": builds,
        "summary": summary,
        "warning": warning,
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = f"{path}.{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
        return True, path
    except Exception as exc:
        return False, str(exc)


def read_build_inventory_cache(conf: Dict[str, str]) -> Tuple[Optional[List[Dict[str, object]]], Dict[str, object], Optional[str], Dict[str, object]]:
    path = build_cache_path(conf)
    meta = build_cache_info(conf)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None, empty_build_summary(conf), "Cache Build vide : clique sur Mise à jour du cache pour l'initialiser.", meta
    except Exception as exc:
        return None, empty_build_summary(conf), f"Cache Build illisible : {path} ({exc}). Relance Mise à jour du cache.", meta

    if not isinstance(data, dict) or not isinstance(data.get("builds"), list):
        return None, empty_build_summary(conf), f"Cache Build invalide : {path}. Relance Mise à jour du cache.", meta

    builds = data.get("builds") or []
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else summarize_build_inventory(conf, builds)
    for key, value in empty_build_summary(conf).items():
        summary.setdefault(key, value)
    warning = data.get("warning") if data.get("warning") else None
    return builds, summary, warning, meta


def rebuild_build_inventory_cache(
    conf: Dict[str, str],
    source: str = "manual",
    check_registry: bool = True,
) -> Tuple[bool, str, List[Dict[str, object]], Dict[str, object], Optional[str]]:
    builds, summary, warning = build_inventory_scan(conf, check_registry=check_registry)
    ok, message = write_build_inventory_cache(conf, builds, summary, warning, source=source)
    return ok, message, builds, summary, warning


def refresh_build_cache_silent(conf: Dict[str, str], source: str = "ui-action", check_registry: bool = True) -> bool:
    try:
        ok, _message, _builds, _summary, _warning = rebuild_build_inventory_cache(conf, source=source, check_registry=check_registry)
        return bool(ok)
    except Exception:
        return False


def build_inventory(conf: Dict[str, str]) -> Tuple[List[Dict[str, object]], Dict[str, object], Optional[str]]:
    # Performance : l'interface lit build.jdom et ne rescane pas les dossiers/TAR
    # à chaque affichage. Les changements hors interface sont pris en compte par
    # l'action volontaire « Mise à jour du cache ». Les actions UI, elles,
    # rafraîchissent le cache juste après modification.
    builds, summary, warning, _meta = read_build_inventory_cache(conf)
    if builds is None:
        return [], summary, warning
    return builds, summary, warning

def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def saved_sha_hash(sha_file: str) -> str:
    try:
        with open(sha_file, "r", encoding="utf-8", errors="ignore") as handle:
            first = handle.read().strip().split()
            return first[0] if first else ""
    except Exception:
        return ""


def tar_sha_ok(tar_file: str, sha_file: str) -> bool:
    if not os.path.isfile(tar_file) or not os.path.isfile(sha_file):
        return False
    saved = saved_sha_hash(sha_file)
    if not saved:
        return False
    try:
        return sha256_file(tar_file) == saved
    except Exception:
        return False


def tar_index_json(tar_file: str) -> Optional[dict]:
    try:
        with tarfile.open(tar_file, "r") as tar:
            member = tar.getmember("index.json")
            extracted = tar.extractfile(member)
            if extracted is None:
                return None
            return json.loads(extracted.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _clean_arch_name(value: object) -> str:
    """Normalise une architecture Docker/OCI et ignore les pseudo-plateformes.

    Buildx peut ajouter des manifests d'attestation/provenance avec
    platform=unknown/unknown dans les images multi-arch. Ces entrées ne sont
    pas des plateformes exécutables ; elles ne doivent donc pas rendre
    l'état TAR -> registre obsolète.
    """
    arch = str(value or "").strip().lower()
    if arch in {"", "unknown", "none", "null", "n/a"}:
        return ""
    return arch


def _manifest_is_attestation(manifest: dict) -> bool:
    annotations = (manifest or {}).get("annotations", {}) or {}
    media_type = str((manifest or {}).get("mediaType", "")).lower()
    ref_type = str(annotations.get("vnd.docker.reference.type", "")).lower()
    return ref_type == "attestation-manifest" or "attestation" in media_type


def _oci_blob_member_name(digest: str) -> str:
    """Convertit sha256:xxxx en chemin OCI blobs/sha256/xxxx."""
    value = str(digest or "").strip()
    if not value.startswith("sha256:"):
        return ""
    hex_digest = value.split(":", 1)[1].strip()
    if not hex_digest:
        return ""
    return f"blobs/sha256/{hex_digest}"


def _load_oci_blob_json(tar: tarfile.TarFile, digest: str) -> Optional[dict]:
    member_name = _oci_blob_member_name(digest)
    if not member_name:
        return None
    try:
        member = tar.getmember(member_name)
        extracted = tar.extractfile(member)
        if extracted is None:
            return None
        data = json.loads(extracted.read().decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def arches_from_manifest_payload(data: dict, blob_loader=None, seen: Optional[set] = None) -> set:
    """Extrait les vraies architectures d'un manifest/index Docker ou OCI.

    Les entrées unknown/unknown générées par BuildKit pour la provenance/SBOM
    sont volontairement ignorées. En TAR OCI local, index.json peut pointer vers
    un autre index ou vers des manifests dont l'architecture se trouve seulement
    dans le blob config. On suit donc les digests quand un blob_loader est fourni.
    """
    arches = set()
    if not isinstance(data, dict):
        return arches
    if seen is None:
        seen = set()

    # Certains payloads mono-arch exposent directement platform ou architecture.
    root_platform = data.get("platform", {}) or {}
    arch = _clean_arch_name(root_platform.get("architecture"))
    if arch:
        arches.add(arch)
    arch = _clean_arch_name(data.get("architecture"))
    if arch:
        arches.add(arch)

    # Manifest image OCI/Docker : architecture dans le blob config.
    config = data.get("config", {}) or {}
    config_digest = str(config.get("digest", "")).strip()
    if blob_loader and config_digest and config_digest not in seen:
        seen.add(config_digest)
        config_payload = blob_loader(config_digest)
        if isinstance(config_payload, dict):
            arch = _clean_arch_name(config_payload.get("architecture"))
            if arch:
                arches.add(arch)

    # Index/manifest list : architectures sur les descriptors ou dans les blobs pointés.
    for manifest in data.get("manifests", []) or []:
        if not isinstance(manifest, dict):
            continue
        if _manifest_is_attestation(manifest):
            continue
        platform = manifest.get("platform", {}) or {}
        arch = _clean_arch_name(platform.get("architecture"))
        if arch:
            arches.add(arch)

        digest = str(manifest.get("digest", "")).strip()
        if blob_loader and digest and digest not in seen:
            seen.add(digest)
            nested = blob_loader(digest)
            if isinstance(nested, dict):
                arches.update(arches_from_manifest_payload(nested, blob_loader=blob_loader, seen=seen))

    return arches


def tar_has_arch(tar_file: str, arch: str) -> bool:
    wanted = _clean_arch_name(arch)
    if not wanted:
        return False
    try:
        return wanted in local_tar_arches(tar_file)
    except Exception:
        index = tar_index_json(tar_file)
        if not index:
            return False
        if wanted in arches_from_manifest_payload(index):
            return True
        # Dernier fallback : certains index générés différemment contiennent l'arch plus bas.
        return f'"architecture":"{wanted}"' in json.dumps(index, separators=(",", ":"))


def tar_is_oci(tar_file: str) -> bool:
    try:
        with tarfile.open(tar_file, "r") as tar:
            return "index.json" in tar.getnames()
    except Exception:
        return False


def tar_matches_platforms(tar_file: str, platforms: str) -> bool:
    """Le TAR doit correspondre exactement aux plateformes demandées.

    Ancien comportement : un TAR linux/amd64+linux/arm64 était accepté si la
    base demandait seulement linux/amd64, parce qu'on vérifiait uniquement que
    les architectures demandées étaient présentes. C'était faux pour le cache :
    après avoir réduit une image de deux plateformes vers une seule, Build → TAR
    pouvait afficher "À jour" alors que TAR → Registre disait "Build d'abord".
    """
    if not tar_is_oci(tar_file):
        return False
    desired_arches = platforms_to_arches(platforms)
    if not desired_arches:
        return False
    try:
        actual_arches = local_tar_arches(tar_file)
    except Exception:
        actual_arches = set()
    return actual_arches == desired_arches


CHECK_UPDATES_MARKER_RE = re.compile(r"^\s*#\s*yoleo:check-updates\b", re.IGNORECASE)
FROM_LINE_RE = re.compile(r"^\s*FROM(?:\s+--platform=\S+)?\s+([^\s]+)(?:\s+AS\s+([A-Za-z0-9._-]+))?", re.IGNORECASE)


def dockerfile_has_check_updates_marker(dockerfile: str) -> bool:
    """Active le check distant des FROM pour les Dockerfile wrappers."""
    try:
        with open(dockerfile, "r", encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
                if CHECK_UPDATES_MARKER_RE.search(line):
                    return True
    except OSError:
        return False
    return False


def dockerfile_external_from_images(dockerfile: str) -> List[str]:
    images: List[str] = []
    stages = set()
    try:
        with open(dockerfile, "r", encoding="utf-8-sig", errors="replace") as handle:
            lines = list(handle)
    except OSError:
        return images

    for raw_line in lines:
        match = FROM_LINE_RE.match(raw_line)
        if not match:
            continue
        image = match.group(1).strip()
        alias = (match.group(2) or "").strip().lower()
        image_key = image.lower()
        if image_key != "scratch" and image_key not in stages and "$" not in image and image not in images:
            images.append(image)
        if alias:
            stages.add(alias)
    return images


def docker_manifest_timeout(conf: Dict[str, str]) -> int:
    try:
        return max(5, int(str(conf.get("FROM_CHECK_TIMEOUT", "30")).strip()))
    except Exception:
        return 30


def _docker_manifest_payload(conf: Dict[str, str], image: str) -> Tuple[int, str, str]:
    docker = (conf.get("DOCKER_BIN", "docker") or "docker").strip() or "docker"
    timeout_seconds = docker_manifest_timeout(conf)
    commands = [
        ([docker, "manifest", "inspect", image], "docker manifest inspect"),
        ([docker, "buildx", "imagetools", "inspect", "--raw", image], "docker buildx imagetools inspect"),
    ]
    last_rc = 1
    last_out = ""
    last_label = ""
    for cmd, label in commands:
        try:
            completed = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
            out = completed.stdout or ""
            if completed.returncode == 0 and out.strip():
                return 0, out, label
            last_rc = completed.returncode
            last_out = out
            last_label = label
        except FileNotFoundError:
            last_rc = 127
            last_out = f"Commande introuvable : {cmd[0]}"
            last_label = label
        except subprocess.TimeoutExpired:
            last_rc = 124
            last_out = f"Timeout après {timeout_seconds}s"
            last_label = label
        except Exception as exc:
            last_rc = 1
            last_out = f"Exception Python : {exc}"
            last_label = label
    return last_rc, last_out, last_label


def dockerfile_remote_from_fingerprint(conf: Dict[str, str], dockerfile: str) -> Tuple[bool, str, List[str]]:
    images = dockerfile_external_from_images(dockerfile)
    if not images:
        return False, "", ["Marqueur yoleo:check-updates présent, mais aucun FROM externe lisible."]

    parts: List[str] = []
    lines: List[str] = []
    for image in images:
        rc, payload, method = _docker_manifest_payload(conf, image)
        if rc != 0 or not payload.strip():
            short = " ".join((payload or "").strip().split())[:220]
            lines.append(f"FROM distant impossible : {image} ({method}, rc={rc}) {short}")
            return False, "", lines
        digest = hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()
        parts.append(f"{image}=sha256:{digest}")
        lines.append(f"FROM distant OK : {image} via {method} -> sha256:{digest[:16]}")

    fingerprint = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    lines.append(f"Fingerprint FROM : {fingerprint}")
    return True, fingerprint, lines


def should_skip_file(root: str, filename: str) -> bool:
    if filename in SKIP_CONTEXT_NAMES:
        return True
    _, ext = os.path.splitext(filename)
    if ext in SKIP_CONTEXT_EXT:
        return True
    parts = root.split(os.sep)
    return any(part in SKIP_CONTEXT_DIRS for part in parts)


def hash_context(context_dir: str) -> str:
    # Version Python autonome. Stable dans le temps, sans dépendre de sha256sum/xargs.
    entries: List[Tuple[str, str]] = []
    for root, dirs, files in os.walk(context_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_CONTEXT_DIRS]
        for filename in files:
            if should_skip_file(root, filename):
                continue
            full = os.path.join(root, filename)
            if not os.path.isfile(full):
                continue
            rel = "./" + os.path.relpath(full, context_dir).replace(os.sep, "/")
            entries.append((rel, full))
    entries.sort(key=lambda item: item[0])

    combined = hashlib.sha256()
    for rel, full in entries:
        try:
            file_hash = sha256_file(full)
        except Exception:
            file_hash = "ERROR"
        combined.update(f"{file_hash}  {rel}\n".encode("utf-8"))
    return combined.hexdigest()


def write_sha_file(tar_file: str, sha_file: str, digest: str) -> None:
    with open(sha_file, "w", encoding="utf-8") as handle:
        handle.write(f"{digest}  {tar_file}\n")
    try:
        os.chmod(sha_file, 0o644)
    except OSError:
        pass


def read_env_login_file(path: str) -> Dict[str, str]:
    # Lit le format simple REGISTRY_USER=... REGISTRY_PASS=... sans exécuter le fichier.
    return parse_kv(local_read_text(path))


def registry_host_from_target(target: str) -> str:
    target = (target or "").strip()
    target = target.removeprefix("http://").removeprefix("https://")
    return target.split("/", 1)[0]


def effective_mode_file(conf: Dict[str, str]) -> str:
    """Retourne mode.conf, avec compatibilité mod.conf/mod.txt en secours."""
    mode_file = (
        conf.get("DOCKER_MODE_FILE")
        or conf.get("HOST_MODE_FILE")
        or os.path.join(conf.get("DOCKER_CONF_DIR", NAS_CONF_DIR), "mode.conf")
    )
    mode_file = mode_file.strip()
    if mode_file and os.path.exists(mode_file):
        return mode_file
    if mode_file:
        for alt_name in ("mod.conf", "mod.txt", "mode.txt"):
            alt = os.path.join(os.path.dirname(mode_file), alt_name)
            if os.path.exists(alt):
                return alt
    return mode_file


def normalize_registry_mode(value: str) -> str:
    """Convention : 0 = HTTP local / TLS désactivé, 1 = HTTPS normal."""
    low = str(value or "").strip().lower()
    if low in {"0", "http", "local", "insecure", "disabled", "tls_disabled", "no_tls"}:
        return "http"
    if low in {"1", "https", "secure", "tls", "enabled"}:
        return "https"
    return "https"


def registry_mode_db_value(value: str) -> str:
    """Valeur stockée dans mode.conf : 0 = HTTP, 1 = HTTPS."""
    if not str(value or "").strip():
        return "0"
    return "0" if normalize_registry_mode(value) == "http" else "1"


def registry_mode_label_from_value(value: str) -> str:
    return "HTTP" if registry_mode_db_value(value) == "0" else "HTTPS"


def get_registry_mode_for(conf: Dict[str, str], name: str) -> str:
    data = normalize_named_map(parse_kv(local_read_text(effective_mode_file(conf))))
    clean_name = normalize_item_name(name)
    raw = data.get(clean_name, data.get("_default", "0"))
    return normalize_registry_mode(raw)
