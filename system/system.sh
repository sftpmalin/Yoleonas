#!/bin/bash
set -Eeuo pipefail

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

SERVICE_NAME="flask-system.service"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}"

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
PIP_OFFLINE="${PIP_OFFLINE:-auto}"
INIT_DIR="${INIT_DIR:-${DOCKERS_DIR}/init}"
INTEGRITY_MANIFEST="${INTEGRITY_MANIFEST:-${INIT_DIR}/system.sha256}"
INTEGRITY_ARCHIVE="${INTEGRITY_ARCHIVE:-${INIT_DIR}/system.tar.gz}"
INTEGRITY_ENABLED="${INTEGRITY_ENABLED:-1}"
INTEGRITY_ROOT_DIR="${INTEGRITY_ROOT_DIR:-${DOCKERS_DIR}}"
INTEGRITY_INCLUDE_DIRS="${INTEGRITY_INCLUDE_DIRS:-system scripts offline bin}"

CONF_DIR="${CONF_DIR:-../conf}"
ENV_FILE="${ENV_FILE:-${CONF_DIR}/flask_system.env}"
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

    [ "$expected_hash" = "$actual_hash" ] || die "sha256 archive different : $INTEGRITY_ARCHIVE"
    [ "$expected_size" = "$actual_size" ] || die "taille archive differente : $INTEGRITY_ARCHIVE"
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
        legacy = legacy or was_legacy
        records[active_rel] = (int(size), sha)
    return records, legacy


manifest_records, legacy_manifest = read_manifest()
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
    changes.append("upgrade catalogue : system/scripts/offline/bin")

if args:
    if not manifest.exists():
        die("catalogue absent : utilise -add sans argument pour creer la premiere reference")
    targets = {normalize_rel(raw).as_posix() for raw in args}
    forbidden = sorted(changed_rels - targets)
    if legacy_manifest:
        die("ancien catalogue detecte : lance -add sans argument pour convertir vers system/scripts/offline/bin")
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

if not changes and manifest.exists() and archive.exists() and archive_sha.exists() and not legacy_manifest:
    print("records=unchanged")
    print("changes=0")
    print("skip archive/catalogue: aucune modification")
    raise SystemExit(0)

records = []
for rel in sorted(source_sigs):
    size, sha = source_sigs[rel]
    records.append((sha, size, rel))

manifest.parent.mkdir(parents=True, exist_ok=True)
with manifest.open("w", encoding="utf-8", newline="\n") as fh:
    fh.write("# YoLeo system integrity catalog v2\n")
    fh.write("# Format: sha256<TAB>size_bytes<TAB>relative_path\n")
    fh.write("# Root: dockers ; protected dirs: system scripts offline bin\n")
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

refresh_bins() {
    PY_BIN="${VENV_DIR}/bin/python"
    PIP_BIN="${VENV_DIR}/bin/pip"
}

build_gunicorn_bind_args() {
    if [ "$HOST" = "0.0.0.0" ] && [ "$IPV6_BIND" != "0" ] && [ "$IPV6_BIND" != "false" ] && [ -r /proc/net/if_inet6 ]; then
        BIND_ARGS=(--bind "[::]:$PORT")
    else
        BIND_ARGS=(--bind "$HOST:$PORT")
    fi
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
            INTEGRITY_INCLUDE_DIRS="system scripts offline bin"
        fi

        refresh_bins
        ensure_dirs
    fi
}

make_requirements_if_missing() {
    if [ ! -f "$REQ_FILE" ]; then
        log "requirements.txt absent, création : $REQ_FILE"
        cat > "$REQ_FILE" <<'REQEOF'
PyYAML==6.0.3
Flask==3.1.3
gunicorn==25.1.0
docker==7.1.0
requests==2.32.5
psutil==7.2.2
paramiko==4.0.0
simple-websocket
websocket-client
REQEOF
    else
        log "requirements.txt présent, vérification des dépendances obligatoires : $REQ_FILE"
        ensure_requirement "simple-websocket" "simple-websocket"
        ensure_requirement "websocket-client" "websocket-client"
    fi
}

ensure_requirement() {
    local package="$1"
    local line="$2"

    touch "$REQ_FILE"
    if grep -Eiq "^[[:space:]]*${package}([=<>!~[:space:]]|$)" "$REQ_FILE"; then
        log "OK dépendance Python déjà déclarée : ${package}"
    else
        log "Ajout dépendance Python manquante : ${line}"
        printf '
%s
' "$line" >> "$REQ_FILE"
    fi
}

verify_python_modules() {
    log "Vérification imports Python critiques dans le venv..."
    "$PY_BIN" - <<'PYVERIFY' 2>&1 | tee -a "$LOG_FILE"
import importlib
modules = [
    ("flask", "Flask"),
    ("gunicorn", "gunicorn"),
    ("simple_websocket", "simple-websocket"),
    ("websocket", "websocket-client"),
]
for module, package in modules:
    obj = importlib.import_module(module)
    print(f"OK {package}: {getattr(obj, '__file__', 'built-in')}")
PYVERIFY
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
        log "pip : paquet absent/incomplet en local, fallback Internet"
        "$PY_BIN" -m pip install "$@"
    else
        log "pip : wheelhouse local absent, installation via Internet"
        "$PY_BIN" -m pip install "$@"
    fi
}

install_apt_deps() {
    if ! command -v apt-get >/dev/null 2>&1; then
        log "apt-get absent, étape paquets Debian ignorée"
        return 0
    fi

    log "Installation des paquets Debian nécessaires..."
    DEBIAN_FRONTEND=noninteractive apt-get update 2>&1 | tee -a "$LOG_FILE"
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        python3-full \
        python3-venv \
        python3-pip \
        iproute2 \
        novnc \
        websockify 2>&1 | tee -a "$LOG_FILE"
}

prepare_app() {
    load_env_file

    command -v python3 >/dev/null 2>&1 || die "python3 introuvable"
    [ -d "$APP_DIR" ] || die "dossier APP_DIR introuvable : $APP_DIR"
    integrity_check_and_restore
    [ -f "$APP_FILE" ] || die "app.py introuvable : $APP_FILE"

    make_requirements_if_missing

    if [ ! -d "$VENV_DIR" ]; then
        log "Création du venv : $VENV_DIR"
        python3 -m venv "$VENV_DIR" 2>&1 | tee -a "$LOG_FILE"
    fi

    [ -x "$PY_BIN" ] || die "python du venv introuvable : $PY_BIN"
    [ -x "$PIP_BIN" ] || die "pip du venv introuvable : $PIP_BIN"

    log "Mise à jour pip/setuptools/wheel dans le venv..."
    pip_install --upgrade pip setuptools wheel 2>&1 | tee -a "$LOG_FILE"

    log "Installation des dépendances Python depuis : $REQ_FILE"
    pip_install -r "$REQ_FILE" 2>&1 | tee -a "$LOG_FILE"

    verify_python_modules
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

write_env_file() {
    ensure_dirs
    log "Écriture du fichier env : $ENV_FILE"
    cat > "$ENV_FILE" <<EOFENV
# Config System Flask hôte
# Généré automatiquement par : $(script_path) -install
# Tu peux modifier ces valeurs puis faire : systemctl restart ${SERVICE_NAME}
# Confs en relatif : ../conf ; logs en standard Linux : /var/log/flask-system
DOCKERS_DIR=${DOCKERS_DIR}
APP_DIR=${APP_DIR}
APP_FILE=${APP_FILE}
APP_MODULE=${APP_MODULE}
VENV_DIR=${VENV_DIR}
REQ_FILE=${REQ_FILE}
INIT_DIR=${INIT_DIR}
INTEGRITY_MANIFEST=${INTEGRITY_MANIFEST}
INTEGRITY_ARCHIVE=${INTEGRITY_ARCHIVE}
INTEGRITY_ENABLED=${INTEGRITY_ENABLED}
INTEGRITY_ROOT_DIR=${INTEGRITY_ROOT_DIR}
INTEGRITY_INCLUDE_DIRS=${INTEGRITY_INCLUDE_DIRS}
CONF_DIR=${CONF_DIR}
ENV_FILE=${ENV_FILE}
SECRET_FILE=${SECRET_FILE}
LOG_DIR=${LOG_DIR}
LOG_FILE=${LOG_FILE}
ACCESS_LOG=${ACCESS_LOG}
PID_FILE=${PID_FILE}
HOST=${HOST}
PORT=${PORT}
WORKERS=${WORKERS}
THREADS=${THREADS}
TIMEOUT=${TIMEOUT}
IPV6_BIND=${IPV6_BIND}
EOFENV
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

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME" 2>&1 | tee -a "$LOG_FILE"
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

run_foreground() {
    load_env_file

    [ -d "$APP_DIR" ] || die "APP_DIR absent : $APP_DIR"
    integrity_check_and_restore
    ensure_secret_key

    [ -x "$PY_BIN" ] || die "venv absent : $PY_BIN"
    [ -f "$APP_FILE" ] || die "app.py absent : $APP_FILE"

    cd "$APP_DIR" || die "impossible d'entrer dans $APP_DIR"
    build_gunicorn_bind_args

    log "Démarrage Gunicorn en mode systemd foreground"
    log "Commande : $PY_BIN -m gunicorn --worker-class $WORKER_CLASS --workers $WORKERS --threads $THREADS --timeout $TIMEOUT ${BIND_ARGS[*]} $APP_MODULE"

    exec "$PY_BIN" -m gunicorn \
        --worker-class "$WORKER_CLASS" \
        --workers "$WORKERS" \
        --threads "$THREADS" \
        --timeout "$TIMEOUT" \
        "${BIND_ARGS[@]}" \
        --pid "$PID_FILE" \
        --access-logfile "$ACCESS_LOG" \
        --error-logfile "-" \
        "$APP_MODULE"
}

install_service() {
    need_root
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
    show_routes
    write_service_file

    log "Démarrage du service..."
    systemctl restart "$SERVICE_NAME" 2>&1 | tee -a "$LOG_FILE"
    status_app
}

start_app() {
    need_root
    load_env_file
    if [ ! -f "$SERVICE_FILE" ]; then
        die "service non installé. Lance d'abord : bash $(script_path) -install"
    fi
    systemctl start "$SERVICE_NAME" 2>&1 | tee -a "$LOG_FILE"
    status_app
}

stop_app() {
    need_root
    load_env_file
    if [ -f "$SERVICE_FILE" ]; then
        systemctl stop "$SERVICE_NAME" 2>&1 | tee -a "$LOG_FILE" || true
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
    systemctl restart "$SERVICE_NAME" 2>&1 | tee -a "$LOG_FILE"
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
    echo "LOG_FILE    : $LOG_FILE"
    echo

    if command -v systemctl >/dev/null 2>&1 && [ -f "$SERVICE_FILE" ]; then
        systemctl status "$SERVICE_NAME" --no-pager -l || true
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
Usage : bash ./system.sh {-install|-start|-stop|-restart|-status|-logs|-routes|-integrity|-add|-aa|-backup|-restaure}

Commandes :
  -install   installe les paquets Debian, recrée le env avec les chemins actuels,
             crée le venv, complète requirements.txt si besoin,
             installe les dépendances Python, crée le service systemd,
             enable + démarre le service
  -start     démarre le service systemd
  -stop      arrête le service systemd
  -restart   redémarre le service systemd
  -status    affiche l'état systemd + le port $PORT
  -logs      affiche les logs systemd + le log fichier
  -routes    teste l'import Flask et affiche les routes
  -integrity verifie/restaure system/scripts/offline/bin depuis init sans lancer Gunicorn
  -add       valide les changements de system/scripts/offline/bin dans init + catalogue + archive
             sans argument : analyse tout ; avec chemin(s) : seulement ces fichiers
  -aa        alias court de -add
  -backup    cree une archive datee de system/scripts/offline/bin dans init/backups
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
