#!/bin/bash
set -Eeuo pipefail

# YOLEO_SCRIPT_VERSION=2.0.3
# YOLEO_SCRIPT_DATE=2026-10-28

# ============================================================
# System Flask / Gunicorn - mode hôte Debian
#
# Chemins AUTO :
#   Si ce script est ici : /chemin/dockers/system/system.sh
#   Alors :
#     APP_DIR     = /chemin/dockers/system
#     DOCKERS_DIR = /chemin/dockers
#     CONF_DIR    = ../conf
#     LOG_DIR     = /var/log/flask-system
#
# Le fichier ENV généré par -install garde les confs en chemins relatifs
# (../conf) et met uniquement les logs dans un chemin Linux standard.
#
# Commandes :
#   bash ./system.sh -install
#   bash ./system.sh -start
#   bash ./system.sh -stop
#   bash ./system.sh -restart
#   bash ./system.sh -status
#   bash ./system.sh -logs
#   bash ./system.sh -routes
# ============================================================

SERVICE_NAME="${SERVICE_NAME:-flask-system.service}"
SERVICE_FILE_AUTO=1
if [ -n "${SERVICE_FILE:-}" ]; then
    SERVICE_FILE_AUTO=0
fi
SERVICE_FILE="${SERVICE_FILE:-/etc/systemd/system/${SERVICE_NAME}}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"

# Chemin réel du script, même s'il est appelé via un lien symbolique.
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || realpath "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd -P)"
AUTO_DOCKERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"

# Base auto : le dossier parent de /system.
# Exemple : /dockers/system/system.sh => DOCKERS_DIR=/dockers
DOCKERS_DIR="${DOCKERS_DIR:-$AUTO_DOCKERS_DIR}"
APP_DIR="${APP_DIR:-$SCRIPT_DIR}"
APP_FILE="${APP_FILE:-${APP_DIR}/app.py}"
APP_MODULE="${APP_MODULE:-app:app}"

VENV_DIR="${VENV_DIR:-${APP_DIR}/.venv}"
REQ_FILE="${REQ_FILE:-${APP_DIR}/requirements.txt}"
WHEELHOUSE_DIR="${WHEELHOUSE_DIR:-${DOCKERS_DIR}/offline/wheels}"
OFFLINE_DEB_DIR="${OFFLINE_DEB_DIR:-${DOCKERS_DIR}/offline/debs}"
OFFLINE_APT_REQUESTED="${OFFLINE_APT_REQUESTED:-${DOCKERS_DIR}/offline/manifests/apt-requested.txt}"
OFFLINE_APT_MISSING="${OFFLINE_APT_MISSING:-${DOCKERS_DIR}/offline/manifests/apt-missing.txt}"
APT_OFFLINE_ONLY="${APT_OFFLINE_ONLY:-0}"
PIP_OFFLINE="${PIP_OFFLINE:-auto}"
INIT_DIR="${INIT_DIR:-${DOCKERS_DIR}/init}"
INTEGRITY_MANIFEST="${INTEGRITY_MANIFEST:-${INIT_DIR}/system.sha256}"
INTEGRITY_ARCHIVE="${INTEGRITY_ARCHIVE:-${INIT_DIR}/system.tar.gz}"
INTEGRITY_ENABLED="${INTEGRITY_ENABLED:-1}"
INTEGRITY_ROOT_DIR="${INTEGRITY_ROOT_DIR:-${DOCKERS_DIR}}"
INTEGRITY_INCLUDE_DIRS="${INTEGRITY_INCLUDE_DIRS:-system scripts bin}"

CONF_DIR="${CONF_DIR:-../conf}"
ENV_FILE="${ENV_FILE:-${CONF_DIR}/.env}"
SECRET_FILE="${SECRET_FILE:-${CONF_DIR}/flask_system.secret_key}"

LOG_DIR="${LOG_DIR:-/var/log/flask-system}"
LOG_FILE="${LOG_FILE:-${LOG_DIR}/flask_system.log}"
ACCESS_LOG="${ACCESS_LOG:-${LOG_DIR}/flask_system_access.log}"
PID_FILE="${PID_FILE:-/run/flask_system.pid}"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-12345}"
WORKERS="${WORKERS:-2}"
THREADS="${THREADS:-4}"
WORKER_CLASS="${WORKER_CLASS:-gthread}"
TIMEOUT="${TIMEOUT:-120}"
IPV6_BIND="${IPV6_BIND:-auto}"

PY_BIN="${VENV_DIR}/bin/python"
PIP_BIN="${VENV_DIR}/bin/pip"

ACTION="${1:--status}"
ACTION="${ACTION#-}"
ACTION="${ACTION,,}"

# Convention portable : les chemins relatifs du script (../conf, .venv, etc.)
# sont résolus depuis le dossier système Flask, pas depuis le dossier courant
# depuis lequel l'utilisateur lance la commande.
cd "$APP_DIR" || {
    echo "ERREUR : impossible d'entrer dans APP_DIR : $APP_DIR" >&2
    exit 1
}

ensure_dirs() {
    mkdir -p "$CONF_DIR" "$LOG_DIR"
    touch "$LOG_FILE" 2>/dev/null || true
}

ensure_dirs

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

die() {
    log "ERREUR : $*"
    exit 1
}

integrity_enabled() {
    case "${INTEGRITY_ENABLED:-1}" in
        0|false|FALSE|no|NO|off|OFF)
            return 1
            ;;
    esac
    return 0
}

integrity_safe_rel_path() {
    local rel_path="$1"
    case "$rel_path" in
        ""|.|/*|../*|*"/../"*|*".."|*"/.."|./*|*"/./"*)
            return 1
            ;;
    esac
    return 0
}

integrity_safe_include_dir() {
    local include_dir="$1"
    case "$include_dir" in
        ""|.|/*|../*|*"/../"*|*".."|*"/.."|./*|*"/./"*)
            return 1
            ;;
    esac
    return 0
}

integrity_validate_scope() {
    local include_dir found=0

    [ -n "${INTEGRITY_ROOT_DIR:-}" ] || die "INTEGRITY_ROOT_DIR vide"
    [ -d "$INTEGRITY_ROOT_DIR" ] || die "racine integrite introuvable : $INTEGRITY_ROOT_DIR"

    for include_dir in $INTEGRITY_INCLUDE_DIRS; do
        found=1
        integrity_safe_include_dir "$include_dir" || die "dossier integrite refuse : $include_dir"
    done

    [ "$found" -eq 1 ] || die "INTEGRITY_INCLUDE_DIRS vide"
}

integrity_rel_in_scope() {
    local rel_path="$1"
    local include_dir

    for include_dir in $INTEGRITY_INCLUDE_DIRS; do
        integrity_safe_include_dir "$include_dir" || return 1
        case "$rel_path" in
            "$include_dir"|"$include_dir"/*)
                return 0
                ;;
        esac
    done

    return 1
}

integrity_active_rel_from_catalog() {
    local rel_path="$1"

    if integrity_rel_in_scope "$rel_path"; then
        printf '%s\n' "$rel_path"
        return 0
    fi

    # Compatibilite avec les anciens catalogues qui etaient relatifs a APP_DIR.
    if integrity_safe_rel_path "system/$rel_path"; then
        printf 'system/%s\n' "$rel_path"
        return 0
    fi

    return 1
}

integrity_rel_is_excluded() {
    local rel_path="$1"
    case "$rel_path" in
        */.git/*|*/.venv/*|*/__pycache__/*|*/.pytest_cache/*|*/node_modules/*|*/backups/*|*.pyc|*.pyo|*/.DS_Store|*/gunicorn.ctl)
            return 0
            ;;
    esac
    return 1
}

integrity_file_size() {
    local file_path="$1"
    stat -c '%s' "$file_path" 2>/dev/null || wc -c < "$file_path" | tr -d ' '
}

integrity_file_sha256() {
    local file_path="$1"
    local line
    line="$(sha256sum "$file_path" 2>/dev/null)" || return 1
    printf '%s\n' "${line%% *}"
}

integrity_file_matches() {
    local expected_sha="$1"
    local expected_size="$2"
    local file_path="$3"
    local actual_size actual_sha

    [ -f "$file_path" ] || return 1
    [ ! -L "$file_path" ] || return 1

    actual_size="$(integrity_file_size "$file_path")" || return 1
    [ "$actual_size" = "$expected_size" ] || return 1

    actual_sha="$(integrity_file_sha256 "$file_path")" || return 1
    [ "$actual_sha" = "$expected_sha" ]
}

integrity_archive_matches() {
    local archive_hash_file="${INTEGRITY_ARCHIVE}.sha256"
    local expected_hash expected_size _ actual_hash actual_size

    [ -f "$INTEGRITY_ARCHIVE" ] || die "archive integrite introuvable : $INTEGRITY_ARCHIVE"
    if [ ! -f "$archive_hash_file" ]; then
        log "ATTENTION : sha256 archive absent : $archive_hash_file"
        return 0
    fi

    read -r expected_hash expected_size _ < "$archive_hash_file" || die "sha256 archive illisible : $archive_hash_file"
    actual_hash="$(integrity_file_sha256 "$INTEGRITY_ARCHIVE")" || die "sha256 archive impossible : $INTEGRITY_ARCHIVE"
    actual_size="$(integrity_file_size "$INTEGRITY_ARCHIVE")" || die "taille archive impossible : $INTEGRITY_ARCHIVE"

    if [ "$expected_hash" != "$actual_hash" ] || [ "$expected_size" != "$actual_size" ]; then
        log "Archive de reference invalide : $INTEGRITY_ARCHIVE"
        log "  SHA256 attendu : $expected_hash"
        log "  SHA256 actuel  : $actual_hash"
        log "  Taille attendue: $expected_size octets"
        log "  Taille actuelle: $actual_size octets"
        log "Analyse detaillee des fichiers actifs et du contenu de l'archive :"
        integrity_show_differences || true
        die "archive integrite differente : $INTEGRITY_ARCHIVE"
    fi
}

integrity_show_differences() {
    integrity_validate_scope
    [ -f "$INTEGRITY_MANIFEST" ] || die "catalogue integrite introuvable : $INTEGRITY_MANIFEST"
    command -v python3 >/dev/null 2>&1 || die "python3 introuvable"

    INTEGRITY_ROOT_DIR="$INTEGRITY_ROOT_DIR" \
    INTEGRITY_INCLUDE_DIRS="$INTEGRITY_INCLUDE_DIRS" \
    INTEGRITY_MANIFEST="$INTEGRITY_MANIFEST" \
    INTEGRITY_ARCHIVE="$INTEGRITY_ARCHIVE" \
    python3 - <<'PYDIFF'
from pathlib import Path
import hashlib
import os
import tarfile

root = Path(os.environ["INTEGRITY_ROOT_DIR"]).resolve()
manifest = Path(os.environ["INTEGRITY_MANIFEST"]).resolve()
archive = Path(os.environ["INTEGRITY_ARCHIVE"]).resolve()
include_dirs = [x for x in os.environ["INTEGRITY_INCLUDE_DIRS"].split() if x]
include_names = set(include_dirs)
excluded_dirs = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules", "backups"}
excluded_files = {".DS_Store", "gunicorn.ctl"}


def sig_bytes(data):
    return len(data), hashlib.sha256(data).hexdigest()


def sig_file(path):
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            size += len(chunk)
            h.update(chunk)
    return size, h.hexdigest()


def active_rel(raw):
    rel = Path(raw.replace("\\", "/"))
    if rel.parts and rel.parts[0] in include_names:
        return rel.as_posix()
    return (Path("system") / rel).as_posix()


def wanted(path):
    rel = path.relative_to(root)
    if any(part in excluded_dirs for part in rel.parts):
        return False
    if path.name in excluded_files or path.suffix in {".pyc", ".pyo"}:
        return False
    return path.is_file() and not path.is_symlink()

records = {}
for line in manifest.read_text(encoding="utf-8").splitlines():
    if not line or line.startswith("#"):
        continue
    parts = line.rstrip("\r").split("\t")
    if len(parts) != 3:
        print(f"CATALOGUE INVALIDE : {line}")
        continue
    sha, size, rel = parts
    records[active_rel(rel)] = (int(size), sha)

active_files = {}
for dirname in include_dirs:
    base = root / dirname
    if not base.is_dir():
        continue
    for path in base.rglob("*"):
        if wanted(path):
            active_files[path.relative_to(root).as_posix()] = path

active_changes = []
for rel in sorted(set(records) | set(active_files)):
    expected = records.get(rel)
    path = active_files.get(rel)
    if expected is None:
        active_changes.append(("HORS CATALOGUE", rel))
    elif path is None:
        active_changes.append(("ABSENT", rel))
    elif sig_file(path) != expected:
        active_changes.append(("MODIFIE", rel))

print("========== FICHIERS ACTIFS / CATALOGUE ==========")
if active_changes:
    for state, rel in active_changes:
        print(f"{state:15} {rel}")
else:
    print("AUCUNE DIFFERENCE : les fichiers actifs correspondent au catalogue.")

print("\n========== ARCHIVE / CATALOGUE ==========")
archive_changes = []
if not archive.exists():
    print(f"ARCHIVE ABSENTE : {archive}")
else:
    try:
        members = {}
        with tarfile.open(archive, "r:gz") as tf:
            regular = [m for m in tf.getmembers() if m.isfile()]
            raw_names = [m.name.lstrip("./") for m in regular]
            legacy = bool(raw_names) and all(Path(n).parts[0] not in include_names for n in raw_names)
            for member, raw_name in zip(regular, raw_names):
                rel = (Path("system") / raw_name).as_posix() if legacy else Path(raw_name).as_posix()
                fh = tf.extractfile(member)
                if fh is None:
                    continue
                members[rel] = sig_bytes(fh.read())
        for rel in sorted(set(records) | set(members)):
            expected = records.get(rel)
            actual = members.get(rel)
            if expected is None:
                archive_changes.append(("HORS CATALOGUE", rel))
            elif actual is None:
                archive_changes.append(("ABSENT ARCHIVE", rel))
            elif actual != expected:
                archive_changes.append(("MODIFIE ARCHIVE", rel))
        if archive_changes:
            for state, rel in archive_changes:
                print(f"{state:17} {rel}")
        else:
            print("AUCUNE DIFFERENCE DE CONTENU : l'archive contient les bons fichiers.")
            print("Le SHA256 global peut differer uniquement parce que le fichier tar.gz a ete recree ou retouche.")
    except Exception as exc:
        print(f"ARCHIVE ILLISIBLE OU ENDOMMAGEE : {exc}")

print("\n========== RESUME ==========")
print(f"fichiers_actifs_differents={len(active_changes)}")
print(f"fichiers_archive_differents={len(archive_changes)}")
PYDIFF
}

integrity_replace_file() {
    local source_path="$1"
    local dest_path="$2"
    local dest_dir

    dest_dir="$(dirname "$dest_path")"
    mkdir -p "$dest_dir" || return 1

    if [ -d "$dest_path" ] && [ ! -L "$dest_path" ]; then
        rm -rf -- "$dest_path" || return 1
    else
        rm -f -- "$dest_path" || return 1
    fi

    cp -p -- "$source_path" "$dest_path"
}

integrity_restore_from_archive() {
    local rel_path="$1"
    local expected_sha="$2"
    local expected_size="$3"
    local dest_path="$4"
    local tmp_dir extracted_path

    [ -f "$INTEGRITY_ARCHIVE" ] || return 1
    command -v tar >/dev/null 2>&1 || return 1

    tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/yoleo-integrity.XXXXXX")" || return 1
    if ! tar -xzf "$INTEGRITY_ARCHIVE" -C "$tmp_dir" -- "$rel_path" 2>/dev/null; then
        if ! tar -xzf "$INTEGRITY_ARCHIVE" -C "$tmp_dir" -- "./$rel_path" 2>/dev/null; then
            rm -rf -- "$tmp_dir"
            return 1
        fi
    fi

    extracted_path="$tmp_dir/$rel_path"
    if ! integrity_file_matches "$expected_sha" "$expected_size" "$extracted_path"; then
        rm -rf -- "$tmp_dir"
        return 1
    fi

    integrity_replace_file "$extracted_path" "$dest_path"
    local rc=$?
    rm -rf -- "$tmp_dir"
    return "$rc"
}

integrity_check_and_restore() {
    local expected_sha expected_size rel_path extra
    local archive_rel active_rel active_file actual_file rel_actual include_dir include_path
    local checked=0 active_restored=0 extra_removed=0 failed=0 legacy_catalog=0
    declare -A expected_paths=()

    if ! integrity_enabled; then
        log "Controle integrite desactive (INTEGRITY_ENABLED=$INTEGRITY_ENABLED)"
        return 0
    fi

    integrity_validate_scope
    [ -f "$INTEGRITY_MANIFEST" ] || die "catalogue integrite introuvable : $INTEGRITY_MANIFEST"
    command -v sha256sum >/dev/null 2>&1 || die "sha256sum introuvable"
    integrity_archive_matches

    log "Controle integrite YoLeo : ROOT=$INTEGRITY_ROOT_DIR DIRS=$INTEGRITY_INCLUDE_DIRS ARCHIVE=$INTEGRITY_ARCHIVE"
    while IFS=$'\t' read -r expected_sha expected_size rel_path extra || [ -n "${expected_sha:-}" ]; do
        [ -n "${expected_sha:-}" ] || continue
        case "$expected_sha" in
            \#*) continue ;;
        esac
        expected_sha="${expected_sha%$'\r'}"
        expected_size="${expected_size%$'\r'}"
        rel_path="${rel_path%$'\r'}"
        extra="${extra:-}"
        extra="${extra%$'\r'}"

        if [ -z "${expected_size:-}" ] || [ -z "${rel_path:-}" ] || [ -n "${extra:-}" ]; then
            log "Catalogue integrite invalide, ligne ignoree : ${expected_sha:-}"
            failed=1
            continue
        fi

        if ! integrity_safe_rel_path "$rel_path"; then
            log "Catalogue integrite dangereux, chemin refuse : $rel_path"
            failed=1
            continue
        fi

        archive_rel="$rel_path"
        case "$rel_path" in
            system/*|scripts/*|offline/*|bin/*)
                if ! integrity_rel_in_scope "$rel_path"; then
                    log "Catalogue ancien hors nouveau périmètre, entrée ignorée : $rel_path"
                    continue
                fi
                ;;
        esac
        if ! active_rel="$(integrity_active_rel_from_catalog "$rel_path")"; then
            log "Catalogue integrite hors scope, chemin refuse : $rel_path"
            failed=1
            continue
        fi
        if [ "$active_rel" != "$rel_path" ]; then
            legacy_catalog=1
        fi

        checked=$((checked + 1))
        active_file="$INTEGRITY_ROOT_DIR/$active_rel"
        expected_paths["$active_rel"]=1

        if ! integrity_file_matches "$expected_sha" "$expected_size" "$active_file"; then
            log "Fichier systeme modifie ou absent, restauration : $active_rel"
            if {
                integrity_restore_from_archive "$active_rel" "$expected_sha" "$expected_size" "$active_file" || \
                { [ "$archive_rel" != "$active_rel" ] && integrity_restore_from_archive "$archive_rel" "$expected_sha" "$expected_size" "$active_file"; }
            } && integrity_file_matches "$expected_sha" "$expected_size" "$active_file"; then
                active_restored=$((active_restored + 1))
                log "Fichier systeme restaure depuis l'archive : $active_rel"
            else
                log "ERREUR : restauration systeme echouee : $active_rel"
                failed=1
            fi
        fi
    done < "$INTEGRITY_MANIFEST"

    for include_dir in $INTEGRITY_INCLUDE_DIRS; do
        integrity_safe_include_dir "$include_dir" || {
            log "Dossier integrite refuse : $include_dir"
            failed=1
            continue
        }
        if [ "$legacy_catalog" -eq 1 ] && [ "$include_dir" != "system" ]; then
            continue
        fi

        include_path="$INTEGRITY_ROOT_DIR/$include_dir"
        [ -d "$include_path" ] || continue

        while IFS= read -r -d '' actual_file; do
            rel_actual="${actual_file#$INTEGRITY_ROOT_DIR/}"
            if integrity_rel_is_excluded "$rel_actual"; then
                continue
            fi
            if [ -z "${expected_paths[$rel_actual]+x}" ]; then
                log "Fichier systeme hors catalogue supprime : $rel_actual"
                rm -f -- "$actual_file" || failed=1
                extra_removed=$((extra_removed + 1))
            fi
        done < <(find "$include_path" -type f -print0)
    done

    [ "$failed" -eq 0 ] || die "controle integrite echoue, Gunicorn ne sera pas lance"
    log "Controle integrite OK : ${checked} fichiers, systeme restaure=${active_restored}, fichiers hors catalogue supprimes=${extra_removed}"
}


integrity_add_reference() {
    [ -d "$APP_DIR" ] || die "APP_DIR absent : $APP_DIR"
    mkdir -p "$INIT_DIR"
    command -v python3 >/dev/null 2>&1 || die "python3 introuvable"

    integrity_validate_scope
    log "Mise a jour reference integrite : ROOT=$INTEGRITY_ROOT_DIR DIRS=$INTEGRITY_INCLUDE_DIRS INIT_DIR=$INIT_DIR"
    APP_DIR="$APP_DIR" \
    INIT_DIR="$INIT_DIR" \
    INTEGRITY_ROOT_DIR="$INTEGRITY_ROOT_DIR" \
    INTEGRITY_INCLUDE_DIRS="$INTEGRITY_INCLUDE_DIRS" \
    INTEGRITY_MANIFEST="$INTEGRITY_MANIFEST" \
    INTEGRITY_ARCHIVE="$INTEGRITY_ARCHIVE" \
    python3 - "$@" <<'PYADD'
from pathlib import Path
import hashlib
import os
import shutil
import sys
import tarfile
import tempfile

app_dir = Path(os.environ["APP_DIR"]).resolve()
root_dir = Path(os.environ["INTEGRITY_ROOT_DIR"]).resolve()
init_dir = Path(os.environ["INIT_DIR"]).resolve()
manifest = Path(os.environ["INTEGRITY_MANIFEST"]).resolve()
archive = Path(os.environ["INTEGRITY_ARCHIVE"]).resolve()
archive_sha = archive.with_name(archive.name + ".sha256")
include_dirs = [part for part in os.environ["INTEGRITY_INCLUDE_DIRS"].split() if part]
include_names = set(include_dirs)
known_top_dirs = {"system", "scripts", "offline", "bin"}

excluded_dirs = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules", "backups"}
excluded_files = {".DS_Store", "gunicorn.ctl"}
args = sys.argv[1:]


def die(message):
    print(f"ERREUR : {message}", file=sys.stderr)
    raise SystemExit(1)


def normalize_rel(raw):
    rel = Path(str(raw).replace("\\", "/"))
    if rel.is_absolute() or not rel.parts:
        die(f"chemin refuse : {raw}")
    if any(part in {"", ".", ".."} for part in rel.parts):
        die(f"chemin refuse : {raw}")
    if any(part in excluded_dirs for part in rel.parts):
        die(f"chemin runtime refuse : {raw}")
    if rel.parts[0] not in include_names:
        # Compatibilite : un ancien -add system_parts/x.py reste relatif au dossier system.
        rel = Path("system") / rel
    if rel.parts[0] not in include_names:
        die(f"chemin hors dossiers proteges : {raw}")
    return rel


def manifest_rel_to_active(raw):
    rel = Path(str(raw).replace("\\", "/"))
    if rel.is_absolute() or not rel.parts:
        die(f"chemin catalogue refuse : {raw}")
    if any(part in {"", ".", ".."} for part in rel.parts):
        die(f"chemin catalogue refuse : {raw}")
    if rel.parts[0] in include_names:
        return rel.as_posix(), False
    if rel.parts[0] in known_top_dirs:
        return None, False
    return (Path("system") / rel).as_posix(), True


def rel_for(path, root):
    return path.relative_to(root).as_posix()


def wanted_file(path, root):
    rel = path.relative_to(root)
    if any(part in excluded_dirs for part in rel.parts):
        return False
    if path.name in excluded_files:
        return False
    if path.suffix in {".pyc", ".pyo"}:
        return False
    return path.is_file() and not path.is_symlink()


def file_sig(path):
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def read_manifest():
    records = {}
    legacy = False
    scope_changed = False
    if not manifest.exists():
        return records, legacy
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.rstrip("\r").split("\t")
        if len(parts) != 3:
            die(f"ligne catalogue invalide : {line}")
        sha, size, rel = parts
        active_rel, was_legacy = manifest_rel_to_active(rel)
        if active_rel is None:
            scope_changed = True
            continue
        legacy = legacy or was_legacy
        records[active_rel] = (int(size), sha)
    return records, legacy, scope_changed


manifest_records, legacy_manifest, scope_changed = read_manifest()
source_files = {}
for include_dir in include_dirs:
    include_root = root_dir / include_dir
    if not include_root.exists():
        continue
    if not include_root.is_dir() or include_root.is_symlink():
        die(f"dossier protege invalide : {include_root}")
    for path in include_root.rglob("*"):
        if wanted_file(path, root_dir):
            source_files[rel_for(path, root_dir)] = path

source_sigs = {rel: file_sig(path) for rel, path in source_files.items()}

changes = []
changed_rels = set()
all_rels = set(source_sigs) | set(manifest_records)
for rel in sorted(all_rels):
    source_sig = source_sigs.get(rel)
    old_sig = manifest_records.get(rel)
    if source_sig != old_sig:
        changed_rels.add(rel)

if legacy_manifest:
    changes.append("upgrade catalogue : system/scripts/bin")
if scope_changed:
    changes.append("mise a jour du perimetre protege : system/scripts/bin")

if args:
    if not manifest.exists():
        die("catalogue absent : utilise -add sans argument pour creer la premiere reference")
    targets = {normalize_rel(raw).as_posix() for raw in args}
    forbidden = sorted(changed_rels - targets)
    if legacy_manifest:
        die("ancien catalogue detecte : lance -add sans argument pour convertir vers system/scripts/bin")
    if forbidden:
        print("Changements non valides car non demandes :", file=sys.stderr)
        for rel in forbidden[:30]:
            print(f"  {rel}", file=sys.stderr)
        if len(forbidden) > 30:
            print(f"  ... {len(forbidden) - 30} autres", file=sys.stderr)
        die("ajoute ces chemins a -add, ou lance -add sans argument pour tout valider")
    for raw in args:
        rel = normalize_rel(raw).as_posix()
        if rel in source_sigs:
            if source_sigs[rel] != manifest_records.get(rel):
                changes.append(f"update {rel}")
        elif rel in manifest_records:
            changes.append(f"delete {rel}")
        else:
            die(f"fichier absent dans system et catalogue : {rel}")
else:
    for rel in sorted(changed_rels):
        changes.append(("update " if rel in source_sigs else "delete ") + rel)

def archive_reference_ok():
    if not archive.exists() or not archive_sha.exists():
        return False
    try:
        parts = archive_sha.read_text(encoding="utf-8").strip().split()
        if len(parts) < 2:
            return False
        expected_sha, expected_size = parts[0], int(parts[1])
        data = archive.read_bytes()
        return len(data) == expected_size and hashlib.sha256(data).hexdigest() == expected_sha
    except (OSError, ValueError):
        return False


archive_ok = archive_reference_ok()
if not changes and manifest.exists() and archive_ok and not legacy_manifest:
    print("records=unchanged")
    print("changes=0")
    print("skip archive/catalogue: aucune modification")
    raise SystemExit(0)

if not archive_ok:
    changes.append("repair archive/catalogue: archive absente ou SHA256/taille invalide")

records = []
for rel in sorted(source_sigs):
    size, sha = source_sigs[rel]
    records.append((sha, size, rel))

manifest.parent.mkdir(parents=True, exist_ok=True)
with manifest.open("w", encoding="utf-8", newline="\n") as fh:
    fh.write("# YoLeo system integrity catalog v2\n")
    fh.write("# Format: sha256<TAB>size_bytes<TAB>relative_path\n")
    fh.write(f"# Root: dockers ; protected dirs: {' '.join(include_dirs)}\n")
    fh.write("# Archive: system.tar.gz\n")
    for sha, size, rel_text in records:
        fh.write(f"{sha}\t{size}\t{rel_text}\n")

tmp_fd, tmp_name = tempfile.mkstemp(prefix="yoleo-system-", suffix=".tar.gz")
os.close(tmp_fd)
try:
    with tarfile.open(tmp_name, "w:gz") as tf:
        for rel, path in sorted(source_files.items()):
            tf.add(path, arcname=rel, recursive=False)
    shutil.move(tmp_name, archive)
finally:
    if os.path.exists(tmp_name):
        os.unlink(tmp_name)

archive_data = archive.read_bytes()
archive_sha.write_text(
    f"{hashlib.sha256(archive_data).hexdigest()}\t{len(archive_data)}\t{archive.name}\n",
    encoding="utf-8",
    newline="\n",
)

print(f"records={len(records)}")
print(f"changes={len(changes)}")
for line in changes[:50]:
    print(line)
if len(changes) > 50:
    print(f"... {len(changes) - 50} autres changements")
PYADD

    local rc=${PIPESTATUS[0]}
    [ "$rc" -eq 0 ] || die "mise a jour reference integrite echouee"
    log "Reference integrite mise a jour"
}


integrity_backup_snapshot() {
    [ -d "$INTEGRITY_ROOT_DIR" ] || die "racine integrite introuvable : $INTEGRITY_ROOT_DIR"
    command -v python3 >/dev/null 2>&1 || die "python3 introuvable"

    integrity_validate_scope

    local backup_dir stamp archive_path archive_hash archive_size
    backup_dir="${INIT_DIR}/backups"
    mkdir -p "$backup_dir"
    stamp="$(date '+%Y%m%d-%H%M%S')"
    archive_path="${backup_dir}/system-${stamp}.tar.gz"

    log "Backup system vers : $archive_path"
    INTEGRITY_ROOT_DIR="$INTEGRITY_ROOT_DIR" \
    INTEGRITY_INCLUDE_DIRS="$INTEGRITY_INCLUDE_DIRS" \
    python3 - "$archive_path" <<'PYBACKUP'
from pathlib import Path
import os
import sys
import tarfile

archive_path = Path(sys.argv[1]).resolve()
root_dir = Path(os.environ["INTEGRITY_ROOT_DIR"]).resolve()
include_dirs = [part for part in os.environ["INTEGRITY_INCLUDE_DIRS"].split() if part]
excluded_dirs = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules", "backups"}
excluded_files = {".DS_Store", "gunicorn.ctl"}


def wanted_file(path, root):
    rel = path.relative_to(root)
    if any(part in excluded_dirs for part in rel.parts):
        return False
    if path.name in excluded_files:
        return False
    if path.suffix in {".pyc", ".pyo"}:
        return False
    return path.is_file() and not path.is_symlink()

count = 0
archive_path.parent.mkdir(parents=True, exist_ok=True)
with tarfile.open(archive_path, "w:gz") as tf:
    for include_dir in include_dirs:
        include_root = root_dir / include_dir
        if not include_root.exists():
            continue
        for path in include_root.rglob("*"):
            if wanted_file(path, root_dir):
                tf.add(path, arcname=path.relative_to(root_dir).as_posix(), recursive=False)
                count += 1
print(f"backup_files={count}")
PYBACKUP

    local rc=${PIPESTATUS[0]}
    [ "$rc" -eq 0 ] || die "backup system echoue"

    archive_hash="$(integrity_file_sha256 "$archive_path")" || die "sha256 backup impossible : $archive_path"
    archive_size="$(integrity_file_size "$archive_path")" || die "taille backup impossible : $archive_path"
    printf '%s\t%s\t%s\n' "$archive_hash" "$archive_size" "$(basename "$archive_path")" > "${archive_path}.sha256"
    log "Backup system OK : $archive_path (${archive_size} octets)"
}


integrity_restore_snapshot() {
    [ -d "$INTEGRITY_ROOT_DIR" ] || die "racine integrite introuvable : $INTEGRITY_ROOT_DIR"
    command -v python3 >/dev/null 2>&1 || die "python3 introuvable"
    command -v sha256sum >/dev/null 2>&1 || die "sha256sum introuvable"

    integrity_validate_scope

    local backup_dir selected archive_path expected_hash expected_size actual_hash actual_size
    local -a archives
    backup_dir="${INIT_DIR}/backups"
    [ -d "$backup_dir" ] || die "aucun dossier backup : $backup_dir"

    mapfile -t archives < <(find "$backup_dir" -maxdepth 1 -type f -name 'system-*.tar.gz' 2>/dev/null | sort -r)
    [ "${#archives[@]}" -gt 0 ] || die "aucune archive system-*.tar.gz dans $backup_dir"

    selected="${1:-}"
    if [ -z "$selected" ]; then
        echo "Archives disponibles :"
        local i size
        for i in "${!archives[@]}"; do
            size="$(du -h "${archives[$i]}" 2>/dev/null | awk '{print $1}')"
            printf '  %s) %s  %s\n' "$((i + 1))" "$(basename "${archives[$i]}")" "$size"
        done
        printf 'Choix archive a restaurer : '
        read -r selected
    fi

    if [[ "$selected" =~ ^[0-9]+$ ]]; then
        [ "$selected" -ge 1 ] && [ "$selected" -le "${#archives[@]}" ] || die "numero archive invalide : $selected"
        archive_path="${archives[$((selected - 1))]}"
    else
        case "$selected" in
            /*)
                archive_path="$selected"
                ;;
            *)
                archive_path="${backup_dir}/${selected}"
                ;;
        esac
    fi

    [ -f "$archive_path" ] || die "archive introuvable : $archive_path"
    case "$(basename "$archive_path")" in
        system-*.tar.gz) ;;
        *) die "archive refusee : $archive_path" ;;
    esac

    if [ -f "${archive_path}.sha256" ]; then
        read -r expected_hash expected_size _ < "${archive_path}.sha256" || die "sha256 backup illisible : ${archive_path}.sha256"
        actual_hash="$(integrity_file_sha256 "$archive_path")" || die "sha256 backup impossible : $archive_path"
        actual_size="$(integrity_file_size "$archive_path")" || die "taille backup impossible : $archive_path"
        [ "$expected_hash" = "$actual_hash" ] || die "sha256 backup different : $archive_path"
        [ "$expected_size" = "$actual_size" ] || die "taille backup differente : $archive_path"
    else
        log "ATTENTION : sha256 backup absent : ${archive_path}.sha256"
    fi

    log "Restauration backup system : $archive_path"
    INTEGRITY_ROOT_DIR="$INTEGRITY_ROOT_DIR" \
    INTEGRITY_INCLUDE_DIRS="$INTEGRITY_INCLUDE_DIRS" \
    python3 - "$archive_path" <<'PYRESTORE'
from pathlib import Path
import os
import shutil
import sys
import tarfile
import tempfile

archive = Path(sys.argv[1]).resolve()
root_dir = Path(os.environ["INTEGRITY_ROOT_DIR"]).resolve()
include_dirs = [part for part in os.environ["INTEGRITY_INCLUDE_DIRS"].split() if part]
include_names = set(include_dirs)
excluded_dirs = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules", "backups"}
excluded_files = {".DS_Store", "gunicorn.ctl"}


def die(message):
    print(f"ERREUR : {message}", file=sys.stderr)
    raise SystemExit(1)


def clean_parts(name):
    raw = str(name).replace("\\", "/")
    if raw.startswith("/"):
        die(f"chemin absolu refuse dans archive : {name}")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        die(f"chemin parent refuse dans archive : {name}")
    return parts


def wanted_file(path, root):
    rel = path.relative_to(root)
    if any(part in excluded_dirs for part in rel.parts):
        return False
    if path.name in excluded_files:
        return False
    if path.suffix in {".pyc", ".pyo"}:
        return False
    return path.is_file() and not path.is_symlink()


def rel_for(path, root):
    return path.relative_to(root).as_posix()

with tempfile.TemporaryDirectory(prefix="yoleo-restore-") as tmp_name:
    tmp_dir = Path(tmp_name)
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf.getmembers():
            clean_parts(member.name)
            if member.issym() or member.islnk():
                die(f"lien refuse dans archive : {member.name}")
        try:
            tf.extractall(tmp_dir, filter="data")
        except TypeError:
            tf.extractall(tmp_dir)

    raw_files = {
        rel_for(path, tmp_dir): path
        for path in tmp_dir.rglob("*")
        if wanted_file(path, tmp_dir)
    }

    legacy_snapshot = bool(raw_files) and all(Path(rel).parts[0] not in include_names for rel in raw_files)
    snapshot_files = {}
    for rel, path in raw_files.items():
        rel_path = Path(rel)
        if rel_path.parts[0] in include_names:
            dest_rel = rel_path.as_posix()
        elif legacy_snapshot:
            dest_rel = (Path("system") / rel_path).as_posix()
        else:
            die(f"chemin hors dossiers proteges dans archive : {rel}")
        snapshot_files[dest_rel] = path

    restore_include_dirs = ["system"] if legacy_snapshot else include_dirs
    app_files = {}
    for include_dir in restore_include_dirs:
        include_root = root_dir / include_dir
        if not include_root.exists():
            continue
        for path in include_root.rglob("*"):
            if wanted_file(path, root_dir):
                app_files[rel_for(path, root_dir)] = path

    for rel, path in sorted(app_files.items()):
        if rel not in snapshot_files:
            path.unlink()

    for rel, source in sorted(snapshot_files.items()):
        dest = root_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() or dest.is_symlink():
            if dest.is_dir() and not dest.is_symlink():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        shutil.copy2(source, dest)

print(f"restored_files={len(snapshot_files)}")
if legacy_snapshot:
    print("legacy_backup=1")
PYRESTORE

    local rc=${PIPESTATUS[0]}
    [ "$rc" -eq 0 ] || die "restauration backup echouee"
    integrity_add_reference
    log "Restauration backup terminee : $archive_path"
}


need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        die "à lancer en root"
    fi
}

script_path() {
    echo "$SCRIPT_PATH"
}

refresh_runtime_paths() {
    PY_BIN="${VENV_DIR}/bin/python"
    PIP_BIN="${VENV_DIR}/bin/pip"
    if [ "$SERVICE_FILE_AUTO" -eq 1 ]; then
        SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}"
    fi
}

normalize_integrity_include_dirs() {
    local include_dir normalized=""

    for include_dir in $INTEGRITY_INCLUDE_DIRS; do
        [ "$include_dir" = "offline" ] && continue
        if [ -z "$normalized" ]; then
            normalized="$include_dir"
        else
            normalized="$normalized $include_dir"
        fi
    done

    [ -n "$normalized" ] || normalized="system scripts bin"
    if [ "$normalized" != "$INTEGRITY_INCLUDE_DIRS" ]; then
        log "Migration du perimetre integrite : $INTEGRITY_INCLUDE_DIRS -> $normalized"
        INTEGRITY_INCLUDE_DIRS="$normalized"
    fi
}

systemctl_cmd() {
    "$SYSTEMCTL_BIN" "$@"
}

build_gunicorn_bind_args() {
    if [ "$HOST" = "0.0.0.0" ] && [ "$IPV6_BIND" != "0" ] && [ "$IPV6_BIND" != "false" ] && [ -r /proc/net/if_inet6 ]; then
        BIND_ARGS=(--bind "[::]:$PORT")
    else
        BIND_ARGS=(--bind "$HOST:$PORT")
    fi
}

ensure_https_certificate() {
    local tls_dir="${CONF_DIR}/https"
    local cert_file="${tls_dir}/yoleo.crt" key_file="${tls_dir}/yoleo.key"

    # setup.sh et chaque démarrage utilisent le même gestionnaire : l'autorité
    # installée sur les PC est conservée, seul le certificat serveur se renouvelle.
    if ! "$PY_BIN" "$APP_DIR/https_certificate.py" \
        --directory "$tls_dir" --addresses "$(hostname -I 2>/dev/null || true)" \
        2>&1 | tee -a "$LOG_FILE"; then
        die "preparation du certificat HTTPS local impossible"
    fi
    HTTPS_ARGS=(--certfile "$cert_file" --keyfile "$key_file")
}

load_env_file() {
    # Fichier optionnel pour changer HOST/PORT/WORKERS/THREADS/etc.
    # Le fichier peut aussi contenir des chemins absolus générés par -install.
    if [ -f "$ENV_FILE" ]; then
        local env_has_integrity_root=0 env_has_integrity_dirs=0
        grep -Eq '^[[:space:]]*INTEGRITY_ROOT_DIR=' "$ENV_FILE" 2>/dev/null && env_has_integrity_root=1
        grep -Eq '^[[:space:]]*INTEGRITY_INCLUDE_DIRS=' "$ENV_FILE" 2>/dev/null && env_has_integrity_dirs=1

        # shellcheck disable=SC1090
        set -a
        . "$ENV_FILE"
        set +a

        if [ "$env_has_integrity_root" -eq 0 ]; then
            INTEGRITY_ROOT_DIR="$DOCKERS_DIR"
        fi
        if [ "$env_has_integrity_dirs" -eq 0 ]; then
            INTEGRITY_INCLUDE_DIRS="system scripts bin"
        fi
        normalize_integrity_include_dirs

        refresh_runtime_paths
        ensure_dirs
    fi
}

validate_requirements_file() {
    [ -f "$REQ_FILE" ] || die "requirements.txt introuvable : $REQ_FILE"
    [ -s "$REQ_FILE" ] || die "requirements.txt vide : $REQ_FILE"

    if ! grep -Eq '^[[:space:]]*[^#[:space:]]' "$REQ_FILE"; then
        die "requirements.txt ne contient aucune dépendance : $REQ_FILE"
    fi

    log "requirements.txt utilisé comme source unique des dépendances Python : $REQ_FILE"
}

verify_python_environment() {
    log "Vérification de la cohérence des dépendances installées depuis requirements.txt..."
    "$PY_BIN" -m pip check 2>&1 | tee -a "$LOG_FILE"
}

pip_use_wheelhouse() {
    if [ "$PIP_OFFLINE" = "0" ] || [ "$PIP_OFFLINE" = "false" ]; then
        return 1
    fi
    [ -d "$WHEELHOUSE_DIR" ] || return 1
    find "$WHEELHOUSE_DIR" -maxdepth 1 -type f \( -name '*.whl' -o -name '*.tar.gz' -o -name '*.zip' \) -print -quit | grep -q .
}

pip_install() {
    if pip_use_wheelhouse; then
        log "pip : essai local uniquement depuis : $WHEELHOUSE_DIR"
        if "$PY_BIN" -m pip install --no-index --find-links "$WHEELHOUSE_DIR" "$@"; then
            return 0
        fi
        if [ "$PIP_OFFLINE" = "1" ] || [ "$PIP_OFFLINE" = "true" ]; then
            die "pip : installation offline echouee depuis $WHEELHOUSE_DIR"
        fi
        log "pip : paquet absent/incomplet en local, fallback Internet"
        "$PY_BIN" -m pip install "$@"
    elif [ "$PIP_OFFLINE" = "1" ] || [ "$PIP_OFFLINE" = "true" ]; then
        die "pip : wheelhouse local absent en mode offline strict : $WHEELHOUSE_DIR"
    else
        log "pip : wheelhouse local absent, installation via Internet"
        "$PY_BIN" -m pip install "$@"
    fi
}

apt_offline_only_enabled() {
    case "${APT_OFFLINE_ONLY:-0}" in
        1|true|TRUE|yes|YES|on|ON|offline|OFFLINE)
            return 0
            ;;
    esac
    return 1
}

configure_offline_apt_source() {
    local repo_dir repo_list

    repo_dir="$(readlink -f "$OFFLINE_DEB_DIR" 2>/dev/null || realpath "$OFFLINE_DEB_DIR" 2>/dev/null || echo "$OFFLINE_DEB_DIR")"
    [ -d "$repo_dir" ] || return 0
    find "$repo_dir" -maxdepth 1 -type f -name '*.deb' -print -quit | grep -q . || return 0

    if command -v dpkg-scanpackages >/dev/null 2>&1; then
        log "APT offline : index local depuis $repo_dir"
        (
            cd "$repo_dir"
            dpkg-scanpackages . > Packages
            gzip -kf Packages
        ) 2>&1 | tee -a "$LOG_FILE"
    elif [ ! -f "$repo_dir/Packages" ] && [ ! -f "$repo_dir/Packages.gz" ]; then
        log "ATTENTION : dpkg-scanpackages absent et aucun index APT offline dans $repo_dir"
    fi

    repo_list="/etc/apt/sources.list.d/yoleo-offline.list"
    printf 'deb [trusted=yes] file:%s ./\n' "$repo_dir" > "$repo_list"
    log "APT offline : source locale activee : $repo_list"
}

validate_apt_manifests() {
    [ -f "$OFFLINE_APT_REQUESTED" ] || die "manifeste APT introuvable : $OFFLINE_APT_REQUESTED"
    [ -s "$OFFLINE_APT_REQUESTED" ] || die "manifeste APT vide : $OFFLINE_APT_REQUESTED"

    if [ -f "$OFFLINE_APT_MISSING" ] && grep -Eq '^[[:space:]]*[^#[:space:]]' "$OFFLINE_APT_MISSING"; then
        log "Paquets APT manquants declares dans : $OFFLINE_APT_MISSING"
        sed -E '/^[[:space:]]*($|#)/d; s/^/  - /' "$OFFLINE_APT_MISSING" | tee -a "$LOG_FILE"
        die "depot APT offline incomplet : relance update_pip.sh avec les depots Debian accessibles"
    fi
}

build_apt_package_list() {
    awk '
        /^[[:space:]]*($|#)/ { next }
        {
            sub(/[[:space:]]+#.*/, "")
            gsub(/^[[:space:]]+|[[:space:]]+$/, "")
            if ($0 != "") print $0
        }
    ' "$OFFLINE_APT_REQUESTED" | awk '!seen[$0]++'
}

install_apt_deps() {
    local apt_sources_tmp=""
    local repo_dir=""
    local post_file=""
    local package status
    local -a apt_options=(-o "APT::Install-Recommends=true")
    local -a packages=()
    local -a requested_missing=()

    if ! command -v apt-get >/dev/null 2>&1; then
        log "apt-get absent, étape paquets Debian ignorée"
        return 0
    fi

    log "Installation des paquets Debian demandes par le manifeste unique..."
    validate_apt_manifests
    configure_offline_apt_source

    if apt_offline_only_enabled; then
        repo_dir="$(readlink -f "$OFFLINE_DEB_DIR" 2>/dev/null || realpath "$OFFLINE_DEB_DIR" 2>/dev/null || echo "$OFFLINE_DEB_DIR")"
        [ -d "$repo_dir" ] || die "APT offline : dossier .deb introuvable : $repo_dir"
        apt_sources_tmp="$(mktemp -d "${TMPDIR:-/tmp}/yoleo-apt.XXXXXX")"
        printf 'deb [trusted=yes] file:%s ./\n' "$repo_dir" > "${apt_sources_tmp}/sources.list"
        apt_options+=(
            -o "Dir::Etc::sourcelist=${apt_sources_tmp}/sources.list"
            -o "Dir::Etc::sourceparts=-"
            -o "APT::Get::List-Cleanup=0"
            -o "APT::Sandbox::User=root"
            -o "Acquire::Retries=0"
        )
        log "APT offline : mode strict, source locale unique : $repo_dir"
    fi

    mapfile -t packages < <(build_apt_package_list)
    [ "${#packages[@]}" -gt 0 ] || die "aucun paquet Debian a installer"
    log "APT : ${#packages[@]} paquet(s) demandé(s)"

    DEBIAN_FRONTEND=noninteractive apt-get "${apt_options[@]}" update 2>&1 | tee -a "$LOG_FILE"
    DEBIAN_FRONTEND=noninteractive apt-get "${apt_options[@]}" install -y "${packages[@]}" 2>&1 | tee -a "$LOG_FILE"
    DEBIAN_FRONTEND=noninteractive apt-get "${apt_options[@]}" check 2>&1 | tee -a "$LOG_FILE"

    # Une seconde simulation doit etre vide. Avec Install-Recommends=true,
    # les recommandations Debian font partie de l'installation universelle.
    post_file="$(mktemp "${TMPDIR:-/tmp}/yoleo-apt-post.XXXXXX")"
    if ! DEBIAN_FRONTEND=noninteractive apt-get "${apt_options[@]}" -s install -y \
        "${packages[@]}" > "$post_file" 2>&1; then
        cat "$post_file" | tee -a "$LOG_FILE" >&2
        rm -f -- "$post_file"
        die "verification APT finale impossible"
    fi
    if grep -q '^Inst ' "$post_file"; then
        log "Paquets encore non installes apres la passe APT :"
        grep '^Inst ' "$post_file" | sed 's/^/  /' | tee -a "$LOG_FILE"
        rm -f -- "$post_file"
        die "installation APT incomplete"
    fi
    rm -f -- "$post_file"

    for package in "${packages[@]}"; do
        status="$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)"
        [ "$status" = "install ok installed" ] || requested_missing+=("$package")
    done
    if [ "${#requested_missing[@]}" -gt 0 ]; then
        log "Paquets demandes mais non installes :"
        printf '  - %s\n' "${requested_missing[@]}" | tee -a "$LOG_FILE"
        die "installation APT incomplete par rapport a apt-requested.txt"
    fi
    log "APT : manifeste complet, dependances et recommandations installees."

    if [ -n "$apt_sources_tmp" ]; then
        rm -rf -- "$apt_sources_tmp"
    fi
}

prepare_app() {
    load_env_file

    command -v python3 >/dev/null 2>&1 || die "python3 introuvable"
    [ -d "$APP_DIR" ] || die "dossier APP_DIR introuvable : $APP_DIR"
    integrity_check_and_restore
    [ -f "$APP_FILE" ] || die "app.py introuvable : $APP_FILE"

    validate_requirements_file

    if [ ! -d "$VENV_DIR" ]; then
        log "Création du venv : $VENV_DIR"
        python3 -m venv "$VENV_DIR" 2>&1 | tee -a "$LOG_FILE"
    fi

    [ -x "$PY_BIN" ] || die "python du venv introuvable : $PY_BIN"
    [ -x "$PIP_BIN" ] || die "pip du venv introuvable : $PIP_BIN"

    log "Installation des dépendances Python définies uniquement dans : $REQ_FILE"
    pip_install --upgrade --upgrade-strategy eager -r "$REQ_FILE" 2>&1 | tee -a "$LOG_FILE"

    verify_python_environment
    ensure_secret_key
}

ensure_secret_key() {
    if [ ! -f "$SECRET_FILE" ] || [ ! -s "$SECRET_FILE" ]; then
        log "Création SECRET_KEY : $SECRET_FILE"
        SECRET_FILE="$SECRET_FILE" python3 - <<'PY'
from pathlib import Path
import os
import secrets
p = Path(os.environ["SECRET_FILE"])
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
print("SECRET_KEY créée")
PY
        chmod 600 "$SECRET_FILE" 2>/dev/null || true
    fi

    export SECRET_KEY="$(cat "$SECRET_FILE" 2>/dev/null || true)"
    [ -n "$SECRET_KEY" ] || die "SECRET_KEY vide"
}

write_env_kv() {
    local key="$1"
    local value="$2"
    printf '%s=%q\n' "$key" "$value"
}

write_env_file() {
    ensure_dirs
    log "Écriture du fichier env : $ENV_FILE"
    {
        cat <<EOFENV
# Config System Flask hôte
# Généré automatiquement par : $(script_path) -install
# Tu peux modifier ces valeurs puis faire : systemctl restart ${SERVICE_NAME}
# Confs en relatif : ../conf ; logs en standard Linux : /var/log/flask-system
EOFENV
        write_env_kv "SERVICE_NAME" "$SERVICE_NAME"
        write_env_kv "SYSTEMCTL_BIN" "$SYSTEMCTL_BIN"
        write_env_kv "DOCKERS_DIR" "$DOCKERS_DIR"
        write_env_kv "APP_DIR" "$APP_DIR"
        write_env_kv "APP_FILE" "$APP_FILE"
        write_env_kv "APP_MODULE" "$APP_MODULE"
        write_env_kv "VENV_DIR" "$VENV_DIR"
        write_env_kv "REQ_FILE" "$REQ_FILE"
        write_env_kv "INIT_DIR" "$INIT_DIR"
        write_env_kv "INTEGRITY_MANIFEST" "$INTEGRITY_MANIFEST"
        write_env_kv "INTEGRITY_ARCHIVE" "$INTEGRITY_ARCHIVE"
        write_env_kv "INTEGRITY_ENABLED" "$INTEGRITY_ENABLED"
        write_env_kv "INTEGRITY_ROOT_DIR" "$INTEGRITY_ROOT_DIR"
        write_env_kv "INTEGRITY_INCLUDE_DIRS" "$INTEGRITY_INCLUDE_DIRS"
        write_env_kv "CONF_DIR" "$CONF_DIR"
        write_env_kv "ENV_FILE" "$ENV_FILE"
        write_env_kv "SECRET_FILE" "$SECRET_FILE"
        write_env_kv "LOG_DIR" "$LOG_DIR"
        write_env_kv "LOG_FILE" "$LOG_FILE"
        write_env_kv "ACCESS_LOG" "$ACCESS_LOG"
        write_env_kv "PID_FILE" "$PID_FILE"
        write_env_kv "HOST" "$HOST"
        write_env_kv "PORT" "$PORT"
        write_env_kv "WORKERS" "$WORKERS"
        write_env_kv "THREADS" "$THREADS"
        write_env_kv "TIMEOUT" "$TIMEOUT"
        write_env_kv "IPV6_BIND" "$IPV6_BIND"
    } > "$ENV_FILE"
}

write_service_file() {
    local sp
    sp="$(script_path)"
    [ -f "$sp" ] || die "script introuvable : $sp"

    log "Écriture du service systemd : $SERVICE_FILE"
    cat > "$SERVICE_FILE" <<EOFUNIT
[Unit]
Description=System Flask host service
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=SERVICE_NAME=${SERVICE_NAME}
ExecStart=/bin/bash ${sp} -run
Restart=always
RestartSec=3
KillSignal=SIGTERM
TimeoutStopSec=30
StandardOutput=append:${LOG_FILE}
StandardError=append:${LOG_FILE}

[Install]
WantedBy=multi-user.target
EOFUNIT

    systemctl_cmd daemon-reload
    systemctl_cmd enable "$SERVICE_NAME" 2>&1 | tee -a "$LOG_FILE"
}

show_routes() {
    load_env_file
    ensure_secret_key
    [ -x "$PY_BIN" ] || die "venv absent, lance d'abord : bash $(script_path) -install"

    log "Routes Flask actuellement chargées :"
    (
        cd "$APP_DIR" || exit 1
        export SECRET_KEY
        "$PY_BIN" - <<'PY'
from app import app
for rule in sorted(app.url_map.iter_rules(), key=lambda r: str(r)):
    methods = ",".join(sorted(rule.methods - {"HEAD", "OPTIONS"}))
    print(f"  {rule}  [{methods}]")
PY
    ) 2>&1 | tee -a "$LOG_FILE"
}

install_pci_boot_support() {
    # setup.sh délègue ici lors de l'installation ; -run répare aussi les liens
    # après une restauration ISO. Ne change ni les choix ni les pilotes actifs.
    log "Vérification des hooks de réservation PCI pour le prochain démarrage"
    NAS_CONF_DIR="${NAS_CONF_DIR:-$(cd "$CONF_DIR" && pwd -P)}" \
        "$PY_BIN" "$APP_DIR/pci_reservations.py" --install 2>&1 | tee -a "$LOG_FILE"
}

run_foreground() {
    load_env_file

    [ -d "$APP_DIR" ] || die "APP_DIR absent : $APP_DIR"
    integrity_check_and_restore
    ensure_secret_key

    [ -x "$PY_BIN" ] || die "venv absent : $PY_BIN"
    [ -f "$APP_FILE" ] || die "app.py absent : $APP_FILE"
    ensure_https_certificate
    install_pci_boot_support

    cd "$APP_DIR" || die "impossible d'entrer dans $APP_DIR"
    build_gunicorn_bind_args

    log "Demarrage Gunicorn HTTPS sur le port $PORT"
    log "Commande : $PY_BIN -m gunicorn --worker-class $WORKER_CLASS --workers $WORKERS --threads $THREADS --timeout $TIMEOUT ${BIND_ARGS[*]} ${HTTPS_ARGS[*]} $APP_MODULE"

    exec "$PY_BIN" -m gunicorn \
        --worker-class "$WORKER_CLASS" \
        --workers "$WORKERS" \
        --threads "$THREADS" \
        --timeout "$TIMEOUT" \
        "${BIND_ARGS[@]}" \
        "${HTTPS_ARGS[@]}" \
        --pid "$PID_FILE" \
        --access-logfile "$ACCESS_LOG" \
        --error-logfile "-" \
        "$APP_MODULE"
}

install_service() {
    need_root
    normalize_integrity_include_dirs
    log "=================================================="
    log "INSTALL SYSTEM FLASK MODE HÔTE"
    log "SCRIPT_PATH=$SCRIPT_PATH"
    log "SCRIPT_DIR=$SCRIPT_DIR"
    log "DOCKERS_DIR=$DOCKERS_DIR"
    log "APP_DIR=$APP_DIR"
    log "APP_FILE=$APP_FILE"
    log "APP_MODULE=$APP_MODULE"
    log "VENV_DIR=$VENV_DIR"
    log "ENV_FILE=$ENV_FILE"
    log "PORT=$PORT"
    log "=================================================="

    install_apt_deps
    write_env_file
    prepare_app
    ensure_https_certificate
    install_pci_boot_support
    show_routes
    write_service_file

    log "Démarrage du service..."
    systemctl_cmd restart "$SERVICE_NAME" 2>&1 | tee -a "$LOG_FILE"
    status_app
}

start_app() {
    need_root
    load_env_file
    if [ ! -f "$SERVICE_FILE" ]; then
        die "service non installé. Lance d'abord : bash $(script_path) -install"
    fi
    systemctl_cmd start "$SERVICE_NAME" 2>&1 | tee -a "$LOG_FILE"
    status_app
}

stop_app() {
    need_root
    load_env_file
    if [ -f "$SERVICE_FILE" ]; then
        systemctl_cmd stop "$SERVICE_NAME" 2>&1 | tee -a "$LOG_FILE" || true
    else
        log "Service non installé, tentative d'arrêt via PID : $PID_FILE"
        if [ -f "$PID_FILE" ]; then
            local pid
            pid="$(cat "$PID_FILE" 2>/dev/null || true)"
            if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null || true
            fi
            rm -f "$PID_FILE"
        fi
    fi
    status_app
}

restart_app() {
    need_root
    load_env_file
    if [ ! -f "$SERVICE_FILE" ]; then
        die "service non installé. Lance d'abord : bash $(script_path) -install"
    fi
    [ -d "$APP_DIR" ] || die "APP_DIR absent : $APP_DIR"
    integrity_check_and_restore
    systemctl_cmd restart "$SERVICE_NAME" 2>&1 | tee -a "$LOG_FILE"
    status_app
}

status_app() {
    load_env_file
    echo "========== STATUS SYSTEM FLASK =========="
    echo "SERVICE     : $SERVICE_NAME"
    echo "SCRIPT_PATH : $SCRIPT_PATH"
    echo "DOCKERS_DIR : $DOCKERS_DIR"
    echo "APP_DIR     : $APP_DIR"
    echo "APP_FILE    : $APP_FILE"
    echo "APP_MODULE  : $APP_MODULE"
    echo "VENV_DIR    : $VENV_DIR"
    echo "ENV_FILE    : $ENV_FILE"
    echo "INIT_DIR    : $INIT_DIR"
    echo "MANIFEST    : $INTEGRITY_MANIFEST"
    echo "ARCHIVE     : $INTEGRITY_ARCHIVE"
    echo "ROOT        : $INTEGRITY_ROOT_DIR"
    echo "DIRS        : $INTEGRITY_INCLUDE_DIRS"
    echo "PORT        : $PORT"
    echo "HTTPS       : https://yoleo.local:${PORT}"
    echo "LOG_FILE    : $LOG_FILE"
    echo

    if command -v "$SYSTEMCTL_BIN" >/dev/null 2>&1 && [ -f "$SERVICE_FILE" ]; then
        systemctl_cmd status "$SERVICE_NAME" --no-pager -l || true
    else
        echo "Service systemd non installé."
    fi

    echo
    echo "Port :"
    ss -lntp 2>/dev/null | grep ":${PORT} " || echo "Port $PORT non vu par ss"
    echo "========================================="
}

show_logs() {
    load_env_file
    echo "========== JOURNAL SYSTEMD =========="
    if command -v journalctl >/dev/null 2>&1 && [ -f "$SERVICE_FILE" ]; then
        journalctl -u "$SERVICE_NAME" -n 120 --no-pager || true
    else
        echo "Service systemd non installé."
    fi

    echo
    echo "========== LOG FICHIER =========="
    touch "$LOG_FILE" 2>/dev/null || true
    tail -150 "$LOG_FILE" 2>/dev/null || true
}

usage() {
    cat <<EOFUSAGE
Usage : bash ./system.sh {-install|-start|-stop|-restart|-status|-logs|-routes|-integrity|-diff|-add|-aa|-backup|-restaure}

Commandes :
  -install   installe les paquets Debian, recrée le env avec les chemins actuels,
             exige requirements.txt comme source unique, puis crée le venv,
             installe les dépendances Python, crée les certificats HTTPS locaux et le service systemd,
             enable + démarre le service
  -start     démarre le service systemd
  -stop      arrête le service systemd
  -restart   redémarre le service systemd
  -status    affiche l'état systemd + le port $PORT
  -logs      affiche les logs systemd + le log fichier
  -routes    teste l'import Flask et affiche les routes
  -integrity verifie/restaure system/scripts/bin depuis init sans lancer Gunicorn
  -diff      affiche les fichiers modifies/absents/hors catalogue sans rien modifier
             compare aussi le contenu de system.tar.gz avec le catalogue
  -add       valide les changements de system/scripts/bin dans init + catalogue + archive
             sans argument : analyse tout ; avec chemin(s) : seulement ces fichiers
  -aa        alias court de -add
  -backup    cree une archive datee de system/scripts/bin dans init/backups
  -restaure  affiche les backups numerotes, restaure le choix,
             puis reconstruit init + catalogue + archive
EOFUSAGE
}

case "$ACTION" in
    install)
        install_service
        ;;
    start)
        start_app
        ;;
    stop)
        stop_app
        ;;
    restart)
        restart_app
        ;;
    status)
        status_app
        ;;
    logs)
        show_logs
        ;;
    routes)
        show_routes
        ;;
    integrity)
        load_env_file
        [ -d "$APP_DIR" ] || die "APP_DIR absent : $APP_DIR"
        integrity_check_and_restore
        ;;
    diff|differences|changes)
        load_env_file
        [ -d "$APP_DIR" ] || die "APP_DIR absent : $APP_DIR"
        integrity_show_differences
        ;;
    add|aa)
        shift || true
        load_env_file
        integrity_add_reference "$@"
        ;;
    backup)
        load_env_file
        integrity_backup_snapshot
        ;;
    restaure|restore)
        shift || true
        load_env_file
        integrity_restore_snapshot "$@"
        ;;
    run)
        run_foreground
        ;;
    help|h|--help)
        usage
        ;;
    *)
        usage
        exit 1
        ;;
esac
