#!/usr/bin/env bash
set -u

SERVER="192.168.1.3"
BASE="/mnt/smb"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LIST_FILE="$SCRIPT_DIR/mnt.txt"
MOUNT_OPTS="guest,vers=3.0,noperm,file_mode=0777,dir_mode=0777,iocharset=utf8"

if [[ $EUID -ne 0 ]]; then
    exec sudo "$0" "$@"
fi

if [[ ! -f "$LIST_FILE" ]]; then
    echo "ERREUR : fichier introuvable : $LIST_FILE" >&2
    exit 1
fi

if ! command -v mount.cifs >/dev/null 2>&1; then
    echo "ERREUR : mount.cifs est absent. Installe cifs-utils." >&2
    exit 1
fi

local_name() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

mount_all() {
    mkdir -p "$BASE"

    # Évite de lancer 18 montages si le serveur SMB est éteint.
    if command -v timeout >/dev/null 2>&1; then
        if ! timeout 2 bash -c ">/dev/tcp/$SERVER/445" 2>/dev/null; then
            echo "ERREUR : serveur SMB $SERVER inaccessible sur le port 445." >&2
            exit 1
        fi
    fi

    while IFS= read -r SHARE || [[ -n "$SHARE" ]]; do
        SHARE="${SHARE%$'\r'}"
        [[ -z "$SHARE" || "$SHARE" == \#* ]] && continue

        LOCAL="$(local_name "$SHARE")"
        TARGET="$BASE/$LOCAL"
        mkdir -p "$TARGET"

        if mountpoint -q "$TARGET"; then
            echo "[OK] Déjà monté : $TARGET"
            continue
        fi

        echo "[MOUNT] //$SERVER/$SHARE -> $TARGET"
        if mount -t cifs "//$SERVER/$SHARE" "$TARGET" -o "$MOUNT_OPTS"; then
            echo "[OK] $SHARE"
        else
            echo "[ERREUR] Impossible de monter $SHARE" >&2
        fi
    done < "$LIST_FILE"
}

unmount_all() {
    mapfile -t SHARES < <(sed 's/\r$//' "$LIST_FILE" | sed '/^[[:space:]]*$/d;/^[[:space:]]*#/d')

    for (( i=${#SHARES[@]}-1; i>=0; i-- )); do
        SHARE="${SHARES[$i]}"
        LOCAL="$(local_name "$SHARE")"
        TARGET="$BASE/$LOCAL"

        if mountpoint -q "$TARGET"; then
            echo "[UNMOUNT] $TARGET"
            if umount "$TARGET"; then
                echo "[OK] $TARGET"
            else
                echo "[ERREUR] Impossible de démonter $TARGET" >&2
            fi
        else
            echo "[OK] Non monté : $TARGET"
        fi
    done
}

case "${1:-}" in
    --mount)
        mount_all
        ;;
    --unmount)
        unmount_all
        ;;
    *)
        echo "Usage : $0 --mount | --unmount"
        exit 2
        ;;
esac
