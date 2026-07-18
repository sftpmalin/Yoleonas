@backup_bp.route('/run/<path:filename>', methods=['GET', 'POST'])
def run_script(filename):
    ajax = (
        request.args.get('ajax') == '1'
        or request.headers.get('X-Requested-With') == 'fetch'
        or 'application/json' in (request.headers.get('Accept') or '')
    )

    def finish(payload=None, status_code=200):
        if ajax:
            response = dict(payload or {})
            response.setdefault('ok', status_code < 400)
            response.setdefault('filename', os.path.basename(slugify_filename(filename)))
            return jsonify(response), status_code
        return backup_tab_redirect('scripts')

    settings = ensure_layout()
    if not backup_is_configured(settings):
        return finish({'ok': False, 'message': backup_config_error_message()}, 400)
    path = safe_script_path(filename)
    safe_name = os.path.basename(slugify_filename(filename))
    if not os.path.exists(path):
        return finish({'ok': False, 'message': 'Script introuvable'}, 404)

    clear_stale_lock(safe_name)
    cleanup_stale_global_lock_file()
    status = read_status(safe_name)
    if status.get('running'):
        return finish({'ok': True, 'already_running': True, 'message': 'Backup déjà en cours', 'status': status})

    log_path = log_file_for(safe_name)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    python_bin = settings.get('PYTHON_BIN') or '/usr/local/bin/python3'
    launch_time = now_label()

    # État immédiat pour l'interface : le tableau peut afficher "En cours"
    # sans attendre que le script enfant ait écrit son premier status.json.
    optimistic_status = {
        'running': True,
        'result': 'En cours',
        'message': 'Lancement demandé depuis Flask',
        'started_at': launch_time,
        'ended_at': '',
        'pid': '',
        'phase': 'starting',
        'queue_relaunch_count': 0,
    }

    try:
        # Écrit AVANT le lancement : le worker enfant reste toujours le dernier
        # à écrire son état final. Avant, Flask écrivait parfois l'état optimiste
        # après le démarrage tmux, ce qui pouvait écraser une fin rapide/fallback.
        write_status_file(safe_name, **optimistic_status)
        with open(log_path, 'a', encoding='utf-8') as log_handle:
            log_handle.write('\n\n')
            log_handle.write('=' * 76 + '\n')
            log_handle.write(f'Lancement depuis Yoleo : {launch_time}\n')
            log_handle.write('Mode : tmux autonome via systemd-run quand disponible\n')
            log_handle.write('=' * 76 + '\n')
        ok, msg, session, unit_name = launch_backup_tmux_systemd(path, safe_name, python_bin, log_path)
        if not ok:
            raise RuntimeError(msg)

        current_status = _read_status_file_raw(safe_name) or optimistic_status
        if current_status.get('running'):
            current_status.setdefault('started_at', launch_time)
            current_status.setdefault('ended_at', '')
            current_status['tmux_session'] = session
            current_status['systemd_unit'] = unit_name
            # Ne remplace pas le message du worker s'il a déjà pris la main.
            if current_status.get('phase') == 'starting' or not current_status.get('pid'):
                current_status['message'] = msg
            write_status_file(safe_name, **current_status)
    except Exception as exc:
        error_status = write_status_file(
            safe_name,
            running=False,
            result='Erreur',
            message='Lancement impossible : ' + str(exc),
            started_at=launch_time,
            ended_at=now_label(),
            pid='',
            phase='starting',
        )
        return finish({'ok': False, 'message': str(exc), 'status': read_status(safe_name) or error_status}, 500)

    return finish({'ok': True, 'message': 'Backup lancé', 'status': read_status(safe_name)})


@backup_bp.route('/stop/<path:filename>', methods=['GET', 'POST'])
def stop_script(filename):
    ajax = (
        request.args.get('ajax') == '1'
        or request.headers.get('X-Requested-With') == 'fetch'
        or 'application/json' in (request.headers.get('Accept') or '')
    )
    safe_name = os.path.basename(slugify_filename(filename))

    def finish(payload=None, status_code=200):
        if ajax:
            response = dict(payload or {})
            response.setdefault('ok', status_code < 400)
            response.setdefault('filename', safe_name)
            return jsonify(response), status_code
        # Secours sans JavaScript : on revient au tableau, jamais aux logs.
        return backup_tab_redirect('scripts')

    status = read_status(safe_name)
    pid = status.get('pid')
    killed = False

    if pid:
        try:
            os.killpg(int(pid), signal.SIGTERM)
            killed = True
        except Exception:
            try:
                os.kill(int(pid), signal.SIGTERM)
                killed = True
            except Exception:
                pass

        for _ in range(10):
            if not pid_alive(pid):
                break
            time.sleep(0.2)

        if pid_alive(pid):
            try:
                os.killpg(int(pid), signal.SIGKILL)
                killed = True
            except Exception:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                    killed = True
                except Exception:
                    pass

    session = backup_tmux_session_name(safe_name)
    if tmux_session_exists(session):
        run_capture(['tmux', 'kill-session', '-t', session], timeout=8)
        killed = True

    remove_lock_file(safe_name)
    removed_global_lock = remove_global_lock_for_script(safe_name, pid)
    new_status = write_status_file(
        safe_name,
        running=False,
        result='Arrêté',
        message=(
            'Stop forcé depuis Flask, locks supprimés' if killed and removed_global_lock
            else 'Stop forcé depuis Flask, lock supprimé' if killed
            else 'Locks supprimés depuis Flask' if removed_global_lock
            else 'Lock supprimé depuis Flask'
        ),
        started_at=status.get('started_at', ''),
        ended_at=now_label(),
        pid=pid or '',
    )

    log_path = log_file_for(safe_name)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, 'a', encoding='utf-8') as handle:
        if removed_global_lock:
            handle.write('\n[STOP] Stop forcé depuis Flask : processus arrêté, lock script et lock global supprimés.\n')
        else:
            handle.write('\n[STOP] Stop forcé depuis Flask : processus arrêté et lock script supprimé.\n')

    return finish({'ok': True, 'message': new_status.get('message', 'Arrêt demandé'), 'status': read_status(safe_name) or new_status})


@backup_bp.route('/settings/save', methods=['POST'])
def save_settings():
    scripts_dir = (request.form.get('SCRIPTS_DIR') or '').strip()
    if not scripts_dir:
        return backup_tab_redirect('settings', error='missing_scripts_dir')

    scripts_dir = os.path.abspath(os.path.expanduser(os.path.expandvars(scripts_dir)))
    settings = load_settings()
    settings['SCRIPTS_DIR'] = scripts_dir
    settings['LOG_DIR'] = GLOBAL_DEFAULTS['LOG_DIR']
    settings['JDOM_DIR'] = GLOBAL_DEFAULTS['JDOM_DIR']
    settings['STATUS_DIR'] = GLOBAL_DEFAULTS['STATUS_DIR']
    settings['BROWSE_SCRIPTS'] = ensure_ini_slash(scripts_dir)
    settings['BROWSE_SOURCE'] = settings.get('BROWSE_SOURCE') or GLOBAL_DEFAULTS['BROWSE_SOURCE']
    settings['BROWSE_CIBLE'] = settings.get('BROWSE_CIBLE') or GLOBAL_DEFAULTS['BROWSE_CIBLE']
    write_settings(settings)
    ensure_layout()
    # Après sauvegarde des réglages, on revient sur la page principale du module.
    # Rester sur /setting avec ?saved=1 est déroutant : l'utilisateur a terminé
    # la configuration et doit retrouver directement le tableau Backup.
    return backup_tab_redirect('scripts')


@backup_bp.route('/browse', methods=['POST'])
def browse():
    path = ensure_ini_slash(request.form.get('path') or '/')
    kind = (request.form.get('kind') or '').strip().lower()
    if not os.path.isdir(path):
        return jsonify({'error': 'Dossier introuvable', 'browse_state': load_browse_state()}), 404
    blacklist = {'bin', 'boot', 'dev', 'etc', 'lib', 'lib32', 'lib64', 'libx32', 'proc', 'root', 'run', 'sbin', 'sys', 'tmp', 'usr', 'var'}
    folders = []
    try:
        current_path = browse_response_path(path)
        if kind in {'source', 'cible', 'target', 'left', 'right'}:
            save_browse_state(kind, current_path)
        if path != '/':
            parent = os.path.dirname(path.rstrip('/')) or '/'
            folders.append({'name': '..', 'path': browse_response_path(parent)})
        for name in os.listdir(path):
            full = os.path.join(path, name)
            if not os.path.isdir(full) or name.startswith('.'):
                continue
            if path == '/' and name in blacklist:
                continue
            folders.append({'name': name, 'path': browse_response_path(full)})
        folders.sort(key=lambda item: (item['name'] != '..', item['name'].lower()))
        return jsonify({'folders': folders, 'current_path': current_path, 'browse_state': load_browse_state()})
    except Exception as exc:
        return jsonify({'error': str(exc), 'browse_state': load_browse_state()}), 500


@backup_bp.route('/browse/state', methods=['GET', 'POST'])
def browse_state_api():
    if request.method == 'POST':
        kind = request.form.get('kind') or ''
        path = request.form.get('path') or '/'
        return jsonify({'browse_state': save_browse_state(kind, path), 'config_file': CONFIG_FILE_DISPLAY})
    return jsonify({'browse_state': load_browse_state(), 'config_file': CONFIG_FILE_DISPLAY})



@backup_bp.route('/api/log/<path:filename>')
def api_log(filename):
    settings = ensure_layout()
    safe_name = os.path.basename(slugify_filename(filename))
    max_lines = request.args.get('max_lines') or settings.get('MAX_LOG_LINES', '900')
    log_path = log_file_for(safe_name)
    return jsonify({
        'filename': safe_name,
        'log_file': log_path,
        'log_text': tail_text(log_path, max_lines),
        'status': read_status(safe_name),
        'updated_at': now_label(),
    })

@backup_bp.route('/api/status')
def api_status():
    return jsonify({'scripts': list_scripts(), 'updated_at': now_label()})


# Route réseau retirée : fonctionnalité déplacée dans Partage / NFS client.
def network_show():
    settings = ensure_layout()
    machine = (request.form.get('machine') or '').strip()
    exports = show_exports(machine)
    scripts = list_scripts()
    selected_log = request.args.get('file') or (scripts[0]['name'] if scripts else '')
    stats = {'total': len(scripts), 'running': sum(1 for item in scripts if item['status'].get('running')), 'dry_run': sum(1 for item in scripts if item.get('dry_run')), 'backup': sum(1 for item in scripts if item.get('mode') == 'backup'), 'mirror': sum(1 for item in scripts if item.get('mode') == 'mirror'), 'archive': sum(1 for item in scripts if item.get('mode') == 'archive'), 'cache': sum(1 for item in scripts if item.get('mode') == 'cache')}
    return render_template('services_backup.html', conf=settings, scripts=scripts, stats=stats, active_tab='network', edit_data=default_form_data(), edit_file='', is_edit=False, selected_log=selected_log, selected_log_stem=Path(selected_log).stem if selected_log else '', selected_log_status=read_status(selected_log) if selected_log else {}, config_file=CONFIG_FILE_DISPLAY, log_text=tail_text(log_file_for(selected_log), settings.get('MAX_LOG_LINES', '900')) if selected_log else '', browse_state=load_browse_state(), browse_ini_file=CONFIG_FILE_DISPLAY, network_general=network_general(), network_mounts=load_known_mounts(), network_exports=exports, network_machine=machine, network_log=tail_text(NETWORK_LOG_FILE, settings.get('MAX_LOG_LINES', '900')))


# Route réseau retirée : fonctionnalité déplacée dans Partage / NFS client.
def network_mount():
    machine = (request.form.get('machine') or '').strip()
    options = (request.form.get('options') or network_general()['default_options']).strip()
    exports = request.form.getlist('exports')
    auto_mount = bool(request.form.get('auto_mount'))
    for export_path in exports:
        mount_export(machine, export_path, options, force=False, auto_mount=auto_mount)
    return backup_tab_redirect('main', machine=machine)


# Route réseau retirée : fonctionnalité déplacée dans Partage / NFS client.
def network_refresh():
    start_network_refresh_async(force=True, reason='manuel')
    return backup_tab_redirect('main')


# Route réseau retirée : fonctionnalité déplacée dans Partage / NFS client.
def network_toggle_auto():
    section = request.form.get('section') or ''
    enabled = bool(request.form.get('auto_mount'))
    set_mount_auto(section, enabled)
    return backup_tab_redirect('main')


# Route réseau retirée : fonctionnalité déplacée dans Partage / NFS client.
def network_delete_mount():
    section = request.form.get('section') or ''
    delete_known_mount(section, unmount=True, remove_empty_dir=True)
    return backup_tab_redirect('main')


# API réseau retirée : fonctionnalité déplacée dans Partage / NFS client.
def api_network():
    settings = ensure_layout()
    return jsonify({
        'mounts': load_known_mounts(),
        'refresh_running': network_refresh_is_running(),
        'log_text': tail_text(NETWORK_LOG_FILE, settings.get('MAX_LOG_LINES', '900')),
        'updated_at': now_label(),
    })
