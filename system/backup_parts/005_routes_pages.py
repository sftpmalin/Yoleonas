@backup_bp.route('/manifest.json')
def manifest():
    data = {
        "short_name": "Backup",
        "name": "Backup",
        "start_url": "/index",
        "display": "standalone",
        "background_color": "#000000",
        "theme_color": "#000000",
        "icons": [
            {
                "src": "/static/logo.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable"
            },
            {
                "src": "/static/logo.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable"
            }
        ]
    }
    resp = make_response(jsonify(data))
    resp.headers['Content-Type'] = 'application/manifest+json'
    return resp


# ==========================================================
# 2. LE SERVICE WORKER
# ==========================================================
@backup_bp.route('/sw.js')
def sw():
    script = "self.addEventListener('fetch', (e) => {});"
    resp = make_response(script)
    resp.headers['Content-Type'] = 'application/javascript'
    return resp


# ==========================================================
# 3. LA PORTE D'ENTRÉE
# ==========================================================
@backup_bp.route('')
@backup_bp.route('/')
def root():
    return redirect(url_for('backup_bp.home_main'))


BACKUP_TAB_ROUTES = {
    'scripts': 'home_main',
    'main': 'home_main',
    'create': 'home_create',
    'settings': 'home_setting',
    'setting': 'home_setting',
    'help': 'home_lexique',
    'lexique': 'home_lexique',
    'logs': 'home_log',
    'log': 'home_log',
    'info': 'home_info',
}


def backup_tab_endpoint(tab: str) -> str:
    return 'backup_bp.' + BACKUP_TAB_ROUTES.get(str(tab or '').strip().lower(), 'home_main')


def backup_tab_redirect(tab: str = 'scripts', **values):
    return redirect(url_for(backup_tab_endpoint(tab), **values))


def backup_active_tab_from_request(default_tab: str) -> str:
    old_tab = request.args.get('tab')
    if old_tab:
        return {
            'main': 'scripts',
            'setting': 'settings',
            'lexique': 'help',
            'log': 'logs',
        }.get(old_tab, old_tab)
    return default_tab


@backup_bp.route('/index')
@backup_bp.route('/main', endpoint='home_main')
@backup_bp.route('/create', endpoint='home_create')
@backup_bp.route('/setting', endpoint='home_setting')
@backup_bp.route('/settings', endpoint='home_settings_legacy')
@backup_bp.route('/lexique', endpoint='home_lexique')
@backup_bp.route('/log', endpoint='home_log')
@backup_bp.route('/logs', endpoint='home_logs_legacy')
@backup_bp.route('/info', endpoint='home_info')
def home(default_tab='scripts'):
    settings = ensure_layout()
    configured = backup_is_configured(settings)
    route_tab = {
        'home_create': 'create',
        'home_setting': 'settings',
        'home_settings_legacy': 'settings',
        'home_lexique': 'help',
        'home_log': 'logs',
        'home_logs_legacy': 'logs',
        'home_info': 'info',
    }.get(request.endpoint.rsplit('.', 1)[-1], 'scripts')
    if request.endpoint and request.endpoint.endswith('.home') and not configured:
        route_tab = 'settings'
    active_tab = backup_active_tab_from_request(route_tab or ('scripts' if configured else 'settings'))
    selected_log = request.args.get('file') or ''
    scripts = list_scripts()
    edit_file = request.args.get('edit') or ''
    is_edit = bool(edit_file)
    edit_data = get_edit_data(edit_file if is_edit else None)
    log_text = ''
    if selected_log:
        log_text = tail_text(log_file_for(selected_log), settings.get('MAX_LOG_LINES', '900'))
    elif scripts:
        selected_log = scripts[0]['name']
        log_text = tail_text(log_file_for(selected_log), settings.get('MAX_LOG_LINES', '900'))

    selected_log_status = read_status(selected_log) if selected_log else {}

    stats = {
        'total': len(scripts),
        'running': sum(1 for item in scripts if item['status'].get('running')),
        'dry_run': sum(1 for item in scripts if item.get('dry_run')),
        'backup': sum(1 for item in scripts if item.get('mode') == 'backup'),
        'mirror': sum(1 for item in scripts if item.get('mode') == 'mirror'),
        'archive': sum(1 for item in scripts if item.get('mode') == 'archive'),
        'cache': sum(1 for item in scripts if item.get('mode') == 'cache'),
    }
    return render_template(
        'services_backup.html',
        conf=settings,
        scripts=scripts,
        stats=stats,
        active_tab=active_tab,
        edit_data=edit_data,
        edit_file=edit_file,
        is_edit=is_edit,
        selected_log=selected_log,
        selected_log_stem=Path(selected_log).stem if selected_log else '',
        selected_log_status=selected_log_status,
        config_file=CONFIG_FILE_DISPLAY,
        service_active='backup',
        backup_base_url=url_for('backup_bp.root').rstrip('/'),
        log_text=log_text,
        browse_state=load_browse_state(),
        browse_ini_file=CONFIG_FILE_DISPLAY,
        config_exists=backup_conf_exists(),
        scripts_configured=configured,
        settings_saved=request.args.get('saved') == '1',
        settings_error=request.args.get('error') or '',
    )


@backup_bp.route('/save', methods=['POST'])
def save_script():
    settings = ensure_layout()
    if not backup_is_configured(settings):
        return backup_tab_redirect('settings', error='missing_scripts_dir')
    filename = slugify_filename(request.form.get('filename'))
    old_filename = request.form.get('old_filename') or ''
    path = safe_script_path(filename)
    data = normalize_form(request.form)
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(build_generated_script(data))
    current_mode = os.stat(path).st_mode
    os.chmod(path, current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    if old_filename and os.path.basename(old_filename) != os.path.basename(filename):
        old_path = safe_script_path(old_filename)
        if os.path.exists(old_path):
            os.remove(old_path)
    return backup_tab_redirect('scripts')


@backup_bp.route('/edit/<path:filename>')
def edit_script(filename):
    return backup_tab_redirect('create', edit=os.path.basename(filename))


@backup_bp.route('/delete', methods=['POST'])
def delete_script():
    filename = request.form.get('filename') or ''
    path = safe_script_path(filename)
    if os.path.exists(path):
        os.remove(path)
    for extra_path in (log_file_for(filename), status_file_for(filename), lock_file_for(filename)):
        try:
            os.remove(extra_path)
        except FileNotFoundError:
            pass
    return backup_tab_redirect('scripts')



def run_capture(cmd, timeout=20):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, ((proc.stdout or '') + (proc.stderr or '')).strip()
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ''
        err = exc.stderr or ''
        if isinstance(out, bytes):
            out = out.decode('utf-8', errors='replace')
        if isinstance(err, bytes):
            err = err.decode('utf-8', errors='replace')
        return 124, (str(out) + str(err)).strip() or 'timeout'
    except Exception as exc:
        return 127, str(exc)


def backup_tmux_safe_part(value: str, default: str = 'backup') -> str:
    value = Path(slugify_filename(value or default)).stem
    value = re.sub(r'[^A-Za-z0-9_.-]+', '_', value).strip('._-')
    return value or default


def backup_tmux_session_name(filename: str) -> str:
    return ('backup-' + backup_tmux_safe_part(filename))[:90]


def backup_tmux_unit_name(session: str) -> str:
    safe = re.sub(r'[^A-Za-z0-9_.-]+', '-', str(session or 'backup')).strip('.-_') or 'backup'
    return ('yoleo-' + safe)[:120]


def tmux_available() -> bool:
    return shutil.which('tmux') is not None


def systemd_run_available() -> bool:
    return shutil.which('systemd-run') is not None


def tmux_session_exists(session: str) -> bool:
    if not session or not tmux_available():
        return False
    rc, _out = run_capture(['tmux', 'has-session', '-t', session], timeout=4)
    return rc == 0


def launch_backup_tmux_systemd(script_path: str, safe_name: str, python_bin: str, log_path: str):
    """Lance le rsync dans une vraie session tmux indépendante de Flask/Gunicorn.

    systemd-run démarre un wrapper qui crée la session tmux puis attend sa fin.
    Si Flask/Gunicorn redémarre, l'unité transitoire et tmux continuent.
    """
    session = backup_tmux_session_name(safe_name)
    unit = backup_tmux_unit_name(session)
    if tmux_session_exists(session):
        return True, f'Session tmux déjà active : {session}', session, unit + '.service'
    if not tmux_available():
        return False, 'tmux introuvable. Installe tmux pour lancer les backups détachés.', session, unit + '.service'

    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    worker_cmd = 'exec ' + shlex.join([python_bin, script_path]) + ' >> ' + shlex.quote(log_path) + ' 2>&1'
    wrapper = "\n".join([
        'set -u',
        f'SESSION={shlex.quote(session)}',
        'if tmux has-session -t "$SESSION" 2>/dev/null; then exit 0; fi',
        'tmux new-session -d -s "$SESSION" ' + shlex.quote(worker_cmd),
        'rc=$?',
        'if [ $rc -ne 0 ]; then exit $rc; fi',
        'while tmux has-session -t "$SESSION" 2>/dev/null; do sleep 2; done',
    ])
    systemd_error = ''
    if systemd_run_available():
        run_capture(['systemctl', 'reset-failed', unit + '.service'], timeout=8)
        rc, out = run_capture([
            'systemd-run',
            '--unit', unit,
            '--description', f'Yoleo Backup tmux {session}',
            '--collect',
            '--property=Type=simple',
            '--property=Restart=no',
            '/bin/bash', '-lc', wrapper,
        ], timeout=15)
        if rc == 0:
            for _ in range(20):
                if tmux_session_exists(session):
                    return True, f'Backup lancé dans tmux autonome : {session} ({unit}.service)', session, unit + '.service'
                time.sleep(0.15)
            return True, f'Backup lancé via systemd-run : {unit}.service', session, unit + '.service'
        systemd_error = out or f'systemd-run impossible pour {unit}.service'
    else:
        systemd_error = 'systemd-run introuvable'

    # Secours si systemd-run n'est pas disponible : la session tmux existe,
    # mais elle n'est pas isolée aussi proprement du cgroup Flask.
    rc, out = run_capture(['tmux', 'new-session', '-d', '-s', session, worker_cmd], timeout=10)
    if rc != 0:
        return False, out or systemd_error or f'Impossible de lancer tmux : {session}', session, unit + '.service'
    return True, f'Backup lancé dans tmux direct : {session} (⚠️ {systemd_error}; isolation systemd indisponible)', session, unit + '.service'
