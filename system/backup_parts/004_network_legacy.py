def append_network_log(message):
    os.makedirs(os.path.dirname(NETWORK_LOG_FILE), exist_ok=True)
    with open(NETWORK_LOG_FILE, 'a', encoding='utf-8') as handle:
        handle.write(f'[{now_label()}] {message}\n')


def sanitize_machine(value):
    value = str(value or '').strip()
    value = re.sub(r'[^A-Za-z0-9_.-]+', '_', value)
    return value.strip('._-') or 'machine'


def sanitize_mount_name(export_path):
    value = str(export_path or '').strip().strip('/') or 'export'
    value = value.replace('/mnt/user/', '')
    value = value.replace('/mnt/', '')
    value = re.sub(r'[^A-Za-z0-9_.-]+', '_', value)
    return value.strip('._-') or 'export'


def ensure_network_ini():
    os.makedirs(os.path.dirname(NETWORK_INI_FILE), exist_ok=True)
    parser = configparser.ConfigParser()
    if os.path.exists(NETWORK_INI_FILE):
        try:
            parser.read(NETWORK_INI_FILE, encoding='utf-8')
        except Exception:
            parser = configparser.ConfigParser()
    if not parser.has_section('GENERAL'):
        parser.add_section('GENERAL')
    defaults = {
        'HOST_BASE': '/mnt/remotes',
        'DOCKER_BASE': '/remotes',
        'DEFAULT_OPTIONS': 'rw,soft,timeo=30,retrans=2',
        # Anti-blocage NFS : 3 essais courts, puis on affiche Non monté.
        'MOUNT_RETRIES': '3',
        'MOUNT_TIMEOUT': '12',
        'RETRY_SLEEP': '2',
        'SHOWMOUNT_TIMEOUT': '8',
    }
    changed = False
    for k, v in defaults.items():
        if not parser['GENERAL'].get(k):
            parser['GENERAL'][k] = v
            changed = True

    # Migration douce : l'ancienne valeur forçait NFSv4 et peut casser les montages
    # sur Unraid/serveur qui expose surtout en NFSv3. On garde les options courtes
    # et mount_export sait maintenant essayer plusieurs versions si besoin.
    if parser['GENERAL'].get('DEFAULT_OPTIONS', '').strip() == 'rw,nfsvers=4,soft,timeo=30,retrans=2':
        parser['GENERAL']['DEFAULT_OPTIONS'] = defaults['DEFAULT_OPTIONS']
        changed = True

    if changed or not os.path.exists(NETWORK_INI_FILE):
        with open(NETWORK_INI_FILE, 'w', encoding='utf-8') as handle:
            parser.write(handle)
    return parser


def write_network_ini(parser):
    os.makedirs(os.path.dirname(NETWORK_INI_FILE), exist_ok=True)
    tmp = NETWORK_INI_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        parser.write(handle)
    os.replace(tmp, NETWORK_INI_FILE)


def safe_int(value, default, minimum=None, maximum=None):
    try:
        number = int(str(value).strip())
    except Exception:
        number = int(default)
    if minimum is not None:
        number = max(int(minimum), number)
    if maximum is not None:
        number = min(int(maximum), number)
    return number


def network_general():
    parser = ensure_network_ini()
    g = parser['GENERAL']
    return {
        'host_base': ensure_folder_slash(g.get('HOST_BASE', '/mnt/user/remotes')),
        'docker_base': ensure_folder_slash(g.get('DOCKER_BASE', '/remotes')),
        'default_options': g.get('DEFAULT_OPTIONS', 'rw,soft,timeo=30,retrans=2'),
        'mount_retries': safe_int(g.get('MOUNT_RETRIES', '3'), 3, 1, 10),
        'mount_timeout': safe_int(g.get('MOUNT_TIMEOUT', '12'), 12, 3, 120),
        'retry_sleep': safe_int(g.get('RETRY_SLEEP', '2'), 2, 0, 30),
        'showmount_timeout': safe_int(g.get('SHOWMOUNT_TIMEOUT', '8'), 8, 3, 60),
        'ini_file': NETWORK_INI_FILE,
        'log_file': NETWORK_LOG_FILE,
    }


def run_cmd(cmd, timeout=40):
    append_network_log('$ ' + shlex.join([str(x) for x in cmd]))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout or '').strip(); err = (proc.stderr or '').strip()
        if out:
            append_network_log('[stdout] ' + out.replace('\n', '\n[stdout] '))
        if err:
            append_network_log('[stderr] ' + err.replace('\n', '\n[stderr] '))
        return proc.returncode, out, err
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or '')
        err = (exc.stderr or '')
        if isinstance(out, bytes):
            out = out.decode('utf-8', errors='replace')
        if isinstance(err, bytes):
            err = err.decode('utf-8', errors='replace')
        append_network_log(f'[TIMEOUT] Commande interrompue après {timeout}s')
        if out:
            append_network_log('[stdout] ' + str(out).strip().replace('\n', '\n[stdout] '))
        if err:
            append_network_log('[stderr] ' + str(err).strip().replace('\n', '\n[stderr] '))
        return 124, str(out).strip(), str(err).strip() or 'timeout'
    except Exception as exc:
        append_network_log('[ERREUR] ' + str(exc))
        return 127, '', str(exc)


def run_cmd_quiet(cmd, timeout=5):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or '').strip(), (proc.stderr or '').strip()
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ''
        err = exc.stderr or ''
        if isinstance(out, bytes):
            out = out.decode('utf-8', errors='replace')
        if isinstance(err, bytes):
            err = err.decode('utf-8', errors='replace')
        return 124, str(out).strip(), str(err).strip() or 'timeout'
    except Exception as exc:
        return 127, '', str(exc)


def host_shell(shell_command, timeout=60):
    # Important : on exporte PATH avant la commande.
    # L'ancienne forme "PATH=... if ..." casse Bash, car "if" n'est pas une commande simple.
    shell_command = str(shell_command or '')
    prefix = 'export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; '
    wrapped = prefix + shell_command
    if os.path.exists('/usr/bin/nsenter') or os.path.exists('/bin/nsenter'):
        return run_cmd(['nsenter', '-t', '1', '-m', '-u', '-i', '-n', '-p', '--', '/bin/bash', '-lc', wrapped], timeout=timeout)
    return run_cmd(['/bin/bash', '-lc', wrapped], timeout=timeout)


def host_shell_quiet(shell_command, timeout=5):
    shell_command = str(shell_command or '')
    prefix = 'export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; '
    wrapped = prefix + shell_command
    if os.path.exists('/usr/bin/nsenter') or os.path.exists('/bin/nsenter'):
        return run_cmd_quiet(['nsenter', '-t', '1', '-m', '-u', '-i', '-n', '-p', '--', '/bin/bash', '-lc', wrapped], timeout=timeout)
    return run_cmd_quiet(['/bin/bash', '-lc', wrapped], timeout=timeout)


def show_exports(machine):
    machine = str(machine or '').strip()
    if not machine:
        return []
    timeout = network_general().get('showmount_timeout', 8)
    append_network_log(f'Affichage des partages NFS : {machine} (timeout {timeout}s)')
    code, out, err = run_cmd(['showmount', '-e', machine], timeout=timeout)
    if code != 0:
        code, out, err = host_shell('showmount -e ' + shlex.quote(machine), timeout=timeout)
    exports = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith('Export list'):
            continue
        parts = line.split()
        if parts and parts[0].startswith('/'):
            exports.append(parts[0])
    return exports


def network_section(machine, export_path):
    return 'NFS:' + sanitize_machine(machine) + ':' + sanitize_mount_name(export_path)


def mountpoint_for(machine, export_path):
    g = network_general(); machine_clean = sanitize_machine(machine); share_clean = sanitize_mount_name(export_path)
    host_path = os.path.join(g['host_base'].rstrip('/'), machine_clean, share_clean)
    docker_path = os.path.join(g['docker_base'].rstrip('/'), machine_clean, share_clean)
    return ensure_folder_slash(host_path), ensure_folder_slash(docker_path)


def save_known_mount(machine, export_path, options=None, auto_mount=True):
    parser = ensure_network_ini(); section = network_section(machine, export_path)
    if not parser.has_section(section):
        parser.add_section(section)
    host_path, docker_path = mountpoint_for(machine, export_path)
    parser[section]['MACHINE'] = str(machine); parser[section]['EXPORT'] = str(export_path)
    parser[section]['HOST_PATH'] = host_path; parser[section]['DOCKER_PATH'] = docker_path
    parser[section]['OPTIONS'] = options or network_general()['default_options']; parser[section]['ENABLED'] = '1'; parser[section]['AUTO_MOUNT'] = '1' if auto_mount else '0'
    write_network_ini(parser)


def shell_timeout_command(seconds, inner_command):
    """Enveloppe une commande shell avec timeout si disponible côté hôte."""
    seconds = safe_int(seconds, 5, 1, 300)
    quoted_inner = shlex.quote(inner_command)
    return (
        'if command -v timeout >/dev/null 2>&1; then '
        f'timeout --foreground {seconds}s /bin/bash -lc {quoted_inner}; '
        'else '
        f'/bin/bash -lc {quoted_inner}; '
        'fi'
    )


def split_nfs_options(options):
    return [part.strip() for part in str(options or '').split(',') if part.strip()]


def join_nfs_options(parts):
    cleaned = []
    seen = set()
    for part in parts:
        part = str(part or '').strip()
        if not part:
            continue
        key = part.split('=', 1)[0].strip().lower()
        # On évite les doublons exacts tout en gardant l'ordre lisible.
        sig = (key, part.lower())
        if sig in seen:
            continue
        seen.add(sig)
        cleaned.append(part)
    return ','.join(cleaned) or 'rw'


def nfs_options_without_version(options):
    return join_nfs_options([
        part for part in split_nfs_options(options)
        if part.split('=', 1)[0].strip().lower() not in {'nfsvers', 'vers'}
    ])


def nfs_options_with_version(options, version):
    base = split_nfs_options(nfs_options_without_version(options))
    base.append('nfsvers=' + str(version))
    return join_nfs_options(base)


def nfs_option_candidates(options):
    """Prépare plusieurs essais NFS.

    Le bug visible dans tes logs est typique d'un serveur qui expose bien en NFS,
    mais qui refuse la version forcée par l'option nfsvers=4. On essaye donc :
    1) les options demandées ; 2) sans version forcée ; 3) NFSv3 ; 4) NFSv4.
    Si un fallback marche, il est mémorisé dans network_mounts.ini.
    """
    original = join_nfs_options(split_nfs_options(options) or split_nfs_options(network_general()['default_options']))
    candidates = [original]
    no_version = nfs_options_without_version(original)
    candidates.append(no_version)
    candidates.append(nfs_options_with_version(original, 3))
    candidates.append(nfs_options_with_version(original, 4))

    result = []
    seen = set()
    for candidate in candidates:
        key = candidate.lower()
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return result


def load_known_mounts():
    parser = ensure_network_ini(); items = []
    for section in parser.sections():
        if not section.startswith('NFS:'):
            continue
        row = dict(parser[section]); machine = row.get('machine', ''); export = row.get('export', '')
        host_path = row.get('host_path', ''); docker_path = row.get('docker_path', '')
        mounted = False
        reachable = False
        count = ''
        state = 'Non monté'
        if host_path:
            code, _, _ = host_shell_quiet('mountpoint -q ' + shlex.quote(host_path), timeout=5)
            mounted = code == 0
            if mounted:
                # Ne jamais faire os.listdir() directement sur un NFS potentiellement mort : ça peut bloquer Flask.
                inner = 'ls -1A ' + shlex.quote(host_path) + ' 2>/dev/null | wc -l'
                count_code, count_out, _ = host_shell_quiet(shell_timeout_command(2, inner), timeout=4)
                if count_code == 0:
                    reachable = True
                    count = (count_out or '').strip().splitlines()[-1] if (count_out or '').strip() else '0'
                    state = 'Monté'
                else:
                    reachable = False
                    count = '?'
                    state = 'Monté mais inaccessible'
            else:
                reachable = False
                state = 'Non monté'
        items.append({
            'section': section,
            'machine': machine,
            'export': export,
            'host_path': host_path,
            'docker_path': docker_path,
            'options': row.get('options', ''),
            'enabled': row.get('enabled', '1') == '1',
            'auto_mount': row.get('auto_mount', row.get('enabled', '1')) == '1',
            'mounted': mounted and reachable,
            'raw_mounted': mounted,
            'reachable': reachable,
            'state': state,
            'count': count,
        })
    items.sort(key=lambda x: (x['machine'], x['export']))
    return items

def mount_export(machine, export_path, options=None, force=False, auto_mount=True, retries=None, attempt_timeout=None, retry_sleep=None):
    g = network_general()
    requested_options = join_nfs_options(split_nfs_options(options or g['default_options']))
    option_candidates = nfs_option_candidates(requested_options)
    retries = safe_int(retries if retries is not None else g.get('mount_retries', 3), 3, 1, 10)
    attempt_timeout = safe_int(attempt_timeout if attempt_timeout is not None else g.get('mount_timeout', 12), 12, 3, 120)
    retry_sleep = safe_int(retry_sleep if retry_sleep is not None else g.get('retry_sleep', 2), 2, 0, 30)
    host_path, docker_path = mountpoint_for(machine, export_path)
    remote = f'{machine}:{export_path}'

    # On mémorise l'entrée même si le serveur n'est pas disponible maintenant.
    save_known_mount(machine, export_path, requested_options, auto_mount=auto_mount)

    if force:
        append_network_log(f'[NFS] Démontage forcé avant remontage : {host_path}')
        host_shell('umount -l ' + shlex.quote(host_path) + ' 2>/dev/null || true', timeout=10)

    for attempt in range(1, retries + 1):
        append_network_log(f'[NFS] Essai montage {attempt}/{retries} : {remote} -> {host_path} (timeout {attempt_timeout}s)')

        for current_options in option_candidates:
            if len(option_candidates) > 1:
                append_network_log(f'[NFS] Options testées : {current_options}')

            inner_mount = (
                'mkdir -p ' + shlex.quote(host_path) +
                ' && if mountpoint -q ' + shlex.quote(host_path) +
                '; then exit 0; fi; '
                'mount -t nfs -o ' + shlex.quote(current_options) + ' ' + shlex.quote(remote) + ' ' + shlex.quote(host_path)
            )
            code, out, err = host_shell(shell_timeout_command(attempt_timeout, inner_mount), timeout=attempt_timeout + 5)

            if code == 0:
                # Le mountpoint peut exister alors que le serveur NFS est mort : on valide aussi un accès rapide.
                check_inner = 'ls -1A ' + shlex.quote(host_path) + ' >/dev/null 2>&1'
                check_code, _, _ = host_shell(shell_timeout_command(2, check_inner), timeout=4)
                if check_code == 0:
                    if current_options != requested_options:
                        append_network_log(f'[NFS] Fallback validé : {requested_options} -> {current_options}')
                        save_known_mount(machine, export_path, current_options, auto_mount=auto_mount)
                    append_network_log(f'[OK] Monté : {remote} -> {host_path} Docker={docker_path}')
                    return True
                append_network_log(f'[WARN] Mountpoint présent mais partage inaccessible : {remote} -> {host_path}')
                code = check_code
            else:
                detail = (err or out or '').strip()
                if detail:
                    append_network_log(f'[NFS] Échec avec options [{current_options}] : {detail}')
                else:
                    append_network_log(f'[NFS] Échec avec options [{current_options}]')

        if attempt < retries and retry_sleep > 0:
            time.sleep(retry_sleep)

    append_network_log(f'[NON MONTÉ] Partage non monté après {retries} essai(s) : {remote} -> {host_path}')
    return False

def refresh_network_mounts(force=True, reason='manuel'):
    count = 0
    total = 0
    failed = 0
    for item in load_known_mounts():
        if item.get('auto_mount'):
            total += 1
            if mount_export(item['machine'], item['export'], item.get('options'), force=force, auto_mount=True):
                count += 1
            else:
                failed += 1
    append_network_log(f'Rafraîchissement {reason} terminé : {count}/{total} montage(s) actif(s), {failed} non monté(s).')
    return count


STARTUP_AUTOMOUNT_DONE = False
NETWORK_REFRESH_RUNNING = False
NETWORK_REFRESH_LOCK = threading.Lock()


def network_refresh_is_running():
    with NETWORK_REFRESH_LOCK:
        return NETWORK_REFRESH_RUNNING


def _network_refresh_worker(force=True, reason='manuel'):
    global NETWORK_REFRESH_RUNNING
    try:
        refresh_network_mounts(force=force, reason=reason)
    except Exception as exc:
        append_network_log('[AUTO][ERREUR] ' + str(exc))
    finally:
        with NETWORK_REFRESH_LOCK:
            NETWORK_REFRESH_RUNNING = False


def start_network_refresh_async(force=True, reason='manuel'):
    global NETWORK_REFRESH_RUNNING
    with NETWORK_REFRESH_LOCK:
        if NETWORK_REFRESH_RUNNING:
            append_network_log(f'[AUTO] Rafraîchissement déjà en cours, demande ignorée : {reason}')
            return False
        NETWORK_REFRESH_RUNNING = True
    thread = threading.Thread(target=_network_refresh_worker, kwargs={'force': force, 'reason': reason}, daemon=True)
    thread.start()
    return True


def startup_automount_once():
    global STARTUP_AUTOMOUNT_DONE
    if STARTUP_AUTOMOUNT_DONE:
        return
    STARTUP_AUTOMOUNT_DONE = True
    try:
        auto_count = sum(1 for item in load_known_mounts() if item.get('auto_mount'))
        if auto_count:
            append_network_log(f'[AUTO] Démarrage Docker : vérification en arrière-plan de {auto_count} montage(s).')
            start_network_refresh_async(force=False, reason='démarrage Docker')
        else:
            append_network_log('[AUTO] Démarrage Docker : aucun montage automatique à vérifier.')
    except Exception as exc:
        append_network_log('[AUTO][ERREUR] ' + str(exc))

def is_safe_network_host_path(path_value):
    """Vérifie qu'on ne démonte/nettoie que sous HOST_BASE."""
    try:
        base = os.path.abspath(network_general()['host_base'].rstrip('/'))
        path = os.path.abspath(str(path_value or '').rstrip('/'))
        return bool(path) and (path == base or path.startswith(base + os.sep))
    except Exception:
        return False


def set_mount_auto(section, enabled):
    parser = ensure_network_ini()
    section = str(section or '').strip()
    if parser.has_section(section):
        parser[section]['AUTO_MOUNT'] = '1' if enabled else '0'
        parser[section]['ENABLED'] = '1' if enabled else '0'
        write_network_ini(parser)
        append_network_log(f"[AUTO] {section} -> {'activé' if enabled else 'désactivé'}")
    else:
        append_network_log(f"[AUTO][ERREUR] Section introuvable : {section}")


def delete_known_mount(section, unmount=True, remove_empty_dir=True):
    """Supprime définitivement un montage connu de network_mounts.ini.

    Sécurité volontaire : on ne supprime jamais les données du partage NFS distant.
    On démonte seulement le point de montage local si actif, puis on tente de supprimer
    les dossiers locaux vides sous HOST_BASE.
    """
    section = str(section or '').strip()
    parser = ensure_network_ini()

    if not section.startswith('NFS:') or not parser.has_section(section):
        append_network_log(f"[DELETE][ERREUR] Montage inconnu : {section}")
        return False

    row = dict(parser[section])
    machine = row.get('machine', '')
    export_path = row.get('export', '')
    host_path = (row.get('host_path') or '').rstrip('/')

    if not host_path and machine and export_path:
        host_path, _docker_path = mountpoint_for(machine, export_path)
        host_path = host_path.rstrip('/')

    label = f'{machine}:{export_path}' if machine or export_path else section

    if host_path:
        if is_safe_network_host_path(host_path):
            if unmount:
                host_shell(
                    'if mountpoint -q ' + shlex.quote(host_path) +
                    '; then umount -l ' + shlex.quote(host_path) + '; fi',
                    timeout=30,
                )
            if remove_empty_dir:
                parent = os.path.dirname(host_path.rstrip('/'))
                host_shell(
                    'rmdir --ignore-fail-on-non-empty ' + shlex.quote(host_path) + ' 2>/dev/null || true; '
                    'rmdir --ignore-fail-on-non-empty ' + shlex.quote(parent) + ' 2>/dev/null || true',
                    timeout=20,
                )
        else:
            append_network_log(f"[DELETE][WARN] Chemin hors HOST_BASE, démontage ignoré : {host_path}")

    parser.remove_section(section)
    write_network_ini(parser)
    append_network_log(f'[DELETE] Montage supprimé de la liste : {label}')
    return True

# ==========================================================
# 1. LE MANIFEST
# ==========================================================
# Partage réseau supprimé de ce module : plus de montage auto ici.
# @backup_bp.before_request
def before_any_request():
    startup_automount_once()


