import os
import shutil
import pwd
import grp
import stat
import time
import configparser
import subprocess
import shlex
import threading
import uuid
from flask import Blueprint, render_template, jsonify, request, send_file

file_bp = Blueprint('file_bp', __name__)

# ==========================================================
# 📁 CONF CENTRALISÉE
# ==========================================================
# app.py pose NAS_CONF_DIR. Les modules le lisent sans importer app.py
# pour éviter les imports circulaires pendant le chargement des blueprints.
_NAS_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_NAS_DEFAULT_CONF_DIR = os.path.abspath(os.path.join(_NAS_MODULE_DIR, "..", "conf"))
NAS_CONF_DIR = os.path.abspath(os.path.expanduser(os.path.expandvars(os.environ.get("NAS_CONF_DIR", _NAS_DEFAULT_CONF_DIR))))
NAS_ROOT_DIR = os.path.abspath(os.path.join(NAS_CONF_DIR, ".."))

def nas_conf_file(name: str) -> str:
    return os.path.join(NAS_CONF_DIR, name)

def nas_root_path(*parts: str) -> str:
    return os.path.join(NAS_ROOT_DIR, *parts)


# --- CONFIGURATION PAR DÉFAUT ---
# Un seul fichier de conf officiel pour ce module.
# L'ancien file.ini est abandonné : l'état UI (panneau gauche/droit/split)
# est maintenant sauvegardé directement dans file.conf.
CONF_SETUP_FILE = nas_conf_file('file.conf')
UNRAID_PASSWD_FILE = '/etc/passwd'
MAX_EDITOR_SIZE = 1024 * 1024  # 1 Mo
EXEC_SCRIPT_EXTS = {'.sh', '.py'}
EXEC_MAX_LOG_CHARS = 500_000
EXEC_JOBS = {}
EXEC_JOBS_LOCK = threading.Lock()

DEFAULT_FILE_CONF = {
    # Standard Linux : utilisé pour résoudre les noms UID/GID.
    'UNRAID_PASSWD_FILE': '/etc/passwd',

    # Ancien état file.ini regroupé ici.
    'LAST_LEFT': '/',
    'LAST_RIGHT': '/',
    'SPLIT_LEFT_PERCENT': '50',
}


def strip_conf_quotes(value):
    value = str(value or '').strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def file_conf_resolve_path(value):
    """Résout les chemins relatifs depuis NAS_CONF_DIR.

    Exemple :
      NAS_CONF_DIR=/dockers/conf
      ../logs/test.log -> /dockers/logs/test.log
    """
    raw = strip_conf_quotes(value)
    if not raw:
        return raw
    raw = os.path.expanduser(os.path.expandvars(raw))
    if os.path.isabs(raw):
        return os.path.abspath(raw)
    return os.path.abspath(os.path.join(NAS_CONF_DIR, raw))


def read_file_conf_raw(path=CONF_SETUP_FILE):
    data = {}
    if not os.path.exists(path):
        return data
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                if key:
                    data[key] = strip_conf_quotes(value)
    except OSError:
        pass
    return data


def write_file_conf_raw(data, path=CONF_SETUP_FILE):
    parent = os.path.dirname(path.rstrip('/')) or '.'
    os.makedirs(parent, exist_ok=True)

    # On garde un ordre lisible, puis on conserve les clés USER_* et inconnues.
    ordered_keys = [
        'UNRAID_PASSWD_FILE',
        'LAST_LEFT',
        'LAST_RIGHT',
        'SPLIT_LEFT_PERCENT',
    ]
    user_keys = sorted(k for k in data if k.startswith('USER_'))
    extra_keys = sorted(k for k in data if k not in set(ordered_keys) and not k.startswith('USER_'))

    lines = [
        '# file.conf - Module File / Gestionnaire de fichiers Flask System',
        '# Ce fichier remplace l\'ancien file.ini.',
        '# Chemins relatifs : résolus depuis le dossier conf central NAS_CONF_DIR.',
        '',
    ]

    for key in ordered_keys:
        if key in data:
            lines.append(f'{key}={data.get(key, "")}')

    if user_keys:
        lines.extend(['', '# Utilisateurs mémorisés depuis l\'interface : USER_<uid>=nom'])
        for key in user_keys:
            lines.append(f'{key}={data.get(key, "")}')

    # CONFIG_FILE est volontairement exclu : on ne recrée plus file.ini.
    extra_keys = [key for key in extra_keys if key != 'CONFIG_FILE']
    if extra_keys:
        lines.extend(['', '# Clés supplémentaires conservées'])
        for key in extra_keys:
            lines.append(f'{key}={data.get(key, "")}')

    with open(path, 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(lines).rstrip() + '\n')
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass


def ensure_file_conf_file():
    """Crée ou complète file.conf avec les valeurs par défaut.

    Contrairement à l'ancienne version, aucun file.ini n'est créé.
    """
    data = read_file_conf_raw(CONF_SETUP_FILE)
    changed = not os.path.exists(CONF_SETUP_FILE)

    # Ancienne clé : on la retire pour éviter que le module réutilise file.ini.
    if 'CONFIG_FILE' in data:
        data.pop('CONFIG_FILE', None)
        changed = True

    for key, value in DEFAULT_FILE_CONF.items():
        if not str(data.get(key, '')).strip():
            data[key] = value
            changed = True

    if changed:
        write_file_conf_raw(data, CONF_SETUP_FILE)
    return changed


# --- LECTURE DE LA CONFIGURATION EXTERNE (file.conf) ---
def _load_setup_config():
    global UNRAID_PASSWD_FILE
    ensure_file_conf_file()
    data = read_file_conf_raw(CONF_SETUP_FILE)
    UNRAID_PASSWD_FILE = file_conf_resolve_path(data.get('UNRAID_PASSWD_FILE', '/etc/passwd'))


_load_setup_config()


# --- GESTION DE L'ÉTAT UI DANS file.conf ---
def load_config():
    ensure_file_conf_file()
    _load_setup_config()
    data = read_file_conf_raw(CONF_SETUP_FILE)

    config = configparser.ConfigParser()
    config['GENERAL'] = {
        'last_left': data.get('LAST_LEFT', data.get('last_left', '/')) or '/',
        'last_right': data.get('LAST_RIGHT', data.get('last_right', '/')) or '/',
        'split_left_percent': data.get('SPLIT_LEFT_PERCENT', data.get('split_left_percent', '50')) or '50',
    }

    config['USERS'] = {}
    for key, value in data.items():
        if key.startswith('USER_') and value:
            uid = key.replace('USER_', '', 1).strip()
            if uid:
                config['USERS'][uid] = value
    return config


def save_config(config):
    try:
        ensure_file_conf_file()
        data = read_file_conf_raw(CONF_SETUP_FILE)
        data.pop('CONFIG_FILE', None)

        if 'GENERAL' not in config:
            config['GENERAL'] = {}
        if 'USERS' not in config:
            config['USERS'] = {}

        data['UNRAID_PASSWD_FILE'] = data.get('UNRAID_PASSWD_FILE') or DEFAULT_FILE_CONF['UNRAID_PASSWD_FILE']
        data['LAST_LEFT'] = config['GENERAL'].get('last_left', '/')
        data['LAST_RIGHT'] = config['GENERAL'].get('last_right', '/')
        data['SPLIT_LEFT_PERCENT'] = config['GENERAL'].get('split_left_percent', '50')

        for key in list(data.keys()):
            if key.startswith('USER_'):
                data.pop(key, None)
        for uid, name in config['USERS'].items():
            uid = str(uid).strip()
            name = str(name).strip()
            if uid and name:
                data[f'USER_{uid}'] = name

        write_file_conf_raw(data, CONF_SETUP_FILE)
        _load_setup_config()
        return True
    except Exception:
        return False


# --- UTILITAIRES ---
def get_json_payload():
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def normalize_path(path, default=None):
    if path is None:
        return default
    if not isinstance(path, str):
        path = str(path)
    path = path.strip()
    if not path:
        return default
    return os.path.abspath(os.path.expanduser(path))


def is_safe_name(name):
    if not isinstance(name, str):
        return False
    if not name.strip():
        return False
    if name in {'.', '..'}:
        return False
    if '/' in name or '\\' in name:
        return False
    return True


def truthy(value):
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on', 'oui'}


def iter_path_recursive(path):
    """
    Retourne le chemin sélectionné + tout son contenu si c'est un dossier.
    On ne suit pas les liens symboliques pour éviter de sortir du dossier choisi.
    """
    yield path
    if not os.path.isdir(path) or os.path.islink(path):
        return

    for root, dirs, files in os.walk(path, topdown=True, followlinks=False):
        for name in dirs:
            yield os.path.join(root, name)
        for name in files:
            yield os.path.join(root, name)


def chmod_path(path, mode):
    # chmod sur un lien symbolique n'a pas vraiment de sens sous Linux.
    # On évite aussi de modifier la cible d'un lien qui pourrait être hors dossier.
    if os.path.islink(path):
        return
    os.chmod(path, mode)


def chown_path(path, uid, gid):
    # lchown modifie le lien lui-même sans suivre la cible.
    if os.path.islink(path) and hasattr(os, 'lchown'):
        os.lchown(path, uid, gid)
    else:
        os.chown(path, uid, gid)


def get_unraid_users():
    users = {}
    if os.path.exists(UNRAID_PASSWD_FILE):
        try:
            with open(UNRAID_PASSWD_FILE, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.split(':')
                    if len(parts) >= 3:
                        users[parts[2]] = parts[0]
        except Exception:
            pass
    return users


def resolve_username(uid):
    uid_str = str(uid)
    try:
        return pwd.getpwuid(uid).pw_name
    except Exception:
        pass
    unraid = get_unraid_users()
    if uid_str in unraid:
        return unraid[uid_str]
    config = load_config()
    if uid_str in config['USERS']:
        return config['USERS'][uid_str]
    return uid_str


def get_all_known_users():
    all_users = {}
    try:
        for p in pwd.getpwall():
            all_users[str(p.pw_uid)] = p.pw_name
    except Exception:
        pass

    all_users.update(get_unraid_users())

    config = load_config()
    if 'USERS' in config:
        for uid, name in config['USERS'].items():
            all_users[uid] = name

    result = []
    for uid, name in all_users.items():
        if uid == '0' or uid == '99' or (len(uid) == 4 and uid.isdigit()):
            result.append({"uid": uid, "name": name})

    result.sort(key=lambda x: int(x['uid']) if x['uid'].isdigit() else 99999)
    return result


def get_file_info(path):
    try:
        st = os.stat(path)
        user_name = resolve_username(st.st_uid)
        try:
            group = grp.getgrgid(st.st_gid).gr_name
        except Exception:
            group = str(st.st_gid)
        mode = stat.filemode(st.st_mode)
        is_dir = os.path.isdir(path)
        ext = os.path.splitext(path)[1].lower()
        is_exec_script = (
            not is_dir
            and ext in EXEC_SCRIPT_EXTS
            and os.path.isfile(path)
            and os.access(path, os.X_OK)
        )

        return {
            "name": os.path.basename(path),
            "path": path,
            "is_dir": is_dir,
            "is_exec_script": is_exec_script,
            "size": st.st_size,
            "mtime": time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime)),
            "perms": mode,
            "octal": format(st.st_mode & 0o777, "03o"),
            "owner": user_name,
            "group": group,
            "uid": st.st_uid,
            "gid": st.st_gid,
        }
    except Exception:
        return None


def is_executable_script(path):
    """Autorise uniquement les fichiers .sh/.py qui ont le bit exécutable."""
    if not path or not os.path.isfile(path):
        return False
    ext = os.path.splitext(path)[1].lower()
    return ext in EXEC_SCRIPT_EXTS and os.access(path, os.X_OK)


def parse_script_command(command_line):
    """
    Découpe une ligne du type :
        /dockers/scripts/test.sh -1 --option "valeur avec espace"

    Important : on utilise shlex.split puis subprocess.Popen(cmd, shell=False).
    Les paramètres sont donc transmis au script, mais ils ne passent jamais par un shell.
    """
    if not isinstance(command_line, str):
        return None, [], "Commande invalide"

    command_line = command_line.strip()
    if not command_line:
        return None, [], "Commande vide"
    if '\x00' in command_line:
        return None, [], "Commande invalide : caractère nul interdit"

    try:
        parts = shlex.split(command_line, posix=True)
    except ValueError as e:
        return None, [], f"Commande invalide : {e}"

    if not parts:
        return None, [], "Commande vide"

    raw_path = parts[0]
    args = parts[1:]

    if any('\x00' in a for a in [raw_path] + args):
        return None, [], "Commande invalide : caractère nul interdit"

    return normalize_path(raw_path), args, None


def get_script_command(path, args=None):
    args = list(args or [])
    ext = os.path.splitext(path)[1].lower()
    if ext == '.sh':
        return ['bash', path] + args
    if ext == '.py':
        return ['python3', '-u', path] + args
    return None


def append_exec_log(job_id, text):
    if not text:
        return

    with EXEC_JOBS_LOCK:
        job = EXEC_JOBS.get(job_id)
        if not job:
            return

        job['log'] += text
        if len(job['log']) > EXEC_MAX_LOG_CHARS:
            job['log'] = (
                "[... log tronqué : seules les dernières lignes sont conservées ...]\n"
                + job['log'][-EXEC_MAX_LOG_CHARS:]
            )


def run_exec_job(job_id):
    with EXEC_JOBS_LOCK:
        job = EXEC_JOBS.get(job_id)
        if not job:
            return
        path = job['path']
        args = list(job.get('args') or [])
        cmd = list(job.get('cmd') or [])

    if not cmd:
        cmd = get_script_command(path, args)

    cwd = os.path.dirname(path) or '/'
    display_cmd = shlex.join(cmd) if cmd else str(path or '')

    append_exec_log(job_id, f"$ {display_cmd}\n")
    append_exec_log(job_id, f"# cwd: {cwd}\n\n")

    if not cmd:
        append_exec_log(job_id, "ERREUR: commande de script invalide\n")
        with EXEC_JOBS_LOCK:
            job = EXEC_JOBS.get(job_id)
            if job:
                job['returncode'] = 1
                job['running'] = False
                job['status'] = 'failed'
                job['finished_at'] = time.time()
                job['process'] = None
        return

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
        )

        with EXEC_JOBS_LOCK:
            job = EXEC_JOBS.get(job_id)
            if job:
                job['pid'] = proc.pid
                job['process'] = proc

        if proc.stdout:
            for line in iter(proc.stdout.readline, ''):
                append_exec_log(job_id, line)

        returncode = proc.wait()
        append_exec_log(job_id, f"\n--- terminé avec code {returncode} ---\n")

        with EXEC_JOBS_LOCK:
            job = EXEC_JOBS.get(job_id)
            if job:
                job['returncode'] = returncode
                job['running'] = False
                job['status'] = 'finished' if returncode == 0 else 'failed'
                job['finished_at'] = time.time()
                job['process'] = None

    except FileNotFoundError as e:
        append_exec_log(job_id, f"\nERREUR: commande introuvable: {e}\n")
        with EXEC_JOBS_LOCK:
            job = EXEC_JOBS.get(job_id)
            if job:
                job['returncode'] = 127
                job['running'] = False
                job['status'] = 'failed'
                job['finished_at'] = time.time()
                job['process'] = None

    except Exception as e:
        append_exec_log(job_id, f"\nERREUR: {e}\n")
        with EXEC_JOBS_LOCK:
            job = EXEC_JOBS.get(job_id)
            if job:
                job['returncode'] = 1
                job['running'] = False
                job['status'] = 'failed'
                job['finished_at'] = time.time()
                job['process'] = None


def public_exec_job(job):
    return {
        "job_id": job.get('id'),
        "path": job.get('path'),
        "name": os.path.basename(job.get('path') or ''),
        "args": list(job.get('args') or []),
        "command": job.get('command') or (shlex.join(job.get('cmd') or []) if job.get('cmd') else job.get('path')),
        "pid": job.get('pid'),
        "running": bool(job.get('running')),
        "status": job.get('status'),
        "returncode": job.get('returncode'),
        "log": job.get('log', ''),
        "started_at": job.get('started_at'),
        "finished_at": job.get('finished_at'),
    }


# --- ROUTES ---
@file_bp.route('/file_manager')
def index():
    config = load_config()
    sl = normalize_path(config['GENERAL'].get('last_left', '/'), '/')
    sr = normalize_path(config['GENERAL'].get('last_right', '/'), '/')
    if not sl or not os.path.exists(sl):
        sl = '/'
    if not sr or not os.path.exists(sr):
        sr = '/'

    try:
        split_left_percent = float(str(config['GENERAL'].get('split_left_percent', '50')).replace(',', '.'))
    except Exception:
        split_left_percent = 50.0
    split_left_percent = max(20.0, min(80.0, split_left_percent))

    return render_template(
        'file.html',
        start_left=sl,
        start_right=sr,
        split_left_percent=f"{split_left_percent:.2f}",
    )


@file_bp.route('/api/users', methods=['GET'])
def api_users():
    return jsonify(get_all_known_users())


@file_bp.route('/api/files/ui_state', methods=['POST'])
def api_files_ui_state():
    data = get_json_payload()
    config = load_config()

    if 'split_left_percent' in data:
        try:
            split = float(str(data.get('split_left_percent')).replace(',', '.'))
        except Exception:
            return jsonify({"error": "Largeur de panneau invalide"}), 400

        split = max(20.0, min(80.0, split))
        config['GENERAL']['split_left_percent'] = f"{split:.2f}"

    if not save_config(config):
        return jsonify({"error": "Impossible de sauvegarder file.conf"}), 500

    return jsonify({
        "status": "ok",
        "split_left_percent": config['GENERAL'].get('split_left_percent', '50'),
    })


@file_bp.route('/api/files/read', methods=['POST'])
def read_file():
    data = get_json_payload()
    path = normalize_path(data.get('path'))

    if not path:
        return jsonify({"error": "Chemin invalide"}), 400
    if not os.path.exists(path):
        return jsonify({"error": "Fichier introuvable"}), 404
    if not os.path.isfile(path):
        return jsonify({"error": "Le chemin demandé n'est pas un fichier"}), 400
    if os.path.getsize(path) > MAX_EDITOR_SIZE:
        return jsonify({"error": "Fichier trop volumineux (>1Mo) pour l'édition web"}), 400

    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        return jsonify({"content": content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@file_bp.route('/api/files/save', methods=['POST'])
def save_file():
    data = get_json_payload()
    path = normalize_path(data.get('path'))
    content = data.get('content', '')

    if not path:
        return jsonify({"error": "Chemin invalide"}), 400
    if not isinstance(content, str):
        return jsonify({"error": "Contenu invalide"}), 400
    if not os.path.exists(path):
        return jsonify({"error": "Fichier introuvable"}), 404
    if not os.path.isfile(path):
        return jsonify({"error": "Le chemin demandé n'est pas un fichier"}), 400
    if len(content.encode('utf-8')) > MAX_EDITOR_SIZE:
        return jsonify({"error": "Contenu trop volumineux (>1Mo) pour l'édition web"}), 400

    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@file_bp.route('/api/files/list', methods=['POST'])
def list_files():
    data = get_json_payload()
    path = normalize_path(data.get('path', '/'), '/')
    panel = data.get('panel')

    if not path:
        return jsonify({"error": "Chemin invalide"}), 400
    if not os.path.exists(path):
        return jsonify({"error": "Chemin introuvable"}), 404
    if not os.path.isdir(path):
        return jsonify({"error": "Le chemin demandé n'est pas un dossier"}), 400

    if panel in ['left', 'right']:
        config = load_config()
        if config['GENERAL'].get(f'last_{panel}') != path:
            config['GENERAL'][f'last_{panel}'] = path
            save_config(config)

    files, dirs = [], []
    try:
        parent = os.path.dirname(path.rstrip('/')) or '/'
        dirs.append({"name": "..", "path": parent, "is_dir": True})
        with os.scandir(path) as it:
            for entry in it:
                info = get_file_info(entry.path)
                if info:
                    if info['is_dir']:
                        dirs.append(info)
                    else:
                        files.append(info)
        dirs.sort(key=lambda x: x['name'].lower())
        files.sort(key=lambda x: x['name'].lower())
        return jsonify({"current": path, "items": dirs + files})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@file_bp.route('/api/files/execute', methods=['POST'])
def execute_file():
    data = get_json_payload()
    command_line = data.get('command')

    if isinstance(command_line, str) and command_line.strip():
        path, args, parse_error = parse_script_command(command_line)
        if parse_error:
            return jsonify({"error": parse_error}), 400
    else:
        path = normalize_path(data.get('path'))
        args = []

    if not path:
        return jsonify({"error": "Chemin invalide"}), 400

    try:
        exists = os.path.exists(path)
        is_file = os.path.isfile(path)
        is_exec = os.access(path, os.X_OK)
    except (OSError, ValueError) as e:
        return jsonify({"error": f"Chemin invalide : {e}"}), 400

    if not exists:
        return jsonify({"error": "Fichier introuvable"}), 404
    if not is_file:
        return jsonify({"error": "Le chemin demandé n'est pas un fichier"}), 400

    ext = os.path.splitext(path)[1].lower()
    if ext not in EXEC_SCRIPT_EXTS:
        return jsonify({"error": "Seuls les scripts .sh et .py peuvent être exécutés ici"}), 400
    if not is_exec:
        return jsonify({"error": "Script non exécutable : appliquez CHMOD 755 ou chmod +x avant de le lancer"}), 400

    cmd = get_script_command(path, args)
    if not cmd:
        return jsonify({"error": "Commande de script invalide"}), 400

    command_display = shlex.join(cmd)
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "path": path,
        "args": args,
        "cmd": cmd,
        "command": command_display,
        "pid": None,
        "running": True,
        "status": "running",
        "returncode": None,
        "log": "",
        "started_at": time.time(),
        "finished_at": None,
        "process": None,
    }

    with EXEC_JOBS_LOCK:
        EXEC_JOBS[job_id] = job

    thread = threading.Thread(target=run_exec_job, args=(job_id,), daemon=True)
    thread.start()

    return jsonify({"status": "ok", "job_id": job_id, "command": command_display})


@file_bp.route('/api/files/execute/status', methods=['POST'])
def execute_file_status():
    data = get_json_payload()
    job_id = str(data.get('job_id') or '').strip()

    if not job_id:
        return jsonify({"error": "Job manquant"}), 400

    with EXEC_JOBS_LOCK:
        job = EXEC_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Job introuvable"}), 404
        payload = public_exec_job(job)

    return jsonify(payload)


@file_bp.route('/api/files/execute/stop', methods=['POST'])
def execute_file_stop():
    data = get_json_payload()
    job_id = str(data.get('job_id') or '').strip()

    if not job_id:
        return jsonify({"error": "Job manquant"}), 400

    with EXEC_JOBS_LOCK:
        job = EXEC_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Job introuvable"}), 404
        proc = job.get('process')

    if not proc or proc.poll() is not None:
        return jsonify({"status": "ok", "message": "Le script est déjà terminé"})

    try:
        proc.terminate()
        append_exec_log(job_id, "\n--- arrêt demandé depuis l'interface ---\n")
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@file_bp.route('/api/files/upload', methods=['POST'])
def upload_file():
    dest_dir = normalize_path(request.form.get('path'), '/')
    overwrite = truthy(request.form.get('overwrite', False))
    uploaded = request.files.get('file')

    if not dest_dir or not os.path.isdir(dest_dir):
        return jsonify({"error": "Dossier de destination invalide"}), 400
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "Aucun fichier reçu"}), 400

    # Les navigateurs peuvent envoyer un chemin complet dans filename : on ne garde
    # que le nom final, puis on refuse tout nom dangereux.
    filename = os.path.basename(str(uploaded.filename).replace('\\', '/')).strip()
    filename = filename.replace('\x00', '')
    if not is_safe_name(filename):
        return jsonify({"error": "Nom de fichier invalide"}), 400

    target = os.path.abspath(os.path.join(dest_dir, filename))
    expected_prefix = os.path.abspath(dest_dir).rstrip(os.sep) + os.sep
    if target != os.path.abspath(dest_dir) and not target.startswith(expected_prefix):
        return jsonify({"error": "Chemin de destination refusé"}), 400
    if os.path.isdir(target):
        return jsonify({"error": "Un dossier porte déjà ce nom"}), 409
    if os.path.exists(target) and not overwrite:
        return jsonify({"error": "Fichier déjà existant"}), 409

    try:
        uploaded.save(target)
        return jsonify({"status": "ok", "path": target, "name": filename})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@file_bp.route('/api/files/download', methods=['GET'])
def download_file():
    path = normalize_path(request.args.get('path'))

    if not path:
        return jsonify({"error": "Chemin invalide"}), 400
    if not os.path.exists(path):
        return jsonify({"error": "Fichier introuvable"}), 404
    if not os.path.isfile(path):
        return jsonify({"error": "Export possible uniquement sur un fichier"}), 400

    try:
        return send_file(path, as_attachment=True, download_name=os.path.basename(path))
    except TypeError:
        # Compatibilité vieux Flask : attachment_filename a été remplacé par download_name.
        return send_file(path, as_attachment=True, attachment_filename=os.path.basename(path))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@file_bp.route('/api/files/action', methods=['POST'])
def file_action():
    data = get_json_payload()
    act = data.get('action')
    raw_items = data.get('items', [])
    items = [p for p in (normalize_path(i) for i in raw_items if i is not None) if p]
    dest = normalize_path(data.get('dest'))

    try:
        if act == 'delete':
            if not items:
                return jsonify({"error": "Aucun élément à supprimer"}), 400
            for i in items:
                if not os.path.exists(i):
                    continue
                if os.path.isdir(i):
                    shutil.rmtree(i)
                else:
                    os.remove(i)

        elif act == 'copy':
            if not items or not dest:
                return jsonify({"error": "Copie incomplète"}), 400
            if not os.path.isdir(dest):
                return jsonify({"error": "Destination invalide"}), 400
            for i in items:
                if not os.path.exists(i):
                    return jsonify({"error": f"Introuvable: {i}"}), 404
                target = os.path.join(dest, os.path.basename(i))
                if os.path.isdir(i):
                    shutil.copytree(i, target)
                else:
                    shutil.copy2(i, target)

        elif act == 'move':
            if not items or not dest:
                return jsonify({"error": "Déplacement incomplet"}), 400
            for i in items:
                if not os.path.exists(i):
                    return jsonify({"error": f"Introuvable: {i}"}), 404
                shutil.move(i, dest)

        elif act == 'mkdir':
            base_path = normalize_path(data.get('path'))
            name = data.get('name')
            if not base_path or not os.path.isdir(base_path):
                return jsonify({"error": "Dossier parent invalide"}), 400
            if not is_safe_name(name):
                return jsonify({"error": "Nom de dossier invalide"}), 400
            os.makedirs(os.path.join(base_path, name), exist_ok=True)

        elif act == 'chmod':
            if not items:
                return jsonify({"error": "Aucun élément sélectionné"}), 400
            mode_raw = str(data.get('mode', '')).strip()
            if len(mode_raw) != 3 or any(ch not in '01234567' for ch in mode_raw):
                return jsonify({"error": "Mode chmod invalide"}), 400
            mode = int(mode_raw, 8)
            recursive = truthy(data.get('recursive', False))
            changed = 0
            for i in items:
                targets = iter_path_recursive(i) if recursive else [i]
                for target in targets:
                    chmod_path(target, mode)
                    changed += 1
            return jsonify({"status": "ok", "changed": changed})

        elif act == 'chown':
            if not items:
                return jsonify({"error": "Aucun élément sélectionné"}), 400
            uid_raw = data.get('uid')
            gid_raw = data.get('gid')
            if uid_raw in (None, '') or gid_raw in (None, ''):
                return jsonify({"error": "UID ou GID manquant"}), 400
            uid, gid = int(uid_raw), int(gid_raw)
            recursive = truthy(data.get('recursive', False))
            changed = 0
            for i in items:
                targets = iter_path_recursive(i) if recursive else [i]
                for target in targets:
                    chown_path(target, uid, gid)
                    changed += 1
            return jsonify({"status": "ok", "changed": changed})

        else:
            return jsonify({"error": "Action inconnue"}), 400

        return jsonify({"status": "ok"})
    except ValueError:
        return jsonify({"error": "Valeur invalide"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
