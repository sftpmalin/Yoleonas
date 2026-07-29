#!/bin/bash
set -Eeuo pipefail

# First boot installer for the YOLEO system service.
# It creates the integrity archive from the installed YOLEO payload when needed,
# then delegates the service installation to system.sh.

SERVICE_NAME="${SERVICE_NAME:-flask-system.service}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-12345}"
SYSTEM_SH_NAME="${SYSTEM_SH_NAME:-system.sh}"

SCRIPT_PATH="${BASH_SOURCE[0]}"
if readlink_bin="$(command -v readlink)"; then
    if candidate="$("$readlink_bin" -f "${BASH_SOURCE[0]}")"; then
        SCRIPT_PATH="$candidate"
    fi
elif realpath_bin="$(command -v realpath)"; then
    if candidate="$("$realpath_bin" "${BASH_SOURCE[0]}")"; then
        SCRIPT_PATH="$candidate"
    fi
fi
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd -P)"
YOLEO_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"

SYSTEM_SH="${SYSTEM_SH:-${SCRIPT_DIR}/${SYSTEM_SH_NAME}}"
INIT_DIR="${INIT_DIR:-${YOLEO_DIR}/init}"
INTEGRITY_MANIFEST="${INTEGRITY_MANIFEST:-${INIT_DIR}/system.sha256}"
INTEGRITY_ARCHIVE="${INTEGRITY_ARCHIVE:-${INIT_DIR}/system.tar.gz}"
INTEGRITY_ARCHIVE_SHA="${INTEGRITY_ARCHIVE}.sha256"
INTEGRITY_ROOT_DIR="${INTEGRITY_ROOT_DIR:-${YOLEO_DIR}}"
INTEGRITY_INCLUDE_DIRS="${INTEGRITY_INCLUDE_DIRS:-system scripts offline bin}"

APP_DIR="${APP_DIR:-${SCRIPT_DIR}}"
APP_FILE="${APP_FILE:-${APP_DIR}/app.py}"
APP_MODULE="${APP_MODULE:-app:app}"
VENV_DIR="${VENV_DIR:-${APP_DIR}/.venv}"
REQ_FILE="${REQ_FILE:-${APP_DIR}/requirements.txt}"
CONF_DIR="${CONF_DIR:-../conf}"
ENV_FILE="${ENV_FILE:-${CONF_DIR}/.env}"
SECRET_FILE="${SECRET_FILE:-${CONF_DIR}/secret.key}"

LOG_DIR="${LOG_DIR:-/var/log/flask-system}"
LOG_FILE="${LOG_FILE:-${LOG_DIR}/flask_system.log}"

log() {
    local message="$*"
    if mkdir -p "$LOG_DIR"; then
        printf '%s %s\n' "$(date '+%F %T')" "$message" | tee -a "$LOG_FILE"
    else
        printf '%s %s\n' "$(date '+%F %T')" "$message"
    fi
}

die() {
    log "ERREUR: $*"
    exit 1
}

usage() {
    cat <<USAGE
Usage: $0 [-install|-archive]

  -install   Cree l'archive system.tar.gz si besoin, puis installe le service.
  -archive   Cree seulement l'archive d'integrite si elle manque.
USAGE
}

normalize_action() {
    local action="${1:--install}"
    action="${action#-}"
    printf '%s' "${action,,}"
}

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        die "ce script doit etre execute en root."
    fi
}

ensure_system_sh() {
    [ -f "$SYSTEM_SH" ] || die "system.sh introuvable: $SYSTEM_SH"
}

archive_is_valid() {
    local expected_hash expected_size _ actual_hash actual_size

    [ -s "$INTEGRITY_MANIFEST" ] || return 1
    [ -s "$INTEGRITY_ARCHIVE" ] || return 1
    [ -s "$INTEGRITY_ARCHIVE_SHA" ] || return 1

    read -r expected_hash expected_size _ < "$INTEGRITY_ARCHIVE_SHA" || return 1
    actual_hash="$(sha256sum "$INTEGRITY_ARCHIVE")" || return 1
    actual_hash="${actual_hash%% *}"
    actual_size="$(stat -c '%s' "$INTEGRITY_ARCHIVE" || wc -c < "$INTEGRITY_ARCHIVE" | tr -d ' ')" || return 1

    [ "$expected_hash" = "$actual_hash" ] && [ "$expected_size" = "$actual_size" ]
}

create_integrity_archive() {
    local tmp_files tmp_manifest tmp_archive
    local records=0
    local rel file_path file_hash file_size archive_hash archive_size

    mkdir -p "$INIT_DIR"

    tmp_files="$(mktemp "${INIT_DIR}/system.files.XXXXXX")"
    tmp_manifest="$(mktemp "${INIT_DIR}/system.sha256.XXXXXX")"
    tmp_archive="$(mktemp "${INIT_DIR}/system.tar.gz.XXXXXX")"

    for dirname in $INTEGRITY_INCLUDE_DIRS; do
        local include_path="${INTEGRITY_ROOT_DIR}/${dirname}"
        [ -e "$include_path" ] || continue
        [ -d "$include_path" ] && [ ! -L "$include_path" ] || die "dossier protege invalide: $include_path"

        (
            cd "$INTEGRITY_ROOT_DIR"
            find "$dirname" -type f \
                ! -path '*/.git/*' \
                ! -path '*/.venv/*' \
                ! -path '*/__pycache__/*' \
                ! -path '*/.pytest_cache/*' \
                ! -path '*/node_modules/*' \
                ! -path '*/backups/*' \
                ! -name '.DS_Store' \
                ! -name 'gunicorn.ctl' \
                ! -name '*.pyc' \
                ! -name '*.pyo' \
                -print0
        )
    done | sort -z > "$tmp_files"

    {
        printf '# YoLeo system integrity catalog v2\n'
        printf '# Format: sha256<TAB>size_bytes<TAB>relative_path\n'
        printf '# Root: %s ; protected dirs: %s\n' "$(basename "$INTEGRITY_ROOT_DIR")" "$INTEGRITY_INCLUDE_DIRS"
        printf '# Archive: system.tar.gz\n'

        while IFS= read -r -d '' rel; do
            file_path="${INTEGRITY_ROOT_DIR}/${rel}"
            file_hash="$(sha256sum "$file_path")" || die "sha256 impossible: $file_path"
            file_hash="${file_hash%% *}"
            file_size="$(stat -c '%s' "$file_path" || wc -c < "$file_path" | tr -d ' ')" || die "taille impossible: $file_path"
            printf '%s\t%s\t%s\n' "$file_hash" "$file_size" "$rel"
            records=$((records + 1))
        done < "$tmp_files"
    } > "$tmp_manifest"

    [ "$records" -gt 0 ] || die "aucun fichier a archiver depuis $INTEGRITY_ROOT_DIR"

    tar -C "$INTEGRITY_ROOT_DIR" --null --files-from "$tmp_files" -czf "$tmp_archive"

    mv "$tmp_manifest" "$INTEGRITY_MANIFEST"
    mv "$tmp_archive" "$INTEGRITY_ARCHIVE"

    archive_hash="$(sha256sum "$INTEGRITY_ARCHIVE")" || die "sha256 archive impossible: $INTEGRITY_ARCHIVE"
    archive_hash="${archive_hash%% *}"
    archive_size="$(stat -c '%s' "$INTEGRITY_ARCHIVE" || wc -c < "$INTEGRITY_ARCHIVE" | tr -d ' ')" || die "taille archive impossible: $INTEGRITY_ARCHIVE"
    printf '%s\t%s\t%s\n' "$archive_hash" "$archive_size" "$(basename "$INTEGRITY_ARCHIVE")" > "$INTEGRITY_ARCHIVE_SHA"

    rm -f "$tmp_files"

    log "Archive creee: $INTEGRITY_ARCHIVE"
    log "Manifest cree: $INTEGRITY_MANIFEST"
    log "Fichiers catalogues: $records"
}

ensure_integrity_archive() {
    if archive_is_valid; then
        log "Archive d'integrite deja presente: $INTEGRITY_ARCHIVE"
        return 0
    fi

    log "Archive d'integrite absente ou incomplete, creation depuis: $INTEGRITY_ROOT_DIR"
    log "Dossiers inclus: $INTEGRITY_INCLUDE_DIRS"
    create_integrity_archive
}

install_system() {
    need_root
    ensure_system_sh
    ensure_integrity_archive

    export SERVICE_NAME HOST PORT
    export APP_DIR APP_FILE APP_MODULE VENV_DIR REQ_FILE
    export CONF_DIR ENV_FILE SECRET_FILE
    export INIT_DIR INTEGRITY_MANIFEST INTEGRITY_ARCHIVE INTEGRITY_ROOT_DIR INTEGRITY_INCLUDE_DIRS
    export DOCKERS_DIR="$YOLEO_DIR"

    log "Lancement de system.sh -install sur le port ${PORT}."
    exec bash "$SYSTEM_SH" -install
}

main() {
    local action
    action="$(normalize_action "${1:--install}")"

    case "$action" in
        install)
            install_system
            ;;
        archive|add)
            ensure_integrity_archive
            ;;
        help|h)
            usage
            ;;
        *)
            usage
            exit 2
            ;;
    esac
}

main "$@"
