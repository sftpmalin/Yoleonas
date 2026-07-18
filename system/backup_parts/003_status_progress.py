def status_file_for(filename):
    settings = ensure_layout()
    stem = Path(slugify_filename(filename)).stem
    return os.path.join(settings['STATUS_DIR'], stem + '.status.json')


def log_file_for(filename):
    settings = ensure_layout()
    stem = Path(slugify_filename(filename)).stem
    return os.path.join(settings['LOG_DIR'], stem + '.log')


def lock_file_for(filename):
    settings = ensure_layout()
    stem = Path(slugify_filename(filename)).stem
    return os.path.join(settings['STATUS_DIR'], stem + '.lock')


def pid_alive(pid):
    try:
        if not pid:
            return False
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def write_status_file(filename, **payload):
    path = status_file_for(filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        'title': Path(slugify_filename(filename)).stem,
        'script': os.path.basename(slugify_filename(filename)),
        'updated_at': now_label(),
    }
    data.update(payload)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return data


def remove_lock_file(filename):
    try:
        os.remove(lock_file_for(filename))
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def clear_stale_lock(filename):
    lock_path = lock_file_for(filename)
    if not os.path.exists(lock_path):
        return False
    pid = read_text(lock_path, '').strip()
    if not pid_alive(pid):
        remove_lock_file(filename)
        return True
    return False


def global_lock_file():
    settings = ensure_layout()
    return os.path.join(settings['STATUS_DIR'], '.backup_global.lock')


def read_global_lock_file():
    raw = read_text(global_lock_file(), '').strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {'pid': raw}


def cleanup_stale_global_lock_file():
    info = read_global_lock_file()
    if not info:
        return False
    pid = info.get('pid')
    if not pid or not pid_alive(pid):
        try:
            os.remove(global_lock_file())
            return True
        except FileNotFoundError:
            return False
        except Exception:
            return False
    return False


def remove_global_lock_for_script(filename, pid=None):
    """Nettoyage de secours côté Flask.
    Le script enfant nettoie normalement le verrou global lui-même. Mais si Flask
    force un arrêt ou si le signal arrive au mauvais moment, on enlève le verrou
    uniquement s'il appartient au script/PID arrêté, ou s'il est déjà obsolète.
    """
    info = read_global_lock_file()
    if not info:
        return False

    safe_name = os.path.basename(slugify_filename(filename))
    lock_pid = str(info.get('pid') or '')
    wanted_pid = str(pid or '')
    lock_script = str(info.get('script') or '')

    owned_by_pid = bool(wanted_pid and lock_pid == wanted_pid)
    owned_by_script = bool(lock_script and lock_script == safe_name)
    stale = bool(lock_pid and not pid_alive(lock_pid)) or not lock_pid

    if owned_by_pid or owned_by_script or stale:
        try:
            os.remove(global_lock_file())
            return True
        except FileNotFoundError:
            return False
        except Exception:
            return False
    return False


def parse_rsync_size_to_bytes(value):
    """Convertit la taille affichée par rsync en octets.
    Accepte les formats classiques : 123456789, 1,234,567, 1.23G, 850.4M, 12.0KB.
    """
    raw = str(value or '').strip()
    if not raw:
        return None
    raw = raw.replace('\xa0', ' ')
    match = re.match(r'^([0-9][0-9., ]*)\s*([KMGTPE]?i?B?|bytes?|octets?)?$', raw, flags=re.IGNORECASE)
    if not match:
        return None

    number = match.group(1).strip().replace(' ', '')
    unit = (match.group(2) or '').strip().upper()

    # Sans unité, rsync affiche souvent 1,234,567 : virgules = séparateurs de milliers.
    if not unit:
        try:
            return int(number.replace(',', '').replace('.', ''))
        except Exception:
            return None

    # Avec unité, la virgule peut être un séparateur décimal français si elle apparaît seule.
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
        power_by_unit = {'K': 1, 'M': 2, 'G': 3, 'T': 4, 'P': 5, 'E': 6}
        factor = base ** power_by_unit.get(unit[:1], 0)
    return int(amount * factor)


def format_go(bytes_value):
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


def parse_rsync_stat_integer(value):
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


def parse_rsync_total_files_from_text(text):
    """Récupère uniquement le total de fichiers prévu par le résumé --stats."""
    if not text:
        return None
    patterns = [
        r'Number of regular files transferred:\s*([0-9][0-9., ]*)',
        r'Number of files transferred:\s*([0-9][0-9., ]*)',
    ]
    last = None
    for pattern in patterns:
        for match in re.finditer(pattern, str(text), flags=re.IGNORECASE):
            value = parse_rsync_stat_integer(match.group(1))
            if value is not None:
                last = value
    return last


def parse_rsync_progress_from_text(text):
    """Récupère la dernière ligne --info=progress2 visible dans le log rsync."""
    if not text:
        return {}

    # Les lignes de progression rsync sont souvent séparées par \r, pas seulement par \n.
    normalized = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', str(text)).replace('\r', '\n')
    candidates = re.split(r'\n+', normalized)
    pattern = re.compile(
        r'^\s*(?P<amount>[0-9][0-9., ]*\s*(?:[KMGTPE]?i?B?|bytes?|octets?)?)\s+'
        r'(?P<percent>100|[0-9]{1,2})%\s+'
        r'(?P<speed>[0-9][0-9.,]*\s*[KMGTPE]?i?B?/s)?\s*'
        r'(?P<eta>\d+:\d{2}:\d{2}|\d+:\d{2})?',
        flags=re.IGNORECASE,
    )

    last = {}
    for line in candidates:
        if '%' not in line:
            continue
        match = pattern.search(line)
        if not match:
            continue
        amount_text = ' '.join((match.group('amount') or '').split())
        percent = int(match.group('percent'))
        bytes_value = parse_rsync_size_to_bytes(amount_text)
        copied_label = format_go(bytes_value) if bytes_value is not None else amount_text
        last = {
            'percent': max(0, min(100, percent)),
            'percent_label': f'{percent}%',
            'copied_label': copied_label,
            'copied_bytes': bytes_value,
            'raw_copied': amount_text,
            'speed': ' '.join((match.group('speed') or '').split()),
            'eta': match.group('eta') or '',
        }
    return last




def update_live_progress_status(line, base_status, throttle=None):
    """Écrit une progression live dans le status.json du script courant.

    Le tableau continue de relire le log comme avant, mais ce champ évite que
    la barre reste bloquée à 0 % quand les retours chariot rsync sont mal
    conservés par tmux/systemd ou par le fallback subprocess.
    """
    progress = parse_rsync_progress_from_text(line)
    if not progress:
        return

    data = dict(base_status or {})
    total_bytes = data.get('progress_total_bytes') or data.get('total_bytes')
    try:
        total_bytes = int(total_bytes) if total_bytes not in (None, '') else None
    except Exception:
        total_bytes = None

    if total_bytes and total_bytes > 0:
        total_label = data.get('progress_total_label') or format_go(total_bytes)
        progress['total_bytes'] = total_bytes
        progress['total_label'] = total_label
        copied_bytes = progress.get('copied_bytes')
        if copied_bytes is not None:
            computed_percent = int(max(0, min(100, round((float(copied_bytes) / float(total_bytes)) * 100))))
            progress['percent'] = computed_percent
            progress['percent_label'] = f'{computed_percent}%'
            progress['copied_total_label'] = f"{progress.get('copied_label') or format_go(copied_bytes)} copiés / {total_label}"

    now = time.monotonic()
    pct = progress.get('percent')
    raw = str(progress.get('raw_copied') or line)[:180]
    throttle = throttle if isinstance(throttle, dict) else {}
    if throttle.get('percent') == pct and throttle.get('raw') == raw and (now - float(throttle.get('time') or 0.0)) < 0.8:
        return
    throttle['time'] = now
    throttle['percent'] = pct
    throttle['raw'] = raw

    data['progress'] = progress
    bits = [progress.get('percent_label') or '']
    if progress.get('copied_total_label'):
        bits.append(progress['copied_total_label'])
    elif progress.get('copied_label'):
        bits.append(progress['copied_label'] + ' copiés')
    data['progress_text'] = ' · '.join(x for x in bits if x)
    try:
        write_status(**data)
    except Exception:
        pass


def progress_for_log(filename, read_full_total=False):
    # On lit la fin du log pour la progression rsync. Le total fichiers peut être
    # plus haut dans le dry-run, donc on peut relire le log entier en secours.
    log_path = log_file_for(filename)
    log_tail = tail_text(log_path, 2500)
    progress = parse_rsync_progress_from_text(log_tail)
    total_files = parse_rsync_total_files_from_text(log_tail)
    if total_files is None and read_full_total:
        total_files = parse_rsync_total_files_from_text(read_text(log_path, ''))
    if total_files is not None:
        progress['_total_files_from_log'] = total_files
    return progress


def with_progress(filename, status_data):
    data = dict(status_data or {})
    if data.get('running') and data.get('phase') == 'simulation':
        data['progress'] = {}
        data['progress_text'] = ''
        return data
    needs_full_total = data.get('progress_total_files') in (None, '') and data.get('total_files') in (None, '')
    stored_progress = data.get('progress') if isinstance(data.get('progress'), dict) else {}
    final_success = (not data.get('running')) and (data.get('result') == 'Succès' or data.get('phase') == 'done')
    if (not data.get('running')) and not final_success:
        progress = dict(stored_progress) if stored_progress else {}
        total_files_from_log = None
    else:
        progress = dict(stored_progress) if final_success and stored_progress else progress_for_log(filename, read_full_total=needs_full_total)
        total_files_from_log = progress.pop('_total_files_from_log', None) if progress else None
    if not progress and stored_progress:
        progress = dict(stored_progress)

    total_bytes = data.get('progress_total_bytes')
    if total_bytes in (None, ''):
        total_bytes = data.get('total_bytes')
    try:
        total_bytes = int(total_bytes) if total_bytes not in (None, '') else None
    except Exception:
        total_bytes = None

    total_files = data.get('progress_total_files')
    if total_files in (None, ''):
        total_files = data.get('total_files')
    if total_files in (None, ''):
        total_files = total_files_from_log
    try:
        total_files = int(total_files) if total_files not in (None, '') else None
    except Exception:
        total_files = None

    if total_files and total_files > 0:
        progress['total_files'] = total_files
        progress['file_total_label'] = f'Fichiers à copier : {total_files}'

    if progress:
        if total_bytes and total_bytes > 0:
            total_label = data.get('progress_total_label') or format_go(total_bytes)
            progress['total_bytes'] = total_bytes
            progress['total_label'] = total_label
            copied_bytes = progress.get('copied_bytes')
            if copied_bytes is not None:
                computed_percent = int(max(0, min(100, round((float(copied_bytes) / float(total_bytes)) * 100))))
                progress['percent'] = computed_percent
                progress['percent_label'] = f'{computed_percent}%'
                progress['copied_total_label'] = f"{progress.get('copied_label') or format_go(copied_bytes)} copiés / {total_label}"
            else:
                progress['copied_total_label'] = f"{progress.get('copied_label') or '—'} copiés / {total_label}"
        elif total_bytes == 0:
            progress['total_bytes'] = 0
            progress['total_label'] = data.get('progress_total_label') or ''

        if final_success:
            progress['percent'] = 100
            progress['percent_label'] = '100%'
            if total_bytes and total_bytes > 0:
                total_label = data.get('progress_total_label') or progress.get('total_label') or format_go(total_bytes)
                progress['total_label'] = total_label
                progress['copied_total_label'] = f'{total_label} copiés / {total_label}'

        data['progress'] = progress
        if data.get('running') or final_success:
            bits = [progress.get('percent_label') or '']
            if progress.get('copied_total_label'):
                bits.append(progress['copied_total_label'])
            elif progress.get('copied_label'):
                bits.append(progress['copied_label'] + ' copiés')
            # Important : on ne met pas le total fichiers dans progress_text.
            # Le haut reste léger : pourcentage + taille seulement.
            data['progress_text'] = ' · '.join(x for x in bits if x)
    else:
        if final_success:
            data['progress'] = {'percent': 100, 'percent_label': '100%'}
            data['progress_text'] = '100%'
        else:
            data['progress'] = {}
            data['progress_text'] = ''
    return data


def _read_status_file_raw(filename):
    path = status_file_for(filename)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except Exception:
        return {}


def infer_finished_status_from_log(filename, data=None):
    """Filet de sécurité pour les environnements sans vrai systemd-run.

    En mode tmux direct/fallback, le script enfant écrit normalement son
    .status.json final lui-même. Si ce fichier reste pourtant bloqué sur
    running=True alors que la session tmux est terminée, on récupère l'état
    minimal depuis la fin du log. Ça évite les cartes bloquées sur "En cours"
    alors que le log contient déjà [OK] Terminé avec succès.
    """
    current = dict(data or {})
    log_tail = tail_text(log_file_for(filename), 2500)
    if not log_tail:
        return None

    normalized = str(log_tail).replace('\r', '\n')
    current = dict(data or {})
    launch_marker = 'Lancement depuis Yoleo :'
    launch_index = normalized.rfind(launch_marker)
    if launch_index >= 0:
        normalized = normalized[launch_index:]
    elif current.get('running'):
        # Ne pas conclure depuis un vieux succès resté dans la fin du log.
        return None
    ended_at = current.get('ended_at') or now_label()
    started_at = current.get('started_at', '')
    pid = current.get('pid', '')

    success_markers = (
        '[OK] Terminé avec succès',
        'Terminé avec succès',
    )
    error_markers = (
        '[ERREUR]',
        'Rsync a retourné le code',
        'Simulation rsync impossible',
        'Prérequis invalides',
    )
    stop_markers = (
        '[STOP] Signal reçu',
        '[STOP]',
        'Interrompu',
    )

    if any(marker in normalized for marker in success_markers):
        current.update({
            'running': False,
            'result': 'Succès',
            'message': 'Terminé avec succès',
            'started_at': started_at,
            'ended_at': ended_at,
            'pid': pid,
            'phase': 'done',
            'recovered_from_log': True,
        })
        return current

    if any(marker in normalized for marker in stop_markers):
        current.update({
            'running': False,
            'result': 'Arrêté',
            'message': 'Arrêt détecté dans le log',
            'started_at': started_at,
            'ended_at': ended_at,
            'pid': pid,
            'phase': 'stopped',
            'recovered_from_log': True,
        })
        return current

    if any(marker in normalized for marker in error_markers):
        current.update({
            'running': False,
            'result': 'Erreur',
            'message': 'Erreur détectée dans le log',
            'started_at': started_at,
            'ended_at': ended_at,
            'pid': pid,
            'phase': current.get('phase') or 'error',
            'recovered_from_log': True,
        })
        return current

    return None



def relaunch_waiting_backup(filename, data):
    """Relance une sauvegarde restée en attente si son worker tmux/PID a disparu.

    Cas corrigé : plusieurs backups lancés à la suite. Un backup peut rester
    affiché "En attente" puis perdre son worker, et l'ancien filet de sécurité
    pouvait le reclasser à tort en "Arrêté" depuis les anciens logs. Ici on ne
    touche qu'aux jobs qui étaient vraiment en phase waiting, jamais aux jobs
    arrêtés volontairement.
    """
    current = dict(data or {})
    try:
        relaunch_count = int(current.get('queue_relaunch_count') or 0)
    except Exception:
        relaunch_count = 0
    if relaunch_count >= 10:
        current.update({
            'running': False,
            'result': 'Erreur',
            'message': 'File d’attente perdue : worker absent après 10 relances',
            'ended_at': current.get('ended_at') or now_label(),
            'phase': 'queue_lost',
        })
        try:
            write_status_file(filename, **current)
        except Exception:
            pass
        return current

    try:
        settings = ensure_layout()
        safe_name = os.path.basename(slugify_filename(filename))
        script_path = safe_script_path(safe_name)
        if not os.path.exists(script_path):
            raise FileNotFoundError('Script introuvable : ' + safe_name)
        python_bin = settings.get('PYTHON_BIN') or '/usr/local/bin/python3'
        log_path = log_file_for(safe_name)
        launch_time = now_label()
        current.update({
            'running': True,
            'result': 'En attente',
            'message': f'Relance automatique de la file d’attente ({relaunch_count + 1}/10)',
            'started_at': current.get('started_at') or launch_time,
            'ended_at': '',
            'pid': '',
            'phase': 'waiting',
            'queue_relaunch_count': relaunch_count + 1,
        })
        write_status_file(safe_name, **current)
        with open(log_path, 'a', encoding='utf-8') as log_handle:
            log_handle.write('\n')
            log_handle.write('[QUEUE] Relance automatique : worker en attente absent à ' + launch_time + '\n')
        ok, msg, session, unit_name = launch_backup_tmux_systemd(script_path, safe_name, python_bin, log_path)
        if not ok:
            raise RuntimeError(msg)
        current.update({
            'running': True,
            'result': 'En attente',
            'message': msg,
            'ended_at': '',
            'pid': '',
            'phase': 'waiting',
            'tmux_session': session,
            'systemd_unit': unit_name,
        })
        write_status_file(safe_name, **current)
        return current
    except Exception as exc:
        current.update({
            'running': False,
            'result': 'Erreur',
            'message': 'Relance file d’attente impossible : ' + str(exc),
            'ended_at': current.get('ended_at') or now_label(),
            'phase': 'queue_lost',
        })
        try:
            write_status_file(filename, **current)
        except Exception:
            pass
        return current

def read_status(filename):
    path = status_file_for(filename)
    data = _read_status_file_raw(filename)

    if not data:
        data = {'running': False, 'result': 'Jamais lancé', 'message': '—', 'started_at': '', 'ended_at': '', 'pid': ''}
    if data.get('running') and data.get('result') == 'En attente' and not data.get('phase'):
        data['phase'] = 'waiting'

    lock_path = lock_file_for(filename)
    lock_pid = read_text(lock_path, '').strip() if os.path.exists(lock_path) else ''
    status_pid = data.get('pid')
    active_pid = status_pid or lock_pid

    if data.get('running') and str(data.get('phase') or '').lower() == 'waiting':
        # Une sauvegarde en attente doit rester vivante jusqu'à obtenir le verrou
        # global. Si son PID/session tmux disparaît, on ne la transforme pas en
        # "Arrêté" depuis un vieux log : on relance uniquement cette attente.
        #
        # Bug corrigé : quand plusieurs backups sont lancés ensemble, le second
        # peut obtenir le verrou global et commencer réellement à copier, mais
        # l'interface peut encore lire un dernier status.json marqué "waiting".
        # Le verrou global est la source fiable : s'il appartient maintenant à
        # ce script/PID, on force l'affichage "En cours" au lieu de rester en
        # badge bleu "En attente" jusqu'à la fin.
        global_owner = read_global_lock_file()
        global_pid = str(global_owner.get('pid') or '')
        global_script = os.path.basename(str(global_owner.get('script') or ''))
        safe_filename = os.path.basename(slugify_filename(filename))
        owns_global_lock = bool(
            global_pid
            and pid_alive(global_pid)
            and (
                (active_pid and global_pid == str(active_pid))
                or (global_script and global_script == safe_filename)
            )
        )
        if owns_global_lock:
            data.update({
                'running': True,
                'result': 'En cours',
                'message': 'Tour obtenu, backup en cours',
                'ended_at': '',
                'pid': global_pid,
                'phase': 'copy',
            })
            try:
                write_status_file(filename, **data)
            except Exception:
                pass
        else:
            waiting_pid_alive = bool(active_pid and pid_alive(active_pid))
            session = data.get('tmux_session') or backup_tmux_session_name(filename)
            session_active = tmux_session_exists(session) if session else False
            if not waiting_pid_alive and not session_active:
                remove_lock_file(filename)
                data = relaunch_waiting_backup(filename, data)
            elif session_active and not waiting_pid_alive:
                data.update({
                    'running': True,
                    'result': 'En attente',
                    'message': 'En attente : session tmux active',
                    'ended_at': '',
                    'phase': 'waiting',
                })
                try:
                    write_status_file(filename, **data)
                except Exception:
                    pass
    elif data.get('running') and active_pid and not pid_alive(active_pid):
        inferred = infer_finished_status_from_log(filename, data)
        if inferred:
            data = inferred
        else:
            data['running'] = False
            if data.get('result') in {'En cours', 'En attente'}:
                data['result'] = 'Inconnu'
                data['message'] = 'Processus absent, lock nettoyé automatiquement'
            data['ended_at'] = data.get('ended_at') or now_label()
        remove_lock_file(filename)
        try:
            write_status_file(filename, **data)
        except Exception:
            pass
    elif data.get('running') and not active_pid:
        # Fallback tmux direct : Flask a pu écrire un état optimiste sans PID.
        # Si la session tmux n'existe plus, on ne laisse pas l'interface bloquée
        # sur "En cours" : on récupère l'état final depuis le log si possible.
        session = data.get('tmux_session') or backup_tmux_session_name(filename)
        session_active = tmux_session_exists(session) if session else False
        if not session_active and not lock_pid:
            inferred = infer_finished_status_from_log(filename, data)
            if inferred:
                data = inferred
            else:
                data['running'] = False
                if data.get('result') in {'En cours', 'En attente'}:
                    data['result'] = 'Inconnu'
                    data['message'] = 'Session tmux absente, fin non confirmée'
                data['ended_at'] = data.get('ended_at') or now_label()
            try:
                write_status_file(filename, **data)
            except Exception:
                pass
    elif (not data.get('running')) and lock_pid:
        if pid_alive(lock_pid):
            data['running'] = True
            data['result'] = 'En cours'
            data['message'] = 'Lock actif détecté'
            data['pid'] = lock_pid
        else:
            remove_lock_file(filename)

    return with_progress(filename, data)


def cache_source_has_payload(source):
    source = str(source or '').strip()
    if not source or not os.path.isdir(source):
        return False
    try:
        for _root, _dirs, files in os.walk(source):
            if files:
                return True
    except Exception:
        # Même règle que l'ancien module Cache : si on ne peut pas lire la
        # source, on préfère signaler qu'il reste quelque chose à vérifier.
        return True
    return False


def build_list_entry(path):
    filename = os.path.basename(path)
    content = read_text(path)
    if not has_script_marker(content):
        return None
    meta = extract_script_metadata(content)
    mode = meta.get('MODE', 'backup')
    status = read_status(filename)
    cache_has_payload = cache_source_has_payload(meta.get('SOURCE', '')) if mode == 'cache' else False
    return {
        'name': filename,
        'title': meta.get('TITLE') or filename,
        'logo': meta.get('LOGO') or '/static/logo.png',
        'source': meta.get('SOURCE', ''),
        'target': meta.get('TARGET', ''),
        'mode': mode,
        'archive_format': meta.get('ARCHIVE_FORMAT', ''),
        'archive_format_label': archive_format_badge_label(meta.get('ARCHIVE_FORMAT', 'tar.7z')),
        'archive_children': meta.get('ARCHIVE_CHILDREN', '1') == '1',
        'archive_date_suffix': meta.get('DATE_SUFFIX') == '1',
        'archive_replace_existing': meta.get('REPLACE_EXISTING', '1') == '1',
        'command_start': bool(str(meta.get('COMMAND_START', '')).strip()),
        'command_end': bool(str(meta.get('COMMAND_END', '')).strip()),
        'cache_delete_source': mode == 'cache',
        'cache_status_label': 'à vider' if cache_has_payload else 'vide',
        'cache_status_class': 'badge-yellow' if cache_has_payload else 'badge-green',
        'dry_run': meta.get('DRY_RUN') == '1',
        'delete': meta.get('DELETE') == '1',
        'delete_excluded': meta.get('DELETE_EXCLUDED') == '1',
        'excludes': meta.get('EXCLUDES', ''),
        'no_owner': meta.get('NO_OWNER') == '1',
        'no_group': meta.get('NO_GROUP') == '1',
        'no_perms': meta.get('NO_PERMS') == '1',
        'status': status,
        'log_file': log_file_for(filename),
    }


def list_scripts():
    settings = ensure_layout()
    files = []
    for path in sorted(glob.glob(os.path.join(settings['SCRIPTS_DIR'], '*.py'))):
        entry = build_list_entry(path)
        if entry:
            files.append(entry)
    files.sort(key=lambda item: item['name'].lower())
    return files


def get_edit_data(filename=None):
    if not filename:
        return default_form_data()
    path = safe_script_path(filename)
    content = read_text(path)
    if not has_script_marker(content):
        return default_form_data()
    data = default_form_data()
    data.update(extract_script_metadata(content))
    return data



# ==========================================================
# PARTAGES RÉSEAU NFS DEPUIS L'HÔTE
# ==========================================================
