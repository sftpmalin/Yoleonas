#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
users.py
Module Flask : gestion des utilisateurs Linux de l'hôte.

Mode prévu maintenant : Flask lancé directement sur l'hôte.
Donc :
- lecture directe de /etc/passwd, /etc/group, /etc/shadow ;
- commandes système Linux lancées directement : useradd, usermod, groupadd,
  groupmod, chpasswd, passwd, deluser/userdel ;
- aucun Docker ;
- aucun chroot par défaut ;
- aucun Samba ;
- aucun miniDLNA ;
- aucun chmod/chown de fichiers ;
- aucune suppression de home.

Le fichier de configuration est détecté automatiquement par rapport à l'emplacement
du module Python :
- ./conf/users.conf
- ../conf/users.conf
- ou via la variable d'environnement USERS_CONF.
"""

import configparser
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from flask import Blueprint, render_template, jsonify, request


users_bp = Blueprint("users_bp", __name__)

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



# --- CONFIG PAR DÉFAUT ---
MODULE_DIR = Path(__file__).resolve().parent

# Nom canonique : users.conf.
# Alias accepté : user.conf, pour éviter les anciens liens ou messages "user.conf introuvable".
CONF_NAME = "users.conf"
CONF_ALIASES = ("users.conf", "user.conf")
USERS_BACKUP_NAME = "users_backup.conf"

# Valeurs de base intégrées : si la conf est absente, le module la recrée.
DEFAULT_CONF_TEXT = "# linux_users.conf\n# Configuration du module Flask linux_users_uid_gid.py\n\n# Racine réelle de l'hôte. En Flask host : /\nHOST_ROOT=/\n\n# Fichiers de l'hôte lus pour affichage\nPASSWD_FILE=/etc/passwd\nGROUP_FILE=/etc/group\nSHADOW_FILE=/etc/shadow\n\n# Active/désactive les boutons qui modifient les utilisateurs\nALLOW_ACTIONS=true\n\n# Protège root et les comptes système UID < 1000\nPROTECT_SYSTEM_USERS=true\n\n# Plage affichée comme utilisateurs utiles\nNORMAL_UID_MIN=1000\nNORMAL_UID_MAX=60000\n\n# Shell par défaut à la création\nDEFAULT_SHELL=/bin/bash\n\n# Shells proposés dans l'interface\nALLOWED_SHELLS=/bin/bash,/bin/sh,/usr/sbin/nologin\n\n# Dossier de sortie pour les certificats P12 intégrés au module UserLinux\n# Ancien paramètre repris depuis p12.conf\nP12_OUTPUT=../p12\n"

_env_conf = os.environ.get("USERS_CONF", "").strip()
CONF_CANDIDATES = []
if _env_conf:
    CONF_CANDIDATES.append(Path(_env_conf).expanduser())

# Cas les plus courants :
# - module dans /dockers/system/users.py            -> ./conf/users.conf
# - module dans /dockers/system/routes/users.py     -> ../conf/users.conf
# - Flask lancé depuis /dockers/system             -> cwd/conf/users.conf
for _conf_name in CONF_ALIASES:
    CONF_CANDIDATES.extend([
        Path(nas_conf_file(_conf_name)),
        MODULE_DIR / "conf" / _conf_name,
        MODULE_DIR.parent / "conf" / _conf_name,
        Path.cwd() / "conf" / _conf_name,
    ])

CONF_FILE = ""
CONFIG_FILE_USED = ""

# En host pur, la racine Linux est directement /
HOST_ROOT = "/"
PASSWD_FILE = "/etc/passwd"
GROUP_FILE = "/etc/group"
SHADOW_FILE = "/etc/shadow"

ALLOW_ACTIONS = True
PROTECT_SYSTEM_USERS = True
NORMAL_UID_MIN = 1000
NORMAL_UID_MAX = 60000

DEFAULT_SHELL = "/bin/bash"
ALLOWED_SHELLS = ["/bin/bash", "/bin/sh", "/usr/sbin/nologin"]

# --- CERTIFICATS / CLÉS ---
# P12 est intégré ici. Le dossier de sortie se règle dans users.conf.
P12_OUTPUT = "../p12"

VALID_P12_KEY_TYPES = {
    "2048": "RSA 2048",
    "3072": "RSA 3072",
    "4096": "RSA 4096",
    "ed25519": "Ed25519",
}

VALID_SSH_KEY_TYPES = {
    "rsa3072": {"label": "RSA 3072", "cmd": ["-t", "rsa", "-b", "3072"]},
    "rsa4096": {"label": "RSA 4096", "cmd": ["-t", "rsa", "-b", "4096"]},
    "ed25519": {"label": "ED25519 256", "cmd": ["-t", "ed25519"]},
}

SSH_KEY_DIR_NAME = "key"


# --- CONFIG EXTERNE ---
def _truthy(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on", "oui"}


def _find_conf_file():
    seen = set()

    for candidate in CONF_CANDIDATES:
        try:
            path = candidate.resolve()
        except Exception:
            continue

        key = str(path)
        if key in seen:
            continue
        seen.add(key)

        if path.exists() and path.is_file():
            return str(path)

    return ""


def _default_conf_target():
    if _env_conf:
        return Path(_env_conf).expanduser()
    return Path(nas_conf_file(CONF_NAME))


def _ensure_conf_file():
    existing = _find_conf_file()
    if existing:
        return existing

    target = _default_conf_target()
    try:
        target = target.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(DEFAULT_CONF_TEXT, encoding="utf-8")
        return str(target)
    except Exception:
        return ""


def _write_conf_value(key_to_write, value_to_write):
    global CONFIG_FILE_USED, CONF_FILE

    conf_path = _ensure_conf_file()
    if not conf_path:
        raise RuntimeError("Impossible de créer ou modifier users.conf")

    path = Path(conf_path)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        lines = DEFAULT_CONF_TEXT.splitlines()

    wanted = f"{key_to_write}={value_to_write}"
    replaced = False
    out = []

    for raw in lines:
        if raw.strip().startswith(f"{key_to_write}="):
            out.append(wanted)
            replaced = True
        else:
            out.append(raw)

    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(wanted)

    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    CONFIG_FILE_USED = str(path)
    CONF_FILE = str(path)


def _load_conf():
    global CONF_FILE, CONFIG_FILE_USED
    global HOST_ROOT, PASSWD_FILE, GROUP_FILE, SHADOW_FILE
    global ALLOW_ACTIONS, PROTECT_SYSTEM_USERS
    global NORMAL_UID_MIN, NORMAL_UID_MAX, DEFAULT_SHELL, ALLOWED_SHELLS
    global P12_OUTPUT

    CONFIG_FILE_USED = _ensure_conf_file()
    CONF_FILE = CONFIG_FILE_USED

    if not CONFIG_FILE_USED:
        return

    try:
        with open(CONFIG_FILE_USED, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue

                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()

                if key == "HOST_ROOT" and value:
                    HOST_ROOT = value.rstrip("/") or "/"
                elif key == "PASSWD_FILE" and value:
                    PASSWD_FILE = value
                elif key == "GROUP_FILE" and value:
                    GROUP_FILE = value
                elif key == "SHADOW_FILE" and value:
                    SHADOW_FILE = value
                elif key == "ALLOW_ACTIONS":
                    ALLOW_ACTIONS = _truthy(value)
                elif key == "PROTECT_SYSTEM_USERS":
                    PROTECT_SYSTEM_USERS = _truthy(value)
                elif key == "NORMAL_UID_MIN":
                    NORMAL_UID_MIN = max(0, int(value))
                elif key == "NORMAL_UID_MAX":
                    NORMAL_UID_MAX = max(NORMAL_UID_MIN + 1, int(value))
                elif key == "DEFAULT_SHELL" and value:
                    DEFAULT_SHELL = value
                elif key == "ALLOWED_SHELLS" and value:
                    ALLOWED_SHELLS = [x.strip() for x in value.split(",") if x.strip()]
                elif key == "P12_OUTPUT":
                    P12_OUTPUT = value
    except Exception:
        pass



_load_conf()




# --- UTILITAIRES ---
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]*[$]?$")


def get_json_payload():
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def read_text_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except Exception:
        return []


def valid_username(username):
    if not isinstance(username, str):
        return False
    username = username.strip()
    if not username:
        return False
    return bool(USERNAME_RE.match(username))


def clean_username(username):
    username = str(username or "").strip()
    if not valid_username(username):
        raise ValueError("Nom utilisateur invalide")
    return username


def clean_home_path(value):
    """Valide un chemin home Linux à écrire dans /etc/passwd via usermod -d."""
    home = str(value or "").strip()
    if not home:
        raise ValueError("Dossier home vide")
    if any(ch in home for ch in ("\x00", "\r", "\n")):
        raise ValueError("Chemin home invalide")
    if "\\" in home:
        raise ValueError("Chemin home invalide : utilise des slashs Linux /")
    if not home.startswith("/"):
        raise ValueError("Le home doit être un chemin absolu, exemple : /home/mobile")
    if ".." in home.split("/"):
        raise ValueError("Chemin home refusé : '..' interdit")
    if len(home) > 240:
        raise ValueError("Chemin home trop long")

    # Nettoie les doubles slashs et le slash final, sans changer la racine /.
    home = os.path.normpath(home)
    if home == ".":
        home = "/"
    return home


def sanitize_filename(value):
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "", (value or "").strip().lower())
    return cleaned or "certificat"


def normalize_common_name(value):
    raw = (value or "").strip()
    if not raw:
        return ""

    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    candidate = parsed.netloc or parsed.path or raw
    candidate = candidate.strip().strip("/")

    # Garde un CN simple compatible avec openssl -subj /CN=...
    candidate = re.sub(r"[^a-zA-Z0-9*._:-]+", "-", candidate)
    candidate = candidate.strip("-")
    return candidate or raw


def run_command(command):
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    )
    return (completed.stdout or "").strip()


def system_hostname():
    try:
        return os.uname().nodename
    except Exception:
        return "host"


def clean_ssh_key_type(value):
    key_type = str(value or "ed25519").strip().lower()
    if key_type not in VALID_SSH_KEY_TYPES:
        raise ValueError("Type de clé SSH invalide")
    return key_type


def resolve_p12_output_dir(value):
    raw = str(value or "").strip()
    if not raw:
        return ""

    expanded = os.path.expandvars(os.path.expanduser(raw))
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)

    # Les chemins relatifs du conf sont résolus depuis le dossier conf.
    # Exemple : /dockers/conf + ../p12 => /dockers/p12.
    return os.path.abspath(os.path.join(NAS_CONF_DIR, expanded))


def current_p12_output_resolved():
    return resolve_p12_output_dir(P12_OUTPUT)


def validate_p12_output_dir(value):
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Choisis d'abord un dossier de sortie pour le P12.")

    resolved = resolve_p12_output_dir(raw)
    if not resolved:
        raise ValueError("Dossier de sortie P12 invalide.")

    return raw, resolved


def browse_directories(path_value):
    start = str(path_value or "").strip()
    resolved = resolve_p12_output_dir(start) if start else NAS_ROOT_DIR

    if not resolved:
        resolved = "/"

    if os.path.isfile(resolved):
        resolved = os.path.dirname(resolved)

    # Si le dossier tapé n'existe pas encore, on remonte au parent existant
    # pour que le navigateur reste utilisable.
    probe = resolved
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            probe = "/"
            break
        probe = parent

    current = os.path.abspath(probe or "/")
    items = []

    try:
        with os.scandir(current) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        items.append({
                            "name": entry.name,
                            "path": entry.path,
                        })
                except OSError:
                    continue
    except PermissionError as exc:
        raise PermissionError(f"Permission refusée : {current}") from exc
    except OSError as exc:
        raise RuntimeError(str(exc)) from exc

    items.sort(key=lambda x: x["name"].lower())

    parent = os.path.dirname(current.rstrip("/")) or "/"
    return {
        "current": current,
        "requested": resolved,
        "parent": parent,
        "items": items,
    }


def generate_p12_files(form_data):
    global P12_OUTPUT

    safe_name = sanitize_filename(form_data.get("p12_name", ""))
    common_name = normalize_common_name(form_data.get("p12_url", ""))
    key_size = str(form_data.get("key_size") or "3072").strip().lower()
    password = str(form_data.get("p12_pwd") or "")
    output_dir_raw = str(form_data.get("p12_output_dir") or P12_OUTPUT or "").strip()

    if not str(form_data.get("p12_name") or "").strip():
        raise ValueError("Le nom du fichier est obligatoire.")
    if not common_name:
        raise ValueError("Le domaine / CN est obligatoire.")
    if key_size not in VALID_P12_KEY_TYPES:
        key_size = "3072"

    output_dir_raw, output_dir = validate_p12_output_dir(output_dir_raw)
    os.makedirs(output_dir, exist_ok=True)

    if not os.path.isdir(output_dir):
        raise ValueError(f"Dossier de sortie P12 invalide : {output_dir}")

    # On garde le dernier dossier choisi dans users.conf.
    P12_OUTPUT = output_dir_raw
    _write_conf_value("P12_OUTPUT", output_dir_raw)

    key_path = os.path.join(output_dir, f"{safe_name}.key")
    cert_path = os.path.join(output_dir, f"{safe_name}.crt")
    p12_path = os.path.join(output_dir, f"{safe_name}.p12")
    p12_compat_path = os.path.join(output_dir, f"{safe_name}_comp.p12")

    if key_size == "ed25519":
        run_command(["openssl", "genpkey", "-algorithm", "ED25519", "-out", key_path])
        run_command([
            "openssl", "req", "-new", "-x509", "-key", key_path,
            "-out", cert_path, "-days", "3650", "-subj", f"/CN={common_name}",
        ])
    else:
        run_command([
            "openssl", "req", "-x509", "-nodes", "-days", "3650",
            "-newkey", f"rsa:{key_size}",
            "-keyout", key_path,
            "-out", cert_path,
            "-subj", f"/CN={common_name}",
        ])

    run_command([
        "openssl", "pkcs12", "-export",
        "-out", p12_path,
        "-inkey", key_path,
        "-in", cert_path,
        "-name", common_name,
        "-passout", f"pass:{password}",
    ])

    run_command([
        "openssl", "pkcs12", "-export", "-legacy",
        "-inkey", key_path,
        "-in", cert_path,
        "-out", p12_compat_path,
        "-name", f"{safe_name}_comp",
        "-keypbe", "PBE-SHA1-3DES",
        "-certpbe", "PBE-SHA1-3DES",
        "-macalg", "sha1",
        "-passout", f"pass:{password}",
    ])

    return {
        "safe_name": safe_name,
        "common_name": common_name,
        "key_type": key_size,
        "key_label": VALID_P12_KEY_TYPES[key_size],
        "key_path": key_path,
        "cert_path": cert_path,
        "p12_path": p12_path,
        "p12_compat_path": p12_compat_path,
        "output_dir": output_dir_raw,
        "output_dir_resolved": output_dir,
    }


def generate_ssh_key_for_user(username, key_type):
    username = clean_username(username)
    key_type = clean_ssh_key_type(key_type)

    users = parse_passwd()
    user = users.get(username)
    if not user:
        raise ValueError("Utilisateur introuvable")

    if is_protected_user(user):
        raise PermissionError("Utilisateur système protégé")

    home = str(user.get("home") or "").strip()
    if not home or not home.startswith("/"):
        raise ValueError("Home utilisateur invalide")

    key_dir = os.path.join(home, SSH_KEY_DIR_NAME)
    private_key = os.path.join(key_dir, username)
    public_key = f"{private_key}.pub"

    # Le dossier est créé sur la vraie racine hôte, même si HOST_ROOT est utilisé.
    os.makedirs(host_file(key_dir), mode=0o700, exist_ok=True)

    # On remplace proprement l'ancienne clé du même nom, sinon ssh-keygen demande confirmation.
    for candidate in (host_file(private_key), host_file(public_key)):
        try:
            if os.path.exists(candidate):
                os.remove(candidate)
        except OSError as exc:
            raise RuntimeError(f"Impossible de remplacer {candidate} : {exc}") from exc

    ssh_keygen = host_command(["/usr/bin/ssh-keygen", "/bin/ssh-keygen"])
    key_info = VALID_SSH_KEY_TYPES[key_type]
    comment = f"{username}@{system_hostname()}"

    cmd = [
        ssh_keygen,
        "-q",
        *key_info["cmd"],
        "-f", private_key,
        "-N", "",
        "-C", comment,
    ]
    keygen_out = run_host(cmd)

    chmod = host_command(["/usr/bin/chmod", "/bin/chmod"])
    chown = host_command(["/usr/bin/chown", "/bin/chown"])

    outputs = [keygen_out]
    outputs.append(run_host([chmod, "700", key_dir]))
    outputs.append(run_host([chmod, "600", private_key]))
    outputs.append(run_host([chmod, "644", public_key]))
    outputs.append(run_host([chown, "-R", f"{user['uid']}:{user['gid']}", key_dir]))

    fingerprint = ""
    try:
        fp = run_host([ssh_keygen, "-lf", public_key])
        fingerprint = fp.get("stdout", "")
    except Exception:
        fingerprint = ""

    return {
        "user": username,
        "home": home,
        "key_type": key_type,
        "key_label": key_info["label"],
        "key_dir": key_dir,
        "private_key": private_key,
        "public_key": public_key,
        "fingerprint": fingerprint,
        "outputs": outputs,
    }


def parse_optional_id(value, label):
    if value is None:
        return None

    value = str(value).strip()
    if value == "":
        return None

    try:
        n = int(value)
    except ValueError:
        raise ValueError(f"{label} invalide")

    if n <= 0 or n > 60000:
        raise ValueError(f"{label} hors plage (1-60000)")

    return n


def parse_passwd():
    users = {}

    for line in read_text_lines(PASSWD_FILE):
        if not line or line.startswith("#"):
            continue

        parts = line.split(":")
        if len(parts) < 7:
            continue

        name, _x, uid, gid, gecos, home, shell = parts[:7]

        try:
            uid_i = int(uid)
            gid_i = int(gid)
        except ValueError:
            continue

        users[name] = {
            "name": name,
            "uid": uid_i,
            "gid": gid_i,
            "gecos": gecos,
            "home": home,
            "shell": shell,
        }

    return users


def parse_groups():
    groups_by_gid = {}
    groups_by_name = {}

    for line in read_text_lines(GROUP_FILE):
        if not line or line.startswith("#"):
            continue

        parts = line.split(":")
        if len(parts) < 4:
            continue

        name, _x, gid, members = parts[:4]

        try:
            gid_i = int(gid)
        except ValueError:
            continue

        item = {
            "name": name,
            "gid": gid_i,
            "members": [m for m in members.split(",") if m],
        }

        groups_by_gid[gid_i] = item
        groups_by_name[name] = item

    return groups_by_gid, groups_by_name


def parse_shadow_state():
    states = {}

    for line in read_text_lines(SHADOW_FILE):
        if not line or line.startswith("#"):
            continue

        parts = line.split(":")
        if len(parts) < 2:
            continue

        name, value = parts[0], parts[1]

        if value == "":
            state = "sans mot de passe"
        elif value.startswith("!"):
            state = "verrouillé"
        elif value.startswith("*"):
            state = "désactivé"
        else:
            state = "mot de passe défini"

        states[name] = state

    return states


def is_human_uid(uid):
    return uid == 0 or (NORMAL_UID_MIN <= uid < NORMAL_UID_MAX)


def is_protected_user(user):
    if not user:
        return True

    if user["name"] == "root":
        return True

    if PROTECT_SYSTEM_USERS and user["uid"] < NORMAL_UID_MIN:
        return True

    return False


def host_file(path_inside_host):
    if HOST_ROOT == "/":
        return path_inside_host
    return os.path.join(HOST_ROOT, path_inside_host.lstrip("/"))


def host_command(candidates):
    for cmd in candidates:
        if os.path.exists(host_file(cmd)):
            return cmd
    return candidates[0]


def run_host(cmd, stdin=None):
    """
    Lance une commande système.

    En host pur :
      /usr/sbin/usermod ...

    Si tu mets volontairement HOST_ROOT=/autre/racine dans la conf :
      chroot /autre/racine /usr/sbin/usermod ...
    """
    if not ALLOW_ACTIONS:
        raise RuntimeError("Actions désactivées dans users.conf")

    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise RuntimeError(
            "Le service Flask doit tourner en root pour gérer les utilisateurs Linux "
            "(/etc/passwd, /etc/shadow, useradd, usermod, chpasswd)."
        )

    if not os.path.isdir(HOST_ROOT):
        raise RuntimeError(f"HOST_ROOT introuvable : {HOST_ROOT}")

    if HOST_ROOT == "/":
        final_cmd = cmd
    else:
        final_cmd = ["chroot", HOST_ROOT] + cmd

    p = subprocess.run(
        final_cmd,
        input=stdin,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()

    if p.returncode != 0:
        msg = err or out or f"Commande échouée : {' '.join(final_cmd)}"
        raise RuntimeError(msg)

    return {
        "cmd": " ".join(final_cmd),
        "stdout": out,
        "stderr": err,
    }


def find_host_command(candidates):
    for cmd in candidates:
        if os.path.exists(host_file(cmd)):
            return cmd
    return ""


def sync_samba_password_if_available(username, password):
    """Synchronise le mot de passe Samba au moment où le mot de passe Linux est saisi.

    Linux ne permet pas de relire le mot de passe en clair plus tard. Le bon
    endroit pour aligner Samba est donc le module Utilisateurs, au moment de la
    création du compte ou du changement de mot de passe. Si Samba n'est pas
    installé, on n'échoue pas l'action Linux : l'utilisateur pourra simplement
    redéfinir le mot de passe après installation de Samba.
    """
    if not password:
        return None

    smbpasswd = find_host_command(["/usr/bin/smbpasswd", "/usr/sbin/smbpasswd"])
    if not smbpasswd:
        return {
            "cmd": "smbpasswd",
            "stdout": "Samba non installé : synchronisation du mot de passe SMB ignorée.",
            "stderr": "",
        }

    try:
        add_out = run_host([smbpasswd, "-a", "-s", username], stdin=f"{password}\n{password}\n")
        enable_out = run_host([smbpasswd, "-e", username])
        return {
            "cmd": f"{smbpasswd} -a -s {username} && {smbpasswd} -e {username}",
            "stdout": "Mot de passe Samba synchronisé avec le mot de passe Linux.",
            "stderr": "\n".join(x.get("stderr", "") for x in (add_out, enable_out) if x.get("stderr")),
        }
    except Exception as exc:
        return {
            "cmd": f"{smbpasswd} -a -s {username}",
            "stdout": "",
            "stderr": f"⚠ Mot de passe Linux changé, mais synchronisation Samba impossible : {exc}",
        }


def require_action_allowed():
    if not ALLOW_ACTIONS:
        conf = CONFIG_FILE_USED or "users.conf"
        return jsonify({"error": f"Actions désactivées dans {conf}"}), 403
    return None


def require_not_protected(username):
    users = parse_passwd()
    user = users.get(username)

    if user and is_protected_user(user):
        return False, "Utilisateur système protégé"

    return True, None


def ensure_group_for_gid(username, gid):
    """
    Retourne un nom de groupe utilisable pour le GID demandé.

    Logique NAS simple :
    - si un groupe avec ce GID existe déjà : on l'utilise ;
    - sinon si le groupe privé du user existe : on change son GID ;
    - sinon on crée un groupe privé du même nom que le user.
    """
    groups_by_gid, groups_by_name = parse_groups()

    existing_target = groups_by_gid.get(gid)
    if existing_target:
        return existing_target["name"], []

    outputs = []
    groupmod = host_command(["/usr/sbin/groupmod", "/usr/bin/groupmod"])
    groupadd = host_command(["/usr/sbin/groupadd", "/usr/bin/groupadd"])

    if username in groups_by_name:
        outputs.append(run_host([groupmod, "-g", str(gid), username]))
        return username, outputs

    outputs.append(run_host([groupadd, "-g", str(gid), username]))
    return username, outputs


def set_user_primary_gid(username, gid):
    """
    Change le GID principal du user.
    Si le GID n'existe pas, on crée/modifie le groupe privé du user.
    """
    outputs = []
    usermod = host_command(["/usr/sbin/usermod", "/usr/bin/usermod"])

    group_name, group_outputs = ensure_group_for_gid(username, gid)
    outputs.extend(group_outputs)

    outputs.append(run_host([usermod, "-g", group_name, username]))
    return outputs


def set_user_uid(username, uid):
    usermod = host_command(["/usr/sbin/usermod", "/usr/bin/usermod"])
    return [run_host([usermod, "-u", str(uid), username])]


def users_backup_file():
    return Path(nas_conf_file(USERS_BACKUP_NAME))


def validate_restore_shell(shell):
    shell = str(shell or DEFAULT_SHELL).strip()
    if shell in ALLOWED_SHELLS:
        return shell
    if not shell.startswith("/") or ":" in shell or any(ch.isspace() for ch in shell):
        raise ValueError(f"Shell invalide : {shell}")
    return shell


def backup_users_to_conf():
    users = parse_passwd()
    backup_users = [
        user for user in users.values()
        if is_human_uid(user["uid"]) and not is_protected_user(user)
    ]
    backup_users.sort(key=lambda item: (item["uid"], item["name"]))

    lines = [
        "# users_backup.conf - sauvegarde portable des utilisateurs Linux Yoleo",
        "# Aucun mot de passe ni hash shadow n'est sauvegarde ici.",
        "# Restore : cree/met a jour les comptes avec nom, UID, GID, home et shell.",
        "",
    ]

    for user in backup_users:
        name = user["name"]
        lines.extend([
            f"[user:{name}]",
            f"name={name}",
            f"uid={user['uid']}",
            f"gid={user['gid']}",
            f"home={user.get('home') or f'/home/{name}'}",
            f"shell={user.get('shell') or DEFAULT_SHELL}",
            "",
        ])

    path = users_backup_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    try:
        path.chmod(0o644)
    except OSError:
        pass

    return {
        "path": str(path),
        "count": len(backup_users),
        "users": [user["name"] for user in backup_users],
    }


def load_users_backup_entries():
    path = users_backup_file()
    if not path.exists():
        raise FileNotFoundError(f"{path} introuvable")

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(path, encoding="utf-8")

    entries = []
    for section in parser.sections():
        if not section.startswith("user:"):
            continue
        raw_name = parser.get(section, "name", fallback=section.split(":", 1)[1]).strip()
        name = clean_username(raw_name)
        try:
            uid = int(parser.get(section, "uid").strip())
            gid = int(parser.get(section, "gid").strip())
        except Exception as exc:
            raise ValueError(f"UID/GID invalide dans {section}") from exc
        if uid <= 0 or uid > 60000 or gid <= 0 or gid > 60000:
            raise ValueError(f"UID/GID hors plage dans {section}")
        home = parser.get(section, "home", fallback=f"/home/{name}").strip() or f"/home/{name}"
        if not home.startswith("/") or "\x00" in home:
            raise ValueError(f"Home invalide dans {section}")
        shell = validate_restore_shell(parser.get(section, "shell", fallback=DEFAULT_SHELL))
        entries.append({
            "name": name,
            "uid": uid,
            "gid": gid,
            "home": home,
            "shell": shell,
        })

    if not entries:
        raise ValueError(f"Aucun utilisateur restaurable dans {path}")
    entries.sort(key=lambda item: (item["uid"], item["name"]))
    return entries


def restore_user_entry(entry):
    username = entry["name"]
    uid = entry["uid"]
    gid = entry["gid"]
    home = entry["home"]
    shell = entry["shell"]

    current_users = parse_passwd()
    current = current_users.get(username)
    if username == "root" or (current and is_protected_user(current)):
        return "skipped", [{"stdout": f"{username} ignore : utilisateur protege."}]
    if PROTECT_SYSTEM_USERS and uid < NORMAL_UID_MIN:
        return "skipped", [{"stdout": f"{username} ignore : UID systeme protege ({uid})."}]

    outputs = []
    if not current:
        useradd = host_command(["/usr/sbin/useradd", "/usr/bin/useradd"])
        group_name, group_outputs = ensure_group_for_gid(username, gid)
        outputs.extend(group_outputs)
        cmd = [useradd, "-m", "-u", str(uid), "-g", group_name, "-s", shell, "-d", home, username]
        outputs.append(run_host(cmd))
        return "created", outputs

    ok, msg = require_not_protected(username)
    if not ok:
        return "skipped", [{"stdout": msg or f"{username} ignore : utilisateur protege."}]

    if current["gid"] != gid:
        outputs.extend(set_user_primary_gid(username, gid))
    if current["uid"] != uid:
        outputs.extend(set_user_uid(username, uid))
    if current.get("home") != home:
        usermod = host_command(["/usr/sbin/usermod", "/usr/bin/usermod"])
        outputs.append(run_host([usermod, "-d", home, username]))
    if current.get("shell") != shell:
        usermod = host_command(["/usr/sbin/usermod", "/usr/bin/usermod"])
        outputs.append(run_host([usermod, "-s", shell, username]))

    if not outputs:
        return "skipped", [{"stdout": f"{username} deja conforme."}]
    return "updated", outputs


def restore_users_from_conf():
    entries = load_users_backup_entries()
    summary = {"created": [], "updated": [], "skipped": [], "errors": [], "outputs": []}

    for entry in entries:
        username = entry["name"]
        try:
            status, outputs = restore_user_entry(entry)
            summary[status].append(username)
            summary["outputs"].extend(outputs)
        except Exception as exc:
            summary["errors"].append({"user": username, "error": str(exc)})

    return summary


# --- ROUTES PAGE ---
USER_TABS = {"general", "p12"}
USER_TAB_ALIASES = {
    "": "general",
    "user": "general",
    "users": "general",
    "utilisateurs": "general",
    "linux": "general",
    "general": "general",
    "ssh": "general",
    "p12": "p12",
}


def normalize_user_tab(value):
    tab = str(value or "general").strip().lower().replace("-", "_")
    return USER_TAB_ALIASES.get(tab, tab if tab in USER_TABS else "general")


USERS_SECTION_TITLES = {
    "general": ("Utilisateur Linux", "Comptes Linux, UID/GID, mots de passe et verrouillage"),
    "p12": ("Certificats P12", "Génération P12 avec dossier de sortie choisi"),
}


def _users_page_context(section="general"):
    active_tab = normalize_user_tab(section or request.args.get("tab", "general"))
    users_current_title = USERS_SECTION_TITLES.get(active_tab, USERS_SECTION_TITLES["general"])

    return {
        "active_user_tab": active_tab,
        "users_current_title": users_current_title,
        "host_root": HOST_ROOT,
        "passwd_file": PASSWD_FILE,
        "config_file": CONFIG_FILE_USED or nas_conf_file(CONF_NAME) + " introuvable",
        "command_mode": "direct" if HOST_ROOT == "/" else f"chroot {HOST_ROOT}",
        "effective_uid": os.geteuid() if hasattr(os, "geteuid") else None,
        "allow_actions": ALLOW_ACTIONS,
        "allowed_shells": ALLOWED_SHELLS,
        "default_shell": DEFAULT_SHELL,
        "ssh_key_types": {k: v["label"] for k, v in VALID_SSH_KEY_TYPES.items()},
        "p12_key_types": VALID_P12_KEY_TYPES,
        "p12_output_dir": P12_OUTPUT,
        "p12_output_dir_resolved": current_p12_output_resolved(),
        "p12_config_file": CONFIG_FILE_USED or nas_conf_file(CONF_NAME) + " introuvable",
    }


def render_users_page(section="general"):
    return render_template("users.html", **_users_page_context(section))


def render_system_users_page(section="general"):
    active_tab = normalize_user_tab(section or request.args.get("tab", "general"))
    template_name = "system_p12.html" if active_tab == "p12" else "system_users.html"

    # Important : ne pas forcer une icône ici.
    # Le template global menu.html sait déjà retrouver l’icône de la page active
    # depuis le vrai menu latéral conf/menu/... . Comme ça, si l’icône du menu
    # est modifiée dans l’interface, le titre de page reprend automatiquement
    # exactement la même icône.
    return render_template(
        template_name,
        **_users_page_context(active_tab),
        active_system_tab="users",
    )


@users_bp.route("/users")
@users_bp.route("/users/linux")
def index():
    requested = request.args.get("tab")
    if requested:
        return render_users_page(requested)
    return render_users_page("general")


@users_bp.route("/users/ssh")
def show_ssh():
    """Route conservée pour ouvrir le générateur SSH dans une petite popup/iframe."""
    raw_username = str(request.args.get("user", "") or "").strip()
    target_user = None
    ssh_error = ""

    if not raw_username:
        ssh_error = "Aucun utilisateur reçu. Ouvre cette fenêtre depuis le bouton SSH d'une ligne utilisateur."
    elif not valid_username(raw_username):
        ssh_error = "Nom utilisateur invalide."
    else:
        all_users = parse_passwd()
        candidate = all_users.get(raw_username)
        if not candidate:
            ssh_error = "Utilisateur introuvable."
        elif is_protected_user(candidate):
            ssh_error = "Utilisateur système protégé."
        else:
            target_user = {
                "name": raw_username,
                "home": candidate.get("home") or f"/home/{raw_username}",
            }

    return render_template(
        "users_ssh_popup.html",
        target_user=target_user,
        target_username=raw_username,
        ssh_error=ssh_error,
        allow_actions=ALLOW_ACTIONS,
        ssh_key_types={k: v["label"] for k, v in VALID_SSH_KEY_TYPES.items()},
    )


@users_bp.route("/users/p12")
def show_p12():
    return render_users_page("p12")


@users_bp.route("/system/users")
def system_users_index():
    """Nouvelle page Utilisateur Linux sous /system/users, sans reprendre le template /users."""
    return render_system_users_page("general")


@users_bp.route("/system/p12")
def system_p12_index():
    """Nouvelle page Certificats P12 sous /system/p12, avec une trame système propre."""
    return render_system_users_page("p12")


# --- API ---
@users_bp.route("/api/users/list", methods=["GET"])
def api_list_users():
    users = parse_passwd()
    groups_by_gid, _groups_by_name = parse_groups()
    shadow = parse_shadow_state()

    result = []
    for name, user in users.items():
        uid = user["uid"]
        gid = user["gid"]

        if not is_human_uid(uid):
            continue

        group = groups_by_gid.get(gid, {})
        protected = is_protected_user(user)

        result.append({
            "name": name,
            "uid": uid,
            "gid": gid,
            "group": group.get("name", str(gid)),
            "gecos": user.get("gecos", ""),
            "home": user.get("home", ""),
            "shell": user.get("shell", ""),
            "password_state": shadow.get(name, "?"),
            "protected": protected,
        })

    result.sort(key=lambda x: (x["uid"], x["name"]))

    return jsonify({
        "status": "ok",
        "host_root": HOST_ROOT,
        "passwd_file": PASSWD_FILE,
        "config_file": CONFIG_FILE_USED,
        "command_mode": "direct" if HOST_ROOT == "/" else f"chroot {HOST_ROOT}",
        "effective_uid": os.geteuid() if hasattr(os, "geteuid") else None,
        "items": result,
    })


@users_bp.route("/api/users/ssh/generate", methods=["POST"])
def api_generate_ssh_key():
    deny = require_action_allowed()
    if deny:
        return deny

    data = get_json_payload()

    try:
        info = generate_ssh_key_for_user(
            data.get("user"),
            data.get("key_type"),
        )

        return jsonify({
            "status": "ok",
            "message": f"Clé SSH générée pour {info['user']} : {info['key_label']}",
            **info,
        })

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@users_bp.route("/api/users/p12/browse", methods=["GET"])
def api_browse_p12_dirs():
    path = request.args.get("path", "")

    try:
        return jsonify({
            "status": "ok",
            **browse_directories(path),
        })
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@users_bp.route("/api/users/p12/output", methods=["POST"])
def api_set_p12_output():
    deny = require_action_allowed()
    if deny:
        return deny

    data = get_json_payload()

    try:
        output_dir_raw, output_dir = validate_p12_output_dir(data.get("p12_output_dir", ""))
        os.makedirs(output_dir, exist_ok=True)
        if not os.path.isdir(output_dir):
            raise ValueError(f"Dossier de sortie P12 invalide : {output_dir}")

        global P12_OUTPUT
        P12_OUTPUT = output_dir_raw
        _write_conf_value("P12_OUTPUT", output_dir_raw)

        return jsonify({
            "status": "ok",
            "message": "Dossier de sortie P12 enregistré.",
            "output_dir": output_dir_raw,
            "output_dir_resolved": output_dir,
            "config_file": CONFIG_FILE_USED,
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@users_bp.route("/api/users/p12/generate", methods=["POST"])
def api_generate_p12():
    deny = require_action_allowed()
    if deny:
        return deny

    data = get_json_payload()

    try:
        info = generate_p12_files({
            "p12_name": data.get("p12_name", ""),
            "p12_url": data.get("p12_url", ""),
            "p12_pwd": data.get("p12_pwd", ""),
            "key_size": data.get("key_size", "3072"),
            "p12_output_dir": data.get("p12_output_dir", ""),
        })

        return jsonify({
            "status": "ok",
            "message": f"Généré : {info['safe_name']}.p12 + {info['safe_name']}_comp.p12 ({info['key_label']})",
            "config_file": CONFIG_FILE_USED,
            **info,
        })

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except subprocess.CalledProcessError as e:
        details = (e.stderr or e.stdout or str(e)).strip()
        return jsonify({"error": f"OpenSSL a renvoyé une erreur : {details}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@users_bp.route("/api/users/action", methods=["POST"])
def api_user_action():
    deny = require_action_allowed()
    if deny:
        return deny

    data = get_json_payload()
    action = str(data.get("action", "")).strip().lower()

    try:
        if action == "add":
            username = clean_username(data.get("user"))
            password = data.get("password")
            shell = str(data.get("shell") or DEFAULT_SHELL).strip()
            raw_home = str(data.get("home") or "").strip()
            home = clean_home_path(raw_home) if raw_home else ""
            uid = parse_optional_id(data.get("uid"), "UID")
            gid = parse_optional_id(data.get("gid"), "GID")

            if shell not in ALLOWED_SHELLS:
                return jsonify({"error": "Shell non autorisé"}), 400

            users = parse_passwd()
            outputs = []

            if username not in users:
                if uid is None and gid is None:
                    adduser = host_command(["/usr/sbin/adduser", "/usr/sbin/useradd"])

                    if adduser.endswith("adduser"):
                        cmd = [
                            adduser,
                            "--disabled-password",
                            "--gecos", "",
                            "--shell", shell,
                        ]
                        if home:
                            cmd.extend(["--home", home])
                        cmd.append(username)
                    else:
                        cmd = [
                            adduser,
                            "-m",
                            "-s", shell,
                        ]
                        if home:
                            cmd.extend(["-d", home])
                        cmd.append(username)

                    outputs.append(run_host(cmd))

                else:
                    useradd = host_command(["/usr/sbin/useradd", "/usr/bin/useradd"])

                    if gid is None and uid is not None:
                        gid = uid

                    group_name, group_outputs = ensure_group_for_gid(username, gid)
                    outputs.extend(group_outputs)

                    cmd = [useradd, "-m", "-s", shell]
                    if home:
                        cmd.extend(["-d", home])
                    cmd.extend(["-g", group_name])
                    if uid is not None:
                        cmd.extend(["-u", str(uid)])
                    cmd.append(username)
                    outputs.append(run_host(cmd))

            else:
                ok, msg = require_not_protected(username)
                if not ok:
                    return jsonify({"error": msg}), 403

            if password:
                chpasswd = host_command(["/usr/sbin/chpasswd", "/usr/bin/chpasswd"])
                outputs.append(run_host([chpasswd], stdin=f"{username}:{password}\n"))
                samba_sync = sync_samba_password_if_available(username, password)
                if samba_sync:
                    outputs.append(samba_sync)

            return jsonify({"status": "ok", "outputs": outputs})

        elif action == "backup_users":
            info = backup_users_to_conf()
            return jsonify({
                "status": "ok",
                "message": f"Sauvegarde utilisateurs : {info['count']} compte(s) dans {info['path']}",
                "backup_file": info["path"],
                "users": info["users"],
            })

        elif action == "restore_users":
            info = restore_users_from_conf()
            message = (
                "Restauration utilisateurs : "
                f"{len(info['created'])} cree(s), "
                f"{len(info['updated'])} mis a jour, "
                f"{len(info['skipped'])} ignore(s), "
                f"{len(info['errors'])} erreur(s)."
            )
            payload = {
                "status": "ok",
                "message": message if not info["errors"] else None,
                "warning": message if info["errors"] else None,
                **info,
            }
            return jsonify(payload)

        elif action == "setids":
            username = clean_username(data.get("user"))
            uid = parse_optional_id(data.get("uid"), "UID")
            gid = parse_optional_id(data.get("gid"), "GID")

            if uid is None and gid is None:
                return jsonify({"error": "Aucun UID/GID demandé"}), 400

            users = parse_passwd()
            user = users.get(username)

            if not user:
                return jsonify({"error": "Utilisateur introuvable"}), 404

            ok, msg = require_not_protected(username)
            if not ok:
                return jsonify({"error": msg}), 403

            outputs = []

            # GID d'abord : groupe principal prêt avant le changement éventuel d'UID.
            if gid is not None and gid != user["gid"]:
                outputs.extend(set_user_primary_gid(username, gid))

            if uid is not None and uid != user["uid"]:
                outputs.extend(set_user_uid(username, uid))

            if not outputs:
                return jsonify({"status": "ok", "message": "Aucun changement nécessaire"})

            return jsonify({
                "status": "ok",
                "outputs": outputs,
                "warning": "UID/GID modifiés. Les propriétaires des fichiers existants ne sont pas corrigés automatiquement.",
            })

        elif action == "setshell":
            username = clean_username(data.get("user"))
            shell = str(data.get("shell") or "").strip()

            if shell not in ALLOWED_SHELLS:
                return jsonify({"error": "Shell non autorise"}), 400

            users = parse_passwd()
            user = users.get(username)

            if not user:
                return jsonify({"error": "Utilisateur introuvable"}), 404

            ok, msg = require_not_protected(username)
            if not ok:
                return jsonify({"error": msg}), 403

            if shell == user.get("shell"):
                return jsonify({"status": "ok", "message": "Aucun changement necessaire"})

            usermod = host_command(["/usr/sbin/usermod", "/usr/bin/usermod"])
            out = run_host([usermod, "-s", shell, username])
            return jsonify({
                "status": "ok",
                "message": f"Shell de {username} modifie : {shell}",
                "output": out,
            })

        elif action == "sethome":
            username = clean_username(data.get("user"))
            home = clean_home_path(data.get("home"))

            users = parse_passwd()
            user = users.get(username)

            if not user:
                return jsonify({"error": "Utilisateur introuvable"}), 404

            ok, msg = require_not_protected(username)
            if not ok:
                return jsonify({"error": msg}), 403

            if home == user.get("home"):
                return jsonify({"status": "ok", "message": "Aucun changement nécessaire"})

            usermod = host_command(["/usr/sbin/usermod", "/usr/bin/usermod"])
            out = run_host([usermod, "-d", home, username])
            return jsonify({
                "status": "ok",
                "message": f"Home de {username} modifié : {home}. Les fichiers n'ont pas été déplacés.",
                "output": out,
            })

        elif action == "setpass":
            username = clean_username(data.get("user"))
            password = data.get("password")

            if not password:
                return jsonify({"error": "Mot de passe vide"}), 400

            if username != "root":
                ok, msg = require_not_protected(username)
                if not ok:
                    return jsonify({"error": msg}), 403

            if username not in parse_passwd():
                return jsonify({"error": "Utilisateur introuvable"}), 404

            chpasswd = host_command(["/usr/sbin/chpasswd", "/usr/bin/chpasswd"])
            out = run_host([chpasswd], stdin=f"{username}:{password}\n")
            samba_sync = sync_samba_password_if_available(username, password)
            return jsonify({"status": "ok", "output": out, "samba_sync": samba_sync})

        elif action == "lock":
            username = clean_username(data.get("user"))

            ok, msg = require_not_protected(username)
            if not ok:
                return jsonify({"error": msg}), 403

            if username not in parse_passwd():
                return jsonify({"error": "Utilisateur introuvable"}), 404

            passwd_cmd = host_command(["/usr/bin/passwd", "/usr/sbin/passwd"])
            out = run_host([passwd_cmd, "-l", username])
            return jsonify({"status": "ok", "output": out})

        elif action == "unlock":
            username = clean_username(data.get("user"))

            ok, msg = require_not_protected(username)
            if not ok:
                return jsonify({"error": msg}), 403

            if username not in parse_passwd():
                return jsonify({"error": "Utilisateur introuvable"}), 404

            passwd_cmd = host_command(["/usr/bin/passwd", "/usr/sbin/passwd"])
            out = run_host([passwd_cmd, "-u", username])
            return jsonify({"status": "ok", "output": out})

        elif action == "remove":
            username = clean_username(data.get("user"))

            ok, msg = require_not_protected(username)
            if not ok:
                return jsonify({"error": msg}), 403

            if username not in parse_passwd():
                return jsonify({"error": "Utilisateur déjà absent"}), 404

            deluser = host_command(["/usr/sbin/deluser", "/usr/sbin/userdel"])

            # Important : jamais --remove-home.
            out = run_host([deluser, username])
            return jsonify({"status": "ok", "output": out})

        else:
            return jsonify({"error": "Action inconnue"}), 400

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
