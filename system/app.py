import importlib
import ctypes
import ctypes.util
import hmac
import os
import re
import secrets
import sys
import time
from datetime import timedelta
from urllib.parse import urlsplit

from flask import Flask, jsonify, make_response, redirect, render_template, request, Response, session, url_for

# On crée l'application principale
app = Flask(__name__, static_folder='static', static_url_path='/static')

# ==========================================================
# 📁 DOSSIER CONF CENTRAL
# ==========================================================
# Alpha : /dockers/conf si app.py est dans /dockers/system.
# Plus tard : il suffira de démarrer Flask avec NAS_CONF_DIR=/etc/nas/conf
# ou de changer uniquement cette valeur ici.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DOCKERS_DIR = os.path.abspath(os.path.join(APP_DIR, ".."))
NAS_CONF_DIR = os.path.abspath(os.path.expanduser(os.path.expandvars(os.environ.get("NAS_CONF_DIR", os.path.join(DOCKERS_DIR, "conf")))))
os.environ.setdefault("NAS_CONF_DIR", NAS_CONF_DIR)

def nas_conf_file(name: str) -> str:
    return os.path.join(NAS_CONF_DIR, name)


# ==========================================================
# CSS EN MEMOIRE
# ==========================================================
# Les fragments templates/styles/*.html sont statiques. Les inclure via Jinja
# dans chaque page coutait cher et gonflait chaque reponse HTML. On les charge
# donc une seule fois au demarrage Flask, puis on sert le bundle depuis la RAM.
STYLE_BUNDLE_ROUTE = "/assets/yoleo-style-bundle.css"
STYLE_BUNDLE_CSS = ""
STYLE_BUNDLE_VERSION = "0"


def _style_template_path() -> str:
    return os.path.join(APP_DIR, "templates", "style.html")


def _style_manifest_path() -> str:
    return os.path.join(APP_DIR, "templates", "style_bundle_manifest.txt")


def _style_fragment_path(include_name: str) -> str:
    return os.path.join(APP_DIR, "templates", include_name)


def _style_include_names() -> list[str]:
    manifest = _style_manifest_path()
    manifest_mtime = 0
    names: list[str] = []
    if os.path.exists(manifest):
        manifest_mtime = os.stat(manifest).st_mtime_ns
        with open(manifest, "r", encoding="utf-8-sig", errors="replace") as handle:
            names = [
                line.strip().lstrip("\ufeff")
                for line in handle
                if line.strip() and not line.lstrip().startswith("#")
            ]

    if names:
        return _style_include_names_with_new_fragments(names, manifest_mtime)

    path = _style_template_path()
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        source = handle.read()
    names = re.findall(r'{%\s*include\s+"([^"]+)"', source)
    if names:
        return _style_include_names_with_new_fragments(names, 0)

    return _style_include_names_with_new_fragments([], 0)


def _style_include_names_with_new_fragments(base_names: list[str], manifest_mtime: int = 0) -> list[str]:
    names = list(base_names)
    known = set(names)
    styles_dir = os.path.join(APP_DIR, "templates", "styles")
    if not os.path.isdir(styles_dir):
        return names

    for name in sorted(os.listdir(styles_dir)):
        if not name.lower().endswith(".html"):
            continue
        include_name = "styles/" + name
        if include_name in known:
            continue
        fragment_path = os.path.join(styles_dir, name)
        # Le manifest fige l'ordre historique. Les fragments ajoutes ensuite
        # sont decouverts au demarrage et ajoutes en fin de bundle.
        if names and manifest_mtime and os.stat(fragment_path).st_mtime_ns <= manifest_mtime:
            continue
        names.append(include_name)
        known.add(include_name)
    return names


def _style_clean_fragment(text: str) -> str:
    text = text.lstrip("\ufeff")
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line in {"<style>", "</style>"}:
            continue
        if line.startswith('<link rel="stylesheet" href="/static/yoleo-mobile-tables.css'):
            continue
        if line.startswith('<script src="/static/yoleo-mobile-tables.js'):
            continue
        lines.append(raw_line.rstrip())
    return "\n".join(lines).strip()


def load_yoleo_style_bundle() -> None:
    global STYLE_BUNDLE_CSS, STYLE_BUNDLE_VERSION

    chunks = [
        "/* Generated in memory from templates/style.html and templates/styles/*.html. */",
        "",
    ]
    latest_mtime = 0
    loaded = 0

    style_path = _style_template_path()
    if os.path.exists(style_path):
        latest_mtime = max(latest_mtime, os.stat(style_path).st_mtime_ns)
    manifest_path = _style_manifest_path()
    if os.path.exists(manifest_path):
        latest_mtime = max(latest_mtime, os.stat(manifest_path).st_mtime_ns)

    for include_name in _style_include_names():
        fragment_path = _style_fragment_path(include_name)
        if not os.path.exists(fragment_path):
            continue
        latest_mtime = max(latest_mtime, os.stat(fragment_path).st_mtime_ns)
        with open(fragment_path, "r", encoding="utf-8-sig", errors="replace") as handle:
            css = _style_clean_fragment(handle.read())
        if not css:
            continue
        chunks.append(f"/* {include_name} */")
        chunks.append(css)
        chunks.append("")
        loaded += 1

    STYLE_BUNDLE_CSS = "\n".join(chunks).rstrip() + "\n"
    STYLE_BUNDLE_VERSION = str(latest_mtime or 1)
    print(f"✅ Styles CSS chargés en mémoire : {loaded} fragments, {len(STYLE_BUNDLE_CSS)} octets")


try:
    load_yoleo_style_bundle()
except Exception as exc:
    STYLE_BUNDLE_CSS = "/* CSS bundle unavailable. */\n"
    STYLE_BUNDLE_VERSION = "0"
    print(f"⚠️ Chargement CSS en mémoire impossible : {exc}")


@app.route(STYLE_BUNDLE_ROUTE)
def yoleo_style_bundle():
    response = Response(STYLE_BUNDLE_CSS, mimetype="text/css")
    response.headers["Cache-Control"] = "public, max-age=86400"
    response.headers["X-Yoleo-Style-Version"] = STYLE_BUNDLE_VERSION
    return response


@app.context_processor
def inject_yoleo_style_bundle_url():
    return {
        "yoleo_style_bundle_url": f"{STYLE_BUNDLE_ROUTE}?v={STYLE_BUNDLE_VERSION}",
    }


# ==========================================================
# 🧩 CONF MINIMALE APP
# ==========================================================
# app.conf est la base du chargement dynamique. Si le dossier ../conf est vide,
# Flask doit recréer ce fichier avant tout le reste, sinon aucun module n'est
# chargé et l'interface démarre sans menu.
APP_CONF = nas_conf_file('app.conf')

APP_DEFAULT_CONF_TEXT = 'system=system\nidex=index\nbrowser=browser\nbuilds=builds\nfile=file\nvm=vm\ndisk=disk\ndockers=dockers\nusers=users\npartage=partage\nservices=services\nbackup=backup\ntask=task\nscripts=scripts\nmeteo=meteo\nterminal=terminal\n'


def ensure_app_conf_file(path: str = "") -> bool:
    """Crée ../conf/app.conf si absent, sans jamais écraser l'existant."""
    target = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path or APP_CONF).strip())))
    if os.path.exists(target):
        return False

    parent = os.path.dirname(target.rstrip(os.sep)) or "."
    os.makedirs(parent, exist_ok=True)

    with open(target, "w", encoding="utf-8") as handle:
        handle.write(APP_DEFAULT_CONF_TEXT.rstrip() + "\n")

    try:
        os.chmod(target, 0o644)
    except OSError:
        pass

    return True


def ensure_app_conf_core_modules(path: str = "") -> list[str]:
    """Ajoute les modules techniques indispensables sans écraser app.conf.

    Le module browser est un composant commun de l'interface : les pages
    métier ne doivent pas réinventer leurs boîtes Parcourir. On l'ajoute donc
    automatiquement si app.conf existe déjà mais ne le référence pas encore.
    """
    target = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path or APP_CONF).strip())))
    if not os.path.exists(target):
        return []

    with open(target, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()

    loaded_modules = set()
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        module_name = line.split("=", 1)[1].strip() if "=" in line else line
        if module_name:
            loaded_modules.add(module_name)

    added = []
    if "browser" not in loaded_modules:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Module technique commun : boîtes Parcourir fichier/dossier/image")
        lines.append("browser=browser")
        added.append("browser")

    if "backup" not in loaded_modules:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Module Service/Backup : rsync host détaché via tmux/systemd")
        lines.append("backup=backup")
        added.append("backup")

    if "task" not in loaded_modules:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Module Système/Task : gestionnaire de tâches host")
        lines.append("task=task")
        added.append("task")

    if "scripts" not in loaded_modules:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Module Système/Scripts : scripts ponctuels host via tmux indépendant")
        lines.append("scripts=scripts")
        added.append("scripts")

    if "meteo" not in loaded_modules:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Module Météo : page météo + affichage optionnel dans le bandeau haut")
        lines.append("meteo=meteo")
        added.append("meteo")

    if added:
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines).rstrip() + "\n")
        try:
            os.chmod(target, 0o644)
        except OSError:
            pass

    return added


try:
    if ensure_app_conf_file(APP_CONF):
        print(f"✅ app.conf recréé automatiquement : {APP_CONF}")
    else:
        print(f"✅ app.conf déjà présent : {APP_CONF}")

    added_core_modules = ensure_app_conf_core_modules(APP_CONF)
    if added_core_modules:
        print("✅ app.conf complété : " + ", ".join(added_core_modules))
except Exception as exc:
    raise RuntimeError(f"Impossible de préparer app.conf dans ../conf : {APP_CONF} : {exc}") from exc


# ==========================================================
# 🧾 CONF MENU CLI
# ==========================================================
# Fichier officiel du menu terminal : ../conf/menu.conf depuis system/app.py.
# Comme conf/menu/ et conf/menu_top/, il doit réapparaître tout seul au
# démarrage si l'utilisateur a supprimé le fichier. On ne réécrit jamais un
# menu existant : les modifications faites dans l'éditeur restent prioritaires.
CLI_MENU_CONF = nas_conf_file("menu.conf")
CLI_MENU_DEFAULT_CONF = os.path.join(APP_DIR, "default_menu_cli", "menu.conf")
CLI_MENU_DEFAULT_CONF_TEXT = """--- Stacks YML ---
Install Stacks = stacks.py --update
,Option Stacks...
,Démarrer les Dockers = stacks.py --start
,Affioche les Stacks = stacks.py --list
,install LAN Ollama = stacks.py --ollama
,Remove Ollama LAN = stacks.py --remove-ollama
-
--- Docker ---
Docker - SAVE tous = docker.py --save
Docker - LOAD tous = docker.py --load
,Option Dockers...
,Docker - SAVE choisir = docker.py --select --save
,Docker - LOAD choisir = docker.py --select --load
,Docker - Liste complète = docker.py --list all
-
--- Backup ---
-
--- Registre ---
Netoyer de Registre = registry.py
-
--- Cache ---
Vider le Cache = cache.py --all
"""


def cli_menu_default_conf_text() -> str:
    if os.path.exists(CLI_MENU_DEFAULT_CONF):
        with open(CLI_MENU_DEFAULT_CONF, "r", encoding="utf-8-sig", errors="replace") as handle:
            text = handle.read()
        return text.rstrip() + "\n"
    return CLI_MENU_DEFAULT_CONF_TEXT.rstrip() + "\n"


def ensure_cli_menu_conf_file(path: str = "") -> bool:
    """Crée ../conf/menu.conf si absent, sans écraser le menu CLI existant."""
    target = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path or CLI_MENU_CONF).strip())))
    if os.path.exists(target):
        return False

    parent = os.path.dirname(target.rstrip(os.sep)) or "."
    os.makedirs(parent, exist_ok=True)

    with open(target, "w", encoding="utf-8") as handle:
        handle.write(cli_menu_default_conf_text())

    try:
        os.chmod(target, 0o644)
    except OSError:
        pass

    return True


try:
    if ensure_cli_menu_conf_file(CLI_MENU_CONF):
        print(f"✅ menu.conf CLI recréé automatiquement : {CLI_MENU_CONF}")
    else:
        print(f"✅ menu.conf CLI déjà présent : {CLI_MENU_CONF}")
except Exception as exc:
    raise RuntimeError(f"Impossible de préparer menu.conf CLI dans ../conf : {CLI_MENU_CONF} : {exc}") from exc


def bootstrap_system_conf() -> None:
    """Prépare les confs indispensables avant le chargement des modules.

    Le menu global est lu depuis system.conf. Si ce fichier est absent au tout
    premier démarrage, certains templates peuvent afficher un menu vide avant
    que le module System ne soit importé par app.conf. On importe donc system.py
    tout de suite : il crée system.conf et mdns.conf si besoin, puis le loader
    dynamique réutilisera le même module Python pour enregistrer le blueprint.
    """
    try:
        system_module = importlib.import_module("system")
        ensure_system = getattr(system_module, "ensure_system_conf_file", None)
        ensure_mdns = getattr(system_module, "ensure_mdns_conf_file", None)

        created = []
        if callable(ensure_system) and ensure_system(nas_conf_file("system.conf")):
            created.append("system.conf")
        if callable(ensure_mdns) and ensure_mdns(nas_conf_file("mdns.conf")):
            created.append("mdns.conf")

        if created:
            print("✅ Bootstrap System : conf créée(s) : " + ", ".join(created))
        else:
            print("✅ Bootstrap System : conf déjà prête.")
    except Exception as exc:
        # On ne bloque pas tout Flask ici : l'import dynamique signalera aussi
        # l'erreur si le module system est réellement cassé.
        print(f"⚠️ Bootstrap System impossible : {exc}")


bootstrap_system_conf()


# ==========================================================
# 🔑 CLÉ DE SÉCURITÉ
# ==========================================================
app.secret_key = os.environ.get("SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("SECRET_KEY manquante : ajoute SECRET_KEY dans le YAML Docker.")

SESSION_CONF = nas_conf_file("session.conf")
SESSION_DEFAULT_MINUTES = 20
SESSION_MIN_MINUTES = 1
SESSION_MAX_MINUTES = 10080


def session_conf_read(path: str = "") -> dict[str, str]:
    target = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path or SESSION_CONF).strip())))
    data: dict[str, str] = {}
    if not os.path.exists(target):
        return data
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or line.startswith(";") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                data[key.strip().upper()] = value.strip()
    except Exception as exc:
        print(f"⚠️ Lecture session.conf impossible : {exc}")
    return data


def session_conf_minutes(data: dict[str, str] | None = None) -> int:
    data = data if data is not None else session_conf_read()
    for key in ("SESSION_MINUTES", "DUREE_MINUTES", "DUREE", "DURATION_MINUTES", "TIME", "MINUTES"):
        raw = str(data.get(key, "")).strip()
        if not raw:
            continue
        try:
            return max(SESSION_MIN_MINUTES, min(SESSION_MAX_MINUTES, int(float(raw))))
        except Exception:
            pass
    return SESSION_DEFAULT_MINUTES


def session_conf_write(minutes: int, path: str = "") -> str:
    target = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path or SESSION_CONF).strip())))
    clean_minutes = max(SESSION_MIN_MINUTES, min(SESSION_MAX_MINUTES, int(minutes or SESSION_DEFAULT_MINUTES)))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("# Configuration session Flask Yoleo\n")
        handle.write("# Duree de session en minutes. Exemple : 720 = 12 heures.\n")
        handle.write(f"SESSION_MINUTES={clean_minutes}\n")
    try:
        os.chmod(target, 0o644)
    except OSError:
        pass
    return target


def ensure_session_conf() -> int:
    if not os.path.exists(SESSION_CONF):
        session_conf_write(SESSION_DEFAULT_MINUTES)
        print(f"✅ session.conf créé automatiquement : {SESSION_CONF}")
    minutes = session_conf_minutes()
    print(f"✅ Durée session Flask : {minutes} minute(s) depuis {SESSION_CONF}")
    return minutes


SESSION_MINUTES = ensure_session_conf()

app.permanent_session_lifetime = timedelta(minutes=SESSION_MINUTES)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


# ==========================================================
# 🔐 AUTHENTIFICATION PAM LINUX
# ==========================================================
PAM_SERVICE = os.environ.get("YOLEO_PAM_SERVICE", "yoleo-flask")
PAM_ALLOWED_USER = "root"
AUTH_SESSION_KEY = "yoleo_authenticated"
AUTH_USER_KEY = "yoleo_username"

PAM_SUCCESS = 0
PAM_PROMPT_ECHO_OFF = 1
PAM_PROMPT_ECHO_ON = 2
PAM_ERROR_MSG = 3
PAM_TEXT_INFO = 4
PAM_CONV_ERR = 19
PAM_BUF_ERR = 5


class PamMessage(ctypes.Structure):
    _fields_ = [
        ("msg_style", ctypes.c_int),
        ("msg", ctypes.c_char_p),
    ]


class PamResponse(ctypes.Structure):
    _fields_ = [
        ("resp", ctypes.c_char_p),
        ("resp_retcode", ctypes.c_int),
    ]


PamConversationCallback = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_int,
    ctypes.POINTER(ctypes.POINTER(PamMessage)),
    ctypes.POINTER(ctypes.POINTER(PamResponse)),
    ctypes.c_void_p,
)


class PamConversation(ctypes.Structure):
    _fields_ = [
        ("conv", PamConversationCallback),
        ("appdata_ptr", ctypes.c_void_p),
    ]


def _load_pam_libs():
    pam_path = ctypes.util.find_library("pam") or "libpam.so.0"
    libc_path = ctypes.util.find_library("c") or "libc.so.6"
    pam = ctypes.CDLL(pam_path)
    libc = ctypes.CDLL(libc_path)

    pam.pam_start.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.POINTER(PamConversation),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    pam.pam_start.restype = ctypes.c_int
    pam.pam_authenticate.argtypes = [ctypes.c_void_p, ctypes.c_int]
    pam.pam_authenticate.restype = ctypes.c_int
    pam.pam_acct_mgmt.argtypes = [ctypes.c_void_p, ctypes.c_int]
    pam.pam_acct_mgmt.restype = ctypes.c_int
    pam.pam_end.argtypes = [ctypes.c_void_p, ctypes.c_int]
    pam.pam_end.restype = ctypes.c_int

    libc.calloc.argtypes = [ctypes.c_size_t, ctypes.c_size_t]
    libc.calloc.restype = ctypes.c_void_p
    libc.strdup.argtypes = [ctypes.c_char_p]
    libc.strdup.restype = ctypes.c_void_p
    libc.free.argtypes = [ctypes.c_void_p]
    libc.free.restype = None

    return pam, libc


PAM_LIB = None
LIBC = None
try:
    PAM_LIB, LIBC = _load_pam_libs()
    print(f"✅ PAM prêt pour l'authentification Flask : service={PAM_SERVICE}")
except Exception as exc:
    print(f"⚠️ PAM indisponible pour l'authentification Flask : {exc}")


def _pam_conversation(num_msg, messages, responses, appdata_ptr):
    if not LIBC or num_msg <= 0:
        return PAM_CONV_ERR

    response_ptr = LIBC.calloc(num_msg, ctypes.sizeof(PamResponse))
    if not response_ptr:
        return PAM_BUF_ERR

    response_array = ctypes.cast(response_ptr, ctypes.POINTER(PamResponse))
    password = ctypes.cast(appdata_ptr, ctypes.c_char_p).value or b""

    for index in range(num_msg):
        try:
            style = messages[index].contents.msg_style
        except Exception:
            LIBC.free(response_ptr)
            return PAM_CONV_ERR

        if style == PAM_PROMPT_ECHO_OFF:
            duplicated = LIBC.strdup(password)
            if not duplicated:
                LIBC.free(response_ptr)
                return PAM_BUF_ERR
            response_array[index].resp = ctypes.cast(duplicated, ctypes.c_char_p)
        elif style == PAM_PROMPT_ECHO_ON:
            duplicated = LIBC.strdup(b"")
            if not duplicated:
                LIBC.free(response_ptr)
                return PAM_BUF_ERR
            response_array[index].resp = ctypes.cast(duplicated, ctypes.c_char_p)
        elif style in (PAM_ERROR_MSG, PAM_TEXT_INFO):
            response_array[index].resp = None
        else:
            LIBC.free(response_ptr)
            return PAM_CONV_ERR

    responses[0] = response_array
    return PAM_SUCCESS


PAM_CONVERSATION_CALLBACK = PamConversationCallback(_pam_conversation)


def pam_authenticate_user(username: str, password: str) -> bool:
    if not PAM_LIB:
        return False

    username = str(username or "").strip()
    password = str(password or "")
    if username != PAM_ALLOWED_USER:
        return False
    if not username or not password or "\x00" in username or "\x00" in password:
        return False

    user_bytes = username.encode("utf-8", errors="ignore")
    password_bytes = password.encode("utf-8", errors="ignore")
    service_bytes = PAM_SERVICE.encode("utf-8", errors="ignore")
    password_holder = ctypes.c_char_p(password_bytes)
    handle = ctypes.c_void_p()
    conv = PamConversation(PAM_CONVERSATION_CALLBACK, ctypes.cast(password_holder, ctypes.c_void_p))

    status = PAM_LIB.pam_start(service_bytes, user_bytes, ctypes.byref(conv), ctypes.byref(handle))
    if status != PAM_SUCCESS:
        return False

    try:
        status = PAM_LIB.pam_authenticate(handle, 0)
        if status != PAM_SUCCESS:
            return False
        status = PAM_LIB.pam_acct_mgmt(handle, 0)
        return status == PAM_SUCCESS
    finally:
        PAM_LIB.pam_end(handle, status)


def _safe_next_url(value: str | None) -> str:
    value = str(value or "").strip()
    if not value:
        return "/index"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//"):
        return "/index"
    return value


def _login_csrf_token() -> str:
    token = secrets.token_urlsafe(32)
    session["_login_csrf"] = token
    return token


@app.before_request
def require_login():
    endpoint = request.endpoint or ""
    # L'API native possède sa propre authentification Bearer. Sur l'URL
    # publique, Nginx Proxy Manager a déjà exigé le certificat client P12.
    # Une application native ne doit jamais être redirigée vers /login HTML.
    if request.path == "/api/v1" or request.path.startswith("/api/v1/"):
        return None
    # La sonde de reprise est strictement en lecture seule ({"ok": true}).
    # Elle doit rester joignable après un reboot, même si la session navigateur
    # a expiré, afin que la popup puisse détecter le retour de l'interface.
    if endpoint in {"login", "logout", "static", "manifest", "sw", "yoleo_style_bundle", "index_bp.index_dev_availability", "terminal_bp.terminal_ttyd_auth"}:
        return None
    if request.path.startswith("/static/"):
        return None
    if session.get(AUTH_SESSION_KEY):
        return None

    next_url = request.full_path if request.method == "GET" else request.path
    if next_url.endswith("?"):
        next_url = next_url[:-1]
    return redirect(url_for("login", next=_safe_next_url(next_url)))


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = _safe_next_url(request.values.get("next"))
    if session.get(AUTH_SESSION_KEY):
        return redirect(next_url)

    error = ""
    if request.method == "POST":
        form_token = str(request.form.get("csrf_token") or "")
        session_token = str(session.get("_login_csrf") or "")
        username = str(request.form.get("username") or "").strip()
        password = str(request.form.get("password") or "")

        if not session_token or not hmac.compare_digest(form_token, session_token):
            error = "Session de connexion expirée. Réessaie."
        elif not username or not password:
            error = "Nom d'utilisateur et mot de passe obligatoires."
        elif username != PAM_ALLOWED_USER:
            error = "Seul le compte root est autorisé."
        elif pam_authenticate_user(username, password):
            session.clear()
            session.permanent = True
            session[AUTH_SESSION_KEY] = True
            session[AUTH_USER_KEY] = username
            session["yoleo_login_at"] = int(time.time())
            return redirect(next_url)
        else:
            error = "Identifiants Linux invalides."

    return render_template(
        "login.html",
        csrf_token=_login_csrf_token(),
        error=error,
        next_url=next_url,
        session_minutes=SESSION_MINUTES,
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ==========================================================
# 🔄 CHARGEMENT DYNAMIQUE DES BLUEPRINTS (Auto-Loader)
# ==========================================================
if os.path.exists(APP_CONF):
    print("🔄 Chargement des modules depuis app.conf...")
    with open(APP_CONF, 'r', encoding='utf-8') as f:
        for line in f:
            # Nettoyage de la ligne
            line = line.strip()
            
            # 1. On ignore les lignes vides, les commentaires (#) ET les catégories ([...])
            if not line or line.startswith('#') or line.startswith('['):
                continue

            # 2. LOGIQUE INVERSÉE (Nom Affiché = Nom du Module)
            # On cherche s'il y a un signe "="
            if '=' in line:
                # On prend ce qui est à DROITE du signe égal (index 1)
                # Ex: "Page Accueil=index" -> On garde "index"
                module_name = line.split('=', 1)[1].strip()
            else:
                # S'il n'y a pas de égal, on prend la ligne telle quelle
                module_name = line

            # Sécurité : Si après le découpage le nom est vide, on passe
            if not module_name:
                continue

            # Si module = "conf_ffmpeg", on cherche la variable "conf_ffmpeg_bp"
            bp_variable_name = f"{module_name}_bp"

            try:
                # 1. On importe le fichier (ex: import conf_ffmpeg)
                module_ref = importlib.import_module(module_name)
                
                # 2. On récupère la variable Blueprint dedans (ex: conf_ffmpeg_bp)
                blueprint_obj = getattr(module_ref, bp_variable_name)
                
                # 3. On enregistre le blueprint dans Flask
                app.register_blueprint(blueprint_obj)
                print(f"   ✅ [OK] {module_name} -> {bp_variable_name}")

            except ImportError as e:
                print(f"   ❌ [ERREUR] Impossible d'importer le fichier '{module_name}.py' : {e}")
            except AttributeError:
                print(f"   ❌ [ERREUR] Le fichier '{module_name}.py' ne contient pas la variable '{bp_variable_name}'")
            except Exception as e:
                print(f"   ❌ [ERREUR] Problème avec {module_name} : {e}")
else:
    print("⚠️ Fichier app.conf introuvable, aucun module chargé.")


# ==========================================================
# API JSON NATIVE ANDROID / WINDOWS
# ==========================================================
# Ce module est volontairement indépendant de app.conf : il ne crée aucune
# page ni entrée de menu et reste disponible quelle que soit la personnalisation
# de l'interface Web.
try:
    from yoleo_api import create_yoleo_api_blueprint

    api_v1_bp = create_yoleo_api_blueprint(
        authenticate_user=pam_authenticate_user,
        allowed_user=PAM_ALLOWED_USER,
        token_db_path=nas_conf_file("api_tokens.sqlite3"),
    )
    app.register_blueprint(api_v1_bp)
    print("   ✅ [OK] API JSON native -> /api/v1")
except Exception as exc:
    raise RuntimeError(f"Impossible de charger l'API JSON native : {exc}") from exc


# ==========================================================
# 📡 RÉENREGISTREMENT SAMBA / WSDD AU DÉMARRAGE
# ==========================================================
# Le module Partage fournit l'équivalent du bouton Samba « OK » : il réécrit
# la configuration puis redémarre smbd/nmbd/wsdd2. Le faire ici, une fois les
# blueprints chargés, garantit qu'il s'exécute au vrai démarrage de Yoleo et
# non pas à l'ouverture de la page Samba.
def _start_samba_wsdd_registration() -> None:
    partage_module = sys.modules.get("partage")
    starter = getattr(partage_module, "start_samba_interface_startup_guard_once", None)
    if not callable(starter):
        return
    try:
        starter(delay_seconds=3.0)
        print("📡 Réenregistrement Samba/WSDD programmé au démarrage.")
    except Exception as exc:
        print(f"⚠️ Réenregistrement Samba/WSDD non lancé : {exc}")


_start_samba_wsdd_registration()


# ==========================================================
# 📱 CONFIGURATION MOBILE / PWA
# ==========================================================
MOBILE_CONF = nas_conf_file('mobile.conf')

MOBILE_DEFAULT_CONF = {
    "SHORT_NAME": "Yoleo",
    "NAME": "Yoleo Nas OS",
    "DESCRIPTION": "Interface NAS Debian",
    "START_URL": "/index",
    "DISPLAY": "standalone",
    "BACKGROUND_COLOR": "#000000",
    "THEME_COLOR": "#000000",
    "ICON_SRC": "/static/logo.png",
}


def _mobile_clean_value(value: str, default: str = "") -> str:
    value = str(value if value is not None else "").strip().replace("\r", " ").replace("\n", " ")
    return value or default


def _mobile_clean_color(value: str, default: str = "#000000") -> str:
    value = _mobile_clean_value(value, default)
    if len(value) == 7 and value.startswith("#"):
        allowed = "0123456789abcdefABCDEF"
        if all(ch in allowed for ch in value[1:]):
            return value.upper()
    return default


def _mobile_clean_static_icon(value: str, default: str = "/static/logo.png") -> str:
    value = _mobile_clean_value(value, default).replace("\\", "/")
    if value.startswith("static/"):
        value = "/" + value
    if not value.startswith("/static/"):
        return default
    lower = value.lower()
    if not lower.endswith((".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico")):
        return default
    return value


def mobile_conf_normalize(data: dict | None = None) -> dict:
    raw = dict(MOBILE_DEFAULT_CONF)
    if data:
        for key, value in data.items():
            clean_key = str(key or "").strip().upper()
            if clean_key in raw:
                raw[clean_key] = str(value).strip()
    raw["SHORT_NAME"] = _mobile_clean_value(raw.get("SHORT_NAME"), MOBILE_DEFAULT_CONF["SHORT_NAME"])
    raw["NAME"] = _mobile_clean_value(raw.get("NAME"), MOBILE_DEFAULT_CONF["NAME"])
    raw["DESCRIPTION"] = _mobile_clean_value(raw.get("DESCRIPTION"), MOBILE_DEFAULT_CONF["DESCRIPTION"])
    raw["START_URL"] = _mobile_clean_value(raw.get("START_URL"), MOBILE_DEFAULT_CONF["START_URL"])
    if not raw["START_URL"].startswith("/"):
        raw["START_URL"] = MOBILE_DEFAULT_CONF["START_URL"]
    raw["DISPLAY"] = raw.get("DISPLAY", MOBILE_DEFAULT_CONF["DISPLAY"]).strip().lower()
    if raw["DISPLAY"] not in {"standalone", "fullscreen", "minimal-ui", "browser"}:
        raw["DISPLAY"] = MOBILE_DEFAULT_CONF["DISPLAY"]
    raw["BACKGROUND_COLOR"] = _mobile_clean_color(raw.get("BACKGROUND_COLOR"), MOBILE_DEFAULT_CONF["BACKGROUND_COLOR"])
    raw["THEME_COLOR"] = _mobile_clean_color(raw.get("THEME_COLOR"), MOBILE_DEFAULT_CONF["THEME_COLOR"])
    raw["ICON_SRC"] = _mobile_clean_static_icon(raw.get("ICON_SRC"), MOBILE_DEFAULT_CONF["ICON_SRC"])
    return raw


def mobile_conf_read(path: str = "") -> dict:
    target = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path or MOBILE_CONF).strip())))
    data = {}
    if os.path.exists(target):
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or line.startswith(";") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    data[key.strip().upper()] = value.strip()
        except Exception as exc:
            print(f"⚠️ Lecture mobile.conf impossible : {exc}")
    return mobile_conf_normalize(data)


def mobile_conf_write(config: dict, path: str = "") -> str:
    target = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path or MOBILE_CONF).strip())))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    clean = mobile_conf_normalize(config)
    lines = [
        "# ============================================================\n",
        "# Configuration mobile / PWA - manifest.json\n",
        "# Fichier créé automatiquement par app.py si absent.\n",
        "# Modifiable depuis Système > Personnalisation > Mobile.\n",
        "# ============================================================\n",
        "\n",
    ]
    for key in ["SHORT_NAME", "NAME", "DESCRIPTION", "START_URL", "DISPLAY", "BACKGROUND_COLOR", "THEME_COLOR", "ICON_SRC"]:
        lines.append(f"{key}={clean[key]}\n")
    with open(target, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    try:
        os.chmod(target, 0o644)
    except OSError:
        pass
    return target


def ensure_mobile_conf_file(path: str = "") -> bool:
    target = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path or MOBILE_CONF).strip())))
    if os.path.exists(target):
        return False
    mobile_conf_write(MOBILE_DEFAULT_CONF, target)
    return True


try:
    if ensure_mobile_conf_file(MOBILE_CONF):
        print(f"✅ mobile.conf créé automatiquement : {MOBILE_CONF}")
    else:
        print(f"✅ mobile.conf déjà présent : {MOBILE_CONF}")
except Exception as exc:
    print(f"⚠️ Préparation mobile.conf impossible : {exc}")

# ==========================================================
# 1. LE MANIFEST
# ==========================================================
@app.route('/manifest.json')
def manifest():
    cfg = mobile_conf_read()
    data = {
        "short_name": cfg["SHORT_NAME"],
        "name": cfg["NAME"],
        "description": cfg["DESCRIPTION"],
        "start_url": cfg["START_URL"],
        "display": cfg["DISPLAY"],
        "background_color": cfg["BACKGROUND_COLOR"],
        "theme_color": cfg["THEME_COLOR"],
        "icons": [
            {
                "src": cfg["ICON_SRC"],
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable"
            },
            {
                "src": cfg["ICON_SRC"],
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
@app.route('/sw.js')
def sw():
    script = "self.addEventListener('fetch', (e) => {});"
    resp = make_response(script)
    resp.headers['Content-Type'] = 'application/javascript'
    return resp

# ==========================================================
# 3. LA PORTE D'ENTRÉE
# ==========================================================
@app.route('/')
def home():
    cfg = mobile_conf_read()
    icon_src = cfg["ICON_SRC"]
    theme_color = cfg["THEME_COLOR"]
    return f"""
    <!DOCTYPE html>
    <html style="background: {theme_color};">
    <head>
        <link rel="manifest" href="/manifest.json">
        <link rel="icon" href="{icon_src}">
        <meta name="theme-color" content="{theme_color}">
        <meta http-equiv="refresh" content="0; url=/index">
    </head>
    <body></body>
    </html>
    """


# === AUTO DEBUG ROUTES FLASK SYSTEM ===
def _flask_system_print_routes():
    import os
    import sys
    from pathlib import Path
    from datetime import datetime

    GREEN = "\033[92m"
    CYAN = "\033[96m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    RESET = "\033[0m"

    def ok(x): print(f"{GREEN}✅ {x}{RESET}", flush=True)
    def info(x): print(f"{CYAN}ℹ️  {x}{RESET}", flush=True)
    def bad(x): print(f"{RED}❌ {x}{RESET}", flush=True)
    def warn(x): print(f"{YELLOW}⚠️  {x}{RESET}", flush=True)

    base = Path(__file__).resolve().parent

    print("\n" + "=" * 80, flush=True)
    print(f"{CYAN}🚀 FLASK SYSTEM - MODULES / BLUEPRINTS / ROUTES{RESET}", flush=True)
    print("=" * 80, flush=True)

    info(f"Date      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    info(f"PID       : {os.getpid()}")
    info(f"Python    : {sys.executable}")
    info(f"App       : {Path(__file__).resolve()}")
    info(f"Dossier   : {base}")

    print("\n📦 MODULES LOCAUX CHARGÉS", flush=True)
    print("-" * 80, flush=True)

    count = 0
    for name, mod in sorted(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        try:
            p = Path(f).resolve()
            p.relative_to(base)
        except Exception:
            continue
        count += 1
        ok(f"Module chargé : {name:<30} -> {p.name}")

    if count == 0:
        warn("Aucun module local détecté")

    print("\n🧩 BLUEPRINTS ENREGISTRÉS", flush=True)
    print("-" * 80, flush=True)

    if not app.blueprints:
        bad("Aucun blueprint enregistré")
    else:
        for name, bp in sorted(app.blueprints.items()):
            ok(f"Blueprint : {name:<25} import={bp.import_name}")

    print("\n🛣️ ROUTES CHARGÉES", flush=True)
    print("-" * 80, flush=True)

    routes = sorted(app.url_map.iter_rules(), key=lambda r: str(r.rule))

    for r in routes:
        methods = ",".join(sorted(r.methods - {"HEAD", "OPTIONS"}))
        ok(f"Route : {str(r.rule):<40} {methods:<10} -> {r.endpoint}")

    print("\n🧪 TEST PARTAGE", flush=True)
    print("-" * 80, flush=True)

    if "partage" in sys.modules:
        ok("Module Python partage importé")
    else:
        bad("Module Python partage NON importé")

    if "partage_bp" in app.blueprints:
        ok("Blueprint partage_bp enregistré")
    else:
        bad("Blueprint partage_bp NON enregistré")

    found = False
    for r in routes:
        if "partage" in str(r.rule).lower() or "partage" in str(r.endpoint).lower():
            found = True
            methods = ",".join(sorted(r.methods - {"HEAD", "OPTIONS"}))
            ok(f"Route partage : {str(r.rule):<32} {methods:<10} -> {r.endpoint}")

    if not found:
        bad("Aucune route partage trouvée")

    print("=" * 80 + "\n", flush=True)

_flask_system_print_routes()
# === FIN AUTO DEBUG ROUTES FLASK SYSTEM ===


if __name__ == '__main__':
    # On lance sur le port 5000
    app.run(host='0.0.0.0', port=5000, debug=True)
