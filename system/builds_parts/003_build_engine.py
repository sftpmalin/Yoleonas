class StreamLogger:
    def __init__(self, *paths: str):
        self.handles = []
        seen = set()
        for path in paths:
            if not path or path in seen:
                continue
            seen.add(path)
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
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


def stream_process(cmd: List[str], logger: StreamLogger, input_text: Optional[str] = None, cwd: Optional[str] = None) -> Iterator[str]:
    yield logger.line(f"$ {shjoin(cmd)}")
    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=cwd,
        )
        if input_text is not None and process.stdin is not None:
            process.stdin.write(input_text)
            process.stdin.close()
        assert process.stdout is not None
        for line in process.stdout:
            yield logger.raw(line)
        rc = process.wait()
        return rc
    except FileNotFoundError:
        yield logger.line(f"❌ Commande introuvable : {cmd[0]}")
        return 127
    except Exception as exc:
        yield logger.line(f"❌ Exception Python pendant la commande : {exc}")
        return 1


def run_capture(cmd: List[str], cwd: Optional[str] = None) -> Tuple[int, str]:
    try:
        completed = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd,
            check=False,
        )
        return completed.returncode, completed.stdout or ""
    except FileNotFoundError:
        return 127, f"Commande introuvable : {cmd[0]}"
    except Exception as exc:
        return 1, f"Exception Python pendant la commande : {exc}"


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
            return False, "Une autre opération build/import est déjà en cours."
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


def mode_flags(mode: str) -> Dict[str, bool]:
    mode = (mode or "normal").strip().lower()
    return {
        "force": mode == "force",
        "check_updates": mode in {"check", "force"},
        "no_cache": mode == "nocache",
        "no_pull": mode == "nopull",
        "skip_binfmt": mode == "skip_binfmt",
    }



def docker_bin(conf: Dict[str, str]) -> str:
    return conf.get("DOCKER_BIN", "docker").strip() or "docker"


def docker_cmd(conf: Dict[str, str], args: List[str]) -> List[str]:
    return [docker_bin(conf)] + list(args)


def local_buildx_cmd(conf: Dict[str, str], args: List[str]) -> List[str]:
    return docker_cmd(conf, ["buildx"] + list(args))


def docker_cli_config_dir(conf: Dict[str, str]) -> str:
    config_dir = conf.get("DOCKER_CLI_CONFIG_DIR", "").strip()
    if not config_dir:
        config_dir = os.path.join(conf.get("DOCKER_CONF_DIR", NAS_CONF_DIR), ".docker_cli")
    os.makedirs(config_dir, exist_ok=True)
    return config_dir


def docker_cli_mounts(conf: Dict[str, str]) -> List[str]:
    mounts: List[str] = []

    def add_mount(value: str) -> None:
        value = (value or "").strip()
        if value and value not in mounts:
            mounts.append(value)

    for part in conf.get("DOCKER_CLI_MOUNTS", "").split(","):
        add_mount(part)

    # Le client docker:*-cli tourne en container frère. Les chemins du contexte,
    # des TAR et de la conf doivent donc exister avec le même chemin dans ce container.
    for key in ("DOCKER_BUILDS_DIR", "DOCKER_TAR_DIR", "DOCKER_CONF_DIR", "DOCKER_LOG_DIR"):
        path = conf.get(key, "").strip().rstrip("/")
        if not path:
            continue
        if path == "/data" or path.startswith("/data/"):
            add_mount("/data:/data")
        else:
            add_mount(f"{path}:{path}")

    return mounts


def container_buildx_cmd(conf: Dict[str, str], args: List[str]) -> List[str]:
    image = conf.get("DOCKER_CLI_IMAGE", "docker:27-cli").strip() or "docker:27-cli"
    sock = conf.get("DOCKER_SOCK", "/var/run/docker.sock").strip() or "/var/run/docker.sock"
    config_dir = docker_cli_config_dir(conf)

    cmd = [
        docker_bin(conf), "run", "--rm", "-i",
        "-v", f"{sock}:{sock}",
        "-e", f"DOCKER_HOST=unix://{sock}",
        "-v", f"{config_dir}:/root/.docker",
    ]
    for mount in docker_cli_mounts(conf):
        cmd.extend(["-v", mount])
    cmd.extend([image, "docker", "buildx"])
    cmd.extend(list(args))
    return cmd


def selected_buildx_cmd(conf: Dict[str, str], args: List[str]) -> List[str]:
    if conf.get("_BUILDX_BACKEND") == "container":
        return container_buildx_cmd(conf, args)
    return local_buildx_cmd(conf, args)


def docker_names_by_prefix(conf: Dict[str, str], prefix: str) -> List[str]:
    rc, out = run_capture(docker_cmd(conf, [
        "ps", "-a",
        "--filter", f"name={prefix}",
        "--format", "{{.Names}}",
    ]))
    if rc != 0:
        return []
    return [line.strip() for line in (out or "").splitlines() if line.strip()]


def docker_images_for_containers(conf: Dict[str, str], names: List[str]) -> List[str]:
    images: List[str] = []
    seen = set()
    for name in names:
        rc, out = run_capture(docker_cmd(conf, ["inspect", "-f", "{{.Config.Image}}", name]))
        image = (out or "").strip()
        if rc == 0 and image and image not in seen:
            seen.add(image)
            images.append(image)
    return images


def docker_volumes_by_prefix(conf: Dict[str, str], prefix: str) -> List[str]:
    rc, out = run_capture(docker_cmd(conf, [
        "volume", "ls",
        "--filter", f"name={prefix}",
        "--format", "{{.Name}}",
    ]))
    if rc != 0:
        return []
    return [line.strip() for line in (out or "").splitlines() if line.strip()]


def keep_buildx_builder(conf: Dict[str, str]) -> bool:
    return conf_bool(conf, "KEEP_BUILDX_BUILDER", "0") or conf_bool(conf, "KEEP_BUILDER", "0")


def buildkit_toml_quote(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def buildkit_http_registry_hosts(conf: Dict[str, str]) -> List[str]:
    hosts = set()
    for name, target in registry_entries(conf):
        if get_registry_mode_for(conf, name) != "http":
            continue
        host = registry_host_from_target(target)
        if host and not host.startswith("$"):
            hosts.add(host)

    registry_url = str(conf.get("REGISTRY_URL") or "").strip()
    if registry_url.lower().startswith("http://"):
        host = normalize_registry_prefix(registry_url)
        if host:
            hosts.add(host)

    return sorted(hosts)


def ensure_buildkit_http_config(conf: Dict[str, str]) -> Tuple[str, List[str], str]:
    hosts = buildkit_http_registry_hosts(conf)
    if not hosts:
        return "", [], ""

    config_dir = os.path.join(conf.get("DOCKER_CONF_DIR", NAS_CONF_DIR), ".buildkit")
    config_path = os.path.join(config_dir, "buildkitd.toml")
    lines = [
        "# Generated by Yoleo Build.",
        "# HTTP registries declared in mode.conf (0/http/local) for docker buildx.",
        "",
    ]
    for host in hosts:
        lines.extend([
            f'[registry."{buildkit_toml_quote(host)}"]',
            "  http = true",
            "  insecure = true",
            "",
        ])

    ok, message = local_write_text(config_path, "\n".join(lines).rstrip() + "\n")
    if not ok:
        return "", hosts, message
    return config_path, hosts, ""


def cleanup_buildx_builder(conf: Dict[str, str], logger: StreamLogger) -> Iterator[int]:
    """Nettoie le builder buildx et les restes Docker générés par buildx.

    Le build multi-arch laisse souvent un conteneur du type :
      buildx_buildkit_mon_builder0
    Ici on reprend la logique de docker.py : buildx rm, puis sécurité docker rm,
    puis volumes buildx, puis image BuildKit si elle n'est plus utilisée.
    """
    if not conf_bool(conf, "_BUILDX_USED", "0"):
        return 0

    builder = conf.get("BUILDER_NAME", "mon_builder").strip() or "mon_builder"

    if keep_buildx_builder(conf):
        yield logger.line(f">>> Builder conservé : {builder} (KEEP_BUILDX_BUILDER=1)")
        conf["_BUILDX_USED"] = "0"
        return 0

    container_prefix = f"buildx_buildkit_{builder}"
    containers = docker_names_by_prefix(conf, container_prefix)
    images = docker_images_for_containers(conf, containers)

    yield logger.line(f">>> Nettoyage builder buildx : {builder}")

    # Méthode propre : demande à buildx de supprimer son builder.
    rc, out = run_capture(selected_buildx_cmd(conf, ["rm", "-f", builder]))
    if rc != 0 and (out or "").strip():
        yield logger.line("⚠️ docker buildx rm a retourné un message :")
        yield logger.raw(out if out.endswith("\n") else out + "\n")

    # Sécurité : si buildx laisse encore le conteneur derrière lui, on le force.
    containers = sorted(set(containers) | set(docker_names_by_prefix(conf, container_prefix)))
    if containers:
        yield logger.line(">>> Suppression conteneur(s) buildkit : " + ", ".join(containers))
        run_capture(docker_cmd(conf, ["rm", "-f", *containers]))

    volumes = docker_volumes_by_prefix(conf, container_prefix)
    if volumes:
        yield logger.line(">>> Suppression volume(s) buildkit : " + ", ".join(volumes))
        run_capture(docker_cmd(conf, ["volume", "rm", "-f", *volumes]))

    if conf_bool(conf, "CLEAN_BUILDX_IMAGE", "1"):
        if not images:
            images = ["moby/buildkit:buildx-stable-1"]
        for image in sorted(set(images)):
            yield logger.line(f">>> Suppression image buildkit si inutilisée : {image}")
            run_capture(docker_cmd(conf, ["rmi", image]))

    conf["_BUILDX_USED"] = "0"
    return 0


def select_buildx_backend(conf: Dict[str, str], logger: StreamLogger) -> Iterator[str]:
    cached = conf.get("_BUILDX_BACKEND", "").strip()
    if cached in {"local", "container"}:
        return 0

    requested = conf.get("BUILDX_BACKEND", "auto").strip().lower()
    if requested in {"local", "native"}:
        conf["_BUILDX_BACKEND"] = "local"
        yield logger.line(">>> Backend buildx imposé : docker local")
        return 0

    if requested in {"container", "fallback", "docker-cli", "docker_cli"}:
        if not conf_bool(conf, "DOCKER_CLI_FALLBACK", "1"):
            yield logger.line("❌ Backend buildx container demandé, mais DOCKER_CLI_FALLBACK=0.")
            return 1
        conf["_BUILDX_BACKEND"] = "container"
        yield logger.line(f">>> Backend buildx imposé : {conf.get('DOCKER_CLI_IMAGE', 'docker:27-cli')}")
        yield logger.line(f">>> État client docker/buildx : {docker_cli_config_dir(conf)}")
        return 0

    rc, out = run_capture(local_buildx_cmd(conf, ["version"]))
    if rc == 0:
        conf["_BUILDX_BACKEND"] = "local"
        first_line = (out or "").strip().splitlines()[0] if (out or "").strip() else "OK"
        yield logger.line(f">>> buildx local disponible : {first_line}")
        return 0

    if not conf_bool(conf, "DOCKER_CLI_FALLBACK", "1"):
        yield logger.line("❌ docker buildx est absent du container Flask et le fallback est désactivé.")
        if out.strip():
            yield logger.line("--- Diagnostic buildx local ---")
            yield logger.raw(out if out.endswith("\n") else out + "\n")
        return 1

    conf["_BUILDX_BACKEND"] = "container"
    yield logger.line("⚠️  docker buildx absent dans ce container Flask : bascule automatique sur un client Docker officiel.")
    yield logger.line(f">>> Image client Docker : {conf.get('DOCKER_CLI_IMAGE', 'docker:27-cli')}")
    yield logger.line(f">>> État client docker/buildx : {docker_cli_config_dir(conf)}")
    yield logger.line(">>> Montage chemins : " + ", ".join(docker_cli_mounts(conf)))
    return 0
def prepare_buildx(conf: Dict[str, str], logger: StreamLogger) -> Iterator[str]:
    builder = conf.get("BUILDER_NAME", "mon_builder")
    conf["_BUILDX_USED"] = "1"

    rc_backend = yield from select_buildx_backend(conf, logger)
    if rc_backend != 0:
        return rc_backend

    buildkit_config, http_hosts, config_error = ensure_buildkit_http_config(conf)
    if http_hosts:
        yield logger.line(">>> BuildKit registre HTTP/insecure : " + ", ".join(http_hosts))
        if buildkit_config:
            yield logger.line(f">>> Config BuildKit : {buildkit_config}")
        else:
            yield logger.line(f"⚠️ Config BuildKit HTTP non écrite : {config_error or 'erreur inconnue'}")

    # Avant, on streamait directement "docker buildx inspect mon_builder".
    # Quand le builder n'existait pas, Docker écrivait une erreur normale avant qu'on le crée.
    # Ça donnait une fausse impression d'échec dans l'interface, surtout en build individuel.
    yield logger.line(f">>> Vérification builder buildx : {builder}")
    rc, out = run_capture(selected_buildx_cmd(conf, ["inspect", builder]))
    if rc == 0 and buildkit_config and not keep_buildx_builder(conf):
        yield logger.line(">>> Recréation builder buildx pour appliquer la config HTTP/insecure")
        run_capture(selected_buildx_cmd(conf, ["rm", "-f", builder]))
        rc = 1
    if rc == 0:
        yield logger.line(f">>> Utilisation builder buildx existant : {builder}")
        if not keep_buildx_builder(conf):
            yield logger.line(">>> Nettoyage prévu en fin de build, sauf KEEP_BUILDX_BUILDER=1")
        rc2 = yield from stream_process(selected_buildx_cmd(conf, ["use", builder]), logger)
        if rc2 != 0:
            return rc2
    else:
        yield logger.line(f">>> Builder buildx absent/non prêt : création de {builder}")
        # On n'affiche pas le bruit attendu de l'inspect raté. On ne garde le détail
        # que si la création échoue vraiment.
        create_args = ["create", "--name", builder, "--driver-opt", "network=host"]
        if buildkit_config:
            create_args.extend(["--config", buildkit_config])
        create_args.append("--use")
        rc2 = yield from stream_process(selected_buildx_cmd(conf, create_args), logger)
        if rc2 != 0:
            if out.strip():
                yield logger.line("--- Diagnostic inspect buildx ---")
                yield logger.raw(out if out.endswith("\n") else out + "\n")
            return rc2

    rc3 = yield from stream_process(selected_buildx_cmd(conf, ["inspect", "--bootstrap"]), logger)
    return rc3
def stream_build_one(conf: Dict[str, str], name: str, mode: str, logger: StreamLogger, index: int = 1, total: int = 1) -> Iterator[str]:
    name = normalize_item_name(name)
    if not is_valid_name(name):
        yield logger.line(f"❌ Nom Docker invalide : {name}")
        return False

    flags = mode_flags(mode)
    registry = normalize_named_map(parse_kv(local_read_text(conf["DOCKER_REGISTRY_FILE"])))
    platforms_map = normalize_named_map(parse_kv(local_read_text(conf["DOCKER_PLATFORMS_FILE"])))
    platforms = get_platforms_for(conf, name, platforms_map)

    tar_dir = conf["DOCKER_TAR_DIR"]
    log_dir = conf["DOCKER_LOG_DIR"]
    state_dir = conf.get("STATE_DIR") or os.path.join(conf["DOCKER_CONF_DIR"], ".save_state")
    context_dir = os.path.join(conf["DOCKER_BUILDS_DIR"], name)
    dockerfile = os.path.join(context_dir, "Dockerfile")
    if not os.path.isfile(dockerfile):
        dockerfile = os.path.join(context_dir, "dockerfile")
    tar_file = os.path.join(tar_dir, f"{name}.tar")
    tmp_file = f"{tar_file}.tmp"
    sha_file = f"{tar_file}.sha256"
    state_prefix = os.path.join(state_dir, name)
    state_context = f"{state_prefix}.context.sha256"
    state_platforms = f"{state_prefix}.platforms"
    state_tar_hash = f"{state_prefix}.tar.sha256"
    state_from_hash = f"{state_prefix}.from.sha256"

    os.makedirs(tar_dir, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)
    os.makedirs(state_dir, exist_ok=True)

    yield logger.line("=" * 76)
    yield logger.line(f"[{index}/{total}] BUILD -> TAR : {name}")
    yield logger.line("=" * 76)
    yield logger.line(f"Date       : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    yield logger.line(f"Contexte   : {context_dir}")
    yield logger.line(f"Plateformes: {platforms}")
    yield logger.line(f"TAR        : {tar_file}")
    yield logger.line(f"Registre   : {registry.get(name, suggested_registry(conf, name))}")
    yield logger.line(f"Mode       : {mode}")
    yield logger.line("")

    if not os.path.isdir(context_dir):
        yield logger.line(f"❌ Dossier Docker introuvable : {context_dir}")
        return False
    if not os.path.isfile(dockerfile):
        yield logger.line(f"❌ Dockerfile introuvable : {context_dir}/Dockerfile")
        return False

    try:
        context_hash = hash_context(context_dir)
    except Exception as exc:
        yield logger.line(f"❌ Hash contexte impossible : {exc}")
        return False

    old_context_hash = local_read_text(state_context).strip()
    old_platforms = local_read_text(state_platforms).strip()
    old_tar_hash = local_read_text(state_tar_hash).strip() or saved_sha_hash(sha_file)
    old_from_hash = local_read_text(state_from_hash).strip()
    check_from_updates = dockerfile_has_check_updates_marker(dockerfile)
    from_check_ok = False
    from_fingerprint = ""

    yield logger.line(f"Contexte actuel    : {context_hash}")
    yield logger.line(f"Ancien contexte    : {old_context_hash or 'aucun'}")
    yield logger.line(f"Anciennes plateformes : {old_platforms or 'aucune'}")
    if check_from_updates:
        yield logger.line("Marqueur Dockerfile : yoleo:check-updates")
        if flags["no_pull"]:
            yield logger.line("Mode No pull actif : check distant du FROM ignoré.")
        else:
            from_check_ok, from_fingerprint, from_lines = dockerfile_remote_from_fingerprint(conf, dockerfile)
            for line in from_lines:
                yield logger.line(line)
            if old_from_hash:
                yield logger.line(f"Ancien fingerprint FROM : {old_from_hash}")
            else:
                yield logger.line("Ancien fingerprint FROM : aucun")

    # Le mode global "check" ne doit pas forcer tous les Dockerfiles à rebuilder.
    # Seul le marqueur yoleo:check-updates active la comparaison distante du FROM.
    # Sans marqueur, "check" conserve donc le fonctionnement local normal : si le
    # contexte, les plateformes et le TAR sont identiques, on skippe immédiatement.
    allow_local_skip = not flags["force"] and not flags["no_cache"]
    if flags["check_updates"] and not flags["force"] and not check_from_updates:
        yield logger.line("Mode Check updates : marqueur absent, contrôle distant ignoré ; skip local conservé.")
    if check_from_updates and not flags["no_pull"]:
        allow_local_skip = (
            not flags["force"]
            and not flags["no_cache"]
            and from_check_ok
            and bool(old_from_hash)
            and from_fingerprint == old_from_hash
        )
        if allow_local_skip:
            yield logger.line("FROM distant inchangé : le skip local est autorisé.")
        elif from_check_ok:
            yield logger.line("FROM distant nouveau ou non enregistré : build avec --pull nécessaire.")
        else:
            fallback_skip = (
                not flags["force"]
                and not flags["check_updates"]
                and not flags["no_cache"]
                and bool(old_from_hash)
                and context_hash == old_context_hash
                and platforms == old_platforms
                and tar_sha_ok(tar_file, sha_file)
                and tar_matches_platforms(tar_file, platforms)
            )
            if fallback_skip:
                allow_local_skip = True
                yield logger.line("Check distant impossible : TAR local inchangé conservé, build --pull ignoré.")
            else:
                yield logger.line("Check distant impossible : build avec --pull par sécurité.")

    if (
        allow_local_skip
        and tar_sha_ok(tar_file, sha_file)
        and context_hash == old_context_hash
        and platforms == old_platforms
    ):
        yield logger.line("⏭️  SKIP DIRECT : contexte inchangé + TAR déjà bon. Rien à refaire.")
        yield logger.line(f"TAR : {tar_file}")
        return True

    if (
        allow_local_skip
        and not old_context_hash
        and not old_platforms
        and tar_sha_ok(tar_file, sha_file)
        and tar_matches_platforms(tar_file, platforms)
    ):
        current_hash = sha256_file(tar_file)
        local_write_text(state_context, context_hash + "\n")
        local_write_text(state_platforms, platforms + "\n")
        local_write_text(state_tar_hash, current_hash + "\n")
        if check_from_updates and from_fingerprint:
            local_write_text(state_from_hash, from_fingerprint + "\n")
        yield logger.line("⏭️  ADOPT + SKIP : TAR OCI existant validé, état créé, rien à refaire.")
        yield logger.line(f"TAR : {tar_file}")
        return True

    if "linux/arm64" in platforms and not flags["skip_binfmt"]:
        yield logger.line(">>> Installation/validation binfmt pour ARM64/multi-arch")
        rc = yield from stream_process(["docker", "run", "--privileged", "--rm", "tonistiigi/binfmt", "--install", "all"], logger)
        if rc != 0:
            yield logger.line("❌ Échec tonistiigi/binfmt")
            return False

    try:
        rc = yield from prepare_buildx(conf, logger)
        if rc != 0:
            yield logger.line("❌ Impossible de préparer docker buildx")
            return False

        try:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)
        except OSError:
            pass

        image = f"localbackup/{name}:backup"
        build_args = [
            "build",
            "--platform", platforms,
            "-t", image,
            "--network=host",
            "--output", f"type=oci,dest={tmp_file}",
        ]
        cmd = selected_buildx_cmd(conf, build_args)
        if not flags["no_pull"]:
            cmd.append("--pull")
        if flags["no_cache"]:
            cmd.append("--no-cache")
        cmd.append(context_dir)

        yield logger.line("")
        yield logger.line(f">>> BUILDX OCI TAR : {name}")
        started = time.time()
        rc = yield from stream_process(cmd, logger)
        duration = int(time.time() - started)
        if rc != 0:
            yield logger.line(f"❌ BUILDX ÉCHEC : {name}")
            try:
                os.remove(tmp_file)
            except OSError:
                pass
            return False

        if not os.path.isfile(tmp_file):
            yield logger.line(f"❌ TAR temporaire introuvable après build : {tmp_file}")
            return False

        new_tar_hash = sha256_file(tmp_file)
        tmp_size = human_size(os.path.getsize(tmp_file))
        yield logger.line(f"✅ BUILDX OK : {name} ({platforms}, {tmp_size}, {duration}s)")

        if (
            not flags["force"]
            and os.path.isfile(tar_file)
            and tar_sha_ok(tar_file, sha_file)
            and old_tar_hash
            and new_tar_hash == old_tar_hash
        ):
            try:
                os.remove(tmp_file)
            except OSError:
                pass
            local_write_text(state_context, context_hash + "\n")
            local_write_text(state_platforms, platforms + "\n")
            local_write_text(state_tar_hash, new_tar_hash + "\n")
            if check_from_updates and from_fingerprint:
                local_write_text(state_from_hash, from_fingerprint + "\n")
            yield logger.line("⏭️  SKIP TAR : résultat identique, ancien TAR gardé.")
            yield logger.line(f"TAR : {tar_file}")
            return True

        try:
            os.replace(tmp_file, tar_file)
            write_sha_file(tar_file, sha_file, new_tar_hash)
            local_write_text(state_context, context_hash + "\n")
            local_write_text(state_platforms, platforms + "\n")
            local_write_text(state_tar_hash, new_tar_hash + "\n")
            if check_from_updates and from_fingerprint:
                local_write_text(state_from_hash, from_fingerprint + "\n")
            try:
                os.sync()
            except AttributeError:
                pass
        except Exception as exc:
            yield logger.line(f"❌ Écriture TAR finale impossible : {exc}")
            return False

        final_size = human_size(os.path.getsize(tar_file))
        yield logger.line(f"✅ TAR OK : {tar_file} ({final_size})")
        yield logger.line(f"✅ SHA OK : {sha_file}")
        yield logger.line(f"✅ ÉTAT OK : {state_prefix}.*")
        return True
    finally:
        yield from cleanup_buildx_builder(conf, logger)


def stream_build_action(conf: Dict[str, str], action: str, name: str, mode: str) -> Iterator[str]:
    name = normalize_item_name(name)
    os.makedirs(conf["DOCKER_LOG_DIR"], exist_ok=True)
    global_log = os.path.join(conf["DOCKER_LOG_DIR"], "builds_python.log")
    try:
        open(global_log, "w", encoding="utf-8").close()
    except Exception:
        pass
    logger = StreamLogger(global_log)
    lock = OperationLock(conf.get("LOCK_FILE", "/tmp/flask_builds_python.lock"))
    ok_lock, lock_msg = lock.acquire()
    if not ok_lock:
        yield logger.line(f"❌ {lock_msg}")
        logger.close()
        return
    try:
        projects = list_project_names(conf["DOCKER_BUILDS_DIR"])
        if action == "build_one":
            projects = [name]
        else:
            projects = [p for p in projects if os.path.isfile(os.path.join(conf["DOCKER_BUILDS_DIR"], p, "Dockerfile")) or os.path.isfile(os.path.join(conf["DOCKER_BUILDS_DIR"], p, "dockerfile"))]
        total = len(projects)
        if total == 0:
            yield logger.line("❌ Aucun Docker à builder.")
            return
        done = 0
        failed = 0
        for idx, project in enumerate(projects, start=1):
            per_log = os.path.join(conf["DOCKER_LOG_DIR"], f"save_{project}.log")
            try:
                open(per_log, "w", encoding="utf-8").close()
            except Exception:
                pass
            per_logger = StreamLogger(global_log, per_log)
            try:
                ok = yield from stream_build_one(conf, project, mode, per_logger, idx, total)
            finally:
                per_logger.close()
            if ok:
                done += 1
            else:
                failed += 1
            pct = int((idx / total) * 100)
            yield logger.line(f"@@PROGRESS {json.dumps({'action': 'build', 'current': idx, 'total': total, 'percent': pct, 'done': done, 'failed': failed, 'name': project}, ensure_ascii=False)}")
        yield logger.line("")
        yield logger.line("==================== RÉSUMÉ BUILD ====================")
        yield logger.line(f"Total   : {total}")
        yield logger.line(f"OK      : {done}")
        yield logger.line(f"Erreurs : {failed}")
        yield logger.line(f"Log     : {global_log}")
        yield logger.line("======================================================")
        yield logger.line("✅ BUILD terminé." if failed == 0 else "⚠️ BUILD terminé avec erreurs.")
    finally:
        lock.release()
        logger.close()



def stream_build_registry_action(conf: Dict[str, str], action: str, name: str, mode: str) -> Iterator[str]:
    """Workflow principal Build : build TAR puis envoi registre.

    Cette fonction est volontairement séparée de stream_build_action et
    stream_registry_action pour que /build/main puisse chaîner les deux phases
    dans un seul job, sans modifier les anciennes pages Build seul et TAR -> registre.
    La structure reste identique : le build crée un vrai .tar dans DOCKER_TAR_DIR,
    puis ce TAR est envoyé vers le registre si le statut registre le demande.
    """
    name = normalize_item_name(name)
    os.makedirs(conf["DOCKER_LOG_DIR"], exist_ok=True)
    global_log = os.path.join(conf["DOCKER_LOG_DIR"], "build_main.log")
    try:
        open(global_log, "w", encoding="utf-8").close()
    except Exception:
        pass

    logger = StreamLogger(global_log)
    lock = OperationLock(conf.get("LOCK_FILE", "/tmp/flask_builds_python.lock"))
    ok_lock, lock_msg = lock.acquire()
    if not ok_lock:
        yield logger.line(f"❌ {lock_msg}")
        logger.close()
        return

    try:
        projects = list_project_names(conf["DOCKER_BUILDS_DIR"])
        if action == "build_registry_one":
            projects = [name]
        else:
            projects = [
                p for p in projects
                if os.path.isfile(os.path.join(conf["DOCKER_BUILDS_DIR"], p, "Dockerfile"))
                or os.path.isfile(os.path.join(conf["DOCKER_BUILDS_DIR"], p, "dockerfile"))
            ]

        total = len(projects)
        if total == 0:
            yield logger.line("❌ Aucun Docker à builder.")
            return

        yield logger.line("=" * 76)
        yield logger.line("BUILD PRINCIPAL : Build -> TAR -> Registre")
        yield logger.line("=" * 76)
        yield logger.line(f"Entrées : {total}")
        yield logger.line(f"Mode build : {mode}")
        yield logger.line(f"Log : {global_log}")
        yield logger.line("")

        done = 0
        failed = 0
        regctl = None
        registry_login_done = False
        registry_map = dict(registry_entries(conf))
        step_total = max(total * 2, 1)

        for idx, project in enumerate(projects, start=1):
            yield logger.line("")
            yield logger.line(f"▶ Étape {idx}/{total} : {project}")
            yield logger.line("🧱 Phase 1/2 : build du TAR")
            build_pct = int((((idx - 1) * 2) / step_total) * 100)
            yield logger.line(f"@@PROGRESS {json.dumps({'action': 'build', 'phase': 'build', 'phase_label': 'Build en cours', 'running_text': 'Build…', 'current': idx, 'total': total, 'percent': build_pct, 'done': done, 'failed': failed, 'name': project}, ensure_ascii=False)}")

            per_log = os.path.join(conf["DOCKER_LOG_DIR"], f"save_{project}.log")
            try:
                open(per_log, "w", encoding="utf-8").close()
            except Exception:
                pass
            per_logger = StreamLogger(global_log, per_log)
            try:
                ok_build = yield from stream_build_one(conf, project, mode, per_logger, idx, total)
            finally:
                per_logger.close()

            build_done_pct = int(((((idx - 1) * 2) + 1) / step_total) * 100)
            if ok_build:
                yield logger.line(f"@@PROGRESS {json.dumps({'action': 'build', 'phase': 'build_done', 'phase_label': 'Build terminé', 'running_text': 'Build terminé…', 'current': idx, 'total': total, 'percent': build_done_pct, 'done': done + 1, 'failed': failed, 'name': project}, ensure_ascii=False)}")
            else:
                yield logger.line(f"@@PROGRESS {json.dumps({'action': 'build', 'phase': 'build_error', 'phase_label': 'Erreur build', 'running_text': 'Relancer', 'current': idx, 'total': total, 'percent': build_done_pct, 'done': done, 'failed': failed + 1, 'name': project}, ensure_ascii=False)}")

            if not ok_build:
                failed += 1
                yield logger.line(f"⛔ Envoi registre ignoré pour {project} : build en erreur.")
                continue

            target = registry_map.get(project, "")
            yield logger.line("")
            yield logger.line("📤 Phase 2/2 : vérification/envoi vers le registre")
            yield logger.line(f"@@PROGRESS {json.dumps({'action': 'registry', 'phase': 'registry', 'phase_label': 'Envoi au registre', 'running_text': 'Envoi registre…', 'current': idx, 'total': total, 'percent': build_done_pct, 'done': done, 'failed': failed, 'name': project}, ensure_ascii=False)}")

            if not target:
                failed += 1
                yield logger.line(f"❌ Registre cible manquant pour {project}. Complète la base de données avant Build principal.")
                yield logger.line(f"@@PROGRESS {json.dumps({'action': 'registry', 'phase': 'registry_error', 'phase_label': 'Erreur registre', 'running_text': 'Registre erreur', 'current': idx, 'total': total, 'percent': build_done_pct, 'done': done, 'failed': failed, 'name': project}, ensure_ascii=False)}")
                continue

            try:
                status = registry_status_for(conf, project)
            except Exception as exc:
                status = {"state": "needed", "needs_action": True, "can_run": True, "message": f"Statut registre impossible : {exc}"}
                yield logger.line(f"⚠️ {status['message']}")

            if status.get("state") == "current" and not status.get("needs_action"):
                done += 1
                yield logger.line(f"⏭️  SKIP REGISTRE : {project} déjà à jour dans le registre.")
                registry_pct = int(((idx * 2) / step_total) * 100)
                yield logger.line(f"@@PROGRESS {json.dumps({'action': 'registry', 'phase': 'registry_done', 'phase_label': 'Registre à jour', 'running_text': 'Registre à jour…', 'current': idx, 'total': total, 'percent': registry_pct, 'done': done, 'failed': failed, 'name': project}, ensure_ascii=False)}")
                continue

            if not status.get("can_run", True):
                failed += 1
                yield logger.line(f"❌ Envoi registre impossible pour {project} : {status.get('message') or status.get('label') or 'statut non exécutable'}")
                yield logger.line(f"@@PROGRESS {json.dumps({'action': 'registry', 'phase': 'registry_error', 'phase_label': 'Erreur registre', 'running_text': 'Registre erreur', 'current': idx, 'total': total, 'percent': build_done_pct, 'done': done, 'failed': failed, 'name': project}, ensure_ascii=False)}")
                continue

            if regctl is None:
                regctl = yield from ensure_regctl_ready(conf, logger)
                if not regctl:
                    failed += 1
                    yield logger.line("❌ regctl indisponible : arrêt du workflow principal.")
                    break

            if not registry_login_done:
                login_entry = next(((p, registry_map.get(p, "")) for p in projects if registry_map.get(p, "") and should_login_registry(conf, p)), None)
                if login_entry is None:
                    yield logger.line(">>> Login registre ignoré : toutes les entrées sont en mode HTTP/local.")
                else:
                    ok_login = yield from ensure_registry_login(conf, login_entry[1], regctl, logger)
                    if not ok_login:
                        failed += 1
                        yield logger.line("❌ Login registre impossible : arrêt du workflow principal.")
                        break
                registry_login_done = True

            ok_registry = yield from stream_import_one(conf, project, target, regctl, False, logger, idx, total)
            if ok_registry:
                done += 1
            else:
                failed += 1
            registry_pct = int(((idx * 2) / step_total) * 100)
            yield logger.line(f"@@PROGRESS {json.dumps({'action': 'registry', 'phase': 'registry_done', 'phase_label': 'Envoi registre terminé', 'running_text': 'Envoi terminé…', 'current': idx, 'total': total, 'percent': registry_pct, 'done': done, 'failed': failed, 'name': project}, ensure_ascii=False)}")

        try:
            os.sync()
        except AttributeError:
            pass

        yield logger.line("")
        yield logger.line("==================== RÉSUMÉ BUILD PRINCIPAL ====================")
        yield logger.line(f"Total        : {total}")
        yield logger.line(f"OK registre  : {done}")
        yield logger.line(f"Erreurs      : {failed}")
        yield logger.line(f"Log          : {global_log}")
        yield logger.line("===============================================================")
        yield logger.line("✅ BUILD PRINCIPAL terminé." if failed == 0 else "⚠️ BUILD PRINCIPAL terminé avec erreurs.")
    finally:
        lock.release()
        logger.close()

def ensure_regctl_ready(conf: Dict[str, str], logger: StreamLogger) -> Iterator[str]:
    path = regctl_path(conf)
    if not path:
        yield logger.line(f"❌ regctl introuvable : {conf.get('REGCTL')}")
        return None
    if os.path.isabs(path):
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass
    rc = yield from stream_process([path, "version"], logger)
    if rc != 0:
        yield logger.line(f"❌ regctl existe mais ne s'exécute pas : {path}")
        return None
    return path


def ensure_registry_login(conf: Dict[str, str], target: str, regctl: str, logger: StreamLogger) -> Iterator[str]:
    login_file = conf.get("DOCKER_REGISTRY_LOGIN_FILE") or os.path.join(conf["DOCKER_CONF_DIR"], "registre_login.conf")
    values = read_env_login_file(login_file)
    host = values.get("REGISTRY_HOST") or registry_host_from_target(target)
    user = values.get("REGISTRY_USER", "")
    password = values.get("REGISTRY_PASS", "")
    pass_file = values.get("REGISTRY_PASS_FILE", "")

    if not host:
        yield logger.line("⚠️ Hôte registre introuvable. Login automatique ignoré.")
        return True
    if not user:
        yield logger.line(f"⚠️ Login registre non configuré : {login_file}")
        yield logger.line("⚠️ L'import continue, mais il échouera si le registre demande une authentification.")
        return True
    if not password and pass_file and os.path.isfile(pass_file):
        password = local_read_text(pass_file).strip()
    if not password:
        yield logger.line(f"⚠️ Mot de passe registre non configuré : {login_file}")
        yield logger.line("⚠️ Renseigne REGISTRY_PASS ou REGISTRY_PASS_FILE.")
        return True

    yield logger.line(f">>> Login registre : {host}")
    rc = yield from stream_process([regctl, "registry", "login", host, "-u", user, "--pass-stdin"], logger, input_text=password)
    if rc == 0:
        yield logger.line(f"✅ Login registre OK : {host}")
        return True
    yield logger.line(f"❌ Login registre échoué : {host}")
    return False


def verify_sha_for_tar(tar_path: str) -> Tuple[bool, str]:
    sha_file = f"{tar_path}.sha256"
    if not os.path.isfile(sha_file):
        return True, "sha256 absent, vérification ignorée"
    saved = saved_sha_hash(sha_file)
    if not saved:
        return False, f"sha256 illisible : {sha_file}"
    current = sha256_file(tar_path)
    if current == saved:
        return True, "sha256 OK"
    return False, f"sha256 ÉCHEC : attendu {saved}, actuel {current}"


def registry_entries(conf: Dict[str, str]) -> List[Tuple[str, str]]:
    # On garde l ordre du fichier registre.conf, comme dans tes scripts shell.
    rows: List[Tuple[str, str]] = []
    text = local_read_text(conf["DOCKER_REGISTRY_FILE"])
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, target = line.split("=", 1)
        key = key.strip()
        target = strip_quotes(target.strip())
        if key == "_default":
            continue
        name = key[:-4] if key.endswith(".tar") else key
        if is_valid_name(name) and target:
            rows.append((name, target))
    return rows


def stream_import_one(conf: Dict[str, str], name: str, target: str, regctl: str, dry_run: bool, logger: StreamLogger, index: int, total: int) -> Iterator[str]:
    tar_path = os.path.join(conf["DOCKER_TAR_DIR"], f"{name}.tar")
    yield logger.line("=" * 76)
    yield logger.line(f"[{index}/{total}] TAR -> REGISTRE : {name}")
    yield logger.line("=" * 76)
    yield logger.line(f"TAR   : {tar_path}")
    yield logger.line(f"IMAGE : {target}")
    mode = get_registry_mode_for(conf, name)
    host_args = regctl_host_args_for(conf, name, target)
    yield logger.line(f"MODE  : {'HTTP local' if mode == 'http' else 'HTTPS'}")
    yield logger.line(f"LOGIN : {'non' if mode == 'http' else 'oui si configuré'}")
    if host_args:
        yield logger.line(f"REGCTL: {' '.join(host_args)}")

    if not os.path.isfile(tar_path):
        yield logger.line(f"❌ MANQUANT : {tar_path}")
        return False
    yield logger.line(f"Taille : {human_size(os.path.getsize(tar_path))}")

    ok_sha, sha_msg = verify_sha_for_tar(tar_path)
    yield logger.line(f">>> Vérification SHA256 : {sha_msg}")
    if not ok_sha:
        return False

    if dry_run:
        yield logger.line("DRY-RUN : import ignoré")
        return True

    started = time.time()
    yield logger.line(">>> Import direct vers le registre...")
    rc = yield from stream_process([regctl, *host_args, "image", "import", target, tar_path], logger)
    duration = int(time.time() - started)
    if rc == 0:
        mark_registry_import_state(conf, name, target, tar_path)
        yield logger.line(f"✅ OK : {target} ({duration}s)")
        yield logger.line("✅ ÉTAT REGISTRE OK : import mémorisé localement.")
        return True
    yield logger.line(f"❌ IMPORT ÉCHEC : {target}")
    return False


def stream_registry_action(conf: Dict[str, str], action: str, name: str, dry_run: bool) -> Iterator[str]:
    name = normalize_item_name(name)
    os.makedirs(conf["DOCKER_LOG_DIR"], exist_ok=True)
    log_path = os.path.join(conf["DOCKER_LOG_DIR"], "registry_python.log")
    try:
        open(log_path, "w", encoding="utf-8").close()
    except Exception:
        pass
    logger = StreamLogger(log_path)
    lock = OperationLock(conf.get("LOCK_FILE", "/tmp/flask_builds_python.lock"))
    ok_lock, lock_msg = lock.acquire()
    if not ok_lock:
        yield logger.line(f"❌ {lock_msg}")
        logger.close()
        return
    try:
        entries = registry_entries(conf)
        if action in {"registry_one", "dry_registry_one"}:
            entries = [(n, t) for n, t in entries if n == name]
        total = len(entries)
        if total == 0:
            yield logger.line("❌ Aucune entrée registre valide à envoyer.")
            return

        regctl = yield from ensure_regctl_ready(conf, logger)
        if not regctl:
            return

        yield logger.line(f"MODE_FILE : {effective_mode_file(conf)}")
        if not dry_run:
            login_entry = next(((entry_name, target) for entry_name, target in entries if should_login_registry(conf, entry_name)), None)
            if login_entry is None:
                yield logger.line(">>> Login registre ignoré : toutes les entrées sont en mode HTTP/local.")
            else:
                ok_login = yield from ensure_registry_login(conf, login_entry[1], regctl, logger)
                if not ok_login:
                    return

        done = 0
        missing_or_failed = 0
        yield logger.line(f"@@PROGRESS {json.dumps({'action': 'registry', 'current': 0, 'total': total, 'percent': 0, 'done': 0, 'failed': 0}, ensure_ascii=False)}")
        for idx, (entry_name, target) in enumerate(entries, start=1):
            ok = yield from stream_import_one(conf, entry_name, target, regctl, dry_run, logger, idx, total)
            if ok:
                done += 1
            else:
                missing_or_failed += 1
            pct = int((idx / total) * 100)
            yield logger.line(f"@@PROGRESS {json.dumps({'action': 'registry', 'current': idx, 'total': total, 'percent': pct, 'done': done, 'failed': missing_or_failed, 'name': entry_name}, ensure_ascii=False)}")

        try:
            os.sync()
        except AttributeError:
            pass
        yield logger.line("")
        yield logger.line("==================== RÉSUMÉ REGISTRE ====================")
        yield logger.line(f"Total      : {total}")
        yield logger.line(f"Importés   : {done}")
        yield logger.line(f"Erreurs    : {missing_or_failed}")
        yield logger.line(f"Log        : {log_path}")
        yield logger.line("==========================================================")
        yield logger.line("✅ IMPORT REGISTRE terminé." if missing_or_failed == 0 else "⚠️ IMPORT REGISTRE terminé avec erreurs.")
    finally:
        lock.release()
        logger.close()
