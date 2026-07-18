class StreamLogger:
    def __init__(self, *paths: str):
        self.handles = []
        seen = set()
        for path in paths:
            if not path or path in seen:
                continue
            seen.add(path)
            try:
                log_dir = os.path.dirname(path)
                if log_dir:
                    os.makedirs(log_dir, exist_ok=True)
                self.handles.append(open(path, "a", encoding="utf-8"))
            except Exception:
                pass

    def close(self) -> None:
        for handle in self.handles:
            try:
                handle.close()
            except Exception:
                pass
        self.handles = []

    def raw(self, text: str) -> str:
        for handle in self.handles:
            try:
                handle.write(text)
                handle.flush()
            except Exception:
                pass
        return text

    def line(self, text: str = "") -> str:
        return self.raw(text + "\n")


def stream_shell_process(command: str, logger: StreamLogger, cwd: Optional[str] = None) -> Iterator[str]:
    command = (command or "").strip()
    if not command:
        yield logger.line("❌ Commande vide.")
        return 1
    yield logger.line(f"$ {command}")
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            executable="/bin/bash",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=cwd if cwd and os.path.isdir(cwd) else None,
        )
        assert process.stdout is not None
        for line in process.stdout:
            yield logger.raw(line)
        return process.wait()
    except Exception as exc:
        yield logger.line(f"❌ Exception Python pendant la commande système : {exc}")
        return 1


class OperationLock:
    def __init__(self, path: str):
        self.path = path
        self.handle = None

    def acquire(self) -> Tuple[bool, str]:
        if fcntl is None:
            return True, ""
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self.handle = open(self.path, "w", encoding="utf-8")
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.handle.write(str(os.getpid()))
            self.handle.flush()
            return True, ""
        except BlockingIOError:
            return False, "Une autre opération stacks/système est déjà en cours."
        except Exception as exc:
            return False, str(exc)

    def release(self) -> None:
        if self.handle is None or fcntl is None:
            return
        try:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
        except Exception:
            pass
        self.handle = None


@dataclass
class SystemStackDefinition:
    name: str
    files: List[str]
    line: int


@dataclass
class SystemStackRuntime:
    name: str
    files: List[Path]
    line: int


@dataclass
class SystemRunOptions:
    pull: bool = False
    no_recreate: bool = False
    force_recreate: bool = False
    remove_orphans: bool = True
    stack_filter: Optional[List[str]] = None
    no_login: bool = False
    force_login: bool = False
    strict_login: bool = False
    dry_run: bool = False
    registry_host: str = "registry.sftpmalin.com"
    login_retries: int = 10
    login_wait: int = 3


SYSTEM_IMAGE_LINE_RE = re.compile(r"^\s*image\s*:\s*(.+?)\s*$")


def system_action_title(action: str, conf: Optional[Dict[str, str]] = None) -> str:
    no_pull_suffix = ""
    if conf is not None and action == "system_stacks_update" and conf_bool(conf, "SYSTEM_STACKS_UPDATE_NO_PULL", "0"):
        no_pull_suffix = " sans pull"
    labels = {
        "system_stacks_update": f"Mise à jour des stacks Compose{no_pull_suffix}",
        "system_stacks_start": "Démarrage des stacks Compose",
        "system_stacks_list": "Lecture des stacks Compose",
        "system_stacks_ollama": "Création réseau Ollama",
        "system_stacks_remove_ollama": "Suppression réseau Ollama",
    }
    return labels.get(action, "Action inconnue")


def system_action_header(action: str) -> str:
    if action in {"system_stacks_ollama", "system_stacks_remove_ollama"}:
        return "LAN DOCKER"
    return "DOCKER COMPOSE"


def system_abs_path(value: str, default: str = "") -> Path:
    raw = (value or default or "").strip()
    if not raw:
        raw = default or "."
    return Path(os.path.abspath(os.path.expanduser(raw)))


def system_clean_conf_value(value: str) -> str:
    value = (value or "").strip().rstrip("\r")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value.strip()


def system_split_csv(value: str) -> List[str]:
    return [x.strip().strip("\"'") for x in (value or "").split(",") if x.strip()]


def system_q_cmd(cmd: List[str]) -> str:
    return " ".join(shlex.quote(str(x)) for x in cmd)


def system_read_stacks_conf(path: Path) -> List[SystemStackDefinition]:
    if not path.exists():
        raise RuntimeError(f"Fichier stacks.conf introuvable : {path}")

    stacks: List[SystemStackDefinition] = []
    current: Optional[SystemStackDefinition] = None
    seen_names: set = set()

    for lineno, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        line = raw.strip().rstrip("\r")
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeError(f"Ligne invalide dans {path}:{lineno} : {raw}")

        key, value = line.split("=", 1)
        key_upper = key.strip().upper()
        value = system_clean_conf_value(value)

        if key_upper.startswith("STACK"):
            if not value:
                raise RuntimeError(f"Nom de stack vide dans {path}:{lineno}")
            normalized = value.lower()
            if normalized in seen_names:
                raise RuntimeError(f"Stack en double dans {path}:{lineno} : {value}")
            seen_names.add(normalized)
            current = SystemStackDefinition(name=value, files=[], line=lineno)
            stacks.append(current)
            continue

        if key_upper.startswith("YML") or key_upper.startswith("YAML"):
            if current is None:
                raise RuntimeError(f"YML déclaré avant STACK dans {path}:{lineno}")
            values = system_split_csv(value)
            if not values:
                raise RuntimeError(f"YML vide dans {path}:{lineno}")
            current.files.extend(values)
            continue

    for stack in stacks:
        if not stack.files:
            raise RuntimeError(f"Stack sans YML dans {path}:{stack.line} : {stack.name}")
    if not stacks:
        raise RuntimeError(f"Aucune stack trouvée dans : {path}")
    return stacks


def system_compose_files_in_yml_dir(yml_dir: Path) -> List[Path]:
    files: List[Path] = []
    for pattern in ("*.yml", "*.yaml"):
        files.extend(path for path in yml_dir.glob(pattern) if path.is_file())
    return sorted(files, key=lambda p: p.name.lower())


def system_build_yml_lookup(yml_dir: Path) -> Dict[str, Path]:
    lookup: Dict[str, Path] = {}
    for path in system_compose_files_in_yml_dir(yml_dir):
        lookup.setdefault(path.name.lower(), path)
        lookup.setdefault(path.stem.lower(), path)
    return lookup


def system_candidate_yml_names(raw_name: str) -> List[str]:
    path = Path(system_clean_conf_value(raw_name))
    if path.suffix.lower() == ".xml":
        return [path.with_suffix(".yml").name, path.with_suffix(".yaml").name, path.stem]
    if path.suffix:
        return [path.name]
    return [path.name, f"{path.name}.yml", f"{path.name}.yaml"]


def system_resolve_yml_file(raw_name: str, *, yml_dir: Path, lookup: Dict[str, Path]) -> Optional[Path]:
    raw_name = system_clean_conf_value(raw_name)
    raw_path = Path(raw_name)
    if raw_path.is_absolute() and raw_path.is_file():
        return raw_path

    for name in system_candidate_yml_names(raw_name):
        candidate = Path(name)
        candidate = candidate if candidate.is_absolute() else yml_dir / candidate
        if candidate.is_file():
            return candidate

    for name in system_candidate_yml_names(raw_name):
        found = lookup.get(Path(name).name.lower()) or lookup.get(Path(name).stem.lower())
        if found:
            return found
    return None


def system_runtime_stacks_from_conf(conf_file: Path, *, yml_dir: Path, stack_filter: Optional[List[str]] = None) -> List[SystemStackRuntime]:
    definitions = system_read_stacks_conf(conf_file)
    if not yml_dir.is_dir():
        raise RuntimeError(f"Dossier YAML introuvable : {yml_dir}")

    wanted = {x.lower() for x in (stack_filter or [])}
    lookup = system_build_yml_lookup(yml_dir)
    result: List[SystemStackRuntime] = []

    for stack in definitions:
        if wanted and stack.name.lower() not in wanted:
            continue

        resolved_files: List[Path] = []
        missing: List[str] = []
        for raw_name in stack.files:
            resolved = system_resolve_yml_file(raw_name, yml_dir=yml_dir, lookup=lookup)
            if resolved is None:
                missing.append(raw_name)
            else:
                resolved_files.append(resolved)

        if missing:
            details = "\n".join(f"   - {item}" for item in missing)
            raise RuntimeError(f"Fichiers YAML introuvables pour la stack [{stack.name}] dans {conf_file}\n{details}\nDossier YAML : {yml_dir}")

        result.append(SystemStackRuntime(name=stack.name, files=resolved_files, line=stack.line))

    if wanted and not result:
        raise RuntimeError(f"Aucune stack trouvée dans stacks.conf pour : {', '.join(stack_filter or [])}")
    return result


def system_strip_image_value(raw_value: str) -> str:
    value = (raw_value or "").strip()
    if "#" in value:
        value = value.split("#", 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value.strip()


def system_image_name_from_ref(image: str) -> str:
    ref = system_strip_image_value(image)
    if not ref:
        return ""
    ref = ref.split("@", 1)[0]
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        last = last.rsplit(":", 1)[0]
    return last.strip()


def system_images_from_compose_file(path: Path) -> List[str]:
    images: List[str] = []
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = SYSTEM_IMAGE_LINE_RE.match(raw)
            if match:
                image = system_strip_image_value(match.group(1))
                if image:
                    images.append(image)
    except FileNotFoundError:
        pass
    return images


def system_images_from_stacks(stacks: List[SystemStackRuntime]) -> List[str]:
    images: List[str] = []
    seen: set = set()
    for stack in stacks:
        for compose_file in stack.files:
            for image in system_images_from_compose_file(compose_file):
                if image not in seen:
                    seen.add(image)
                    images.append(image)
    return images


def system_effective_mode_file(conf: Dict[str, str]) -> Path:
    """Retourne le fichier mode HTTP/HTTPS réellement utilisé par Docker Up.

    Convention Yoleo : 0 = registre local HTTP/insecure, 1 = registre HTTPS.
    Compatibilité conservée avec les anciens noms que l'utilisateur a pu créer
    pendant les essais : mode.conf, mod.conf, mod.txt, mode.txt.
    """
    configured = str(conf.get("DOCKER_MODE_FILE") or "").strip() or nas_conf_file("mode.conf")
    path = system_abs_path(configured)
    if path.exists():
        return path
    for alt_name in ("mode.conf", "mod.conf", "mod.txt", "mode.txt"):
        alt = path.parent / alt_name
        if alt.exists():
            return alt
    return path


def system_normalize_mode(value: str) -> str:
    value = (value or "").strip().lower()
    if value in {"0", "http", "local", "insecure", "disabled", "tls_disabled", "no_tls", "no", "false", "off"}:
        return "0"
    if value in {"1", "https", "remote", "secure", "tls", "enabled", "yes", "true", "on"}:
        return "1"
    return value or "0"


def system_mode_key_candidates(name: str) -> List[str]:
    clean = (name or "").strip()
    out: List[str] = []
    if clean:
        out.append(clean)
        if clean.endswith(".tar"):
            out.append(clean[:-4])
    return out


def system_mode_for_name(modes: Dict[str, str], name: str) -> str:
    for key in system_mode_key_candidates(name):
        if key in modes:
            return system_normalize_mode(modes[key])
    # default=0, _default=0 ou *=0 sont acceptés. Sans ligne, Yoleo reste en HTTP local par défaut.
    for default_key in ("_default", "default", "DEFAULT", "*"):
        if default_key in modes:
            return system_normalize_mode(modes[default_key])
    return "0"


def system_registry_login_required(mode_file: Path, stacks: List[SystemStackRuntime], logger: StreamLogger) -> bool:
    modes = read_kv_file(str(mode_file))
    images = system_images_from_stacks(stacks)
    if not images:
        yield logger.line("ℹ️ Login registre ignoré : aucune image détectée dans les YAML.")
        return False

    yield logger.line(f"🧭 Mode registre : {mode_file} (0=HTTP local, 1=HTTPS)")
    if not modes:
        yield logger.line("ℹ️ mode.conf absent/vide : défaut Yoleo = HTTP local/0.")

    https_images: List[str] = []
    local_images: List[str] = []
    for image in images:
        name = system_image_name_from_ref(image)
        mode = system_mode_for_name(modes, name)
        if mode == "0":
            local_images.append(f"{name} ({image})")
        else:
            https_images.append(f"{name} ({image})")

    if https_images:
        yield logger.line("🔐 Login registre nécessaire : au moins une image est en mode HTTPS/1.")
        for item in https_images[:12]:
            yield logger.line(f"   - {item}")
        if len(https_images) > 12:
            yield logger.line(f"   - ... {len(https_images) - 12} autre(s)")
        return True

    yield logger.line(f"ℹ️ Docker login sauté : toutes les images détectées sont en mode HTTP/local dans {mode_file}")
    for item in local_images[:12]:
        yield logger.line(f"   - {item}")
    if len(local_images) > 12:
        yield logger.line(f"   - ... {len(local_images) - 12} autre(s)")
    return False


def system_registry_host_from_image(image: str) -> str:
    ref = system_strip_image_value(image).split("@", 1)[0]
    if "/" not in ref:
        return ""
    host = ref.split("/", 1)[0].strip()
    # Docker ne considère le premier segment comme registre que s'il contient . ou : ou vaut localhost.
    if "." in host or ":" in host or host == "localhost":
        return host
    return ""


def system_http_registries_from_modes(mode_file: Path, stacks: List[SystemStackRuntime]) -> List[str]:
    modes = read_kv_file(str(mode_file))
    registries: List[str] = []
    for image in system_images_from_stacks(stacks):
        name = system_image_name_from_ref(image)
        if system_mode_for_name(modes, name) != "0":
            continue
        host = system_registry_host_from_image(image)
        if host and host not in registries:
            registries.append(host)
    return registries


def system_current_insecure_registries() -> List[str]:
    daemon_json = Path("/etc/docker/daemon.json")
    if not daemon_json.exists():
        return []
    try:
        data = json.loads(daemon_json.read_text(encoding="utf-8", errors="replace") or "{}")
    except Exception:
        return []
    values = data.get("insecure-registries", [])
    if not isinstance(values, list):
        return []
    return [str(v).strip() for v in values if str(v).strip()]


def system_ensure_insecure_registries(registries: List[str], options: SystemRunOptions, logger: StreamLogger) -> Iterator[bool]:
    """Applique les registres HTTP à Docker quand mode.conf demande du HTTP.

    Sans cette étape, le daemon Docker tente https://host:port/v2/ et Compose
    peut échouer avec « HTTP response to HTTPS client ». On ne redémarre Docker
    que si /etc/docker/daemon.json ne contient pas déjà les registres requis.
    """
    clean: List[str] = []
    for registry in registries:
        registry = _normalize_registry_env_value(registry)
        if registry and registry not in clean:
            clean.append(registry)
    if not clean:
        return True

    current = system_current_insecure_registries()
    missing = [registry for registry in clean if registry not in current]
    if not missing:
        yield logger.line(f"✅ Registre(s) HTTP déjà autorisé(s) dans Docker : {', '.join(clean)}")
        return True

    yield logger.line(f"🧩 mode.conf demande HTTP/0 : ajout insecure-registries Docker : {', '.join(missing)}")
    if options.dry_run:
        yield logger.line("DRY-RUN: /etc/docker/daemon.json serait mis à jour puis Docker redémarré.")
        return True

    daemon_dir = Path("/etc/docker")
    daemon_json = daemon_dir / "daemon.json"
    try:
        daemon_dir.mkdir(parents=True, exist_ok=True)
        data: Dict[str, Any] = {}
        if daemon_json.exists():
            backup = daemon_json.with_name(f"daemon.json.bak.{time.strftime('%Y%m%d_%H%M%S')}")
            shutil.copy2(daemon_json, backup)
            yield logger.line(f"💾 Sauvegarde Docker : {backup}")
            try:
                loaded = json.loads(daemon_json.read_text(encoding="utf-8", errors="replace") or "{}")
                if isinstance(loaded, dict):
                    data = loaded
            except Exception as exc:
                yield logger.line(f"⚠️ daemon.json illisible, réécriture propre : {exc}")

        merged = list(current)
        for registry in missing:
            if registry not in merged:
                merged.append(registry)
        data["insecure-registries"] = merged

        tmp = daemon_json.with_name(f"daemon.json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, daemon_json)
        yield logger.line(f"✅ /etc/docker/daemon.json mis à jour : {', '.join(merged)}")

        rc = yield from stream_system_run_command(["systemctl", "restart", "docker"], logger, dry_run=False)
        if rc != 0:
            yield logger.line("❌ Redémarrage Docker impossible après ajout du registre HTTP.")
            return False
        yield logger.line("✅ Docker redémarré avec le registre HTTP autorisé.")
        return True
    except Exception as exc:
        yield logger.line(f"❌ Configuration insecure-registries impossible : {exc}")
        return False


def system_apply_extra_args(options: SystemRunOptions, extra_args: str) -> SystemRunOptions:
    raw = (extra_args or "").strip()
    if not raw:
        return options
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if token == "--stack" and nxt:
            options.stack_filter = system_split_csv(nxt)
            i += 2
            continue
        if token.startswith("--stack="):
            options.stack_filter = system_split_csv(token.split("=", 1)[1])
        elif token == "--no-pull":
            options.pull = False
        elif token == "--pull":
            options.pull = True
        elif token == "--no-recreate":
            options.no_recreate = True
            options.force_recreate = False
        elif token == "--force-recreate":
            options.force_recreate = True
            options.no_recreate = False
        elif token == "--no-remove-orphans":
            options.remove_orphans = False
        elif token == "--no-login":
            options.no_login = True
        elif token == "--login":
            options.force_login = True
        elif token == "--strict-login":
            options.strict_login = True
        elif token == "--dry-run":
            options.dry_run = True
        elif token == "--registry-host" and nxt:
            options.registry_host = nxt
            i += 2
            continue
        elif token.startswith("--registry-host="):
            options.registry_host = token.split("=", 1)[1]
        elif token == "--login-retries" and nxt:
            try:
                options.login_retries = int(nxt)
            except ValueError:
                pass
            i += 2
            continue
        elif token.startswith("--login-retries="):
            try:
                options.login_retries = int(token.split("=", 1)[1])
            except ValueError:
                pass
        elif token == "--login-wait" and nxt:
            try:
                options.login_wait = int(nxt)
            except ValueError:
                pass
            i += 2
            continue
        elif token.startswith("--login-wait="):
            try:
                options.login_wait = int(token.split("=", 1)[1])
            except ValueError:
                pass
        i += 1
    return options


def system_options_from_conf(conf: Dict[str, str], action: str) -> SystemRunOptions:
    options = SystemRunOptions(
        pull=(action == "system_stacks_update" and not conf_bool(conf, "SYSTEM_STACKS_UPDATE_NO_PULL", "0")),
        no_recreate=conf_bool(conf, "SYSTEM_UP_NO_RECREATE", "0"),
        remove_orphans=conf_bool(conf, "SYSTEM_UP_REMOVE_ORPHANS", "1"),
    )
    return system_apply_extra_args(options, conf.get("SYSTEM_STACKS_EXTRA_ARGS", ""))


def stream_system_run_command(cmd: List[str], logger: StreamLogger, *, cwd: Optional[Path] = None, input_text: Optional[str] = None, dry_run: bool = False) -> Iterator[int]:
    if cwd:
        yield logger.line(f"$ cd {cwd}")
    yield logger.line(f"$ {system_q_cmd(cmd)}")
    if dry_run:
        return 0

    env = os.environ.copy()
    env.setdefault("COMPOSE_PROGRESS", "auto")
    try:
        process = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        if input_text is not None and process.stdin is not None:
            try:
                process.stdin.write(input_text)
                if not input_text.endswith("\n"):
                    process.stdin.write("\n")
                process.stdin.close()
            except Exception:
                pass
        assert process.stdout is not None
        for line in process.stdout:
            yield logger.raw(line)
        return process.wait()
    except FileNotFoundError:
        yield logger.line(f"❌ Commande introuvable : {cmd[0]}")
        return 127
    except Exception as exc:
        yield logger.line(f"❌ Exception Python pendant la commande : {exc}")
        return 1


def system_capture_ok(cmd: List[str]) -> bool:
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8).returncode == 0
    except Exception:
        return False


_SYSTEM_COMPOSE_COMMAND_CACHE: Optional[List[str]] = None


def system_detect_compose_command() -> List[str]:
    """
    Retourne la commande Compose disponible sur l'hôte.

    Debian peut fournir Compose en plugin Docker v2 (`docker compose`) ou en
    binaire compatible (`docker-compose`). Le moteur ne doit pas supposer un
    format unique, sinon l'onglet Compose casse dès qu'une install diffère.

    Important : on ne met pas en cache un résultat négatif. Si Compose est
    installé depuis Système > Installation pendant que Flask tourne déjà,
    l'action Compose doit pouvoir le voir sans redémarrage obligatoire.
    """
    global _SYSTEM_COMPOSE_COMMAND_CACHE
    if _SYSTEM_COMPOSE_COMMAND_CACHE is not None:
        cached = list(_SYSTEM_COMPOSE_COMMAND_CACHE)
        if cached and system_capture_ok(cached + ["version"]):
            return cached
        _SYSTEM_COMPOSE_COMMAND_CACHE = None

    candidates = (
        ["docker", "compose"],
        ["docker-compose"],
    )
    for candidate in candidates:
        if system_capture_ok(candidate + ["version"]):
            _SYSTEM_COMPOSE_COMMAND_CACHE = list(candidate)
            return list(candidate)

    return []


def system_compose_base_command_for_files(stack_name: str, files: List[Path]) -> List[str]:
    cmd = system_detect_compose_command()
    if not cmd:
        return []
    cmd += ["-p", stack_name]
    for yml in files:
        cmd += ["-f", str(yml)]
    return cmd


def system_compose_base_command(stack: SystemStackRuntime) -> List[str]:
    return system_compose_base_command_for_files(stack.name, stack.files)


def system_progress_log_line(action: Optional[str], progress_state: Optional[Dict[str, int]], logger: StreamLogger, *, name: str = "", phase: str = "") -> str:
    if not action or not progress_state:
        return ""
    total = max(1, int(progress_state.get("total", 1) or 1))
    current = max(0, min(total, int(progress_state.get("current", 0) or 0)))
    percent = int((current / total) * 100)
    payload = {
        "action": action,
        "current": current,
        "total": total,
        "percent": percent,
        "done": int(progress_state.get("done", 0) or 0),
        "failed": int(progress_state.get("failed", 0) or 0),
    }
    if name:
        payload["name"] = name
    if phase:
        payload["phase"] = phase
    return logger.line(f"@@PROGRESS {json.dumps(payload, ensure_ascii=False)}")


def system_progress_step(action: Optional[str], progress_state: Optional[Dict[str, int]], logger: StreamLogger, *, ok: bool, name: str = "", phase: str = "") -> str:
    if not action or not progress_state:
        return ""
    progress_state["current"] = int(progress_state.get("current", 0) or 0) + 1
    if ok:
        progress_state["done"] = int(progress_state.get("done", 0) or 0) + 1
    else:
        progress_state["failed"] = int(progress_state.get("failed", 0) or 0) + 1
    return system_progress_log_line(action, progress_state, logger, name=name, phase=phase)


def stream_system_compose_pull_stack_ymls(stack: SystemStackRuntime, yml_dir: Path, options: SystemRunOptions, logger: StreamLogger, *, action: Optional[str] = None, progress_state: Optional[Dict[str, int]] = None) -> Iterator[bool]:
    total_yml = len(stack.files)
    if total_yml <= 0:
        yield logger.line(f"❌ Aucun YAML à pull pour [{stack.name}].")
        if action and progress_state:
            yield system_progress_step(action, progress_state, logger, ok=False, name=stack.name, phase="pull")
        return False

    yield logger.line(f"⬇️ Pull Compose par YAML : {total_yml} fichier(s) dans [{stack.name}]")
    pulled = 0
    failed = 0

    for yml_index, compose_file in enumerate(stack.files, start=1):
        yield logger.line("")
        yield logger.line(f"⬇️ Pull YAML {yml_index}/{total_yml} [{stack.name}] : {compose_file.name}")
        pull_cmd = system_compose_base_command_for_files(stack.name, [compose_file])
        if not pull_cmd:
            yield logger.line("❌ Docker Compose est introuvable sur cet hôte.")
            yield logger.line("   Installe docker-compose-plugin ou docker-compose, puis relance l'action Compose.")
            failed += 1
            if action and progress_state:
                yield system_progress_step(action, progress_state, logger, ok=False, name=compose_file.name, phase="pull")
            continue

        rc_pull = yield from stream_system_run_command(pull_cmd + ["pull"], logger, cwd=yml_dir, dry_run=options.dry_run)
        ok = rc_pull == 0
        if ok:
            pulled += 1
            yield logger.line(f"  ✅ Pull OK : {compose_file.name}")
        else:
            failed += 1
            yield logger.line(f"  ❌ Pull échoué : {compose_file.name}")
        if action and progress_state:
            yield system_progress_step(action, progress_state, logger, ok=ok, name=compose_file.name, phase="pull")

    yield logger.line("")
    yield logger.line(f"📊 Pull YAML [{stack.name}] : {pulled}/{total_yml} OK, {failed} erreur(s)")
    if pulled + failed != total_yml:
        yield logger.line(f"❌ Pull incomplet sur [{stack.name}] : {pulled + failed}/{total_yml} YAML traités.")
        return False
    return failed == 0


def stream_system_compose_up_stack(stack: SystemStackRuntime, yml_dir: Path, options: SystemRunOptions, logger: StreamLogger, *, action: Optional[str] = None, progress_state: Optional[Dict[str, int]] = None) -> Iterator[bool]:
    yield logger.line("")
    yield logger.line(f"📂 Stack détectée : [{stack.name}]")
    yield logger.line(f"🧩 YAML ({len(stack.files)}) : {', '.join(path.name for path in stack.files)}")

    base_cmd = system_compose_base_command(stack)
    if not base_cmd:
        yield logger.line("❌ Docker Compose est introuvable sur cet hôte.")
        yield logger.line("   Installe docker-compose-plugin ou docker-compose, puis relance l'action Compose.")
        if action and progress_state:
            yield system_progress_step(action, progress_state, logger, ok=False, name=stack.name, phase="up")
        return False

    if options.pull:
        if not (yield from stream_system_compose_pull_stack_ymls(stack, yml_dir, options, logger, action=action, progress_state=progress_state)):
            yield logger.line(f"  ❌ Pull Compose incomplet sur [{stack.name}], up annulé.")
            if action and progress_state:
                yield system_progress_step(action, progress_state, logger, ok=False, name=stack.name, phase="up")
            return False

    up_cmd = base_cmd + ["up", "-d"]
    if options.remove_orphans:
        up_cmd.append("--remove-orphans")
    if options.no_recreate:
        up_cmd.append("--no-recreate")
    if options.force_recreate:
        up_cmd.append("--force-recreate")

    yield logger.line("")
    yield logger.line(f"▶ Up Compose complet [{stack.name}] avec {len(stack.files)} YAML")
    rc_up = yield from stream_system_run_command(up_cmd, logger, cwd=yml_dir, dry_run=options.dry_run)
    ok_up = rc_up == 0
    if action and progress_state:
        yield system_progress_step(action, progress_state, logger, ok=ok_up, name=stack.name, phase="up")
    if ok_up:
        yield logger.line(f"  ✅ Stack [{stack.name}] OK")
        return True
    yield logger.line(f"  ❌ Erreur sur [{stack.name}]")
    return False


def stream_system_login_registry(conf: Dict[str, str], options: SystemRunOptions, logger: StreamLogger) -> Iterator[bool]:
    if options.no_login:
        return True

    login_file = system_abs_path(conf.get("DOCKER_REGISTRY_LOGIN_FILE", nas_conf_file("registre_login.txt")))
    registry_conf = read_kv_file(str(login_file))
    raw_host = os.environ.get("REGISTRY_HOST", registry_conf.get("REGISTRY_HOST", options.registry_host)).strip()
    host = _normalize_registry_env_value(raw_host)
    user = os.environ.get("REGISTRY_USER", registry_conf.get("REGISTRY_USER", "")).strip()
    password = os.environ.get("REGISTRY_PASS", registry_conf.get("REGISTRY_PASS", "")).strip()

    if not password:
        password_file = os.environ.get("REGISTRY_PASS_FILE", registry_conf.get("REGISTRY_PASS_FILE", "")).strip()
        if password_file:
            try:
                password = Path(password_file).read_text(encoding="utf-8", errors="replace").strip()
            except FileNotFoundError:
                password = ""

    if raw_host and host != raw_host:
        yield logger.line(f"🛠️ REGISTRY_HOST normalisé : {raw_host} -> {host}")

    if not host:
        yield logger.line("⚠️ Login registre ignoré : REGISTRY_HOST vide.")
        return not options.strict_login
    if not user:
        yield logger.line(f"⚠️ Login registre ignoré : REGISTRY_USER absent dans {login_file}")
        return not options.strict_login
    if not password:
        yield logger.line(f"⚠️ Login registre ignoré : REGISTRY_PASS ou REGISTRY_PASS_FILE absent dans {login_file}")
        return not options.strict_login

    retries = max(1, int(options.login_retries or 1))
    wait_s = max(0, int(options.login_wait or 0))
    yield logger.line(f"🔐 Login registre : {host}")
    last_rc = 1
    for attempt in range(1, retries + 1):
        if retries > 1:
            yield logger.line(f"➡️  Tentative login {attempt}/{retries}")
        last_rc = yield from stream_system_run_command(["docker", "login", host, "-u", user, "--password-stdin"], logger, input_text=password, dry_run=options.dry_run)
        if last_rc == 0:
            yield logger.line(f"✅ Login registre OK : {host}")
            return True
        if attempt < retries and wait_s > 0 and not options.dry_run:
            yield logger.line(f"⏳ Registre pas encore prêt, nouvelle tentative dans {wait_s}s...")
            time.sleep(wait_s)
    yield logger.line(f"❌ Login registre échoué : {host}")
    return False


def stream_system_ensure_network(conf: Dict[str, str], options: SystemRunOptions, logger: StreamLogger) -> Iterator[bool]:
    name = (conf.get("SYSTEM_NETWORK_NAME", "ollama_lan") or "ollama_lan").strip()
    subnet = (conf.get("SYSTEM_NETWORK_SUBNET", "172.20.0.0/16") or "172.20.0.0/16").strip()
    gateway = (conf.get("SYSTEM_NETWORK_GATEWAY", "172.20.0.1") or "172.20.0.1").strip()
    bridge = (conf.get("SYSTEM_NETWORK_BRIDGE", "ollama_lan") or "ollama_lan").strip()

    if not options.dry_run and system_capture_ok(["docker", "network", "inspect", name]):
        yield logger.line(f"✅ Réseau Ollama [{name}] déjà présent")
        return True

    yield logger.line(f"🌐 Création du réseau Ollama [{name}]")
    cmd = [
        "docker", "network", "create",
        "-d", "bridge",
        "--subnet", subnet,
        "--gateway", gateway,
        "-o", f"com.docker.network.bridge.name={bridge}",
        name,
    ]
    rc = yield from stream_system_run_command(cmd, logger, dry_run=options.dry_run)
    if rc == 0:
        yield logger.line(f"✅ Réseau Ollama [{name}] OK")
        return True
    yield logger.line(f"❌ Création du réseau Ollama [{name}] échouée")
    return False


def stream_system_remove_network(conf: Dict[str, str], options: SystemRunOptions, logger: StreamLogger) -> Iterator[bool]:
    name = (conf.get("SYSTEM_NETWORK_NAME", "ollama_lan") or "ollama_lan").strip()
    if not options.dry_run and not system_capture_ok(["docker", "network", "inspect", name]):
        yield logger.line(f"✅ Réseau Ollama [{name}] déjà absent")
        return True

    yield logger.line(f"🧹 Suppression du réseau Ollama [{name}]")
    yield logger.line("⚠️ Si un container utilise encore ce réseau, Docker refusera.")
    rc = yield from stream_system_run_command(["docker", "network", "rm", name], logger, dry_run=options.dry_run)
    if rc == 0:
        yield logger.line(f"✅ Réseau Ollama [{name}] supprimé")
        return True
    yield logger.line(f"❌ Suppression du réseau Ollama [{name}] échouée")
    return False


def stream_system_list_stacks(conf: Dict[str, str], options: SystemRunOptions, logger: StreamLogger) -> Iterator[bool]:
    yml_dir = system_abs_path(conf.get("YML_FOLDER", "/dockers/yml"))
    stacks_conf = system_abs_path(conf.get("SYSTEM_STACKS_CONF_FILE") or conf.get("STACKS_FILE", nas_conf_file("stacks.conf")))
    stack_list = system_runtime_stacks_from_conf(stacks_conf, yml_dir=yml_dir, stack_filter=options.stack_filter)
    yield logger.line(f"Stacks détectées dans l'ordre de : {stacks_conf}")
    for stack in stack_list:
        yield logger.line(f"✅ {stack.name:<24} {', '.join(str(p) for p in stack.files)}")
    return True


def system_repair_compose_env_registry(conf: Dict[str, str], yml_dir: Path) -> List[str]:
    """Répare les fautes de registre les plus courantes dans les .env Compose.

    Docker Compose charge automatiquement ``.env`` depuis le dossier de travail.
    Si REGISTRY vaut ``192.168.1.140.7777``, Docker tente ensuite de joindre
    ``https://192.168.1.140.7777/v2/`` et échoue au DNS avant même le vrai
    problème HTTP/HTTPS. Cette réparation est volontairement limitée aux clés
    de registre connues et aux valeurs manifestement mal formées.
    """
    candidates: List[Path] = []

    def add_candidate(path: Path) -> None:
        try:
            path = path.expanduser().resolve()
        except Exception:
            path = Path(str(path))
        if path not in candidates:
            candidates.append(path)

    add_candidate(yml_dir / ".env")
    env_file = (conf.get("ENV_FILE") or "").strip()
    if env_file:
        add_candidate(system_abs_path(env_file))

    registry_keys = {"REGISTRY", "REGISTRY_HOST", "REGISTRY_URL", "DOCKER_REGISTRY", "DOCKER_REGISTRY_HOST"}
    messages: List[str] = []
    key_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

    for env_path in candidates:
        if not env_path.exists() or not env_path.is_file():
            continue
        try:
            original = env_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            messages.append(f"⚠️ Lecture .env impossible : {env_path} ({exc})")
            continue

        changed = False
        new_lines: List[str] = []
        for raw_line in original.splitlines():
            match = key_re.match(raw_line.strip())
            if not match:
                new_lines.append(raw_line)
                continue
            key, raw_value = match.groups()
            if key.upper() not in registry_keys:
                new_lines.append(raw_line)
                continue

            clean_value = _normalize_registry_env_value(raw_value)
            if clean_value and clean_value != strip_quotes(raw_value).strip():
                new_lines.append(f"{key}={clean_value}")
                messages.append(f"🛠️ .env corrigé : {env_path} : {key}={clean_value}")
                changed = True
            else:
                new_lines.append(raw_line)

        if changed:
            try:
                suffix = "\n" if original.endswith("\n") else ""
                env_path.write_text("\n".join(new_lines).rstrip("\n") + suffix, encoding="utf-8")
            except Exception as exc:
                messages.append(f"❌ Écriture .env impossible : {env_path} ({exc})")

    return messages


def system_warn_malformed_registry_images(stacks: List[SystemStackRuntime]) -> List[str]:
    """Signale les registres IP.port restés en dur dans les YAML Compose."""
    messages: List[str] = []
    seen: set = set()
    host_re = re.compile(r"^((?:\d{1,3}\.){3}\d{1,3})\.(\d{2,5})(?=/|$)")

    for stack in stacks:
        for compose_file in stack.files:
            for image in system_images_from_compose_file(compose_file):
                clean_image = system_strip_image_value(image)
                match = host_re.match(clean_image)
                if not match:
                    continue
                ip, port = match.groups()
                try:
                    ipaddress.ip_address(ip)
                    port_i = int(port)
                    if not (1 <= port_i <= 65535):
                        continue
                except Exception:
                    continue
                fixed = f"{ip}:{port_i}" + clean_image[match.end():]
                key = (str(compose_file), clean_image)
                if key in seen:
                    continue
                seen.add(key)
                messages.append(
                    f"⚠️ Registre mal formé dans {compose_file.name} : {clean_image} -> {fixed}"
                )
    return messages


def stream_system_update_or_start_stacks(conf: Dict[str, str], action: str, options: SystemRunOptions, logger: StreamLogger) -> Iterator[bool]:
    yml_dir = system_abs_path(conf.get("YML_FOLDER", "/dockers/yml"))
    stacks_conf = system_abs_path(conf.get("SYSTEM_STACKS_CONF_FILE") or conf.get("STACKS_FILE", nas_conf_file("stacks.conf")))
    mode_file = system_effective_mode_file(conf)
    stack_list = system_runtime_stacks_from_conf(stacks_conf, yml_dir=yml_dir, stack_filter=options.stack_filter)

    yield logger.line("")
    total_yml = sum(len(stack.files) for stack in stack_list)
    progress_total = sum((len(stack.files) + 1) if options.pull else 1 for stack in stack_list)

    yield logger.line("-------------------------------------------------------")
    yield logger.line("🚀 Action Docker Compose intégrée Yoleo")
    yield logger.line(f"📅 {time.strftime('%Y-%m-%d %H:%M:%S')}")
    yield logger.line("-------------------------------------------------------")
    yield logger.line(f"📁 YAML source : {yml_dir}")
    yield logger.line(f"🧾 Conf stacks : {stacks_conf}")
    yield logger.line(f"🔄 Pull        : {'oui' if options.pull else 'non'}")
    yield logger.line(f"📦 Stacks      : {len(stack_list)} ({', '.join(s.name for s in stack_list)})")
    yield logger.line(f"🧩 YAML prévus : {total_yml}")

    for repair_msg in system_repair_compose_env_registry(conf, yml_dir):
        yield logger.line(repair_msg)
    for warn_msg in system_warn_malformed_registry_images(stack_list):
        yield logger.line(warn_msg)

    http_registries = system_http_registries_from_modes(mode_file, stack_list)
    if http_registries:
        if not (yield from system_ensure_insecure_registries(http_registries, options, logger)):
            yield logger.line(f"@@PROGRESS {json.dumps({'action': action, 'current': 0, 'total': max(1, len(stack_list)), 'percent': 0, 'done': 0, 'failed': 1}, ensure_ascii=False)}")
            return False

    total = max(1, progress_total)
    progress_state = {"current": 0, "total": total, "done": 0, "failed": 0}
    yield system_progress_log_line(action, progress_state, logger)

    if options.pull and stack_list:
        first_stack = stack_list[0]
        yield logger.line("")
        yield logger.line("🧩 Bootstrap registre : démarrage de la première stack avant docker login")
        yield logger.line(f"📦 Première stack : [{first_stack.name}]")
        boot_options = SystemRunOptions(**{**options.__dict__, "pull": False})
        if not (yield from stream_system_compose_up_stack(first_stack, yml_dir, boot_options, logger)):
            yield logger.line(f"@@PROGRESS {json.dumps({'action': action, 'current': 0, 'total': total, 'percent': 0, 'done': 0, 'failed': 1}, ensure_ascii=False)}")
            return False

        login_required = yield from system_registry_login_required(mode_file, stack_list, logger)
        if login_required:
            if not (yield from stream_system_login_registry(conf, options, logger)):
                yield logger.line(f"@@PROGRESS {json.dumps({'action': action, 'current': 0, 'total': total, 'percent': 0, 'done': 0, 'failed': 1}, ensure_ascii=False)}")
                return False
        else:
            yield logger.line("ℹ️ Docker login sauté : mode local/HTTP.")
    elif options.force_login:
        login_required = yield from system_registry_login_required(mode_file, stack_list, logger)
        if login_required and not (yield from stream_system_login_registry(conf, options, logger)):
            yield logger.line(f"@@PROGRESS {json.dumps({'action': action, 'current': 0, 'total': total, 'percent': 0, 'done': 0, 'failed': 1}, ensure_ascii=False)}")
            return False
    else:
        yield logger.line("ℹ️ Login registre ignoré : start sans pull.")

    ok_count = 0
    ko_count = 0
    for stack in stack_list:
        ok = yield from stream_system_compose_up_stack(stack, yml_dir, options, logger, action=action, progress_state=progress_state)
        if ok:
            ok_count += 1
        else:
            ko_count += 1

    # Garde-fou : une action Compose ne doit jamais finir en succès si le plan
    # annoncé au début n'a pas été entièrement consommé. Cela évite les faux
    # “terminé” quand une grosse stack/YAML s'arrête trop tôt sans erreur claire.
    if int(progress_state.get("current", 0) or 0) != total:
        ko_count += 1
        yield logger.line("")
        yield logger.line(f"❌ Action Compose incomplète : {progress_state.get('current', 0)}/{total} étape(s) traitée(s).")
        yield system_progress_log_line(action, progress_state, logger)

    yield logger.line("")
    yield logger.line("-------------------------------------------------------")
    yield logger.line(f"✅ Stacks OK      : {ok_count}")
    if ko_count == 0:
        yield logger.line("✅ Stacks erreur  : 0")
    else:
        yield logger.line(f"❌ Stacks erreur  : {ko_count}")
    yield logger.line(f"🧩 YAML prévus    : {total_yml}")
    yield logger.line(f"📊 Étapes traitées : {progress_state.get('current', 0)}/{total}")
    yield logger.line("-------------------------------------------------------")
    return ko_count == 0


def stream_integrated_system_action(conf: Dict[str, str], action: str, logger: StreamLogger) -> Iterator[bool]:
    options = system_options_from_conf(conf, action)
    if options.no_recreate and options.force_recreate:
        yield logger.line("❌ SYSTEM_STACKS_EXTRA_ARGS invalide : --no-recreate et --force-recreate ne peuvent pas être utilisés ensemble.")
        return False

    if action == "system_stacks_list":
        yield logger.line(f"@@PROGRESS {json.dumps({'action': action, 'current': 0, 'total': 1, 'percent': 0, 'done': 0, 'failed': 0}, ensure_ascii=False)}")
        ok = yield from stream_system_list_stacks(conf, options, logger)
        yield logger.line(f"@@PROGRESS {json.dumps({'action': action, 'current': 1, 'total': 1, 'percent': 100, 'done': 1 if ok else 0, 'failed': 0 if ok else 1}, ensure_ascii=False)}")
        return ok

    if action == "system_stacks_ollama":
        yield logger.line(f"@@PROGRESS {json.dumps({'action': action, 'current': 0, 'total': 1, 'percent': 0, 'done': 0, 'failed': 0}, ensure_ascii=False)}")
        ok = yield from stream_system_ensure_network(conf, options, logger)
        yield logger.line(f"@@PROGRESS {json.dumps({'action': action, 'current': 1, 'total': 1, 'percent': 100, 'done': 1 if ok else 0, 'failed': 0 if ok else 1}, ensure_ascii=False)}")
        return ok

    if action == "system_stacks_remove_ollama":
        yield logger.line(f"@@PROGRESS {json.dumps({'action': action, 'current': 0, 'total': 1, 'percent': 0, 'done': 0, 'failed': 0}, ensure_ascii=False)}")
        ok = yield from stream_system_remove_network(conf, options, logger)
        yield logger.line(f"@@PROGRESS {json.dumps({'action': action, 'current': 1, 'total': 1, 'percent': 100, 'done': 1 if ok else 0, 'failed': 0 if ok else 1}, ensure_ascii=False)}")
        return ok

    if action in {"system_stacks_update", "system_stacks_start"}:
        return (yield from stream_system_update_or_start_stacks(conf, action, options, logger))

    yield logger.line(f"❌ Action Docker inconnue : {action}")
    return False


def stream_system_action(conf: Dict[str, str], action: str) -> Iterator[str]:
    title = system_action_title(action, conf)
    log_path = conf.get("SYSTEM_LOG_FILE", "").strip() or DEFAULT_CONFIG["SYSTEM_LOG_FILE"]
    log_dir = os.path.dirname(log_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    if os.environ.get("YOLEO_DOCKER_KEEP_SYSTEM_LOG") != "1":
        try:
            open(log_path, "w", encoding="utf-8").close()
        except Exception:
            pass

    logger = StreamLogger(log_path)
    # Ce verrou protège les actions Compose entre elles, sans bloquer les builds.
    lock = OperationLock(system_compose_lock_path(conf))
    ok_lock, lock_msg = lock.acquire()
    if not ok_lock:
        yield logger.line(f"❌ {lock_msg}")
        logger.close()
        return

    try:
        yield logger.line("=" * 76)
        yield logger.line(f"{system_action_header(action)} : {title}")
        yield logger.line("=" * 76)
        yield logger.line(f"Date       : {time.strftime('%Y-%m-%d %H:%M:%S')}")
        yield logger.line(f"Log        : {log_path}")
        yield logger.line("")
        started = time.time()
        try:
            ok = yield from stream_integrated_system_action(conf, action, logger)
        except Exception as exc:
            ok = False
            yield logger.line(f"❌ Exception Compose : {exc}")
        duration = int(time.time() - started)
        yield logger.line("")
        if ok:
            yield logger.line(f"✅ {title} terminé avec succès ({duration}s).")
        else:
            yield logger.line(f"❌ {title} terminé avec erreur ({duration}s).")
    finally:
        lock.release()
        logger.close()


def system_normalize_compose_action(action: str) -> str:
    action = str(action or "").strip()
    if action in {"system_stacks_all", "system_stacks_create", "system_compose_create", "system_docker_up"}:
        return "system_stacks_update"
    if action in {"__attach", "attach"}:
        return ""
    return action


def system_compose_log_path(conf: Dict[str, str]) -> str:
    return conf.get("SYSTEM_LOG_FILE", "").strip() or DEFAULT_CONFIG["SYSTEM_LOG_FILE"]


def system_compose_lock_path(conf: Dict[str, str]) -> str:
    """Verrou dédié aux opérations Compose (pull, update, start).

    Le build conserve son propre verrou afin qu'un pull initié dans l'interface
    puisse démarrer pendant un build, comme avec les commandes Docker directes.
    """
    return conf.get("SYSTEM_LOCK_FILE", "").strip() or "/tmp/flask_stacks_system.lock"


def system_compose_operation_running(conf: Dict[str, str]) -> bool:
    lock = OperationLock(system_compose_lock_path(conf))
    ok_lock, _ = lock.acquire()
    if ok_lock:
        lock.release()
        return False
    return True


def system_start_action_subprocess(conf: Dict[str, str], action: str) -> Tuple[bool, str]:
    action = system_normalize_compose_action(action)
    if not action.startswith("system_stacks_"):
        return False, "Action Docker inconnue."
    if system_compose_operation_running(conf):
        return False, "already-running"

    log_path = system_compose_log_path(conf)
    log_dir = os.path.dirname(log_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    try:
        open(log_path, "w", encoding="utf-8").close()
    except Exception:
        pass

    parent_file = globals().get("__chunk_parent_file__", __file__)
    module_dir = os.path.dirname(os.path.abspath(str(parent_file)))
    code = (
        "import os, sys\n"
        "action = sys.argv[1]\n"
        "module_dir = sys.argv[2]\n"
        "os.chdir(module_dir)\n"
        "sys.path.insert(0, module_dir)\n"
        "os.environ['YOLEO_DOCKER_KEEP_SYSTEM_LOG'] = '1'\n"
        "import dockers\n"
        "conf = dockers.get_config()\n"
        "for _chunk in dockers.stream_system_action(conf, action):\n"
        "    pass\n"
    )
    env = dict(os.environ)
    env["YOLEO_DOCKER_KEEP_SYSTEM_LOG"] = "1"
    try:
        subprocess.Popen(
            [sys.executable, "-c", code, action, module_dir],
            cwd=module_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    except Exception as exc:
        return False, str(exc)
    return True, "started"


def _system_read_compose_log_chunk(log_path: str, position: int) -> Tuple[str, int]:
    try:
        size = os.path.getsize(log_path)
    except Exception:
        return "", position
    if size < position:
        position = 0
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
            handle.seek(position)
            data = handle.read()
            return data, handle.tell()
    except Exception:
        return "", position


def stream_system_action_log(conf: Dict[str, str], action: str = "") -> Iterator[str]:
    action = system_normalize_compose_action(action)
    if action and not action.startswith("system_stacks_"):
        yield "Action Docker inconnue.\n"
        return

    launched = False
    if action:
        started, message = system_start_action_subprocess(conf, action)
        launched = started
        if not started and message != "already-running":
            yield f"Erreur lancement subprocess Compose : {message}\n"
            return

    log_path = system_compose_log_path(conf)
    position = 0
    idle_after_done = 0
    empty_seen = 0

    while True:
        data, position = _system_read_compose_log_chunk(log_path, position)
        if data:
            empty_seen = 0
            idle_after_done = 0
            yield data
        else:
            empty_seen += 1

        running = system_compose_operation_running(conf)
        if not running:
            if launched and position == 0:
                if empty_seen >= 12:
                    yield "Erreur lancement subprocess Compose : aucun log produit.\n"
                    break
            elif position > 0:
                idle_after_done += 1
                if idle_after_done >= 2:
                    break
            elif not action and empty_seen >= 2:
                break
            elif action and empty_seen >= 12:
                yield "Erreur lancement subprocess Compose : aucun log produit.\n"
                break
        time.sleep(1)


def system_compose_log_status(conf: Dict[str, str]) -> Dict[str, Any]:
    log_path = system_compose_log_path(conf)
    result: Dict[str, Any] = {
        "running": system_compose_operation_running(conf),
        "log_path": log_path,
        "progress": None,
        "percent": 0,
        "failed": 0,
    }
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 70000), os.SEEK_SET)
            text = handle.read()
    except Exception:
        return result

    last_progress: Optional[Dict[str, Any]] = None
    failed = 0
    for line in text.splitlines():
        if not line.startswith("@@PROGRESS "):
            continue
        try:
            progress = json.loads(line[len("@@PROGRESS "):])
        except Exception:
            continue
        if isinstance(progress, dict):
            last_progress = progress
            try:
                failed = int(progress.get("failed") or 0)
            except Exception:
                failed = 0

    if last_progress:
        result["progress"] = last_progress
        try:
            result["percent"] = int(last_progress.get("percent") or 0)
        except Exception:
            result["percent"] = 0
        result["failed"] = failed
    return result


# -----------------------------------------------------------------------------
# Onglet Images Docker intégré au module Stacks
# -----------------------------------------------------------------------------
