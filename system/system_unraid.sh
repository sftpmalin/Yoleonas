#!/bin/bash

# ============================================================
# System Flask / Gunicorn - mode hôte Unraid
#
# Commandes compatibles :
#   bash /mnt/user/dockers/scripts/system.sh start
#   bash /mnt/user/dockers/scripts/system.sh stop
#   bash /mnt/user/dockers/scripts/system.sh restart
#   bash /mnt/user/dockers/scripts/system.sh status
#   bash /mnt/user/dockers/scripts/system.sh logs
#   bash /mnt/user/dockers/scripts/system.sh routes
#
# Compatibilité tirets :
#   bash /mnt/user/dockers/scripts/system.sh -start
#   bash /mnt/user/dockers/scripts/system.sh -stop
#   bash /mnt/user/dockers/scripts/system.sh -restart
#   bash /mnt/user/dockers/scripts/system.sh -status
#   bash /mnt/user/dockers/scripts/system.sh -logs
#   bash /mnt/user/dockers/scripts/system.sh -routes
#
# Application lancée depuis : /mnt/user/dockers/system
# Logs centraux dans       : /mnt/user/dockers/logs/system
# Secret/config dans       : /mnt/user/dockers/conf
#
# Dans /boot/config/go :
#   /bin/bash /mnt/user/dockers/scripts/system.sh start &
# ============================================================

DOCKERS_DIR="/mnt/user/dockers"
APP_DIR="$DOCKERS_DIR/system"
APP_MODULE="app:app"

REQ_FILE="$APP_DIR/requirements.txt"
INIT_DIR="${INIT_DIR:-$DOCKERS_DIR/init}"
INTEGRITY_MANIFEST="${INTEGRITY_MANIFEST:-$INIT_DIR/system.sha256}"
INTEGRITY_ARCHIVE="${INTEGRITY_ARCHIVE:-$INIT_DIR/system.tar.gz}"
INTEGRITY_ENABLED="${INTEGRITY_ENABLED:-1}"
INTEGRITY_ROOT_DIR="${INTEGRITY_ROOT_DIR:-${DOCKERS_DIR}}"
INTEGRITY_INCLUDE_DIRS="${INTEGRITY_INCLUDE_DIRS:-system scripts offline bin}"
CONF_DIR="$DOCKERS_DIR/conf"
SECRET_FILE="$CONF_DIR/flask_system.secret_key"

LOG_DIR="$DOCKERS_DIR/logs/system"
LOG_FILE="$LOG_DIR/flask_system.log"
PID_FILE="/var/run/flask_system.pid"

HOST="0.0.0.0"
PORT="5055"

WORKERS="2"
THREADS="4"

RAW_ACTION="${1:-start}"

# Compatibilité historique :
#   start / stop / restart / status / logs / routes
# Compatibilité ajoutée :
#   -start / -stop / -restart / -status / -logs / -routes
#   --start / --stop / --restart / --status / --logs / --routes
case "$RAW_ACTION" in
    --*) ACTION="${RAW_ACTION#--}" ;;
    -*)  ACTION="${RAW_ACTION#-}" ;;
    *)   ACTION="$RAW_ACTION" ;;
esac

mkdir -p "$CONF_DIR" "$LOG_DIR"

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
REQEOF
    fi
}

install_deps() {
    make_requirements_if_missing

    log "Vérification/installation des dépendances Python..."
    python3 -m pip install --root-user-action=ignore -r "$REQ_FILE" 2>&1 | tee -a "$LOG_FILE"
    local rc=${PIPESTATUS[0]}

    if [ "$rc" -ne 0 ]; then
        die "pip install a échoué avec le code $rc"
    fi
}

ensure_secret_key() {
    if [ ! -f "$SECRET_FILE" ] || [ ! -s "$SECRET_FILE" ]; then
        log "Création SECRET_KEY : $SECRET_FILE"
        SECRET_FILE="$SECRET_FILE" python3 - <<'PY' 2>&1 | tee -a "$LOG_FILE"
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

    export SECRET_KEY="$(cat "$SECRET_FILE" 2>/dev/null)"

    if [ -z "$SECRET_KEY" ]; then
        die "SECRET_KEY vide"
    fi
}

is_running() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid="$(cat "$PID_FILE" 2>/dev/null)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

show_routes() {
    log "Routes Flask actuellement chargées :"
    (
        cd "$APP_DIR" || exit 1
        export SECRET_KEY="$(cat "$SECRET_FILE" 2>/dev/null)"
        python3 - <<'PY'
from app import app
for rule in sorted(app.url_map.iter_rules(), key=lambda r: str(r)):
    methods = ",".join(sorted(rule.methods - {"HEAD", "OPTIONS"}))
    print(f"  {rule}  [{methods}]")
PY
    ) 2>&1 | tee -a "$LOG_FILE"
}

status_app() {
    echo "========== STATUS SYSTEM FLASK =========="
    echo "DOCKERS_DIR: $DOCKERS_DIR"
    echo "APP_DIR    : $APP_DIR"
    echo "CONF_DIR   : $CONF_DIR"
    echo "APP_MODULE : $APP_MODULE"
    echo "INIT_DIR   : $INIT_DIR"
    echo "MANIFEST   : $INTEGRITY_MANIFEST"
    echo "ARCHIVE    : $INTEGRITY_ARCHIVE"
    echo "ROOT       : $INTEGRITY_ROOT_DIR"
    echo "DIRS       : $INTEGRITY_INCLUDE_DIRS"
    echo "PORT       : $PORT"
    echo "PID_FILE  : $PID_FILE"
    echo "LOG_FILE  : $LOG_FILE"
    echo

    if is_running; then
        local pid
        pid="$(cat "$PID_FILE")"
        echo "ÉTAT      : EN COURS"
        echo "PID       : $pid"
        ps -fp "$pid"
        echo
        echo "Port :"
        ss -lntp 2>/dev/null | grep ":$PORT " || echo "Port $PORT non vu par ss"
    else
        echo "ÉTAT      : ARRÊTÉ"
        if [ -f "$PID_FILE" ]; then
            echo "PID mort détecté : $(cat "$PID_FILE" 2>/dev/null)"
        fi
    fi
    echo "========================================="
}

stop_app() {
    log "Demande d'arrêt System Flask..."

    if is_running; then
        local pid
        pid="$(cat "$PID_FILE")"
        log "Arrêt Gunicorn PID $pid"
        kill "$pid" 2>/dev/null || true

        for i in {1..20}; do
            if kill -0 "$pid" 2>/dev/null; then
                sleep 0.5
            else
                break
            fi
        done

        if kill -0 "$pid" 2>/dev/null; then
            log "Le PID $pid résiste, kill -9"
            kill -9 "$pid" 2>/dev/null || true
        fi

        rm -f "$PID_FILE"
        log "System Flask arrêté"
    else
        log "System Flask déjà arrêté"
        rm -f "$PID_FILE"
    fi
}

start_app() {
    echo
    log "=================================================="
    log "DÉMARRAGE SYSTEM FLASK"
    log "DOCKERS_DIR=$DOCKERS_DIR"
    log "APP_DIR=$APP_DIR"
    log "CONF_DIR=$CONF_DIR"
    log "INIT_DIR=$INIT_DIR"
    log "INTEGRITY_MANIFEST=$INTEGRITY_MANIFEST"
    log "INTEGRITY_ROOT_DIR=$INTEGRITY_ROOT_DIR"
    log "INTEGRITY_INCLUDE_DIRS=$INTEGRITY_INCLUDE_DIRS"
    log "APP_MODULE=$APP_MODULE"
    log "PORT=$PORT"
    log "LOG_FILE=$LOG_FILE"
    log "=================================================="

    command -v python3 >/dev/null 2>&1 || die "python3 introuvable"

    [ -d "$APP_DIR" ] || die "dossier APP_DIR introuvable : $APP_DIR"
    integrity_check_and_restore
    [ -f "$APP_DIR/app.py" ] || die "app.py introuvable dans $APP_DIR"

    if is_running; then
        local pid
        pid="$(cat "$PID_FILE")"
        log "Déjà lancé avec PID $pid"
        status_app
        exit 0
    fi

    if [ -f "$PID_FILE" ]; then
        log "Nettoyage PID mort : $(cat "$PID_FILE" 2>/dev/null)"
        rm -f "$PID_FILE"
    fi

    install_deps
    ensure_secret_key

    log "Test import Flask app + affichage des routes avant lancement..."
    show_routes

    if ss -lnt 2>/dev/null | grep -q ":$PORT "; then
        log "ATTENTION : le port $PORT est déjà occupé :"
        ss -lntp 2>/dev/null | grep ":$PORT " | tee -a "$LOG_FILE"
        die "port $PORT déjà utilisé"
    fi

    cd "$APP_DIR" || die "impossible d'entrer dans $APP_DIR"

    log "Lancement Gunicorn en arrière-plan..."
    log "Commande : python3 -m gunicorn --workers $WORKERS --threads $THREADS --bind $HOST:$PORT $APP_MODULE"

    nohup python3 -m gunicorn \
        --workers "$WORKERS" \
        --threads "$THREADS" \
        --bind "$HOST:$PORT" \
        "$APP_MODULE" >> "$LOG_FILE" 2>&1 &

    local new_pid=$!
    echo "$new_pid" > "$PID_FILE"

    sleep 2

    if kill -0 "$new_pid" 2>/dev/null; then
        log "System Flask lancé avec PID $new_pid"
        log "URL : http://IP_UNRAID:$PORT"
        status_app
    else
        log "Gunicorn s'est arrêté juste après le démarrage."
        rm -f "$PID_FILE"
        log "Dernières lignes du log :"
        tail -80 "$LOG_FILE"
        exit 1
    fi
}

restart_app() {
    stop_app
    echo
    start_app
}

show_logs() {
    touch "$LOG_FILE"
    tail -150 "$LOG_FILE"
}

case "$ACTION" in
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
        ensure_secret_key
        show_routes
        ;;
    integrity)
        [ -d "$APP_DIR" ] || die "dossier APP_DIR introuvable : $APP_DIR"
        integrity_check_and_restore
        ;;
    add|aa)
        shift || true
        integrity_add_reference "$@"
        ;;
    backup)
        integrity_backup_snapshot
        ;;
    restaure|restore)
        shift || true
        integrity_restore_snapshot "$@"
        ;;
    *)
        echo "Usage : bash /mnt/user/dockers/scripts/system.sh {start|stop|restart|status|logs|routes|integrity|add|aa|backup|restaure} ou {-start|-stop|-restart|-status|-logs|-routes|-integrity|-add|-aa|-backup|-restaure}"
        exit 1
        ;;
esac
