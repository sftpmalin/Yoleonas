def build_generated_script(data):
    metadata = build_metadata_block(data)
    config_json = json.dumps(data, ensure_ascii=False, indent=2)
    runtime_json = json.dumps(runtime_config(), ensure_ascii=False, indent=2)
    title = data.get('TITLE', 'Backup rsync').replace('\n', ' ').strip()
    logo = data.get('LOGO', '').replace('\n', ' ').strip()
    template = r'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# BACKUP_RSYNC
# TITLE=__TITLE__
# LOGO=__LOGO__
__METADATA__

import fnmatch
import json
import os
import re
import shlex
import shutil
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

CONFIG = json.loads(r''' + "'''__CONFIG_JSON__'''" + r''')
RUNTIME = json.loads(r''' + "'''__RUNTIME_JSON__'''" + r''')
CHILD_PROC = None
STOP_REQUESTED = False
GLOBAL_LOCK_ACQUIRED = False


def now_label():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def script_stem():
    return Path(__file__).stem


def log(message):
    print(message, flush=True)


def status_path():
    status_dir = Path(RUNTIME.get('status_dir') or STANDARD_STATUS_DIR)
    status_dir.mkdir(parents=True, exist_ok=True)
    return status_dir / (script_stem() + '.status.json')


def lock_path():
    status_dir = Path(RUNTIME.get('status_dir') or STANDARD_STATUS_DIR)
    status_dir.mkdir(parents=True, exist_ok=True)
    return status_dir / (script_stem() + '.lock')


def write_status(**payload):
    data = {
        'title': CONFIG.get('TITLE', script_stem()),
        'script': Path(__file__).name,
        'updated_at': now_label(),
    }
    data.update(payload)
    tmp = str(status_path()) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, status_path())


def acquire_lock():
    path = lock_path()
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(str(os.getpid()))
        return True
    except FileExistsError:
        return False


def release_lock():
    try:
        lock_path().unlink()
    except FileNotFoundError:
        pass


def global_lock_path():
    status_dir = Path(RUNTIME.get('status_dir') or STANDARD_STATUS_DIR)
    status_dir.mkdir(parents=True, exist_ok=True)
    return status_dir / '.backup_global.lock'


def local_pid_alive(pid):
    try:
        if not pid:
            return False
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def read_global_lock():
    path = global_lock_path()
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            raw = handle.read().strip()
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        # Compatibilité si le fichier contient seulement un PID.
        return {'pid': raw}


def cleanup_stale_global_lock():
    path = global_lock_path()
    info = read_global_lock()
    pid = info.get('pid')
    if not pid or not local_pid_alive(pid):
        try:
            path.unlink()
            if pid:
                log(f'[WAIT] Verrou global obsolète nettoyé, ancien PID absent : {pid}')
            return True
        except FileNotFoundError:
            return False
        except Exception as exc:
            log('[WARN] Impossible de nettoyer le verrou global obsolète : ' + str(exc))
    return False


def acquire_global_backup_lock(started_at):
    """Un seul backup rsync à la fois pour éviter de massacrer les disques mécaniques."""
    global GLOBAL_LOCK_ACQUIRED
    path = global_lock_path()
    already_waiting = False
    while True:
        cleanup_stale_global_lock()
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            payload = {
                'pid': os.getpid(),
                'script': Path(__file__).name,
                'title': CONFIG.get('TITLE', script_stem()),
                'acquired_at': now_label(),
            }
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            GLOBAL_LOCK_ACQUIRED = True
            if already_waiting:
                log('[OK] Tour obtenu : démarrage du backup.')
            write_status(
                running=True,
                result='En cours',
                message='Tour obtenu, préparation du backup',
                started_at=started_at,
                ended_at='',
                pid=os.getpid(),
                phase='starting',
            )
            return True
        except FileExistsError:
            info = read_global_lock()
            owner = info.get('title') or info.get('script') or 'un autre backup'
            owner_pid = info.get('pid') or '?'
            if not already_waiting:
                log('[WAIT] Une autre sauvegarde est déjà en cours : ' + str(owner) + ' (PID ' + str(owner_pid) + ').')
                log('[WAIT] Ce backup reste en attente et démarrera automatiquement quand le verrou sera libéré.')
                already_waiting = True
            write_status(
                running=True,
                result='En attente',
                message='En attente : une autre sauvegarde est en cours',
                started_at=started_at,
                ended_at='',
                pid=os.getpid(),
                phase='waiting',
                waiting_for=str(owner),
                waiting_for_pid=str(owner_pid),
            )
            time.sleep(2)
        except Exception as exc:
            log('[ERREUR] Impossible de prendre le verrou global : ' + str(exc))
            return False


def release_global_backup_lock():
    global GLOBAL_LOCK_ACQUIRED
    if not GLOBAL_LOCK_ACQUIRED:
        return
    path = global_lock_path()
    info = read_global_lock()
    pid = str(info.get('pid') or '')
    if not pid or pid == str(os.getpid()):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except Exception as exc:
            log('[WARN] Impossible de supprimer le verrou global : ' + str(exc))
    GLOBAL_LOCK_ACQUIRED = False


def is_remote_path(path_value):
    value = str(path_value or '')
    if value.startswith('rsync://'):
        return True
    # Simple détection user@host:/path ou host:/path, sans confondre C:\ Windows.
    return ':' in value and not value.startswith('/') and not value.startswith('./')



def ensure_runtime_folder_slash(path_value):
    value = str(path_value or '').strip()
    if not value or value.endswith('/'):
        return value
    return value + '/'


def normalize_exclude_pattern(pattern):
    """Accepte une exclusion relative rsync ou un chemin absolu placé sous SOURCE.
    Exemples dans le formulaire : AMELYS/ ; /AMELYS/ ; /tower/mnt/user/Antony/AMELYS/ ; *.tmp
    """
    value = str(pattern or '').strip()
    if not value:
        return ''

    source = ensure_runtime_folder_slash(CONFIG.get('SOURCE', ''))
    if value.startswith('/') and source and value.startswith(source):
        value = value[len(source):].lstrip('/')

    return value


def split_excludes(raw):
    items = []
    for line in str(raw or '').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        normalized = normalize_exclude_pattern(line)
        if normalized:
            items.append(normalized)
    return items


def find_remote_mountpoint(path_value):
    value = ensure_runtime_folder_slash(path_value)
    if not value.startswith('/remotes/'):
        return ''
    # Ne pas appeler Path.exists() sur un NFS potentiellement mort : ça peut bloquer.
    current = Path(value.rstrip('/'))
    remotes_root = Path('/remotes')
    while str(current) != str(remotes_root) and str(current).startswith('/remotes'):
        if os.path.ismount(str(current)):
            return str(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return ''


def path_accessible_quick(path_value, timeout_seconds=5):
    value = ensure_runtime_folder_slash(path_value)
    timeout_bin = shutil.which('timeout')
    if timeout_bin:
        cmd = [timeout_bin, str(int(timeout_seconds)) + 's', 'bash', '-lc', 'test -d "$1" && ls -1A "$1" >/dev/null', 'backup-path-check', value]
        try:
            return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=int(timeout_seconds) + 2).returncode == 0
        except Exception:
            return False
    # Fallback minimal : éviter os.listdir() sur un NFS mort, donc on ne valide pas l'accès si timeout est indisponible.
    return os.path.isdir(value) and not value.startswith('/remotes/')


def target_mount_ok():
    target = ensure_runtime_folder_slash(CONFIG.get('TARGET', ''))
    if target.startswith('/remotes/'):
        mountpoint = find_remote_mountpoint(target)
        if not mountpoint:
            log('[ERREUR] Cible sous /remotes mais aucun montage actif détecté pour cette cible.')
            log('[ERREUR] Sécurité anti-écriture dans l’image Docker : monte le partage NFS avant de lancer le backup.')
            log('[ERREUR] Cible demandée : ' + target)
            return False
        if not path_accessible_quick(target, timeout_seconds=5):
            log('[ERREUR] Partage NFS monté mais inaccessible ou serveur éteint.')
            log('[ERREUR] Le backup est annulé pour éviter un blocage rsync.')
            log('[ERREUR] Cible demandée : ' + target)
            return False
    return True


def terminate_child():
    global CHILD_PROC
    if CHILD_PROC and CHILD_PROC.poll() is None:
        try:
            CHILD_PROC.terminate()
        except Exception:
            pass
        try:
            CHILD_PROC.wait(timeout=5)
        except Exception:
            try:
                CHILD_PROC.kill()
            except Exception:
                pass


def handle_stop_signal(signum, frame):
    log(f'[STOP] Signal reçu: {signum}. Arrêt de rsync et nettoyage des locks.')
    terminate_child()
    release_global_backup_lock()
    release_lock()
    sys.exit(128 + int(signum))


def update_live_progress_status(line, base_status, throttle=None):
    if not base_status or base_status.get('phase') == 'simulation':
        return

    match = re.search(
        r'^\s*(?P<amount>[0-9][0-9., ]*\s*(?:[KMGTPE]?i?B?|bytes?|octets?)?)\s+'
        r'(?P<percent>100|[0-9]{1,2})%\s+'
        r'(?P<speed>[0-9][0-9.,]*\s*[KMGTPE]?i?B?/s)?\s*'
        r'(?P<eta>\d+:\d{2}:\d{2}|\d+:\d{2})?',
        str(line or ''),
        flags=re.IGNORECASE,
    )
    if not match:
        return

    amount_text = ' '.join((match.group('amount') or '').split())
    bytes_value = parse_size_to_bytes(amount_text)
    percent = max(0, min(100, int(match.group('percent'))))
    progress = {
        'percent': percent,
        'percent_label': str(percent) + '%',
        'copied_label': human_size(bytes_value) if bytes_value is not None else amount_text,
        'copied_bytes': bytes_value,
        'raw_copied': amount_text,
        'speed': ' '.join((match.group('speed') or '').split()),
        'eta': match.group('eta') or '',
    }

    data = dict(base_status or {})
    total_bytes = data.get('progress_total_bytes') or data.get('total_bytes')
    try:
        total_bytes = int(total_bytes) if total_bytes not in (None, '') else None
    except Exception:
        total_bytes = None
    if total_bytes and total_bytes > 0 and bytes_value is not None:
        total_label = data.get('progress_total_label') or human_size(total_bytes)
        computed_percent = int(max(0, min(100, round((float(bytes_value) / float(total_bytes)) * 100))))
        progress['percent'] = computed_percent
        progress['percent_label'] = str(computed_percent) + '%'
        progress['total_bytes'] = total_bytes
        progress['total_label'] = total_label
        progress['copied_total_label'] = f"{progress.get('copied_label') or human_size(bytes_value)} copies / {total_label}"

    now = time.monotonic()
    raw = str(progress.get('raw_copied') or line)[:180]
    throttle = throttle if isinstance(throttle, dict) else {}
    if throttle.get('percent') == progress.get('percent') and throttle.get('raw') == raw and (now - float(throttle.get('time') or 0.0)) < 0.8:
        return
    throttle['time'] = now
    throttle['percent'] = progress.get('percent')
    throttle['raw'] = raw

    data['progress'] = progress
    bits = [progress.get('percent_label') or '']
    if progress.get('copied_total_label'):
        bits.append(progress['copied_total_label'])
    elif progress.get('copied_label'):
        bits.append(progress['copied_label'] + ' copies')
    data['progress_text'] = ' - '.join(x for x in bits if x)
    try:
        write_status(**data)
    except Exception:
        pass


def run_rsync_command(cmd, capture_output=False, live_status=None):
    """Lance rsync en gardant la sortie visible dans le log.

    Avant le passage tmux/systemd, rsync écrivait directement dans le parent
    Flask et la progression était relue plus facilement. En session tmux, il
    faut reprendre la sortie caractère par caractère : rsync écrit
    --info=progress2 avec des retours chariot (\r), pas toujours avec des
    lignes classiques. On recopie donc tout dans stdout pour le fichier log,
    et on met aussi le status.json à jour pendant la copie réelle.
    """
    global CHILD_PROC
    CHILD_PROC = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding='utf-8',
        errors='replace',
        bufsize=0,
    )
    captured = []
    buffer = ''
    throttle = {'time': 0.0, 'percent': None, 'raw': ''}
    try:
        assert CHILD_PROC.stdout is not None
        while True:
            ch = CHILD_PROC.stdout.read(1)
            if ch == '' and CHILD_PROC.poll() is not None:
                break
            if not ch:
                time.sleep(0.03)
                continue
            if capture_output:
                captured.append(ch)
            print(ch, end='', flush=True)
            if ch in {'\r', '\n'}:
                clean = buffer.strip()
                buffer = ''
                if clean and live_status is not None:
                    update_live_progress_status(clean, live_status, throttle)
            else:
                buffer += ch
        clean = buffer.strip()
        if clean and live_status is not None:
            update_live_progress_status(clean, live_status, throttle)
        return CHILD_PROC.wait(), ''.join(captured) if capture_output else ''
    finally:
        CHILD_PROC = None


def strip_dry_run_options(cmd):
    cleaned = []
    for item in cmd:
        if item in {'--dry-run', '-n'}:
            continue
        # Cas simple : options courtes combinées, ex: -avhn -> -avh.
        if item.startswith('-') and not item.startswith('--') and len(item) > 2 and 'n' in item[1:]:
            item = '-' + item[1:].replace('n', '')
            if item == '-':
                continue
        cleaned.append(item)
    return cleaned


def ensure_option(cmd, option):
    if option not in cmd:
        cmd.append(option)


def parse_size_to_bytes(value):
    raw = str(value or '').strip().replace('\xa0', ' ')
    if not raw:
        return None
    match = re.match(r'^([0-9][0-9., ]*)\s*([KMGTPE]?i?B?|bytes?|octets?)?$', raw, flags=re.IGNORECASE)
    if not match:
        return None
    number = match.group(1).strip().replace(' ', '')
    unit = (match.group(2) or '').strip().upper()
    if not unit:
        try:
            return int(number.replace(',', '').replace('.', ''))
        except Exception:
            return None
    if ',' in number and '.' not in number:
        number = number.replace(',', '.')
    else:
        number = number.replace(',', '')
    try:
        amount = float(number)
    except Exception:
        return None
    if unit.startswith('BYTE') or unit.startswith('OCTET') or unit == 'B':
        factor = 1
    else:
        base = 1024 if 'I' in unit else 1000
        factor = base ** {'K': 1, 'M': 2, 'G': 3, 'T': 4, 'P': 5, 'E': 6}.get(unit[:1], 0)
    return int(amount * factor)


def parse_dry_run_total_bytes(text):
    """Récupère le volume réellement prévu en transfert depuis le résumé --stats."""
    patterns = [
        r'Total transferred file size:\s*(.+?)(?:\n|$)',
        r'Literal data:\s*(.+?)(?:\n|$)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text or '', flags=re.IGNORECASE)
        if not match:
            continue
        raw = match.group(1).strip()
        raw = re.sub(r'\s+bytes?$', '', raw, flags=re.IGNORECASE)
        value = parse_size_to_bytes(raw)
        if value is not None:
            return value
    return None


def parse_stat_integer(value):
    raw = str(value or '').replace('\xa0', ' ')
    match = re.search(r'[0-9][0-9., ]*', raw)
    if not match:
        return None
    digits = re.sub(r'\D+', '', match.group(0))
    if not digits:
        return None
    try:
        return int(digits)
    except Exception:
        return None


def parse_dry_run_total_files(text):
    """Récupère uniquement le total de fichiers prévu par le dry-run rsync.
    On n'essaie pas d'en déduire une progression en temps réel : rsync ne donne pas
    un compteur fiable fichier par fichier avec --info=progress2.
    """
    patterns = [
        r'Number of regular files transferred:\s*([0-9][0-9., ]*)',
        r'Number of files transferred:\s*([0-9][0-9., ]*)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text or '', flags=re.IGNORECASE)
        if not match:
            continue
        value = parse_stat_integer(match.group(1))
        if value is not None:
            return value
    return None


def human_size(bytes_value):
    try:
        value = float(bytes_value)
    except Exception:
        return ''
    to = value / (1000 ** 4)
    if to >= 1:
        return f'{to:.2f} To' if to < 10 else f'{to:.1f} To'
    go = value / (1000 ** 3)
    if go >= 100:
        return f'{go:.0f} Go'
    if go >= 10:
        return f'{go:.1f} Go'
    if go >= 1:
        return f'{go:.2f} Go'
    mo = value / (1000 ** 2)
    if mo >= 1:
        return f'{mo:.0f} Mo'
    ko = value / 1000
    if ko >= 1:
        return f'{ko:.0f} Ko'
    return f'{int(value)} o'


def build_rsync_command(dry_run=None, force_progress=False, force_stats=False, suppress_progress=False):
    rsync_bin = RUNTIME.get('rsync_bin') or '/usr/bin/rsync'
    cmd = [rsync_bin]
    dry_run_enabled = (CONFIG.get('DRY_RUN') == '1') if dry_run is None else bool(dry_run)

    if CONFIG.get('ARCHIVE') == '1':
        cmd.append('-a')
    elif CONFIG.get('DELETE') == '1':
        # --delete a besoin de parcourir les dossiers. Sans -a, on ajoute au minimum -r.
        cmd.append('-r')
    if CONFIG.get('NO_OWNER') == '1':
        cmd.append('--no-owner')
    if CONFIG.get('NO_GROUP') == '1':
        cmd.append('--no-group')
    if CONFIG.get('NO_PERMS') == '1':
        cmd.append('--no-perms')
    if CONFIG.get('VERBOSE') == '1':
        cmd.append('-v')
    if CONFIG.get('HUMAN') == '1':
        cmd.append('-h')
    if not suppress_progress and (CONFIG.get('PROGRESS') == '1' or force_progress):
        cmd.append('--info=progress2')
    if CONFIG.get('DELETE') == '1':
        cmd.append('--delete')
    if CONFIG.get('DELETE_EXCLUDED') == '1':
        cmd.append('--delete-excluded')
    if dry_run_enabled:
        cmd.append('--dry-run')
    if CONFIG.get('COMPRESS') == '1':
        cmd.append('-z')
    if CONFIG.get('CHECKSUM') == '1':
        cmd.append('-c')
    if CONFIG.get('HARD_LINKS') == '1':
        cmd.append('-H')
    if CONFIG.get('NUMERIC_IDS') == '1':
        cmd.append('--numeric-ids')
    if CONFIG.get('ONE_FILE_SYSTEM') == '1':
        cmd.append('-x')
    if CONFIG.get('PARTIAL') == '1':
        cmd.append('--partial')
    if CONFIG.get('INPLACE') == '1':
        cmd.append('--inplace')
    if CONFIG.get('WHOLE_FILE') == '1':
        cmd.append('-W')
    if CONFIG.get('STATS') == '1' or force_stats:
        cmd.append('--stats')

    for pattern in split_excludes(CONFIG.get('EXCLUDES', '')):
        cmd.append('--exclude=' + pattern)

    extra = str(CONFIG.get('EXTRA_ARGS') or '').strip()
    if extra:
        cmd.extend(shlex.split(extra))

    if dry_run_enabled:
        ensure_option(cmd, '--dry-run')
    else:
        cmd = strip_dry_run_options(cmd)

    source = ensure_runtime_folder_slash(CONFIG['SOURCE'])
    target = ensure_runtime_folder_slash(CONFIG['TARGET'])
    cmd.extend([source, target])
    return cmd


def prerequisites_ok():
    rsync_bin = RUNTIME.get('rsync_bin') or '/usr/bin/rsync'
    if os.path.isabs(rsync_bin):
        if not os.path.exists(rsync_bin):
            log(f'[ERREUR] rsync introuvable: {rsync_bin}')
            return False
    elif shutil.which(rsync_bin) is None:
        log(f'[ERREUR] rsync introuvable dans le PATH: {rsync_bin}')
        return False

    source = ensure_runtime_folder_slash(CONFIG.get('SOURCE', ''))
    target = ensure_runtime_folder_slash(CONFIG.get('TARGET', ''))
    if not source or not target:
        log('[ERREUR] SOURCE ou TARGET vide.')
        return False

    if not is_remote_path(source):
        if source.startswith('/remotes/'):
            if not find_remote_mountpoint(source) or not path_accessible_quick(source, timeout_seconds=5):
                log(f'[ERREUR] Source NFS non montée ou inaccessible: {source}')
                return False
        elif not os.path.exists(source):
            log(f'[ERREUR] Source introuvable: {source}')
            return False

    if not target_mount_ok():
        return False

    if CONFIG.get('AUTO_MKDIR_TARGET') == '1' and not is_remote_path(target):
        try:
            Path(target).mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            log(f'[ERREUR] Création cible impossible: {target} / {exc}')
            return False
    return True


def cache_realpath_clean(path_value):
    raw = str(path_value or '').strip()
    if not raw:
        return ''
    real = os.path.normpath(os.path.realpath(raw))
    return real.rstrip('/') or '/'


def cache_is_dangerous_endpoint(path_value):
    return cache_realpath_clean(path_value) in {'', '/', '/mnt'}


def cache_paths_overlap(left, right):
    left_s = cache_realpath_clean(left)
    right_s = cache_realpath_clean(right)
    if not left_s or not right_s:
        return False
    return left_s == right_s or left_s.startswith(right_s + '/') or right_s.startswith(left_s + '/')


def cache_safe_path_checks(source, destination):
    source_s = cache_realpath_clean(source)
    destination_s = cache_realpath_clean(destination)
    if not source_s.startswith('/') or not destination_s.startswith('/'):
        raise RuntimeError('Source et cible cache doivent etre des chemins absolus locaux.')
    if cache_is_dangerous_endpoint(source_s):
        raise RuntimeError('Source cache trop large ou dangereuse : ' + source_s)
    if cache_is_dangerous_endpoint(destination_s):
        raise RuntimeError('Cible cache trop large ou dangereuse : ' + destination_s)
    if cache_paths_overlap(source_s, destination_s):
        raise RuntimeError('Source et cible cache identiques ou chevauchantes : ' + source_s + ' <-> ' + destination_s)


def cache_run_quiet(cmd):
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
    except Exception as exc:
        return subprocess.CompletedProcess(cmd, 127, '', str(exc))


def cache_has_payload(path):
    path = Path(path)
    if not path.is_dir():
        return False
    find_bin = shutil.which('find')
    if find_bin:
        cp = cache_run_quiet([find_bin, str(path), '(', '-type', 'f', '-o', '-type', 'l', ')', '-print', '-quit'])
        return bool((cp.stdout or '').strip())
    for _root, _dirs, files in os.walk(path):
        if files:
            return True
    return False


def cache_cleanup_empty_dirs(source, dry_run=False):
    source = Path(source)
    if dry_run:
        log('DRY-RUN : nettoyage dossiers vides ignore : ' + str(source))
        return
    if not source.exists():
        return
    find_bin = shutil.which('find')
    if find_bin:
        cache_run_quiet([find_bin, str(source), '-mindepth', '1', '-type', 'd', '-empty', '-delete'])
    else:
        for root, dirs, _files in os.walk(source, topdown=False):
            for dirname in dirs:
                candidate = Path(root) / dirname
                try:
                    candidate.rmdir()
                except OSError:
                    pass
    try:
        source.rmdir()
        log('[OK] Dossier source vide supprime : ' + str(source))
    except OSError:
        pass


def cache_prepare_destination(source, destination, dry_run=False):
    source = Path(source)
    destination = Path(destination)
    if dry_run:
        log('DRY-RUN : creation destination ignoree : ' + str(destination))
        return
    destination.mkdir(parents=True, exist_ok=True)
    chmod_bin = shutil.which('chmod')
    chown_bin = shutil.which('chown')
    if chmod_bin:
        cache_run_quiet([chmod_bin, '--reference=' + str(source), str(destination)])
    if chown_bin:
        cache_run_quiet([chown_bin, '--reference=' + str(source), str(destination)])


def cache_build_rsync_command(dry_run=False):
    rsync_bin = RUNTIME.get('rsync_bin') or '/usr/bin/rsync'
    source = cache_realpath_clean(CONFIG.get('SOURCE', ''))
    target = cache_realpath_clean(CONFIG.get('TARGET', ''))
    cmd = [
        rsync_bin,
        '-aAXHhv',
        '--outbuf=L',
        '--info=progress2',
        '--stats',
        '--remove-source-files',
    ]
    if dry_run:
        cmd.append('--dry-run')
    cmd.extend([source.rstrip('/') + '/', target.rstrip('/') + '/'])
    return cmd


def cache_prerequisites_ok():
    rsync_bin = RUNTIME.get('rsync_bin') or '/usr/bin/rsync'
    if os.path.isabs(rsync_bin):
        if not os.path.exists(rsync_bin):
            log('[ERREUR] rsync introuvable: ' + rsync_bin)
            return False
    elif shutil.which(rsync_bin) is None:
        log('[ERREUR] rsync introuvable dans le PATH: ' + rsync_bin)
        return False

    if not str(CONFIG.get('SOURCE') or '').strip() or not str(CONFIG.get('TARGET') or '').strip():
        log('[ERREUR] SOURCE ou TARGET cache vide.')
        return False

    source = Path(cache_realpath_clean(CONFIG.get('SOURCE', '')))
    target = Path(cache_realpath_clean(CONFIG.get('TARGET', '')))
    try:
        cache_safe_path_checks(source, target)
    except Exception as exc:
        log('[ERREUR] ' + str(exc))
        return False
    if not target_mount_ok():
        return False
    return True


def run_cache_mode(started_at):
    source = Path(cache_realpath_clean(CONFIG.get('SOURCE', '')))
    target = Path(cache_realpath_clean(CONFIG.get('TARGET', '')))
    dry_run = parse_config_bool('DRY_RUN')
    return_code = 1
    command_start_done = False
    status_extra = {}
    final_message = 'Cache non termine'
    final_phase = 'cache'
    write_status(running=True, result='En cours', message='Preparation cache', started_at=started_at, ended_at='', pid=os.getpid(), phase='cache')
    if not cache_prerequisites_ok():
        write_status(running=False, result='Erreur', message='Prerequis cache invalides', started_at=started_at, ended_at=now_label(), pid=os.getpid(), phase='cache')
        return 1

    if not source.exists():
        log('[SKIP] Source cache absente : ' + str(source))
        write_status(running=False, result='Succès', message='Source cache absente', started_at=started_at, ended_at=now_label(), pid=os.getpid(), phase='done', progress={'percent': 100, 'percent_label': '100%'}, progress_text='100%')
        return 0

    if not cache_has_payload(source):
        log('[SKIP] Aucun fichier a deplacer : ' + str(source))
        cache_cleanup_empty_dirs(source, dry_run=dry_run)
        write_status(running=False, result='Succès', message='Rien a deplacer', started_at=started_at, ended_at=now_label(), pid=os.getpid(), phase='done', progress={'percent': 100, 'percent_label': '100%'}, progress_text='100%')
        return 0

    try:
        if not run_config_command(CONFIG.get('COMMAND_START', ''), 'COMMANDE DEBUT CACHE', started_at=started_at, phase='command_start'):
            log('[ERREUR] Commande de debut echouee. Cache annule.')
            return_code = 1
            final_message = 'Commande de debut cache echouee'
            final_phase = 'command_start'
        else:
            command_start_done = True

            log('')
            log('============================================================================')
            log('CACHE MOVER')
            log('============================================================================')
            log('Source      : ' + str(source) + '/')
            log('Destination : ' + str(target) + '/')
            cache_prepare_destination(source, target, dry_run=dry_run)

            cmd = cache_build_rsync_command(dry_run=dry_run)
            log('')
            log('============================================================================')
            log('DEPLACEMENT RSYNC CACHE')
            log('============================================================================')
            log('$ ' + shlex.join(cmd))
            log('')
            write_status(
                running=True,
                result='En cours',
                message='Deplacement cache',
                started_at=started_at,
                ended_at='',
                pid=os.getpid(),
                phase='cache',
            )
            live_status = {
                'running': True,
                'result': 'En cours',
                'message': 'Deplacement cache',
                'started_at': started_at,
                'ended_at': '',
                'pid': os.getpid(),
                'phase': 'cache',
            }
            return_code, _unused_output = run_rsync_command(cmd, live_status=live_status)
            try:
                with open(status_path(), 'r', encoding='utf-8') as handle:
                    current_status = json.load(handle)
                if isinstance(current_status.get('progress'), dict):
                    status_extra['progress'] = current_status.get('progress')
                    if current_status.get('progress_text'):
                        status_extra['progress_text'] = current_status.get('progress_text')
            except Exception:
                pass
            if return_code == 0:
                cache_cleanup_empty_dirs(source, dry_run=dry_run)
                progress = status_extra.get('progress') if isinstance(status_extra.get('progress'), dict) else {}
                progress = dict(progress)
                progress['percent'] = 100
                progress['percent_label'] = '100%'
                status_extra['progress'] = progress
                status_extra['progress_text'] = status_extra.get('progress_text') or '100%'
                log('')
                log('[OK] Cache deplace avec succes')
                final_message = 'Cache deplace'
                final_phase = 'done'
            else:
                final_message = 'Rsync cache a retourne le code ' + str(return_code)
                log('')
                log('[ERREUR] ' + final_message)
    finally:
        if command_start_done and CONFIG.get('COMMAND_END', '').strip():
            if not run_config_command(CONFIG.get('COMMAND_END', ''), 'COMMANDE FIN CACHE', started_at=started_at, phase='command_end') and return_code == 0:
                return_code = 1
                final_message = 'Commande de fin cache echouee'
                final_phase = 'cache'
                log('[ERREUR] Commande de fin cache echouee')
        if command_start_done or return_code != 1 or final_message != 'Cache non termine':
            ended_at = now_label()
            if return_code == 0:
                if CONFIG.get('COMMAND_START', '').strip() or CONFIG.get('COMMAND_END', '').strip():
                    final_message = final_message + ' - commandes start/stop OK'
                write_status(running=False, result='Succès', message=final_message, started_at=started_at, ended_at=ended_at, pid=os.getpid(), phase=final_phase, **status_extra)
            else:
                write_status(running=False, result='Erreur', message=final_message, started_at=started_at, ended_at=ended_at, pid=os.getpid(), phase=final_phase, **status_extra)
    return return_code




def parse_config_bool(key, default=False):
    raw = str(CONFIG.get(key, '1' if default else '0') or '').strip().lower()
    if raw in {'1', 'true', 'yes', 'y', 'on', 'oui'}:
        return True
    if raw in {'0', 'false', 'no', 'n', 'off', 'non', ''}:
        return False
    return bool(default)


def expand_config_command(command):
    date_stamp = datetime.now().strftime('%Y-%m-%d_%H%M')
    return command.replace('{date}', shlex.quote(date_stamp))


def run_config_command(command, label, started_at=None, phase='command'):
    command = str(command or '').strip()
    if not command:
        return True
    log('')
    log('============================================================================')
    log(label)
    log('============================================================================')
    log(command)
    write_status(running=True, result='En cours', message=label, started_at=started_at or now_label(), ended_at='', pid=os.getpid(), phase=phase)
    if parse_config_bool('DRY_RUN'):
        log('DRY-RUN : commande ignorée')
        log(expand_config_command(command))
        write_status(running=True, result='En cours', message=label + ' OK (dry-run)', started_at=started_at or now_label(), ended_at='', pid=os.getpid(), phase=phase)
        return True
    expanded = expand_config_command(command)
    rc = run_stream_command(['bash', '-e', '-o', 'pipefail', '-c', expanded])
    ok = rc == 0
    write_status(
        running=True,
        result='En cours' if ok else 'Erreur',
        message=(label + ' OK') if ok else (label + ' echouee (code ' + str(rc) + ')'),
        started_at=started_at or now_label(),
        ended_at='',
        pid=os.getpid(),
        phase=phase,
    )
    return ok


def run_stream_command(cmd):
    global CHILD_PROC
    log('$ ' + shlex.join([str(x) for x in cmd]))
    CHILD_PROC = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding='utf-8',
        errors='replace',
        bufsize=1,
    )
    try:
        assert CHILD_PROC.stdout is not None
        for line in CHILD_PROC.stdout:
            log(line.rstrip('\n'))
        return CHILD_PROC.wait()
    finally:
        CHILD_PROC = None


def archive_safe_basename(name):
    value = str(name or '').strip().replace('/', '_').replace('\\', '_').replace('\0', '')
    return value or 'archive'


def archive_should_exclude(path, source):
    patterns = split_excludes(CONFIG.get('EXCLUDES', ''))
    if not patterns:
        return False
    path = Path(path)
    try:
        rel = str(path.relative_to(source)).replace('\\', '/')
    except Exception:
        rel = path.name
    candidates = {path.name, rel, rel.rstrip('/') + '/', str(path), str(path).rstrip('/') + '/'}
    for pat in patterns:
        clean = str(pat or '').strip().replace('\\', '/')
        if not clean:
            continue
        clean_no = clean.strip('/')
        for candidate in candidates:
            cand = candidate.replace('\\', '/')
            if fnmatch.fnmatch(cand, clean) or fnmatch.fnmatch(cand.strip('/'), clean_no):
                return True
    return False


def archive_output_path(item, source, target, whole_source):
    fmt = str(CONFIG.get('ARCHIVE_FORMAT') or 'tar.7z').lower()
    if whole_source and str(CONFIG.get('ARCHIVE_NAME') or '').strip():
        base = CONFIG.get('ARCHIVE_NAME')
    else:
        base = item.name
    base = archive_safe_basename(base)
    if parse_config_bool('DATE_SUFFIX'):
        base = base + '_' + datetime.now().strftime('%Y-%m-%d_%H%M')
    if fmt == 'tar':
        suffix = '.tar'
    elif fmt in {'tar.gz', 'tgz', 'gz'}:
        suffix = '.tar.gz'
    else:
        suffix = '.tar.7z'
    return Path(target) / (base + suffix)


def find_7z():
    for name in ('7z', '7zz', '7za'):
        found = shutil.which(name)
        if found:
            return found
    return None


def archive_item_tar_gz(item, outfile):
    parent = item.parent
    tmp = outfile.with_name(outfile.name + '.tmp')
    cmd = ['tar', '--xattrs', '--acls', '--numeric-owner', '-czf', str(tmp), '-C', str(parent), item.name]
    if parse_config_bool('DRY_RUN'):
        log('DRY-RUN : ' + shlex.join(cmd))
        log(f'DRY-RUN : mv {tmp} {outfile}')
        return True
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    rc = run_stream_command(cmd)
    if rc != 0:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        return False
    tmp.replace(outfile)
    return True


def archive_item_tar(item, outfile):
    parent = item.parent
    tmp = outfile.with_name(outfile.name + '.tmp')
    cmd = ['tar', '--xattrs', '--acls', '--numeric-owner', '-cf', str(tmp), '-C', str(parent), item.name]
    if parse_config_bool('DRY_RUN'):
        log('DRY-RUN : ' + shlex.join(cmd))
        log(f'DRY-RUN : mv {tmp} {outfile}')
        return True
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    rc = run_stream_command(cmd)
    if rc != 0:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        return False
    tmp.replace(outfile)
    return True


def archive_item_tar_7z(item, outfile):
    seven = find_7z()
    if not seven:
        raise RuntimeError('7z/7zz/7za introuvable. Installe p7zip / 7zip.')
    level = str(CONFIG.get('COMPRESSION_LEVEL') or '7').strip() or '7'
    parent = item.parent
    tmp = outfile.with_name(outfile.name + '.tmp')
    tar_cmd = ['tar', '--xattrs', '--acls', '--numeric-owner', '-C', str(parent), '-cf', '-', item.name]
    seven_cmd = [seven, 'a', '-t7z', f'-mx={level}', f'-si{item.name}.tar', str(tmp)]
    if parse_config_bool('DRY_RUN'):
        log('DRY-RUN : ' + shlex.join(tar_cmd) + ' | ' + shlex.join(seven_cmd))
        log(f'DRY-RUN : mv {tmp} {outfile}')
        return True
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    log('$ ' + shlex.join(tar_cmd) + ' | ' + shlex.join(seven_cmd))
    global CHILD_PROC
    tar_proc = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert tar_proc.stdout is not None
    seven_proc = subprocess.Popen(seven_cmd, stdin=tar_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
    CHILD_PROC = seven_proc
    try:
        tar_proc.stdout.close()
        assert seven_proc.stdout is not None
        for line in seven_proc.stdout:
            log(line.rstrip('\n'))
        seven_rc = seven_proc.wait()
        tar_err = tar_proc.stderr.read().decode('utf-8', errors='replace') if tar_proc.stderr else ''
        tar_rc = tar_proc.wait()
        if tar_err.strip():
            log(tar_err.strip())
    finally:
        CHILD_PROC = None
    if tar_rc != 0 or seven_rc != 0:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        log(f'[ERREUR] Archive tar.7z : tar={tar_rc}, 7z={seven_rc}')
        return False
    tmp.replace(outfile)
    return True


def archive_one_item(item, source, target, whole_source=False):
    outfile = archive_output_path(item, source, target, whole_source)
    if outfile.exists() and not parse_config_bool('REPLACE_EXISTING', True):
        log('Archive déjà présente, ignorée : ' + str(outfile))
        return True
    Path(target).mkdir(parents=True, exist_ok=True)
    log('')
    log('→ Archive : ' + str(item))
    log('  Sortie  : ' + str(outfile))
    fmt = str(CONFIG.get('ARCHIVE_FORMAT') or 'tar.7z').lower()
    if fmt == 'tar':
        ok = archive_item_tar(item, outfile)
    elif fmt in {'tar.gz', 'tgz', 'gz'}:
        ok = archive_item_tar_gz(item, outfile)
    else:
        ok = archive_item_tar_7z(item, outfile)
    log(('✅ OK → ' if ok else '❌ Échec archive → ') + str(outfile))
    return ok


def archive_prerequisites_ok():
    if not shutil.which('tar') and not parse_config_bool('DRY_RUN'):
        log('[ERREUR] tar introuvable.')
        return False
    fmt = str(CONFIG.get('ARCHIVE_FORMAT') or 'tar.7z').lower()
    if fmt in {'tar.7z', '7z'} and not find_7z() and not parse_config_bool('DRY_RUN'):
        log('[ERREUR] 7z/7zz/7za introuvable.')
        return False
    source = Path(str(CONFIG.get('SOURCE') or '').rstrip('/'))
    target = Path(str(CONFIG.get('TARGET') or '').rstrip('/'))
    if not source.exists() or not source.is_dir():
        log('[ERREUR] Source archive introuvable ou non dossier : ' + str(source))
        return False
    if CONFIG.get('AUTO_MKDIR_TARGET') == '1':
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            log('[ERREUR] Création destination impossible : ' + str(exc))
            return False
    return True


def run_archive_mode(started_at):
    source = Path(str(CONFIG.get('SOURCE') or '').rstrip('/'))
    target = Path(str(CONFIG.get('TARGET') or '').rstrip('/'))
    return_code = 1
    write_status(running=True, result='En cours', message='Préparation archive', started_at=started_at, ended_at='', pid=os.getpid(), phase='archive')
    if not archive_prerequisites_ok():
        write_status(running=False, result='Erreur', message='Prérequis archive invalides', started_at=started_at, ended_at=now_label(), pid=os.getpid(), phase='archive')
        return 1
    try:
        ok = run_config_command(CONFIG.get('COMMAND_START', ''), 'COMMANDE DÉBUT', started_at=started_at, phase='command_start')
        if not ok:
            log('[ERREUR] Commande de début échouée. Archive annulée.')
            return_code = 1
            return return_code
        log('')
        log('============================================================================')
        log('ARCHIVE')
        log('============================================================================')
        log('Source      : ' + str(source))
        log('Destination : ' + str(target))
        log('Format      : ' + str(CONFIG.get('ARCHIVE_FORMAT') or 'tar.7z'))
        log('Children    : ' + str(CONFIG.get('ARCHIVE_CHILDREN') or '1'))
        ok = True
        if parse_config_bool('ARCHIVE_CHILDREN', True):
            items = sorted([p for p in source.iterdir() if not archive_should_exclude(p, source)], key=lambda p: p.name.lower())
            total = max(1, len(items))
            if not items:
                log('Source vide : aucune archive à créer.')
            for idx, item in enumerate(items, start=1):
                pct = int(round(((idx - 1) * 100.0) / total))
                write_status(running=True, result='En cours', message=f'Archive {idx}/{total} : {item.name}', started_at=started_at, ended_at='', pid=os.getpid(), phase='archive', progress={'percent': pct, 'percent_label': f'{pct}%'}, progress_text=f'{pct}% · Archive {idx}/{total}')
                if not archive_one_item(item, source, target, whole_source=False):
                    ok = False
            pct = 100
            write_status(running=True, result='En cours', message='Finalisation archive', started_at=started_at, ended_at='', pid=os.getpid(), phase='archive', progress={'percent': pct, 'percent_label': '100%'}, progress_text='100%')
        else:
            write_status(running=True, result='En cours', message='Archive source complète', started_at=started_at, ended_at='', pid=os.getpid(), phase='archive', progress={'percent': 0, 'percent_label': '0%'}, progress_text='0%')
            ok = archive_one_item(source, source, target, whole_source=True)
        return_code = 0 if ok else 1
        return return_code
    finally:
        end_ok = True
        if CONFIG.get('COMMAND_END', '').strip():
            end_ok = run_config_command(CONFIG.get('COMMAND_END', ''), 'COMMANDE FIN', started_at=started_at, phase='command_end')
        ended_at = now_label()
        if return_code == 0 and end_ok:
            log('[OK] Archive terminée avec succès')
            write_status(running=False, result='Succès', message='Archive terminée', started_at=started_at, ended_at=ended_at, pid=os.getpid(), phase='done', progress={'percent': 100, 'percent_label': '100%'}, progress_text='100%')
        else:
            msg = 'Archive terminée avec erreur' if end_ok else 'Commande de fin échouée'
            log('[ERREUR] ' + msg)
            write_status(running=False, result='Erreur', message=msg, started_at=started_at, ended_at=ended_at, pid=os.getpid(), phase='archive')
def main():
    signal.signal(signal.SIGTERM, handle_stop_signal)
    signal.signal(signal.SIGINT, handle_stop_signal)
    started_at = now_label()
    if not acquire_lock():
        log('[SKIP] Script déjà en cours, lock actif.')
        write_status(running=False, result='Skip', message='Déjà en cours', started_at=started_at, ended_at=now_label(), pid=os.getpid())
        return 2

    write_status(running=True, result='En cours', message='Démarrage', started_at=started_at, ended_at='', pid=os.getpid())
    try:
        log('============================================================================')
        log('BACKUP RSYNC : ' + CONFIG.get('TITLE', script_stem()))
        log('============================================================================')
        log('Date       : ' + started_at)
        log('Script     : ' + __file__)
        log('Source     : ' + CONFIG.get('SOURCE', ''))
        log('Cible      : ' + CONFIG.get('TARGET', ''))
        log('Mode       : ' + CONFIG.get('MODE', 'backup'))
        if CONFIG.get('DRY_RUN') == '1':
            log('[INFO] Pré-analyse active : rsync lance d’abord un --dry-run pour calculer le volume, puis lance la vraie copie.')
        excludes_preview = split_excludes(CONFIG.get('EXCLUDES', ''))
        if excludes_preview:
            log('[INFO] Exclusions actives : ' + ', '.join(excludes_preview))
        if CONFIG.get('DELETE_EXCLUDED') == '1':
            log('[WARN] --delete-excluded actif : les exclusions peuvent aussi être supprimées côté cible.')

        if not acquire_global_backup_lock(started_at):
            write_status(running=False, result='Erreur', message='Verrou global impossible', started_at=started_at, ended_at=now_label(), pid=os.getpid())
            return 1

        if CONFIG.get('MODE') == 'archive':
            return run_archive_mode(started_at)
        if CONFIG.get('MODE') == 'cache':
            return run_cache_mode(started_at)

        if not prerequisites_ok():
            write_status(running=False, result='Erreur', message='Prérequis invalides', started_at=started_at, ended_at=now_label(), pid=os.getpid())
            return 1

        total_bytes = None
        total_label = ''
        total_files = None
        total_files_label = ''

        if CONFIG.get('DRY_RUN') == '1':
            dry_cmd = build_rsync_command(dry_run=True, force_stats=True, suppress_progress=True)
            log('')
            log('============================================================================')
            log('SIMULATION RSYNC : calcul du volume à copier')
            log('============================================================================')
            log('$ ' + shlex.join(dry_cmd))
            log('')
            write_status(
                running=True,
                result='En cours',
                message='Simulation : calcul du volume à copier',
                started_at=started_at,
                ended_at='',
                pid=os.getpid(),
                phase='simulation',
                progress_total_bytes=0,
                progress_total_label='',
            )
            dry_code, dry_output = run_rsync_command(dry_cmd, capture_output=True)
            if dry_code != 0:
                ended_at = now_label()
                msg = f'Simulation rsync impossible, code {dry_code}'
                log('')
                log('[ERREUR] ' + msg)
                write_status(running=False, result='Erreur', message=msg, started_at=started_at, ended_at=ended_at, pid=os.getpid(), phase='simulation')
                return dry_code

            total_bytes = parse_dry_run_total_bytes(dry_output)
            if total_bytes is not None:
                total_label = human_size(total_bytes)
                log('')
                log('[INFO] Volume prévu par la simulation : ' + total_label)
            else:
                log('')
                log('[WARN] Volume prévu non trouvé dans le résumé rsync --stats. La copie continue avec total non calculé.')

            total_files = parse_dry_run_total_files(dry_output)
            if total_files is not None:
                total_files_label = f'Fichiers à copier : {total_files}'
                log('[INFO] ' + total_files_label)
            else:
                log('[WARN] Nombre de fichiers prévu non trouvé dans le résumé rsync --stats.')

        cmd = build_rsync_command(dry_run=False, force_progress=(CONFIG.get('DRY_RUN') == '1'))
        log('')
        log('============================================================================')
        log('COPIE RSYNC RÉELLE')
        log('============================================================================')
        log('$ ' + shlex.join(cmd))
        log('')
        status_extra = {}
        if total_bytes is not None:
            status_extra.update({'progress_total_bytes': total_bytes, 'progress_total_label': total_label})
        if total_files is not None:
            status_extra.update({'progress_total_files': total_files, 'progress_total_files_label': total_files_label})
        write_status(
            running=True,
            result='En cours',
            message='Copie réelle',
            started_at=started_at,
            ended_at='',
            pid=os.getpid(),
            phase='copy',
            **status_extra,
        )
        live_status = {
            'running': True,
            'result': 'En cours',
            'message': 'Copie réelle',
            'started_at': started_at,
            'ended_at': '',
            'pid': os.getpid(),
            'phase': 'copy',
        }
        live_status.update(status_extra)
        return_code, _unused_output = run_rsync_command(cmd, live_status=live_status)
        ended_at = now_label()
        final_extra = dict(status_extra)
        try:
            with open(status_path(), 'r', encoding='utf-8') as handle:
                current_status = json.load(handle)
            if isinstance(current_status.get('progress'), dict):
                final_extra['progress'] = current_status.get('progress')
                if current_status.get('progress_text'):
                    final_extra['progress_text'] = current_status.get('progress_text')
        except Exception:
            pass
        if return_code == 0:
            msg = 'Terminé avec succès'
            progress = final_extra.get('progress') if isinstance(final_extra.get('progress'), dict) else {}
            if progress:
                progress = dict(progress)
                progress['percent'] = 100
                progress['percent_label'] = '100%'
                if progress.get('total_label') and not progress.get('copied_total_label'):
                    progress['copied_total_label'] = f"{progress.get('total_label')} copiés / {progress.get('total_label')}"
                final_extra['progress'] = progress
                bits = [progress.get('percent_label') or '100%']
                if progress.get('copied_total_label'):
                    bits.append(progress['copied_total_label'])
                final_extra['progress_text'] = ' · '.join(x for x in bits if x)
            else:
                final_extra['progress'] = {'percent': 100, 'percent_label': '100%'}
                final_extra['progress_text'] = '100%'
            log('')
            log('[OK] ' + msg)
            write_status(running=False, result='Succès', message=msg, started_at=started_at, ended_at=ended_at, pid=os.getpid(), phase='done', **final_extra)
        else:
            msg = f'Rsync a retourné le code {return_code}'
            log('')
            log('[ERREUR] ' + msg)
            write_status(running=False, result='Erreur', message=msg, started_at=started_at, ended_at=ended_at, pid=os.getpid(), phase='copy', **final_extra)
        return return_code
    except KeyboardInterrupt:
        ended_at = now_label()
        log('[STOP] Interrompu par signal clavier.')
        write_status(running=False, result='Arrêté', message='Interrompu', started_at=started_at, ended_at=ended_at, pid=os.getpid())
        return 130
    except Exception as exc:
        ended_at = now_label()
        log('[ERREUR] Exception: ' + str(exc))
        write_status(running=False, result='Erreur', message=str(exc), started_at=started_at, ended_at=ended_at, pid=os.getpid())
        return 1
    finally:
        release_global_backup_lock()
        release_lock()


if __name__ == '__main__':
    sys.exit(main())
'''
    return (
        template
        .replace('__TITLE__', title)
        .replace('__LOGO__', logo)
        .replace('__METADATA__', metadata)
        .replace('__CONFIG_JSON__', config_json)
        .replace('__RUNTIME_JSON__', runtime_json)
    )
