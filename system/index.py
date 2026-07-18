import os
import re
import shlex
import shutil
import subprocess
from typing import Optional

from flask import Blueprint, render_template, request, redirect, url_for, jsonify
from urllib.parse import unquote

index_bp = Blueprint('index_bp', __name__)


# ==========================================================
# 🔄 OUTIL DEV TEMPORAIRE : redémarrage rapide Flask
# ==========================================================
# Bouton branché depuis le nouveau menu latéral pendant la refonte.
# On le garde dans index.py pour qu'il reste disponible dès la page d'accueil,
# sans ajouter de logique de dev dans app.py.
def _find_first_existing_script(candidates) -> str:
    seen = set()
    for candidate in candidates:
        candidate = os.path.abspath(os.path.expanduser(os.path.expandvars(candidate)))
        if candidate in seen:
            continue
        seen.add(candidate)
        if os.path.isfile(candidate):
            return candidate
    return ""


def _find_dev_system_sh() -> str:
    return _find_first_existing_script([
        os.path.join(MODULE_DIR, "system.sh"),
        os.path.join(os.getcwd(), "system.sh"),
        os.path.join(os.path.dirname(MODULE_DIR), "system.sh"),
        os.path.join(NAS_ROOT_DIR, "system", "system.sh"),
        os.path.join(NAS_ROOT_DIR, "system.sh"),
    ])


def _find_dev_system_unraid_sh() -> str:
    return _find_first_existing_script([
        os.path.join(MODULE_DIR, "system_unraid.sh"),
        os.path.join(os.getcwd(), "system_unraid.sh"),
        "/mnt/user/dockers/system/system_unraid.sh",
        os.path.join(os.path.dirname(MODULE_DIR), "system_unraid.sh"),
        os.path.join(NAS_ROOT_DIR, "system", "system_unraid.sh"),
        os.path.join(NAS_ROOT_DIR, "system_unraid.sh"),
    ])


def _open_dev_action_log(log_name: str):
    preferred_dir = "/var/log/yoleo"
    fallback_dir = "/tmp"
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", log_name).strip("-._") or "yoleo-dev-action.log"
    try:
        os.makedirs(preferred_dir, exist_ok=True)
        return open(os.path.join(preferred_dir, safe_name), "ab", buffering=0)
    except Exception:
        return open(os.path.join(fallback_dir, safe_name), "ab", buffering=0)


def _spawn_dev_shell_command(command: str, log_name: str):
    log_handle = _open_dev_action_log(log_name)
    log_path = getattr(log_handle, "name", "")
    try:
        subprocess.Popen(
            ["bash", "-lc", command],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            close_fds=True,
            start_new_session=True,
        )
    finally:
        try:
            log_handle.close()
        except Exception:
            pass
    return log_path


@index_bp.route('/index/dev/availability', methods=['GET'])
def index_dev_availability():
    """Sonde légère utilisée par la popup de redémarrage côté navigateur.

    Ce n'est pas un ping réseau et cette route ne redirige jamais le navigateur.
    Le JavaScript l'appelle en arrière-plan pour savoir quand l'interface est de
    nouveau prête après un redémarrage du serveur.
    """
    response = jsonify({"ok": True, "service": "flask-system"})
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return response


@index_bp.route('/index/dev/restart-flask', methods=['POST'])
def index_dev_restart_flask():
    script_path = _find_dev_system_sh()
    if not script_path:
        return jsonify({
            "ok": False,
            "message": "system.sh introuvable dans le dossier Flask ou les dossiers proches.",
            "command": "system.sh -restart",
        }), 404

    script_dir = os.path.dirname(script_path)
    command = f"sleep 1; cd {shlex.quote(script_dir)} && bash {shlex.quote(script_path)} -restart"

    try:
        log_path = _spawn_dev_shell_command(command, "restart-flask.log")
    except Exception as exc:
        return jsonify({
            "ok": False,
            "message": f"Impossible de lancer system.sh -restart : {exc}",
            "command": "system.sh -restart",
        }), 500

    return jsonify({
        "ok": True,
        "message": "Redémarrage Flask lancé : system.sh -restart",
        "command": "system.sh -restart",
        "script": script_path,
        "log": log_path,
    })


@index_bp.route('/index/dev/restart-flask-unraid', methods=['POST'])
def index_dev_restart_flask_unraid():
    script_path = _find_dev_system_unraid_sh()
    if not script_path:
        return jsonify({
            "ok": False,
            "message": "system_unraid.sh introuvable dans /mnt/user/dockers/system ou les dossiers proches.",
            "command": "system_unraid.sh -restart",
        }), 404

    script_dir = os.path.dirname(script_path)
    command = f"sleep 1; cd {shlex.quote(script_dir)} && bash {shlex.quote(script_path)} -restart"

    try:
        log_path = _spawn_dev_shell_command(command, "restart-flask-unraid.log")
    except Exception as exc:
        return jsonify({
            "ok": False,
            "message": f"Impossible de lancer system_unraid.sh -restart : {exc}",
            "command": "system_unraid.sh -restart",
        }), 500

    return jsonify({
        "ok": True,
        "message": "Redémarrage Flask Unraid lancé : system_unraid.sh -restart",
        "command": "system_unraid.sh -restart",
        "script": script_path,
        "log": log_path,
    })


@index_bp.route('/index/dev/poweroff-server', methods=['POST'])
def index_dev_poweroff_server():
    command = "sleep 1; systemctl poweroff || shutdown -h now"
    try:
        log_path = _spawn_dev_shell_command(command, "server-poweroff.log")
    except Exception as exc:
        return jsonify({
            "ok": False,
            "message": f"Impossible de demander l'arrêt serveur : {exc}",
            "command": "systemctl poweroff",
        }), 500

    return jsonify({
        "ok": True,
        "message": "Arrêt serveur demandé.",
        "command": "systemctl poweroff",
        "log": log_path,
    })


@index_bp.route('/index/dev/reboot-server', methods=['POST'])
def index_dev_reboot_server():
    command = "sleep 1; systemctl reboot || shutdown -r now"
    try:
        log_path = _spawn_dev_shell_command(command, "server-reboot.log")
    except Exception as exc:
        return jsonify({
            "ok": False,
            "message": f"Impossible de demander le redémarrage serveur : {exc}",
            "command": "systemctl reboot",
        }), 500

    return jsonify({
        "ok": True,
        "message": "Redémarrage serveur demandé.",
        "command": "systemctl reboot",
        "log": log_path,
    })


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


# ==========================================================
# 🧭 CONF MENU / SIDEBAR
# ==========================================================
# Fichier officiel : ../conf/menu_ui.conf depuis le dossier Python.
# Sert à mémoriser la largeur de la barre latérale entre deux redémarrages.
# Important : ../conf/menu.conf est réservé au menu CLI scripts/menu.sh.
MENU_CONFIG_DEFAULTS = {
    "SIDEBAR_WIDTH": "252",
    "SIDEBAR_COMPACT": "70",
}
MENU_WIDTH_MIN = 210
MENU_WIDTH_MAX = 380


def _menu_config_path() -> str:
    return nas_conf_file("menu_ui.conf")


def _read_simple_conf(path: str, defaults: dict) -> dict:
    data = dict(defaults)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip().upper()
                if key in defaults:
                    data[key] = value.strip()
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return data


def _safe_int(value, default: int, min_value: int, max_value: int) -> int:
    try:
        number = int(str(value).strip())
    except Exception:
        number = default
    return max(min_value, min(max_value, number))


def read_menu_conf() -> dict:
    raw = _read_simple_conf(_menu_config_path(), MENU_CONFIG_DEFAULTS)
    return {
        "sidebar_width": _safe_int(raw.get("SIDEBAR_WIDTH"), 252, MENU_WIDTH_MIN, MENU_WIDTH_MAX),
        "sidebar_compact": _safe_int(raw.get("SIDEBAR_COMPACT"), 70, 56, 110),
    }


def write_menu_conf(sidebar_width: int, sidebar_compact: Optional[int] = None) -> str:
    current = read_menu_conf()
    width = _safe_int(sidebar_width, current["sidebar_width"], MENU_WIDTH_MIN, MENU_WIDTH_MAX)
    compact = _safe_int(sidebar_compact if sidebar_compact is not None else current["sidebar_compact"], current["sidebar_compact"], 56, 110)
    path = _menu_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Configuration du menu Yoleo\n")
        fh.write("# Largeurs en pixels. Modifiable aussi depuis la barre latérale.\n")
        fh.write(f"SIDEBAR_WIDTH={width}\n")
        fh.write(f"SIDEBAR_COMPACT={compact}\n")
    return path


@index_bp.route('/index/api/menu-conf', methods=['GET'])
def index_api_menu_conf_get():
    conf = read_menu_conf()
    return jsonify({
        "ok": True,
        "sidebar_width": conf["sidebar_width"],
        "sidebar_compact": conf["sidebar_compact"],
        "path": _menu_config_path(),
    })


@index_bp.route('/index/api/menu-conf', methods=['POST'])
def index_api_menu_conf_save():
    payload = request.get_json(silent=True) or {}
    width = payload.get("sidebar_width", payload.get("width", MENU_CONFIG_DEFAULTS["SIDEBAR_WIDTH"]))
    compact = payload.get("sidebar_compact")
    try:
        path = write_menu_conf(width, compact)
    except Exception as exc:
        return jsonify({
            "ok": False,
            "message": f"Impossible d'enregistrer menu.conf : {exc}",
        }), 500
    conf = read_menu_conf()
    return jsonify({
        "ok": True,
        "message": "Largeur du menu enregistrée.",
        "sidebar_width": conf["sidebar_width"],
        "sidebar_compact": conf["sidebar_compact"],
        "path": path,
    })


MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
SYSTEM_CONFIG_ENV = 'SYSTEM_CONF'
PRIMARY_CONFIG_PATH = nas_conf_file('system.conf')
FALLBACK_CONFIG_PATH = nas_conf_file('system.conf')
PERSONALIZATION_BLOCK_START = '# BEGIN_SYSTEM_PERSONALIZATION_MENU'
PERSONALIZATION_BLOCK_END = '# END_SYSTEM_PERSONALIZATION_MENU'
CONFIG_KEY_ORDER = ['base_dir', 'titre_tab', 'titre_h1', 'titre_logo', 'logo_emoji', 'nav_icons']
CONFIG_KEY_NAMES = {
    'base_dir': 'BASE_DIR',
    'titre_tab': 'titre_tab',
    'titre_h1': 'titre_h1',
    'titre_logo': 'titre_logo',
    'logo_emoji': 'logo_emoji',
    'nav_icons': 'nav_icons',
}

DEFAULT_CONFIG = {
    'base_dir': '../tabs',
    'titre_tab': 'System Manager',
    'titre_h1': 'Personnalisation Tabs',
    'titre_logo': 'System Manager',
    'logo_emoji': '../static/logo.png',
    'nav_icons': '../static/logo.png',
}

# Configuration de la vraie page d'accueil NAS.
# Fichier officiel : ../conf/index.conf depuis /dockers/system/index.py.
HOME_CONFIG_DEFAULTS = {
    "SHOW_TIME": "1",
    "SHOW_CPU": "1",
    "SHOW_RAM": "1",
    "SHOW_DOCKER_TOTAL": "0",
    "SHOW_DOCKER_RUNNING": "0",
    "SHOW_BUILD": "1",
    "SHOW_STORAGE": "0",
    "SHOW_SERVICES": "0",
    "SHOW_UPTIME": "0",
    "SHOW_NVIDIA_LOCAL": "0",
    "SHOW_NVIDIA_SSH": "0",
    "SHOW_INTEL_GPU": "0",
    "SHOW_NETWORK": "1",
    "SHOW_HOST": "1",
    "SHOW_LOCAL_RESOLUTION": "0",
    "SHOW_DISK_MOUNTS": "0",
    "SHOW_FANS": "0",
}

HOME_CONFIG_LABELS = {
    "SHOW_TIME": "Heure",
    "SHOW_CPU": "Processeur",
    "SHOW_RAM": "Mémoire RAM",
    "SHOW_DOCKER_TOTAL": "Docker",
    "SHOW_DOCKER_RUNNING": "VM",
    "SHOW_BUILD": "Build",
    "SHOW_STORAGE": "Stockage",
    "SHOW_SERVICES": "Services",
    "SHOW_UPTIME": "Uptime",
    "SHOW_NVIDIA_LOCAL": "NVIDIA GPU (local)",
    "SHOW_NVIDIA_SSH": "NVIDIA GPU (SSH)",
    "SHOW_INTEL_GPU": "Intel GPU",
    "SHOW_NETWORK": "Résolution locale / réseau",
    "SHOW_HOST": "Hôte",
    "SHOW_LOCAL_RESOLUTION": "Résolution locale",
    "SHOW_DISK_MOUNTS": "Vérifier montages disque",
    "SHOW_FANS": "Ventilateurs",
}


EDITABLE_CONFIG_KEYS = {'titre_tab', 'titre_h1', 'titre_logo', 'logo_emoji', 'nav_icons'}


def _normalize_config_key(key: str) -> str:
    return key.strip().lower()


def _unique_paths(paths):
    seen = set()
    out = []
    for raw_path in paths:
        if not raw_path:
            continue
        path = os.path.abspath(os.path.expanduser(os.path.expandvars(str(raw_path))))
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def _project_root_candidates():
    """Racines possibles du projet Flask.

    Cas normal de ton installation :
      /dockers/system/index.py  -> racine projet /dockers
      /dockers/system/system.py -> racine projet /dockers
      /dockers/conf/system.conf -> conf officielle
      /dockers/tabs/Menu       -> onglets du haut

    On met donc le parent de /system AVANT /system lui-même, sinon Python peut
    créer /dockers/system/conf/system.conf ou /dockers/system/tabs par erreur.
    """
    roots = []

    def add(path):
        if not path:
            return
        path = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path))))
        if path not in roots:
            roots.append(path)

    for env_name in ('FLASK_SYSTEM_ROOT', 'SYSTEM_ROOT', 'PROJECT_ROOT'):
        add(os.environ.get(env_name, '').strip())

    for base in (MODULE_DIR, os.getcwd()):
        base = os.path.abspath(base)
        parent = os.path.dirname(base)
        grandparent = os.path.dirname(parent)
        if os.path.basename(base).lower() in {'system', 'modules', 'app'}:
            add(parent)
            add(base)
        else:
            add(base)
            add(parent)
        add(grandparent)

    return _unique_paths(roots)


def _config_candidates():
    candidates = []
    env_path = os.environ.get(SYSTEM_CONFIG_ENV, '').strip()
    if env_path:
        candidates.append(env_path)

    roots = _project_root_candidates()

    # Priorité absolue : conf/system.conf à la racine projet.
    # Si MODULE_DIR=/dockers/system, le premier candidat devient /dockers/conf/system.conf.
    for root in roots:
        candidates.append(os.path.join(root, 'conf', 'system.conf'))

    # Fallbacks seulement après, pour compatibilité.
    for root in roots:
        candidates.append(os.path.join(root, 'system.conf'))

    # Très vieux chemins relatifs, gardés en dernier pour ne plus piéger gunicorn
    # quand il démarre depuis /dockers/system.
    candidates.extend([
        PRIMARY_CONFIG_PATH,
        FALLBACK_CONFIG_PATH,
        nas_conf_file('system.conf'),
        nas_conf_file('system.conf'),
        'system.conf',
    ])
    return _unique_paths(candidates)


def _is_bad_system_subconf(path: str) -> bool:
    """Détecte /.../system/conf/system.conf quand /.../conf/system.conf existe."""
    path = os.path.abspath(path)
    conf_dir = os.path.dirname(path)
    system_dir = os.path.dirname(conf_dir)
    project_dir = os.path.dirname(system_dir)
    if os.path.basename(system_dir).lower() != 'system':
        return False
    official = os.path.join(project_dir, 'conf', 'system.conf')
    return os.path.exists(official) and os.path.abspath(official) != path


def _get_config_path() -> str:
    env_path = os.environ.get(SYSTEM_CONFIG_ENV, '').strip()
    if env_path and os.path.exists(env_path):
        return os.path.abspath(env_path)

    for candidate in _config_candidates():
        if not os.path.exists(candidate):
            continue
        if _is_bad_system_subconf(candidate):
            continue
        return os.path.abspath(candidate)

    # Si le fichier n'existe pas encore, on crée/complète celui de la racine projet,
    # jamais index.conf et jamais /system/conf/system.conf.
    root = _project_root_candidates()[0] if _project_root_candidates() else MODULE_DIR
    return os.path.abspath(os.path.join(root, 'conf', 'system.conf'))



def _home_config_normalize_key(key: str) -> str:
    return str(key or '').strip().upper()


def _home_config_bool(value) -> bool:
    return str(value if value is not None else '').strip().lower() in {'1', 'true', 'yes', 'oui', 'on', 'checked'}


def get_home_config_path() -> str:
    env_path = os.environ.get('INDEX_CONF', '').strip()
    if env_path:
        return os.path.abspath(os.path.expanduser(os.path.expandvars(env_path)))

    roots = _project_root_candidates()
    candidates = []
    for root in roots:
        candidates.append(os.path.join(root, 'conf', 'index.conf'))
    candidates.extend([
        nas_conf_file('index.conf'),
        nas_conf_file('index.conf'),
        'index.conf',
    ])

    for candidate in _unique_paths(candidates):
        if os.path.exists(candidate):
            return os.path.abspath(candidate)

    root = roots[0] if roots else MODULE_DIR
    return os.path.abspath(os.path.join(root, 'conf', 'index.conf'))


def read_home_config(path: str = '') -> dict:
    path = path or get_home_config_path()
    data = {}
    if not path or not os.path.exists(path):
        return data

    with open(path, 'r', encoding='utf-8', errors='replace') as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith('#') or line.startswith(';') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = _home_config_normalize_key(key)
            if key in HOME_CONFIG_DEFAULTS:
                data[key] = '1' if _home_config_bool(value) else '0'
    return data


def write_home_config(updates: dict) -> str:
    path = get_home_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)

    merged = HOME_CONFIG_DEFAULTS.copy()
    if os.path.exists(path):
        merged.update(read_home_config(path))

    for key, value in (updates or {}).items():
        key = _home_config_normalize_key(key)
        if key in HOME_CONFIG_DEFAULTS:
            merged[key] = '1' if _home_config_bool(value) else '0'

    lines = [
        '# ============================================================\n',
        "# Page d'accueil NAS - index.py / index.html\n",
        '# 1 = affiche le bloc ; 0 = masque le bloc.\n',
        '# Les futurs déplacements de blocs pourront réutiliser ce fichier.\n',
        '# ============================================================\n',
        '\n',
    ]
    for key in HOME_CONFIG_DEFAULTS:
        label = HOME_CONFIG_LABELS.get(key, key)
        lines.append(f'# {label}\n')
        lines.append(f'{key}={merged[key]}\n')

    with open(path, 'w', encoding='utf-8') as handle:
        handle.writelines(lines)
    return path


def load_home_config() -> dict:
    path = get_home_config_path()
    merged = HOME_CONFIG_DEFAULTS.copy()

    if os.path.exists(path):
        merged.update(read_home_config(path))
    else:
        write_home_config(merged)

    out = {key.lower(): '1' if _home_config_bool(value) else '0' for key, value in merged.items()}
    out['_config_path'] = path
    return out


# ==========================================================
# 🧩 Ordre des blocs de la page d'accueil
# ==========================================================
# Fichier officiel : ../conf/index_top.conf.
# index.conf garde les cases afficher/masquer ; index_top.conf garde uniquement
# l'ordre visuel des étiquettes/blocs de l'accueil.
def get_index_top_config_path() -> str:
    env_path = os.environ.get('INDEX_TOP_CONF', '').strip()
    if env_path:
        return os.path.abspath(os.path.expanduser(os.path.expandvars(env_path)))

    roots = _project_root_candidates()
    candidates = []
    for root in roots:
        candidates.append(os.path.join(root, 'conf', 'index_top.conf'))
    candidates.extend([
        nas_conf_file('index_top.conf'),
        'index_top.conf',
    ])

    for candidate in _unique_paths(candidates):
        if os.path.exists(candidate):
            return os.path.abspath(candidate)

    root = roots[0] if roots else MODULE_DIR
    return os.path.abspath(os.path.join(root, 'conf', 'index_top.conf'))


def _index_top_split_order(value: str) -> list:
    raw = str(value or '').replace(';', ',').replace('|', ',').replace('\n', ',')
    return [part.strip() for part in raw.split(',') if part.strip()]


def normalize_index_top_order(order) -> list:
    known = list(HOME_CONFIG_DEFAULTS.keys())
    out = []
    for raw in order or []:
        key = _home_config_normalize_key(raw)
        if key in known and key not in out:
            out.append(key)
    for key in known:
        if key not in out:
            out.append(key)
    return out


def read_index_top_order(path: str = '') -> list:
    path = path or get_index_top_config_path()
    if not path or not os.path.exists(path):
        return []

    values = []
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith('#') or line.startswith(';'):
                    continue
                if '=' in line:
                    key, value = line.split('=', 1)
                    if key.strip().upper() in {'ORDER', 'INDEX_ORDER', 'INDEX_TOP_ORDER'}:
                        values.extend(_index_top_split_order(value))
                else:
                    values.extend(_index_top_split_order(line))
    except Exception:
        return []
    return normalize_index_top_order(values)


def write_index_top_order(order) -> str:
    path = get_index_top_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    normalized = normalize_index_top_order(order)
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write('# ============================================================\n')
        handle.write("# Ordre des étiquettes/blocs de la page d'accueil Yoleo\n")
        handle.write('# index.conf garde afficher/masquer ; ce fichier garde seulement l’ordre.\n')
        handle.write('# ============================================================\n')
        handle.write('ORDER=' + ','.join(normalized) + '\n')
    return path


def load_index_top_order() -> list:
    path = get_index_top_config_path()
    if os.path.exists(path):
        return read_index_top_order(path)
    write_index_top_order(list(HOME_CONFIG_DEFAULTS.keys()))
    return normalize_index_top_order(list(HOME_CONFIG_DEFAULTS.keys()))


def load_index_top_items() -> list:
    return [
        {
            'key': key,
            'key_lower': key.lower(),
            'label': HOME_CONFIG_LABELS.get(key, key),
        }
        for key in load_index_top_order()
    ]


def _line_config_key(line: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith('#') or '=' not in stripped:
        return ''
    key, _value = stripped.split('=', 1)
    return _normalize_config_key(key)


def _select_personalization_lines(lines: list) -> list:
    """
    Le menu du haut ne doit lire que son bloc en bas de system.conf.
    Compatibilité : si le bloc n'existe pas encore, on accepte l'ancien format plat,
    mais uniquement pour les clés attendues.
    """
    start_idx = None
    end_idx = None

    for idx, raw_line in enumerate(lines):
        if raw_line.strip() == PERSONALIZATION_BLOCK_START:
            start_idx = idx
        elif raw_line.strip() == PERSONALIZATION_BLOCK_END and start_idx is not None:
            end_idx = idx
            break

    if start_idx is not None and end_idx is not None and end_idx > start_idx:
        return lines[start_idx + 1:end_idx]

    return lines


def _read_key_value_file(path: str) -> dict:
    data = {}
    if not path or not os.path.exists(path):
        return data

    with open(path, 'r', encoding='utf-8') as handle:
        lines = _select_personalization_lines(handle.readlines())

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        normalized_key = _normalize_config_key(key)
        if normalized_key in DEFAULT_CONFIG:
            data[normalized_key] = value.strip()
    return data


def _format_personalization_block(config: dict) -> list:
    lines = [
        '',
        '# ============================================================',
        '# Personnalisation du menu haut / onglets',
        '# Ancien index.conf fusionné ici.',
        '# Ce bloc seul alimente le menu global et l\'onglet Système > Personnalisation.',
        '# Le futur index.conf pourra donc servir à la vraie page d\'accueil du NAS.',
        '# ============================================================',
        PERSONALIZATION_BLOCK_START,
    ]

    for key in CONFIG_KEY_ORDER:
        output_key = CONFIG_KEY_NAMES.get(key, key)
        value = str(config.get(key, DEFAULT_CONFIG.get(key, ''))).strip()
        lines.append(f'{output_key}={value}')

    lines.append(PERSONALIZATION_BLOCK_END)
    return lines


def _write_personalization_block(path: str, config: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)

    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as handle:
            lines = handle.read().splitlines()
    else:
        lines = []

    block = _format_personalization_block(config)

    start_idx = None
    end_idx = None
    for idx, raw_line in enumerate(lines):
        if raw_line.strip() == PERSONALIZATION_BLOCK_START:
            start_idx = idx
        elif raw_line.strip() == PERSONALIZATION_BLOCK_END and start_idx is not None:
            end_idx = idx
            break

    if start_idx is not None and end_idx is not None and end_idx >= start_idx:
        new_lines = lines[:start_idx]
        # On remplace aussi les commentaires juste au-dessus si c'est l'ancien bloc généré.
        while new_lines and (
            new_lines[-1].strip() == ''
            or new_lines[-1].strip().startswith('# Personnalisation du menu haut')
            or new_lines[-1].strip().startswith('# Ancien index.conf')
            or new_lines[-1].strip().startswith('# Ce bloc seul')
            or new_lines[-1].strip().startswith('# Le futur index.conf')
            or new_lines[-1].strip() == '# ============================================================'
        ):
            new_lines.pop()
        new_lines.extend(block)
        new_lines.extend(lines[end_idx + 1:])
    else:
        # Premier passage : on retire les anciennes clés plates du bloc non marqué
        # pour éviter les doublons, puis on ajoute le bloc propre en bas.
        filtered = []
        for raw_line in lines:
            key = _line_config_key(raw_line)
            if key in DEFAULT_CONFIG:
                continue
            filtered.append(raw_line)
        new_lines = filtered
        if new_lines and new_lines[-1].strip():
            new_lines.append('')
        new_lines.extend(block)

    with open(path, 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(new_lines).rstrip() + '\n')


def load_module_config() -> dict:
    config_path = _get_config_path()
    file_config = _read_key_value_file(config_path)

    merged = DEFAULT_CONFIG.copy()
    merged.update({k: v for k, v in file_config.items() if k in DEFAULT_CONFIG})
    merged['_config_path'] = config_path
    merged['_config_dir'] = os.path.dirname(os.path.abspath(config_path))
    return merged


def save_module_config(config: dict) -> None:
    config_path = _get_config_path()

    existing = _read_key_value_file(config_path)
    merged = DEFAULT_CONFIG.copy()
    merged.update(existing)

    for key in DEFAULT_CONFIG:
        if key in config:
            merged[key] = str(config[key]).strip()

    _write_personalization_block(config_path, merged)


def _resolve_base_dir_path(base_dir: str, config_dir: str = '', create: bool = False) -> str:
    """Résout BASE_DIR sans créer de faux dossier dans /dockers/system.

    Règles :
      - BASE_DIR=../tabs dans /dockers/conf/system.conf => /dockers/tabs
      - BASE_DIR=tabs accepte aussi /dockers/tabs
      - on choisit d'abord le candidat qui contient Menu/
    """
    base_dir = (base_dir or '').strip() or DEFAULT_CONFIG['base_dir']
    if os.path.isabs(base_dir):
        resolved = os.path.abspath(base_dir)
        if create:
            os.makedirs(resolved, exist_ok=True)
        return resolved

    candidates = []
    config_dir = os.path.abspath(config_dir or os.path.dirname(_get_config_path()))
    project_root = os.path.dirname(config_dir) if os.path.basename(config_dir).lower() == 'conf' else (_project_root_candidates()[0] if _project_root_candidates() else MODULE_DIR)

    # Chemin relatif au fichier system.conf : c'est la règle officielle.
    candidates.append(os.path.join(config_dir, base_dir))

    # Chemin relatif à la racine projet : pratique si l'utilisateur met BASE_DIR=tabs.
    candidates.append(os.path.join(project_root, base_dir))

    # Si base_dir finit par tabs, on teste explicitement /racine/tabs.
    if os.path.basename(base_dir.rstrip('/\\')).lower() in {'tab', 'tabs'}:
        candidates.append(os.path.join(project_root, os.path.basename(base_dir.rstrip('/\\'))))
        candidates.append(os.path.join(project_root, 'tabs'))

    # Fallbacks anciens, mais sans priorité.
    for root in _project_root_candidates():
        candidates.append(os.path.join(root, base_dir))
    candidates.extend([
        os.path.join(MODULE_DIR, base_dir),
        os.path.join(os.getcwd(), base_dir),
    ])
    candidates = _unique_paths(candidates)

    for candidate in candidates:
        if os.path.isdir(os.path.join(candidate, 'Menu')):
            return os.path.abspath(candidate)
    for candidate in candidates:
        if os.path.isdir(candidate) and not os.path.abspath(candidate).replace('\\', '/').endswith('/system/tabs'):
            return os.path.abspath(candidate)

    resolved = os.path.abspath(candidates[0] if candidates else base_dir)
    if create:
        os.makedirs(resolved, exist_ok=True)
    return resolved


def get_base_dir(create: bool = False) -> str:
    config = load_module_config()
    base_dir = config.get('base_dir', DEFAULT_CONFIG['base_dir']).strip()
    return _resolve_base_dir_path(base_dir, config.get('_config_dir') or os.path.dirname(_get_config_path()), create=create)


def sanitize_conf_value(value: str, default: str = '') -> str:
    cleaned = (value or '').replace('\r', ' ').replace('\n', ' ').strip()
    return cleaned or default



def sanitize_segment(value: str, default: str) -> str:
    cleaned = sanitize_conf_value(value, default)
    cleaned = cleaned.replace('/', '-').replace('\\', '-')
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' .')
    if cleaned in {'', '.', '..'}:
        return default
    return cleaned[:120]



def safe_join(base_dir: str, *parts: str) -> str:
    base_dir = os.path.abspath(base_dir)
    candidate = os.path.abspath(os.path.join(base_dir, *parts))
    if os.path.commonpath([base_dir, candidate]) != base_dir:
        raise ValueError('Invalid path')
    return candidate



def read_service_file(filepath: str, base_dir: str) -> dict:
    data = {
        'id': '',
        'nom': 'Inconnu',
        'url': '#',
        'icone': '❓',
        'categorie': 'Autre',
    }

    if not os.path.isfile(filepath):
        return data

    data['categorie'] = os.path.basename(os.path.dirname(filepath))
    data['id'] = os.path.relpath(filepath, base_dir)

    with open(filepath, 'r', encoding='utf-8') as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip().lower()
            value = value.strip()

            if key in {'name', 'nom'}:
                data['nom'] = value
            elif key in {'icones', 'icone', 'icon', 'src', 'logo'}:
                data['icone'] = value
            elif key in {'url', 'lien'}:
                data['url'] = value

    return data



def write_service_file(category: str, name: str, url: str, icon: str, *, previous_rel_path: Optional[str] = None) -> str:
    base_dir = get_base_dir(create=True)
    safe_category = sanitize_segment(category, 'Autre')
    safe_name = sanitize_segment(name, 'Service')
    filename = safe_name if safe_name.endswith('.conf') else f'{safe_name}.conf'

    folder = safe_join(base_dir, safe_category)
    os.makedirs(folder, exist_ok=True)
    filepath = safe_join(folder, filename)

    if previous_rel_path:
        old_path = safe_join(base_dir, previous_rel_path)
        if os.path.isfile(old_path) and os.path.abspath(old_path) != os.path.abspath(filepath):
            os.remove(old_path)
            old_folder = os.path.dirname(old_path)
            if os.path.isdir(old_folder) and not os.listdir(old_folder):
                os.rmdir(old_folder)

    with open(filepath, 'w', encoding='utf-8') as handle:
        handle.write(
            f"name={sanitize_conf_value(name, 'Service')}\n"
            f"icone={sanitize_conf_value(icon, '🌐')}\n"
            f"url={sanitize_conf_value(url, '#')}\n"
        )

    return filepath



def get_config() -> dict:
    config = load_module_config()
    return {
        'base_dir': config.get('base_dir', DEFAULT_CONFIG['base_dir']),
        'titre_tab': config.get('titre_tab', DEFAULT_CONFIG['titre_tab']),
        'titre_h1': config.get('titre_h1', DEFAULT_CONFIG['titre_h1']),
        'titre_logo': config.get('titre_logo', DEFAULT_CONFIG['titre_logo']),
        'logo_emoji': config.get('logo_emoji', DEFAULT_CONFIG['logo_emoji']),
        'nav_icons': config.get('nav_icons', DEFAULT_CONFIG['nav_icons']),
    }



def build_menu_items() -> list:
    menu_items = []
    base_dir = get_base_dir()
    menu_path = safe_join(base_dir, 'Menu')

    if os.path.isdir(menu_path):
        for filename in sorted(os.listdir(menu_path)):
            if not filename.endswith('.conf'):
                continue
            filepath = os.path.join(menu_path, filename)
            conf_data = read_service_file(filepath, base_dir)
            menu_items.append({
                'nom': conf_data['nom'],
                'lien': conf_data['url'],
                'icone': conf_data['icone'],
            })
    return menu_items


def load_system_navigation_context() -> dict:
    """Lit le menu officiel dans system.conf pour le layout menu.html.

    system.py possède déjà toute la tuyauterie de personnalisation. index.py
    ne recrée rien : il se contente d'exposer ces valeurs aux templates, avec
    un fallback vers l'ancien menu en dossiers si le module System est indisponible.
    """
    try:
        from system import personalization_get_config, system_menu_load_items
        return {
            'system_dashboard_config': personalization_get_config(),
            'system_global_menu': system_menu_load_items(),
        }
    except Exception:
        return {
            'system_dashboard_config': None,
            'system_global_menu': [],
        }


@index_bp.app_context_processor
def inject_dashboard_context():
    context = {
        'raccourcis_menu': build_menu_items(),
        'dashboard_config': get_config(),
        'home_config': load_home_config(),
        'home_top_items': load_index_top_items(),
        'home_top_config_path': get_index_top_config_path(),
    }
    context.update(load_system_navigation_context())
    return context


@index_bp.route('/index', methods=['GET', 'POST'])
def home():
    if request.args.get('mode') == 'edit':
        return redirect(url_for('system_bp.system_page', tab='personalization'))

    try:
        from system import collect_overview
        overview = collect_overview()
    except Exception as exc:
        overview = {
            "time": "--:--:--",
            "date": "",
            "host": {"hostname": "System", "os": str(exc), "kernel": ""},
            "cpu": {},
            "ram": {},
            "disk": {},
            "network": {},
            "services": {},
            "docker": {"available": False},
            "vms": {"available": False, "total": 0, "running": 0, "stopped": 0},
            "processes": {},
            "fans": {"available": False, "count": 0, "rows": [], "source": ""},
            "uptime": "—",
        }

    return render_template('index.html', overview=overview, config=get_config(), home_config=load_home_config(), home_config_path=get_home_config_path(), home_top_items=load_index_top_items(), home_top_config_path=get_index_top_config_path())

    base_dir = get_base_dir()
    mode = 'edit' if request.args.get('mode') == 'edit' else 'view'

    delete_rel = request.args.get('delete')
    if delete_rel:
        try:
            full_path = safe_join(base_dir, unquote(delete_rel))
            if os.path.isfile(full_path) and full_path.endswith('.conf'):
                os.remove(full_path)
                parent = os.path.dirname(full_path)
                if os.path.isdir(parent) and not os.listdir(parent):
                    os.rmdir(parent)
        except ValueError:
            pass
        return redirect(url_for('index_bp.home', mode='edit'))

    if request.method == 'POST' and request.form.get('action') == 'update_service':
        previous_rel_path = request.form.get('id', '').strip()
        category = os.path.dirname(previous_rel_path) or 'Autre'
        name = request.form.get('nom', 'Service')
        url = request.form.get('url', '#')
        icon = request.form.get('icone', '🌐')
        write_service_file(category, name, url, icon, previous_rel_path=previous_rel_path)
        return redirect(url_for('index_bp.home', mode='edit'))

    stacks = {}
    categories = []

    if os.path.isdir(base_dir):
        for category in sorted(os.listdir(base_dir)):
            if category.startswith('.'):
                continue

            cat_path = os.path.join(base_dir, category)
            if not os.path.isdir(cat_path):
                continue

            categories.append(category)
            items = []
            for filename in sorted(os.listdir(cat_path)):
                if filename.endswith('.conf'):
                    items.append(read_service_file(os.path.join(cat_path, filename), base_dir))
            if items:
                stacks[category] = items

    return render_template(
        'index.html',
        stacks=stacks,
        cats=categories,
        config=get_config(),
        mode=mode,
    )


@index_bp.route('/api_dashboard/debug_menu', methods=['GET'])
def api_dashboard_debug_menu():
    config = load_module_config()
    base_dir = get_base_dir(create=False)
    menu_path = safe_join(base_dir, 'Menu')
    return {
        'config_path': config.get('_config_path'),
        'config_dir': config.get('_config_dir'),
        'base_dir_value': config.get('base_dir'),
        'base_dir_resolved': base_dir,
        'menu_path': menu_path,
        'menu_exists': os.path.isdir(menu_path),
        'menu_files': sorted(os.listdir(menu_path)) if os.path.isdir(menu_path) else [],
        'home_config_path': get_home_config_path(),
        'home_config': load_home_config(),
        'home_top_items': load_index_top_items(),
        'home_top_config_path': get_index_top_config_path(),
    }


@index_bp.route('/api_dashboard', methods=['POST'])
def api_dashboard():
    base_dir = get_base_dir()
    action = request.form.get('action', '').strip()

    if 'id' in request.form and 'categorie' in request.form and not action:
        old_rel = request.form.get('id', '').strip()
        new_category = sanitize_segment(request.form.get('categorie', ''), 'Autre')
        if not old_rel:
            return 'Missing data', 400

        try:
            old_path = safe_join(base_dir, old_rel)
            new_folder = safe_join(base_dir, new_category)
        except ValueError:
            return 'Invalid path', 400

        if not os.path.isfile(old_path):
            return 'Item not found', 404

        os.makedirs(new_folder, exist_ok=True)
        target_path = os.path.join(new_folder, os.path.basename(old_path))
        if os.path.exists(target_path):
            return 'Destination already exists', 409

        shutil.move(old_path, target_path)
        old_folder = os.path.dirname(old_path)
        if os.path.isdir(old_folder) and not os.listdir(old_folder):
            os.rmdir(old_folder)
        return 'Moved', 200

    if action == 'update_config':
        key = _normalize_config_key(request.form.get('cle', ''))
        value = sanitize_conf_value(request.form.get('valeur', ''))
        if key not in EDITABLE_CONFIG_KEYS:
            return 'Invalid config key', 400
        if not value:
            return 'Empty value', 400

        config = get_config()
        config[key] = value
        save_module_config(config)
        return 'OK', 200

    if action == 'rename_cat':
        old_name = request.form.get('old_name', '').strip()
        new_name = sanitize_segment(request.form.get('new_name', ''), 'Autre')
        if not old_name or not new_name:
            return 'Missing category name', 400

        try:
            old_path = safe_join(base_dir, old_name)
            new_path = safe_join(base_dir, new_name)
        except ValueError:
            return 'Invalid path', 400

        if not os.path.isdir(old_path):
            return 'Category not found', 404
        if os.path.exists(new_path):
            return 'Category already exists', 409

        shutil.move(old_path, new_path)
        return 'Renamed', 200

    if action == 'delete_cat':
        cat_name = request.form.get('cat_name', '').strip()
        if not cat_name:
            return 'Missing category name', 400

        try:
            cat_path = safe_join(base_dir, cat_name)
        except ValueError:
            return 'Invalid path', 400

        if not os.path.isdir(cat_path):
            return 'Category not found', 404

        shutil.rmtree(cat_path)
        return 'Deleted', 200

    return 'OK', 200


@index_bp.route('/gestion_add', methods=['POST'])
def gestion_add():
    name = request.form.get('nom', 'Service')
    url = request.form.get('url', '#')
    icon = request.form.get('icone', '🌐')
    new_category = request.form.get('cat_new', '').strip()
    selected_category = request.form.get('cat_select', 'Autre').strip()
    category = new_category if new_category else selected_category

    write_service_file(category, name, url, icon)
    return redirect(url_for('index_bp.home', mode='edit'))
