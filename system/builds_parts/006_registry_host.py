def registry_host_resolve_path(path: str, base: Optional[str] = None) -> str:
    path = strip_quotes(path or "").strip()
    if not path:
        return ""
    if os.path.isabs(path):
        return os.path.abspath(path)
    candidates: List[str] = []
    if base:
        candidates.append(os.path.abspath(os.path.join(base, path)))
    candidates.append(os.path.abspath(os.path.join(BASE_DIR, path)))
    candidates.append(os.path.abspath(path))
    for candidate in candidates:
        if os.path.exists(candidate) or os.path.exists(os.path.dirname(candidate) or "."):
            return candidate
    return candidates[0]


def registry_host_df_mount(path: str) -> str:
    try:
        proc = subprocess.run(["df", "-P", path], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 6 and parts[5]:
                return parts[5]
    except Exception:
        pass
    return os.path.dirname(path.rstrip("/")) or "/"


def registry_host_settings(conf: Dict[str, str]) -> Dict[str, str]:
    host_conf = conf.get("REGISTRY_HOST_CONF_FILE", "").strip()
    if not host_conf:
        host_conf_dir = conf.get("HOST_CONF_DIR", "../conf").strip() or "../conf"
        host_conf = os.path.join(host_conf_dir, "registry.conf")
    human_conf = registry_host_resolve_path(host_conf)
    conf_dir = os.path.dirname(human_conf)

    base_dir = conf.get("REGISTRY_HOST_BASE_DIR", "").strip()
    if base_dir:
        # Les chemins REGISTRY_HOST_* viennent du fichier conf. Un chemin relatif
        # doit donc être compris depuis le dossier conf.
        base_dir = registry_host_resolve_path(base_dir, conf_dir)
    else:
        base_dir = os.path.abspath(os.path.join(conf_dir, ".."))

    # Le serveur Registry conserve ses binaires amd64/arm64 dans son propre
    # dossier configurable. Il ne depend plus du chemin d'un autre executable.
    explicit_bin_dir = strip_quotes(conf.get("REGISTRY_HOST_BIN_DIR", "../bin")).strip() or "../bin"
    bin_dir = registry_host_resolve_path(explicit_bin_dir, conf_dir)

    log_dir = registry_host_resolve_path(conf.get("REGISTRY_HOST_LOG_DIR", "/var/log/registry"), conf_dir)
    yaml_conf = registry_host_resolve_path(conf.get("REGISTRY_HOST_YAML_FILE", os.path.join(conf_dir, "registry.yml")), conf_dir)
    log_file = registry_host_resolve_path(conf.get("REGISTRY_HOST_LOG_FILE", os.path.join(log_dir, "registry.log")), conf_dir)
    pid_file = strip_quotes(conf.get("REGISTRY_HOST_PID_FILE", "/var/run/registry_labo_host.pid")).strip() or "/var/run/registry_labo_host.pid"
    runtime_bin = strip_quotes(conf.get("REGISTRY_HOST_RUNTIME_BIN", "/tmp/registry-host-labo")).strip() or "/tmp/registry-host-labo"
    service_name = strip_quotes(conf.get("REGISTRY_HOST_SERVICE_NAME", "registry-labo-host.service")).strip() or "registry-labo-host.service"
    service_file = strip_quotes(conf.get("REGISTRY_HOST_SERVICE_FILE", f"/etc/systemd/system/{service_name}")).strip() or f"/etc/systemd/system/{service_name}"
    mnt_root = strip_quotes(conf.get("REGISTRY_HOST_MNT_ROOT", "")).strip() or registry_host_df_mount(base_dir)
    mnt_ready_raw = strip_quotes(conf.get("REGISTRY_HOST_MNT_READY_DIR", "")).strip() or base_dir
    mnt_ready_dir = registry_host_resolve_path(mnt_ready_raw, conf_dir)

    return {
        "base_dir": base_dir,
        "conf_dir": conf_dir,
        "human_conf": human_conf,
        "yaml_conf": yaml_conf,
        "bin_dir": bin_dir,
        "bin_amd64": os.path.join(bin_dir, "registry_amd64"),
        "bin_arm64": os.path.join(bin_dir, "registry_arm64"),
        "runtime_bin": runtime_bin,
        "log_dir": log_dir,
        "log_file": log_file,
        "pid_file": pid_file,
        "service_name": service_name,
        "service_file": service_file,
        "mnt_root": mnt_root,
        "mnt_ready_dir": mnt_ready_dir,
    }


def registry_host_detect_bin(settings: Dict[str, str]) -> str:
    arch = subprocess.run(["uname", "-m"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5).stdout.strip()
    if arch in {"x86_64", "amd64"}:
        return settings["bin_amd64"]
    if arch in {"aarch64", "arm64"}:
        return settings["bin_arm64"]
    raise RuntimeError(f"architecture inconnue: {arch or '?'}")


def registry_host_load_conf(settings: Dict[str, str]) -> Dict[str, str]:
    path = settings["human_conf"]
    if not os.path.isfile(path):
        raise RuntimeError(f"../conf/registry.conf introuvable ! chemin attendu : {path}")
    raw = read_config_file(path)
    required = ("PORT", "BIND_ADDR", "DATA_DIR", "LOG_LEVEL", "DELETE_ENABLED", "HTTP_SECRET")
    data: Dict[str, str] = {}
    for key in required:
        value = strip_quotes(raw.get(key, "")).strip()
        if not value:
            raise RuntimeError(f"{key} manquant ou vide dans ../conf/registry.conf")
        data[key] = value

    if not data["PORT"].isdigit():
        raise RuntimeError(f"PORT invalide dans {path} : {data['PORT']}")
    if data["DELETE_ENABLED"] not in {"true", "false"}:
        raise RuntimeError(f"DELETE_ENABLED doit être true ou false dans {path}")
    if data["LOG_LEVEL"] not in {"debug", "info", "warn", "warning", "error", "fatal", "panic"}:
        raise RuntimeError(f"LOG_LEVEL invalide dans {path} : {data['LOG_LEVEL']}")

    data_dir = data["DATA_DIR"]
    if os.path.isabs(data_dir):
        data["DATA_DIR"] = os.path.abspath(data_dir)
    else:
        data["DATA_DIR"] = os.path.abspath(os.path.join(settings["conf_dir"], data_dir))
    return data


def registry_host_render_yaml(values: Dict[str, str]) -> str:
    return "\n".join([
        "version: 0.1",
        "",
        "log:",
        f"  level: {values['LOG_LEVEL']}",
        "",
        "storage:",
        "  filesystem:",
        f"    rootdirectory: {values['DATA_DIR']}",
        "  delete:",
        f"    enabled: {values['DELETE_ENABLED']}",
        "",
        "http:",
        f"  addr: {values['BIND_ADDR']}:{values['PORT']}",
        f"  secret: {values['HTTP_SECRET']}",
        "  headers:",
        "    X-Content-Type-Options:",
        "      - nosniff",
        "",
    ])


def registry_host_ensure_yaml(conf: Dict[str, str], lines: Optional[List[str]] = None) -> Tuple[Dict[str, str], Dict[str, str]]:
    settings = registry_host_settings(conf)
    values = registry_host_load_conf(settings)
    os.makedirs(settings["conf_dir"], exist_ok=True)
    os.makedirs(values["DATA_DIR"], exist_ok=True)
    content = registry_host_render_yaml(values)
    yaml_path = settings["yaml_conf"]
    old = local_read_text(yaml_path)
    if old == content:
        if lines is not None:
            lines.append(f"OK: registry.yml déjà correct, aucune réécriture : {yaml_path}")
        return settings, values
    if os.path.exists(yaml_path):
        backup = f"{yaml_path}.backup_{time.strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(yaml_path, backup)
        if lines is not None:
            lines.append(f"registry.yml différent, sauvegarde : {backup}")
    else:
        if lines is not None:
            lines.append(f"registry.yml absent, création : {yaml_path}")
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="registry-yml.", dir="/tmp", text=True)
    with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    shutil.move(tmp_path, yaml_path)
    if lines is not None:
        lines.append(f"OK: registry.yml généré : {yaml_path}")
    return settings, values


def registry_host_pid_alive(pid: int) -> bool:
    try:
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def registry_host_pids_by_cmd(settings: Dict[str, str]) -> List[int]:
    target = [settings["runtime_bin"], "serve", settings["yaml_conf"]]
    pids: List[int] = []
    proc_dir = "/proc"
    if not os.path.isdir(proc_dir):
        return pids
    for name in os.listdir(proc_dir):
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc_dir, name, "cmdline"), "rb") as handle:
                parts = [p.decode("utf-8", "ignore") for p in handle.read().split(b"\0") if p]
        except Exception:
            continue
        if len(parts) >= 3 and parts[:3] == target:
            pids.append(int(name))
    return sorted(set(pids))


def registry_host_adopt_pid(settings: Dict[str, str], lines: Optional[List[str]] = None) -> Optional[int]:
    pid_path = settings["pid_file"]
    try:
        old = int(local_read_text(pid_path).strip().split()[0]) if os.path.exists(pid_path) else 0
    except Exception:
        old = 0
    if old and registry_host_pid_alive(old):
        return old
    pids = registry_host_pids_by_cmd(settings)
    if pids:
        os.makedirs(os.path.dirname(pid_path), exist_ok=True)
        local_write_text(pid_path, str(pids[0]) + "\n")
        if lines is not None:
            lines.append(f"PID adopté depuis process existant : {pids[0]}")
        return pids[0]
    return None


def registry_host_run_cmd(cmd: List[str], timeout: int = 30) -> Tuple[int, str]:
    try:
        proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return proc.returncode, proc.stdout or ""
    except Exception as exc:
        return 127, str(exc)


def registry_host_systemctl(settings: Dict[str, str], args: List[str], timeout: int = 60) -> Tuple[int, str]:
    return registry_host_run_cmd(["systemctl", *args, settings["service_name"]], timeout=timeout)


def registry_host_is_service_installed(settings: Dict[str, str]) -> bool:
    if os.path.exists(settings["service_file"]):
        return True
    rc, _ = registry_host_systemctl(settings, ["status", "--no-pager"], timeout=8)
    return rc == 0


def registry_host_service_state(settings: Dict[str, str]) -> Tuple[str, str]:
    active_rc, active_out = registry_host_systemctl(settings, ["is-active", "--quiet"], timeout=8)
    enabled_rc, enabled_out = registry_host_systemctl(settings, ["is-enabled", "--quiet"], timeout=8)
    active = "active" if active_rc == 0 else "inactive"
    enabled = "enabled" if enabled_rc == 0 else "disabled"
    if active_out and active_rc != 0:
        active = "unknown"
    if enabled_out and enabled_rc != 0 and "No such" in enabled_out:
        enabled = "not-installed"
    return active, enabled


def registry_host_port_lines(port: str) -> List[str]:
    if not port:
        return []
    rc, out = registry_host_run_cmd(["ss", "-lntp"], timeout=8)
    if rc != 0:
        return []
    return [line for line in out.splitlines() if f":{port} " in line or f":{port}\n" in line]


def registry_host_tail(path: str, max_lines: int = 80) -> str:
    try:
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-max_lines:]
        return "".join(lines).strip()
    except Exception as exc:
        return f"Erreur lecture log : {exc}"


def registry_host_storage_ready(settings: Dict[str, str]) -> bool:
    return os.path.ismount(settings["mnt_root"]) and os.path.isdir(settings["mnt_ready_dir"])


def registry_host_require_root() -> None:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise RuntimeError("Flask doit tourner en root pour gérer le service registry host.")


def registry_host_stop_processes(settings: Dict[str, str], lines: List[str]) -> None:
    killed = False
    pids: List[int] = []
    try:
        old = int(local_read_text(settings["pid_file"]).strip().split()[0]) if os.path.exists(settings["pid_file"]) else 0
        if old:
            pids.append(old)
    except Exception:
        pass
    pids.extend(registry_host_pids_by_cmd(settings))
    for pid in sorted(set(pids)):
        if not registry_host_pid_alive(pid):
            continue
        lines.append(f"Arrêt du registry labo PID {pid}...")
        try:
            os.kill(pid, 15)
            killed = True
        except OSError:
            continue
    for _ in range(5):
        if not any(registry_host_pid_alive(pid) for pid in set(pids)):
            break
        time.sleep(1)
    for pid in sorted(set(pids)):
        if registry_host_pid_alive(pid):
            lines.append(f"Forçage arrêt PID {pid}...")
            try:
                os.kill(pid, 9)
                killed = True
            except OSError:
                pass
    try:
        os.remove(settings["pid_file"])
    except OSError:
        pass
    lines.append("OK: Registry labo arrêté" if killed else "Registry labo déjà arrêté")


def registry_host_start(conf: Dict[str, str], lines: List[str]) -> bool:
    registry_host_require_root()
    settings, values = registry_host_ensure_yaml(conf, lines)
    pid = registry_host_adopt_pid(settings, lines)
    if pid:
        lines.append(f"Registry labo déjà démarré PID {pid}")
        return True

    os.makedirs(settings["log_dir"], exist_ok=True)
    os.makedirs(settings["bin_dir"], exist_ok=True)
    if not registry_host_storage_ready(settings):
        lines.append(f"ERREUR: stockage pas prêt : {settings['mnt_root']} + {settings['mnt_ready_dir']}")
        return False

    if registry_host_is_service_installed(settings):
        lines.append(f"Démarrage via systemd : {settings['service_name']}")
        try:
            registry_host_write_service_file(settings, values, lines)
            rc_reload, out_reload = registry_host_run_cmd(["systemctl", "daemon-reload"], timeout=60)
            if out_reload.strip():
                lines.extend(out_reload.rstrip().splitlines())
            if rc_reload != 0:
                lines.append(f"ERREUR: systemctl daemon-reload a échoué (code {rc_reload}).")
                return False
        except Exception as exc:
            lines.append(f"ERREUR: mise à jour du service registry impossible : {exc}")
            return False
        rc, out = registry_host_systemctl(settings, ["start"], timeout=190)
        if out.strip():
            lines.extend(out.rstrip().splitlines())
        time.sleep(1)
        pid = registry_host_adopt_pid(settings, lines)
        if rc == 0 and (pid or registry_host_service_state(settings)[0] == "active"):
            lines.append(f"OK: service démarré : {settings['service_name']}")
            return True
        lines.append(f"ERREUR: systemctl start a échoué (code {rc}).")
        return False

    port_lines = registry_host_port_lines(values["PORT"])
    if port_lines:
        lines.append(f"ERREUR: le port {values['PORT']} est déjà utilisé.")
        lines.extend(port_lines)
        return False

    source_bin = registry_host_detect_bin(settings)
    if not os.path.isfile(source_bin):
        lines.append(f"ERREUR: binaire introuvable: {source_bin}")
        return False
    shutil.copy2(source_bin, settings["runtime_bin"])
    os.chmod(settings["runtime_bin"], 0o755)
    if not os.access(settings["runtime_bin"], os.X_OK):
        lines.append(f"ERREUR: binaire runtime non exécutable: {settings['runtime_bin']}")
        return False

    lines.append("Démarrage du registry labo host...")
    lines.append(f"Binaire source  : {source_bin}")
    lines.append(f"Binaire runtime : {settings['runtime_bin']}")
    lines.append(f"Conf humain     : {settings['human_conf']}")
    lines.append(f"Config YAML     : {settings['yaml_conf']}")
    lines.append(f"Data            : {values['DATA_DIR']}")
    lines.append(f"Log             : {settings['log_file']}")
    lines.append(f"PID             : {settings['pid_file']}")
    lines.append(f"Port            : {values['PORT']}")

    env = os.environ.copy()
    env["OTEL_TRACES_EXPORTER"] = "none"
    log_handle = open(settings["log_file"], "ab", buffering=0)
    try:
        proc = subprocess.Popen([settings["runtime_bin"], "serve", settings["yaml_conf"]], stdout=log_handle, stderr=subprocess.STDOUT, env=env)
    finally:
        log_handle.close()
    local_write_text(settings["pid_file"], str(proc.pid) + "\n")
    time.sleep(1)
    pid = registry_host_adopt_pid(settings, lines)
    if pid:
        lines.append(f"OK: Registry labo démarré PID {pid}")
        lines.append(f"URL: http://{values['BIND_ADDR']}:{values['PORT']}/v2/")
        return True
    lines.append("ERREUR: Registry labo n'a pas démarré")
    tail = registry_host_tail(settings["log_file"])
    if tail:
        lines.append(tail)
    try:
        os.remove(settings["pid_file"])
    except OSError:
        pass
    return False


def registry_host_stop(conf: Dict[str, str], lines: List[str]) -> bool:
    registry_host_require_root()
    settings = registry_host_settings(conf)
    if registry_host_is_service_installed(settings):
        lines.append(f"Arrêt via systemd : {settings['service_name']}")
        rc, out = registry_host_systemctl(settings, ["stop"], timeout=80)
        if out.strip():
            lines.extend(out.rstrip().splitlines())
        if rc != 0:
            lines.append(f"Info: systemctl stop a retourné {rc}, nettoyage manuel quand même.")
    registry_host_stop_processes(settings, lines)
    return True


def registry_host_restart(conf: Dict[str, str], lines: List[str]) -> bool:
    registry_host_stop(conf, lines)
    time.sleep(1)
    return registry_host_start(conf, lines)


def registry_host_autostart_after_options(conf: Dict[str, str], lines: List[str]) -> bool:
    """Après validation Options, rend le registre directement utilisable.

    À ce moment-là les chemins, l'IP, le port et le mode HTTP viennent
    d'être validés par l'utilisateur. On ne devine donc rien : on applique
    la configuration choisie, on installe/actualise le service systemd,
    on active le boot et on démarre le registre.
    """
    lines.append("Initialisation automatique du registre host après validation Options...")
    try:
        settings = registry_host_settings(conf)
        active_before, _enabled_before = registry_host_service_state(settings)

        # Si un ancien process manuel traîne alors que systemd n'a pas la main,
        # on le nettoie avant d'installer/démarrer le service proprement.
        if active_before != "active" and registry_host_is_service_installed(settings):
            manual_pids = [pid for pid in registry_host_pids_by_cmd(settings) if registry_host_pid_alive(pid)]
            if manual_pids:
                lines.append("Processus registry manuel détecté : reprise propre par systemd.")
                registry_host_stop_processes(settings, lines)

        ok = registry_host_install_service(conf, lines)
        if not ok:
            return False

        # Si le service tournait déjà, registry.conf/registry.yml peuvent avoir
        # changé : on redémarre pour appliquer le nouveau port, bind ou data dir.
        if active_before == "active":
            lines.append("Redémarrage automatique du registre déjà actif pour appliquer les nouvelles options...")
            ok = registry_host_restart(conf, lines) and ok

        return ok
    except Exception as exc:
        lines.append(f"ERREUR: activation automatique du registre impossible : {exc}")
        return False


def registry_host_write_service_file(settings: Dict[str, str], values: Dict[str, str], lines: List[str]) -> None:
    source_bin = registry_host_detect_bin(settings)
    if not os.path.isfile(source_bin):
        raise RuntimeError(f"binaire introuvable: {source_bin}")
    qbin = shlex.quote(source_bin)
    qrun = shlex.quote(settings["runtime_bin"])
    qyaml = shlex.quote(settings["yaml_conf"])
    qlog = shlex.quote(settings["log_file"])
    qpid = shlex.quote(settings["pid_file"])
    qlogdir = shlex.quote(settings["log_dir"])
    qdatadir = shlex.quote(values["DATA_DIR"])
    service = f"""[Unit]
Description=Registry Docker LABO host
After=local-fs.target network-online.target
Wants=network-online.target
RequiresMountsFor={settings['mnt_root']}

[Service]
Type=simple
PIDFile={settings['pid_file']}
Environment=OTEL_TRACES_EXPORTER=none
ExecStartPre=/bin/sh -lc 'mkdir -p {qlogdir} {qdatadir}'
ExecStartPre=/bin/sh -lc 'cp {qbin} {qrun} && chmod 755 {qrun}'
ExecStart=/bin/sh -lc 'echo $$ > {qpid}; exec {qrun} serve {qyaml} >> {qlog} 2>&1'
ExecStopPost=/bin/rm -f {settings['pid_file']}
Restart=no
TimeoutStartSec=180
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
"""
    os.makedirs(os.path.dirname(settings["service_file"]), exist_ok=True)
    with open(settings["service_file"], "w", encoding="utf-8") as handle:
        handle.write(service)
    lines.append(f"OK: service écrit : {settings['service_file']}")


def registry_host_install_service(conf: Dict[str, str], lines: List[str]) -> bool:
    registry_host_require_root()
    settings, values = registry_host_ensure_yaml(conf, lines)
    already_installed = registry_host_is_service_installed(settings)
    active_before, _enabled_before = registry_host_service_state(settings)

    if already_installed:
        lines.append(f"Service déjà installé : {settings['service_name']}")
        lines.append("Mise à jour idempotente du fichier systemd, sans arrêt forcé du registre actif.")
    else:
        lines.append(f"Installation service systemd : {settings['service_name']}")
        # À la première installation seulement, on nettoie les process manuels/orphelins
        # pour que systemd prenne la main proprement.
        registry_host_stop_processes(settings, lines)

    try:
        registry_host_write_service_file(settings, values, lines)
    except RuntimeError as exc:
        lines.append(f"ERREUR: {exc}")
        return False

    commands: List[List[str]] = [["daemon-reload"], ["enable"], ["reset-failed"]]
    if active_before == "active":
        lines.append(f"OK: service déjà actif, pas de redémarrage forcé : {settings['service_name']}")
    else:
        commands.append(["start"])

    for args in commands:
        if args[0] == "daemon-reload":
            rc, out = registry_host_run_cmd(["systemctl", "daemon-reload"], timeout=60)
        elif args[0] == "reset-failed":
            rc, out = registry_host_systemctl(settings, ["reset-failed"], timeout=30)
            if rc != 0:
                rc, out = 0, out
        else:
            rc, out = registry_host_systemctl(settings, list(args), timeout=190)
        if out.strip():
            lines.extend(out.rstrip().splitlines())
        if rc != 0:
            lines.append(f"ERREUR: systemctl {' '.join(args)} a échoué (code {rc}).")
            return False

    active_after, enabled_after = registry_host_service_state(settings)
    if active_after == "active":
        lines.append(f"OK: service installé/à jour et actif : {settings['service_name']}")
    else:
        lines.append(f"OK: service installé/à jour. État systemd : {active_after}")
    lines.append(f"Boot : {enabled_after}")
    return True


def registry_host_remove_service(conf: Dict[str, str], lines: List[str]) -> bool:
    registry_host_require_root()
    settings = registry_host_settings(conf)
    lines.append(f"Suppression service systemd : {settings['service_name']}")
    for args in (["disable", "--now"],):
        rc, out = registry_host_systemctl(settings, list(args), timeout=80)
        if out.strip():
            lines.extend(out.rstrip().splitlines())
    try:
        os.remove(settings["service_file"])
        lines.append(f"OK: service supprimé : {settings['service_file']}")
    except FileNotFoundError:
        lines.append("Service déjà absent.")
    rc, out = registry_host_run_cmd(["systemctl", "daemon-reload"], timeout=60)
    if out.strip():
        lines.extend(out.rstrip().splitlines())
    registry_host_stop_processes(settings, lines)
    return True


def registry_host_enable_disable(conf: Dict[str, str], enable: bool, lines: List[str]) -> bool:
    registry_host_require_root()
    settings = registry_host_settings(conf)
    if not registry_host_is_service_installed(settings):
        lines.append("ERREUR: service non installé.")
        return False
    rc, out = registry_host_systemctl(settings, ["enable" if enable else "disable"], timeout=60)
    if out.strip():
        lines.extend(out.rstrip().splitlines())
    if rc == 0:
        lines.append("OK: service activé au démarrage." if enable else "OK: service désactivé au démarrage.")
        return True
    lines.append(f"ERREUR: systemctl {'enable' if enable else 'disable'} a échoué (code {rc}).")
    return False


def registry_host_status_payload(conf: Dict[str, str]) -> Dict[str, object]:
    settings = registry_host_settings(conf)
    conf_error = ""
    values: Dict[str, str] = {}
    try:
        values = registry_host_load_conf(settings)
    except Exception as exc:
        conf_error = str(exc)
    pid = registry_host_adopt_pid(settings)
    active, enabled = registry_host_service_state(settings)
    service_installed = registry_host_is_service_installed(settings)
    port = values.get("PORT", "")
    port_lines = registry_host_port_lines(port) if port else []
    running = bool(pid)
    try:
        source_bin = registry_host_detect_bin(settings)
    except Exception:
        source_bin = ""
    source_bin_ok = bool(source_bin and os.path.isfile(source_bin))
    runtime_bin_ok = os.path.isfile(settings["runtime_bin"])
    return {
        "ok": not bool(conf_error),
        "running": running,
        "pid": pid or "",
        "service_installed": service_installed,
        "service_active": active,
        "service_enabled": enabled,
        "conf_error": conf_error,
        "port": port,
        "bind_addr": values.get("BIND_ADDR", ""),
        "data_dir": values.get("DATA_DIR", ""),
        "log_level": values.get("LOG_LEVEL", ""),
        "delete_enabled": values.get("DELETE_ENABLED", ""),
        "storage_ready": registry_host_storage_ready(settings),
        "source_bin": source_bin,
        "source_bin_ok": source_bin_ok,
        "runtime_bin_ok": runtime_bin_ok,
        "port_listen": "\n".join(port_lines),
        "log_tail": registry_host_tail(settings["log_file"], 40),
        "paths": settings,
        "detail": "\n".join([
            f"BASE        : {settings['base_dir']}",
            f"CONF HUMAIN : {settings['human_conf']}",
            f"CONF YAML   : {settings['yaml_conf']}",
            f"BIN_DIR     : {settings['bin_dir']}",
            f"BIN SOURCE  : {source_bin or '—'} ({'OK' if source_bin_ok else 'absent'})",
            f"BIN RUNTIME : {settings['runtime_bin']} ({'OK' if runtime_bin_ok else 'absent'})",
            f"DATA_DIR    : {values.get('DATA_DIR', '—')}",
            f"LOG         : {settings['log_file']}",
            f"PID         : {settings['pid_file']}",
            f"SERVICE     : {settings['service_name']}",
            f"SERVICEFILE : {settings['service_file']}",
            f"MNT_ROOT    : {settings['mnt_root']}",
            f"READY_DIR   : {settings['mnt_ready_dir']}",
        ]),
    }


def registry_host_format_status(payload: Dict[str, object]) -> str:
    paths = payload.get("paths") or {}
    return "\n".join([
        "========== STATUS REGISTRY LABO HOST ==========",
        f"Processus   : {'actif' if payload.get('running') else 'arrêté'}",
        f"PID         : {payload.get('pid') or '—'}",
        f"Service     : {paths.get('service_name', '—')}",
        f"Systemd     : {payload.get('service_active')}",
        f"Boot        : {payload.get('service_enabled')}",
        f"Port        : {payload.get('port') or '—'}",
        f"BIND_ADDR   : {payload.get('bind_addr') or '—'}",
        f"DATA_DIR    : {payload.get('data_dir') or '—'}",
        f"Binaire src : {payload.get('source_bin') or '—'} ({'OK' if payload.get('source_bin_ok') else 'absent'})",
        f"Runtime bin : {((payload.get('paths') or {}).get('runtime_bin') if isinstance(payload.get('paths'), dict) else '') or '—'} ({'OK' if payload.get('runtime_bin_ok') else 'absent'})",
        f"Stockage    : {'prêt' if payload.get('storage_ready') else 'NON prêt'}",
        f"Config      : {payload.get('conf_error') or 'OK'}",
        "",
        "Chemins :",
        str(payload.get("detail") or ""),
        "",
        "Port :",
        str(payload.get("port_listen") or "Port non vu par ss"),
        "===============================================",
    ])


def registry_delete_all_tags(conf: Dict[str, str], lines: List[str]) -> bool:
    repos = registry_get_repo_list(conf)
    deleted = 0
    missing = 0
    failed = 0
    total = 0

    lines.append("Suppression de tous les tags du registre...")
    lines.append(f"Depots trouves : {len(repos)}")
    for repo in repos:
        tags = registry_get_repo_tags(conf, repo)
        if not tags:
            lines.append(f"{repo}: aucun tag.")
            continue
        lines.append(f"{repo}: {len(tags)} tag(s)")
        for tag in tags:
            total += 1
            digest = registry_get_digest(conf, repo, tag)
            if not digest:
                missing += 1
                lines.append(f"ERREUR: digest introuvable pour {repo}:{tag}")
                continue
            response = registry_catalog_request(conf, f"{repo}/manifests/{digest}", method="DELETE")
            if response is not None and response.status_code in (200, 202):
                deleted += 1
                _REGISTRY_ARCH_CACHE.pop(f"{registry_browser_url(conf)}|{repo}:{tag}", None)
                lines.append(f"OK: tag supprime : {repo}:{tag}")
            else:
                failed += 1
                status = response.status_code if response is not None else "connexion"
                lines.append(f"ERREUR: suppression impossible {repo}:{tag} ({status})")

    if deleted:
        _REGISTRY_ARCH_CACHE.clear()

    # Le registre vient d'etre vide (ou confirme vide) : les anciens marqueurs
    # "TAR deja envoye" ne sont plus fiables. Sans ca, l'onglet TAR -> Registre
    # continue d'afficher "A jour" meme apres suppression des tags.
    invalidated = clear_registry_import_state(conf)
    lines.append(f"Etat local TAR -> Registre invalide : {invalidated} fichier(s) supprime(s).")

    lines.append(f"Bilan tags : {total} total, {deleted} supprime(s), {missing} digest absent(s), {failed} echec(s).")
    return failed == 0 and missing == 0


def registry_storage_data_dir(conf: Dict[str, str]) -> str:
    settings = registry_host_settings(conf)
    values = registry_host_load_conf(settings)
    return values.get("DATA_DIR", "")


def registry_is_safe_data_dir(path: str) -> bool:
    if not path:
        return False
    real = os.path.realpath(os.path.abspath(path))
    forbidden = {
        "/",
        "/bin",
        "/boot",
        "/data",
        "/dev",
        "/dockers",
        "/etc",
        "/home",
        "/lib",
        "/lib64",
        "/mnt",
        "/opt",
        "/proc",
        "/root",
        "/run",
        "/sbin",
        "/srv",
        "/sys",
        "/tmp",
        "/usr",
        "/var",
    }
    return real not in forbidden and len(real.split(os.sep)) >= 3


def registry_clean_storage(conf: Dict[str, str], lines: List[str]) -> bool:
    registry_host_require_root()
    data_dir = registry_storage_data_dir(conf)
    if not registry_is_safe_data_dir(data_dir):
        lines.append(f"ERREUR: DATA_DIR refuse par securite : {data_dir or 'vide'}")
        return False

    size_before = registry_path_size(data_dir)
    lines.append("Nettoyage complet du stockage registre...")
    lines.append(f"DATA_DIR : {data_dir}")
    lines.append(f"Taille avant : {human_size(size_before)}")

    stopped = registry_host_stop(conf, lines)
    if not stopped:
        lines.append("ERREUR: impossible d'arreter le registre avant nettoyage.")
        return False

    removed = 0
    clean_ok = True
    try:
        os.makedirs(data_dir, exist_ok=True)
        for entry in os.scandir(data_dir):
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)
            removed += 1
    except Exception as exc:
        lines.append(f"ERREUR: nettoyage du dossier impossible : {exc}")
        clean_ok = False

    os.makedirs(data_dir, exist_ok=True)
    _REGISTRY_ARCH_CACHE.clear()
    invalidated = clear_registry_import_state(conf)
    size_after = registry_path_size(data_dir)
    lines.append(f"Elements supprimes : {removed}")
    lines.append(f"Taille apres : {human_size(size_after)}")
    lines.append(f"Etat local TAR -> Registre invalide : {invalidated} fichier(s) supprime(s).")
    lines.append("Redemarrage du registre...")
    return registry_host_start(conf, lines) and clean_ok


def stream_registry_host_service_action(conf: Dict[str, str], action: str) -> Iterator[str]:
    lines: List[str] = []
    ok = False
    try:
        if action == "registry_service_status":
            payload = registry_host_status_payload(conf)
            lines.append(registry_host_format_status(payload))
            ok = True
        elif action == "registry_service_generate_yaml":
            registry_host_ensure_yaml(conf, lines)
            ok = True
        elif action == "registry_service_install":
            ok = registry_host_install_service(conf, lines)
        elif action == "registry_service_start":
            ok = registry_host_start(conf, lines)
        elif action == "registry_service_stop":
            ok = registry_host_stop(conf, lines)
        elif action == "registry_service_restart":
            ok = registry_host_restart(conf, lines)
        elif action == "registry_service_enable":
            ok = registry_host_enable_disable(conf, True, lines)
        elif action == "registry_service_disable":
            ok = registry_host_enable_disable(conf, False, lines)
        elif action == "registry_service_remove":
            ok = registry_host_remove_service(conf, lines)
        elif action == "registry_service_delete_all_tags":
            ok = registry_delete_all_tags(conf, lines)
        elif action == "registry_service_clean_storage":
            ok = registry_clean_storage(conf, lines)
        else:
            lines.append("ERREUR: action service registre inconnue.")
            ok = False
    except Exception as exc:
        lines.append(f"ERREUR: {exc}")
        ok = False
    for line in lines:
        for part in str(line).splitlines() or [""]:
            yield (f"❌ {part}" if part.startswith("ERREUR") else part) + "\n"
    yield f"@@PROGRESS {json.dumps({'action': action, 'current': 1, 'total': 1, 'percent': 100, 'done': 1 if ok else 0, 'failed': 0 if ok else 1}, ensure_ascii=False)}\n"
    yield ("✅ Action terminée.\n" if ok else "❌ Action terminée avec erreur.\n")



def split_registry_host_port(value: str, default_port: str = "7777") -> Tuple[str, str]:
    raw = registry_host_from_target(value or "").strip().rstrip("/")
    if not raw:
        return "", default_port
    if ":" in raw and raw.count(":") == 1:
        host, port = raw.rsplit(":", 1)
        return host.strip(), (port.strip() or default_port)
    return raw, default_port


def detect_host_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1)
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    try:
        proc = subprocess.run(["hostname", "-I"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3)
        for item in proc.stdout.split():
            if item and not item.startswith("127.") and ":" not in item:
                return item
    except Exception:
        pass
    return "127.0.0.1"


def validate_host_value(value: str) -> bool:
    value = (value or "").strip()
    return bool(value and not re.search(r'[\s/"\'\[\]{}]', value))


def validate_port_value(value: str) -> bool:
    if not str(value or "").isdigit():
        return False
    port = int(value)
    return 1 <= port <= 65535


def http_conf_path_for(conf: Dict[str, str]) -> str:
    return registry_host_resolve_path(os.path.join(conf.get("HOST_CONF_DIR", "../conf"), "http.conf"))


def parse_http_conf(path: str) -> List[str]:
    registries: List[str] = []
    for raw in local_read_text(path).splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" in line:
            line = line.split("=", 1)[1].strip()
        if line and line not in registries:
            registries.append(line)
    return registries


def write_http_conf(path: str, registries: List[str]) -> Tuple[bool, str]:
    clean = []
    for registry in registries:
        registry = registry_host_from_target(registry).strip()
        if registry and registry not in clean:
            clean.append(registry)
    if not clean:
        return False, "Aucune adresse HTTP a ecrire."
    text = "\n".join([
        "# Registres Docker HTTP autorises",
        "# Format compatible editeur key=value : 1=IP:PORT",
        "",
        *[f"{idx}={registry}" for idx, registry in enumerate(clean, start=1)],
        "",
    ])
    return local_write_text(path, text)


def apply_docker_http_registries(registries: List[str], lines: List[str], restart_docker: bool = True) -> bool:
    clean = []
    for registry in registries:
        registry = registry_host_from_target(registry).strip()
        if registry and registry not in clean:
            clean.append(registry)
    if not clean:
        lines.append("ERREUR: aucune adresse HTTP valide.")
        return False

    docker_dir = "/etc/docker"
    daemon_json = os.path.join(docker_dir, "daemon.json")
    try:
        os.makedirs(docker_dir, exist_ok=True)
        data = {}
        if os.path.exists(daemon_json):
            backup = f"{daemon_json}.bak.{time.strftime('%Y%m%d_%H%M%S')}"
            shutil.copy2(daemon_json, backup)
            lines.append(f"Sauvegarde daemon.json : {backup}")
            try:
                with open(daemon_json, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    data = loaded
            except Exception as exc:
                lines.append(f"Ancien daemon.json illisible, reecriture propre : {exc}")
        data["insecure-registries"] = clean
        tmp_path = f"{daemon_json}.{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_path, daemon_json)
        lines.append(f"OK: Docker HTTP autorise : {', '.join(clean)}")
        if restart_docker:
            proc = subprocess.run(["systemctl", "restart", "docker"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
            if proc.stdout.strip():
                lines.append(proc.stdout.strip())
            if proc.returncode != 0:
                lines.append(f"ERREUR: redemarrage Docker impossible (code {proc.returncode}).")
                return False
            lines.append("OK: Docker redemarre.")
        return True
    except Exception as exc:
        lines.append(f"ERREUR: configuration Docker HTTP impossible : {exc}")
        return False


def build_options_payload(conf: Dict[str, str]) -> Dict:
    settings = registry_host_settings(conf)
    raw_registry_conf = read_config_file(settings["human_conf"])
    registry_port = strip_quotes(raw_registry_conf.get("PORT", "7777")).strip() or "7777"
    bind_addr = strip_quotes(raw_registry_conf.get("BIND_ADDR", "0.0.0.0")).strip() or "0.0.0.0"
    data_dir = strip_quotes(raw_registry_conf.get("DATA_DIR", "/var/lib/registry")).strip() or "/var/lib/registry"
    log_level = strip_quotes(raw_registry_conf.get("LOG_LEVEL", "info")).strip() or "info"
    delete_enabled = strip_quotes(raw_registry_conf.get("DELETE_ENABLED", "true")).strip() or "true"
    http_secret = strip_quotes(raw_registry_conf.get("HTTP_SECRET", "registry-labo-host-secret-change-me")).strip() or "registry-labo-host-secret-change-me"

    registry_host, url_port = split_registry_host_port(conf.get("REGISTRY_URL", ""), registry_port)
    if is_placeholder_value(registry_host):
        registry_host = ""
    if is_placeholder_value(registry_port):
        registry_port = ""
    if not registry_host:
        registry_host = detect_host_lan_ip()
    if not registry_port:
        registry_port = url_port if (url_port and not is_placeholder_value(url_port)) else "7777"

    login_file = conf.get("DOCKER_REGISTRY_LOGIN_FILE") or os.path.join(conf.get("DOCKER_CONF_DIR", NAS_CONF_DIR), "registre_login.conf")
    login_data = read_env_login_file(login_file)
    http_conf = http_conf_path_for(conf)
    http_registries = parse_http_conf(http_conf)
    docker_http_value = f"{registry_host}:{registry_port}"
    setup = build_setup_status(conf)

    return {
        "host_ip": detect_host_lan_ip(),
        "registry_host": registry_host,
        "registry_port": registry_port,
        "registry_url": f"http://{registry_host}:{registry_port}",
        "bind_addr": bind_addr,
        "data_dir": data_dir,
        "log_level": log_level,
        "delete_enabled": delete_enabled,
        "http_secret": http_secret,
        "login_host": strip_quotes(login_data.get("REGISTRY_HOST", docker_http_value)).strip() or docker_http_value,
        "login_user": strip_quotes(login_data.get("REGISTRY_USER", conf.get("REGISTRY_USER", ""))).strip(),
        "login_password": "",
        "builds_dir": conf.get("DOCKER_BUILDS_DIR", ""),
        "tar_dir": conf.get("DOCKER_TAR_DIR", ""),
        "builds_dir_exists": os.path.isdir(conf.get("DOCKER_BUILDS_DIR", "")),
        "tar_dir_exists": os.path.isdir(conf.get("DOCKER_TAR_DIR", "")),
        "setup_required": bool(setup.get("required")),
        "setup_reasons": setup.get("reasons", []),
        "builds_conf_path": registry_host_resolve_path(CONFIG_FILE),
        "registry_conf_path": settings["human_conf"],
        "registry_yaml_path": settings["yaml_conf"],
        "registry_login_path": login_file,
        "registry_file_path": conf.get("DOCKER_REGISTRY_FILE", ""),
        "mode_file_path": effective_mode_file(conf),
        "platforms_file_path": conf.get("DOCKER_PLATFORMS_FILE", ""),
        "build_cache_file_path": conf.get("BUILD_CACHE_FILE", ""),
        "http_conf_path": http_conf,
        "http_registry": docker_http_value,
        "http_registries": http_registries,
    }


def save_build_options(conf: Dict[str, str], form) -> Tuple[bool, List[str]]:
    lines: List[str] = []

    # Dossiers de travail : ils sont choisis dans Options et créés ici.
    builds_dir_raw = strip_quotes(str(form.get("builds_dir", ""))).strip() or "../docker_buils"
    tar_dir_raw = strip_quotes(str(form.get("tar_dir", ""))).strip() or "../tar"
    cache_file_raw = strip_quotes(str(form.get("build_cache_file", "../conf/build.jdom"))).strip() or "../conf/build.jdom"
    builds_dir = build_conf_resolve_path(builds_dir_raw)
    tar_dir = build_conf_resolve_path(tar_dir_raw)

    for label, path in (("Dossier builds", builds_dir), ("Dossier TAR", tar_dir)):
        if not path:
            return False, [f"{label} vide."]
        if os.path.exists(path) and not os.path.isdir(path):
            return False, [f"{label} invalide : un fichier existe déjà à cet emplacement : {path}"]
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            return False, [f"Impossible de créer {label} : {path}\n{exc}"]

    auto_ip = str(form.get("auto_ip", "")).strip() == "1"
    registry_host = strip_quotes(str(form.get("registry_host", ""))).strip()
    if auto_ip or not registry_host or is_placeholder_value(registry_host):
        registry_host = detect_host_lan_ip()
    registry_host = registry_host_from_target(registry_host).split(":", 1)[0].strip()
    registry_port = strip_quotes(str(form.get("registry_port", "7777"))).strip() or "7777"
    if is_placeholder_value(registry_port):
        registry_port = "7777"

    bind_addr = strip_quotes(str(form.get("bind_addr", "0.0.0.0"))).strip() or "0.0.0.0"
    data_dir_raw = strip_quotes(str(form.get("data_dir", "/var/lib/registry"))).strip() or "/var/lib/registry"
    if data_dir_raw in {"../registry", "./registry", "registry"}:
        data_dir_raw = "/var/lib/registry"
    data_dir = build_conf_resolve_path(data_dir_raw)
    log_level = strip_quotes(str(form.get("log_level", "info"))).strip() or "info"
    delete_enabled = "true" if str(form.get("delete_enabled", "true")).strip().lower() in {"1", "true", "on", "yes"} else "false"
    http_secret = strip_quotes(str(form.get("http_secret", "registry-labo-host-secret-change-me"))).strip() or "registry-labo-host-secret-change-me"
    login_host = registry_host_from_target(str(form.get("login_host", ""))).strip() or f"{registry_host}:{registry_port}"
    login_user = strip_quotes(str(form.get("login_user", ""))).strip()
    login_file = conf.get("DOCKER_REGISTRY_LOGIN_FILE") or os.path.join(conf.get("DOCKER_CONF_DIR", NAS_CONF_DIR), "registre_login.conf")
    old_login_data = read_env_login_file(login_file)
    old_password = strip_quotes(old_login_data.get("REGISTRY_PASS", conf.get("REGISTRY_PASSWORD", ""))).strip()
    login_password = strip_quotes(str(form.get("login_password", ""))).strip() or old_password
    apply_http = str(form.get("apply_http", "")).strip().lower() in {"1", "true", "on", "yes"}

    if not validate_host_value(registry_host):
        return False, ["Adresse registre invalide."]
    if not validate_port_value(registry_port):
        return False, ["Port registre invalide."]
    if not validate_host_value(bind_addr):
        return False, ["Bind address invalide."]
    if log_level not in {"debug", "info", "warn", "warning", "error", "fatal", "panic"}:
        return False, ["Niveau de log invalide."]

    registry_url = f"http://{registry_host}:{registry_port}"
    http_registry = f"{registry_host}:{registry_port}"
    builds_conf_path = registry_host_resolve_path(CONFIG_FILE)

    # builds.conf devient la source de vérité du module Build.
    # On le réécrit proprement au lieu de préserver les anciennes clés obsolètes
    # Compose/Stacks qui appartiennent maintenant au module Docker.
    clean_builds_conf = {
        "HOST_BUILDS_DIR": builds_dir_raw,
        "HOST_TAR_DIR": tar_dir_raw,
        "DOCKER_BUILDS_DIR": builds_dir_raw,
        "DOCKER_TAR_DIR": tar_dir_raw,
        "HOST_CONF_DIR": "../conf",
        "DOCKER_CONF_DIR": "../conf",
        "HOST_LOG_DIR": "/var/log/builds",
        "DOCKER_LOG_DIR": "/var/log/builds",
        "HOST_REGISTRY_FILE": "../conf/registre.conf",
        "HOST_MODE_FILE": "../conf/mode.conf",
        "HOST_PLATFORMS_FILE": "../conf/platforms.conf",
        "HOST_REGISTRY_LOGIN_FILE": "../conf/registre_login.conf",
        "HOST_REGISTRY_CONFIG_FILE": "../conf/builds.conf",
        "BUILD_CACHE_FILE": cache_file_raw,
        "DOCKER_REGISTRY_FILE": "../conf/registre.conf",
        "DOCKER_MODE_FILE": "../conf/mode.conf",
        "DOCKER_PLATFORMS_FILE": "../conf/platforms.conf",
        "DOCKER_REGISTRY_LOGIN_FILE": "../conf/registre_login.conf",
        "DOCKER_REGISTRY_CONFIG_FILE": "../conf/builds.conf",
        "REGISTRY_URL": registry_url,
        "REGISTRY_USER": login_user,
        "REGISTRY_PASSWORD": login_password,
        "YML_DIR": "../yml",
        "SYSTEM_LOG_FILE": "/var/log/builds/system_python.log",
        "REGISTRY_HOST_LOG_DIR": "/var/log/registry",
        "REGISTRY_HOST_LOG_FILE": "/var/log/registry/registry.log",
    }
    ok, err = local_write_text(builds_conf_path, builds_default_conf_text(clean_builds_conf))
    if not ok:
        return False, [f"Erreur builds.conf : {err}"]
    lines.append(f"OK: builds.conf mis a jour : {builds_conf_path}")
    lines.append(f"OK: dossier builds prêt : {builds_dir}")
    lines.append(f"OK: dossier TAR prêt : {tar_dir}")

    settings = registry_host_settings(get_config())
    ok, err = write_kv_file_preserve(settings["human_conf"], {
        "PORT": registry_port,
        "BIND_ADDR": bind_addr,
        "DATA_DIR": data_dir,
        "LOG_LEVEL": log_level,
        "DELETE_ENABLED": delete_enabled,
        "HTTP_SECRET": http_secret,
    })
    if not ok:
        return False, [f"Erreur registry.conf : {err}"]
    lines.append(f"OK: registry.conf mis a jour : {settings['human_conf']}")

    ok, err = local_write_text(login_file, "\n".join([
        f"REGISTRY_HOST={quote_env_value(login_host)}",
        f"REGISTRY_USER={quote_env_value(login_user)}",
        f"REGISTRY_PASS={quote_env_value(login_password)}",
        "",
    ]))
    if not ok:
        return False, [f"Erreur registre_login.conf : {err}"]
    lines.append(f"OK: registre_login.conf mis a jour : {login_file}")

    http_conf = http_conf_path_for(get_config())
    ok, err = write_http_conf(http_conf, [http_registry])
    if not ok:
        return False, [f"Erreur http.conf : {err}"]
    lines.append(f"OK: http.conf mis a jour : {http_conf}")

    new_conf = get_config()
    for created_path in ensure_build_support_conf_files(new_conf):
        lines.append(f"OK: fichier conf créé : {created_path}")
    try:
        registry_host_ensure_yaml(new_conf, lines)
    except Exception as exc:
        lines.append(f"ERREUR: generation registry.yml impossible : {exc}")
        return False, lines

    ok = True
    if apply_http:
        ok = apply_docker_http_registries([http_registry], lines, restart_docker=True) and ok
    else:
        lines.append("Docker HTTP non applique a /etc/docker/daemon.json.")

    ok_registry_auto = registry_host_autostart_after_options(new_conf, lines)
    ok = ok and ok_registry_auto

    return ok, lines


