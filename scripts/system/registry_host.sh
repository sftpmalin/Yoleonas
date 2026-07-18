#!/bin/bash
set -Eeuo pipefail

# ============================================================
# Registry Docker - mode hôte Debian LABO
# Version avec registry.conf humain + registry.yml généré seulement si besoin
#
# Organisation des chemins :
#   base          : déduite automatiquement depuis le dossier parent ../
#   script        : <base>/scripts/registry_host_labo.sh
#   binaires      : <base>/bin
#   conf humain   : <base>/conf/registry.conf
#   yaml généré   : <base>/conf/registry.yml
#   data          : obligatoire dans <base>/conf/registry.conf via DATA_DIR
#   logs          : <base>/logs/registry/registry.log
#   pid           : /var/run/registry_labo_host.pid
#   service       : /etc/systemd/system/registry-labo-host.service
#
# Commandes :
#   bash registry_host_labo.sh start
#   bash registry_host_labo.sh stop
#   bash registry_host_labo.sh restart
#   bash registry_host_labo.sh status
#   bash registry_host_labo.sh log
#   bash registry_host_labo.sh service
#   bash registry_host_labo.sh service-remove
#
# Principe :
#   - registry.conf est le fichier humain/simple obligatoire.
#   - registry.yml est généré depuis registry.conf.
#   - registry.yml n'est réécrit QUE s'il est absent ou différent.
#   - aucune valeur par défaut ne crée de dossier data fantôme.
# ============================================================

SELF_PATH="$(readlink -f "$0")"
AUTO_SCRIPT_DIR="$(cd "$(dirname "$SELF_PATH")" && pwd)"

# Depuis <base>/scripts, on remonte avec ../ vers <base>.
DOCKERS_DIR="${DOCKERS_DIR:-$(cd "$AUTO_SCRIPT_DIR/.." && pwd)}"

SCRIPT_DIR="${SCRIPT_DIR:-$DOCKERS_DIR/scripts}"
BIN_DIR="${BIN_DIR:-$DOCKERS_DIR/bin}"
CONF_DIR="${CONF_DIR:-$DOCKERS_DIR/conf}"
LOG_DIR="${LOG_DIR:-$DOCKERS_DIR/logs/registry}"

BIN_AMD64="$BIN_DIR/registry_amd64"
BIN_ARM64="$BIN_DIR/registry_arm64"
RUNTIME_BIN="/tmp/registry-host-labo"

HUMAN_CONF="$CONF_DIR/registry.conf"
CONF="$CONF_DIR/registry.yml"
LOG="$LOG_DIR/registry.log"
PID="/var/run/registry_labo_host.pid"

# Par défaut, on prend le vrai point de montage qui contient le dossier LABO.
# Si aucun point de montage dédié n'est trouvé, on retombe sur le dossier parent.
MNT_ROOT="${MNT_ROOT:-$(df -P "$DOCKERS_DIR" 2>/dev/null | awk 'NR==2 {print $6}')}"
[ -n "$MNT_ROOT" ] || MNT_ROOT="$(dirname "$DOCKERS_DIR")"
MNT_READY_DIR="${MNT_READY_DIR:-$DOCKERS_DIR}"

SERVICE_NAME="registry-labo-host.service"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME"

WAIT_INTERVAL="5"

# Registry v3 active OpenTelemetry par défaut si rien n'est précisé.
# En local/host, on coupe l'export de traces.
export OTEL_TRACES_EXPORTER="none"

# Valeurs chargées depuis registry.conf.
# On les laisse volontairement vides : si registry.conf est absent ou incomplet,
# le script doit s'arrêter au lieu d'inventer un chemin par défaut.
PORT=""
BIND_ADDR=""
DATA_DIR=""
LOG_LEVEL=""
DELETE_ENABLED=""
HTTP_SECRET=""

say() {
    echo "$(date '+%F %T') | $*"
}

die() {
    say "ERREUR: $*"
    exit 1
}

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        die "lance ce script en root"
    fi
}

trim() {
    local value="$*"
    value="${value#${value%%[![:space:]]*}}"
    value="${value%${value##*[![:space:]]}}"
    printf '%s' "$value"
}

strip_quotes() {
    local value="$1"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    printf '%s' "$value"
}

resolve_path_from_conf_dir() {
    local path="$1"

    if [ -z "$path" ]; then
        die "DATA_DIR vide dans ../conf/registry.conf"
    fi

    case "$path" in
        /*)
            printf '%s' "$path"
            ;;
        *)
            # Chemin relatif depuis le dossier conf/.
            # Exemple : DATA_DIR=../registry -> <base>/registry
            (cd "$CONF_DIR" && readlink -m "$path")
            ;;
    esac
}

load_human_conf() {
    if [ ! -f "$HUMAN_CONF" ]; then
        die "../conf/registry.conf introuvable ! chemin attendu : $HUMAN_CONF"
    fi

    # Remise à zéro volontaire à chaque relecture.
    # Comme ça, une clé supprimée du conf est vraiment détectée comme manquante.
    PORT=""
    BIND_ADDR=""
    DATA_DIR=""
    LOG_LEVEL=""
    DELETE_ENABLED=""
    HTTP_SECRET=""

    local line key value

    while IFS= read -r line || [ -n "$line" ]; do
        # Supprime CR Windows si fichier édité ailleurs.
        line="${line%$'\r'}"

        # Ignore commentaires et lignes vides.
        case "$(trim "$line")" in
            ""|\#*) continue ;;
        esac

        if [[ "$line" != *=* ]]; then
            continue
        fi

        key="$(trim "${line%%=*}")"
        value="$(trim "${line#*=}")"
        value="$(strip_quotes "$value")"

        case "$key" in
            PORT) PORT="$value" ;;
            BIND_ADDR) BIND_ADDR="$value" ;;
            DATA_DIR) DATA_DIR="$(resolve_path_from_conf_dir "$value")" ;;
            LOG_LEVEL) LOG_LEVEL="$value" ;;
            DELETE_ENABLED) DELETE_ENABLED="$value" ;;
            HTTP_SECRET) HTTP_SECRET="$value" ;;
            *) say "Info: option ignorée dans registry.conf : $key" ;;
        esac
    done < "$HUMAN_CONF"

}

require_conf_value() {
    local key="$1"
    local value="$2"

    if [ -z "$value" ]; then
        die "$key manquant ou vide dans ../conf/registry.conf"
    fi
}

validate_loaded_conf() {
    require_conf_value "PORT" "$PORT"
    require_conf_value "BIND_ADDR" "$BIND_ADDR"
    require_conf_value "DATA_DIR" "$DATA_DIR"
    require_conf_value "LOG_LEVEL" "$LOG_LEVEL"
    require_conf_value "DELETE_ENABLED" "$DELETE_ENABLED"
    require_conf_value "HTTP_SECRET" "$HTTP_SECRET"

    if ! printf '%s' "$PORT" | grep -Eq '^[0-9]+$'; then
        die "PORT invalide dans $HUMAN_CONF : $PORT"
    fi

    case "$DELETE_ENABLED" in
        true|false) ;;
        *) die "DELETE_ENABLED doit être true ou false dans $HUMAN_CONF" ;;
    esac

    case "$LOG_LEVEL" in
        debug|info|warn|warning|error|fatal|panic) ;;
        *) die "LOG_LEVEL invalide dans $HUMAN_CONF : $LOG_LEVEL" ;;
    esac
}

render_registry_yaml() {
    cat <<EOF_YAML
version: 0.1

log:
  level: $LOG_LEVEL

storage:
  filesystem:
    rootdirectory: $DATA_DIR
  delete:
    enabled: $DELETE_ENABLED

http:
  addr: $BIND_ADDR:$PORT
  secret: $HTTP_SECRET
  headers:
    X-Content-Type-Options:
      - nosniff
EOF_YAML
}

ensure_registry_yaml() {
    load_human_conf
    validate_loaded_conf

    mkdir -p "$CONF_DIR" "$DATA_DIR"

    local tmp
    tmp="$(mktemp /tmp/registry-yml.XXXXXX)"
    render_registry_yaml > "$tmp"

    if [ -f "$CONF" ] && cmp -s "$tmp" "$CONF"; then
        rm -f "$tmp"
        say "OK: registry.yml déjà correct, aucune réécriture."
        return 0
    fi

    if [ -f "$CONF" ]; then
        local backup
        backup="$CONF.backup_$(date '+%Y%m%d_%H%M%S')"
        cp -a "$CONF" "$backup"
        say "registry.yml différent, sauvegarde : $backup"
    else
        say "registry.yml absent, création : $CONF"
    fi

    mv "$tmp" "$CONF"
    say "OK: registry.yml généré : $CONF"
}

get_port_from_conf() {
    # Le port officiel vient maintenant de registry.conf.
    load_human_conf
    validate_loaded_conf
    echo "$PORT"
}

registry_pids_by_cmd() {
    # Retrouve seulement les process lancés par CE script :
    # /tmp/registry-host-labo serve <base>/conf/registry.yml
    pgrep -f "$RUNTIME_BIN serve $CONF" 2>/dev/null || true
}

adopt_running_pid_if_needed() {
    # Si le PID file a disparu mais que le process tourne encore,
    # on le reprend au lieu de croire que le registre est arrêté.
    if [ -f "$PID" ]; then
        local old
        old="$(cat "$PID" 2>/dev/null || true)"
        if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
            return 0
        fi
    fi

    local pids first
    pids="$(registry_pids_by_cmd | tr '\n' ' ' | awk '{$1=$1; print}')"
    first="$(printf '%s\n' "$pids" | awk '{print $1}')"

    if [ -n "$first" ] && kill -0 "$first" 2>/dev/null; then
        echo "$first" > "$PID"
        say "PID adopté depuis process existant : $first"
        return 0
    fi

    return 1
}

wait_storage() {
    say "Attente du stockage Debian labo avant de lancer le registry..."
    say "Check toutes les ${WAIT_INTERVAL}s : $MNT_ROOT + $MNT_READY_DIR"

    while true; do
        if mountpoint -q "$MNT_ROOT" && [ -d "$MNT_READY_DIR" ]; then
            say "OK: stockage prêt."
            say "OK: $MNT_ROOT est monté."
            say "OK: $MNT_READY_DIR existe."
            return 0
        fi

        say "Stockage pas prêt, j'attends ${WAIT_INTERVAL}s..."

        if ! mountpoint -q "$MNT_ROOT"; then
            say "- $MNT_ROOT n'est pas encore un point de montage."
        fi
        if [ ! -d "$MNT_READY_DIR" ]; then
            say "- $MNT_READY_DIR absent."
        fi

        say "Sécurité: aucun lancement tant que le stockage réel n'est pas prêt."
        sleep "$WAIT_INTERVAL"
    done
}

ensure_dirs_after_storage_ready() {
    wait_storage
    mkdir -p "$CONF_DIR" "$LOG_DIR" "$BIN_DIR"
}

detect_bin() {
    ARCH="$(uname -m)"

    case "$ARCH" in
        x86_64|amd64) BIN="$BIN_AMD64" ;;
        aarch64|arm64) BIN="$BIN_ARM64" ;;
        *) die "architecture inconnue: $ARCH" ;;
    esac
}

is_running() {
    adopt_running_pid_if_needed >/dev/null 2>&1
}

start() {
    detect_bin

    if is_running; then
        say "Registry labo déjà démarré PID $(cat "$PID")"
        exit 0
    fi

    ensure_dirs_after_storage_ready
    ensure_registry_yaml

    if [ ! -f "$BIN" ]; then
        die "binaire introuvable: $BIN"
    fi

    cp "$BIN" "$RUNTIME_BIN"
    chmod 755 "$RUNTIME_BIN"

    if [ ! -x "$RUNTIME_BIN" ]; then
        die "binaire runtime non exécutable: $RUNTIME_BIN"
    fi

    if [ ! -f "$CONF" ]; then
        die "config introuvable: $CONF"
    fi

    if ss -lntp 2>/dev/null | grep -q ":$PORT "; then
        say "ERREUR: le port $PORT est déjà utilisé."
        ss -lntp 2>/dev/null | grep ":$PORT " || true
        say "Astuce : lance status pour voir si c'est un vieux process registry sans PID."
        exit 1
    fi

    say "Démarrage du registry labo host..."
    say "Binaire source  : $BIN"
    say "Binaire runtime : $RUNTIME_BIN"
    say "Conf humain     : $HUMAN_CONF"
    say "Config YAML     : $CONF"
    say "Data            : $DATA_DIR"
    say "Log             : $LOG"
    say "PID             : $PID"
    say "Port            : $PORT"

    nohup "$RUNTIME_BIN" serve "$CONF" >> "$LOG" 2>&1 &
    echo $! > "$PID"

    sleep 1

    if is_running; then
        say "OK: Registry labo démarré PID $(cat "$PID")"
        say "URL: http://$(hostname -I | awk '{print $1}'):$PORT/v2/"
    else
        say "ERREUR: Registry labo n'a pas démarré"
        tail -n 80 "$LOG" || true
        rm -f "$PID"
        exit 1
    fi
}

stop() {
    local killed=0

    # Arrêt par PID file.
    if [ -f "$PID" ]; then
        OLD_PID="$(cat "$PID" 2>/dev/null || true)"
        if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
            say "Arrêt du registry labo PID $OLD_PID..."
            kill "$OLD_PID" 2>/dev/null || true
            killed=1

            for i in 1 2 3 4 5; do
                if kill -0 "$OLD_PID" 2>/dev/null; then
                    sleep 1
                else
                    break
                fi
            done

            if kill -0 "$OLD_PID" 2>/dev/null; then
                say "Forçage arrêt PID $OLD_PID..."
                kill -9 "$OLD_PID" 2>/dev/null || true
            fi
        fi
    fi

    # Arrêt des vieux process orphelins lancés par ce même script.
    local pids pid
    pids="$(registry_pids_by_cmd | tr '\n' ' ' | awk '{$1=$1; print}')"
    if [ -n "$pids" ]; then
        say "Nettoyage process registry orphelins : $pids"
        for pid in $pids; do
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null || true
                killed=1
            fi
        done

        sleep 1

        for pid in $pids; do
            if kill -0 "$pid" 2>/dev/null; then
                say "Forçage process orphelin PID $pid..."
                kill -9 "$pid" 2>/dev/null || true
            fi
        done
    fi

    rm -f "$PID"

    if [ "$killed" -eq 1 ]; then
        say "OK: Registry labo arrêté"
    else
        say "Registry labo déjà arrêté"
    fi
}

restart() {
    stop
    sleep 1
    start
}

status() {
    load_human_conf
    validate_loaded_conf

    echo "========== STATUS REGISTRY LABO HOST =========="
    echo "DOCKERS_DIR : $DOCKERS_DIR"
    echo "SCRIPT_DIR  : $SCRIPT_DIR"
    echo "BIN_DIR     : $BIN_DIR"
    echo "CONF HUMAIN : $HUMAN_CONF"
    echo "CONF YAML   : $CONF"
    echo "DATA_DIR    : $DATA_DIR"
    echo "LOG         : $LOG"
    echo "PID         : $PID"
    echo "PORT        : $PORT"
    echo "BIND_ADDR   : $BIND_ADDR"
    echo "LOG_LEVEL   : $LOG_LEVEL"
    echo "DELETE      : $DELETE_ENABLED"
    echo "MNT_ROOT    : $MNT_ROOT"
    echo "SERVICE     : $SERVICE_NAME"
    echo

    if is_running; then
        say "Registry labo actif PID $(cat "$PID")"
        echo "Binaire : $(readlink -f /proc/$(cat "$PID")/exe 2>/dev/null || echo '?')"
    else
        say "Registry labo arrêté"
    fi

    if mountpoint -q "$MNT_ROOT"; then
        echo "Stockage: $MNT_ROOT monté"
    else
        echo "Stockage: $MNT_ROOT NON monté"
    fi

    echo
    echo "YAML :"
    if [ -f "$CONF" ]; then
        grep -E 'rootdirectory:|addr:|level:|enabled:' "$CONF" || true
    else
        echo "registry.yml absent"
    fi

    echo
    echo "Port :"
    ss -lntp 2>/dev/null | grep ":$PORT " || echo "Port $PORT non vu par ss"

    echo
    echo "Process registry par commande :"
    registry_pids_by_cmd | xargs -r ps -fp || echo "Aucun process registry-host-labo trouvé"

    echo
    systemctl --no-pager --quiet is-enabled "$SERVICE_NAME" >/dev/null 2>&1 && echo "Service: enabled" || echo "Service: disabled/non installé"
    systemctl --no-pager --quiet is-active "$SERVICE_NAME" >/dev/null 2>&1 && echo "Systemd: active" || echo "Systemd: inactive/non installé"
    echo "==============================================="
}

show_log() {
    touch "$LOG"
    tail -f "$LOG"
}

write_service_file() {
    SELF="$(readlink -f "$0")"

    if [ ! -f "$SELF" ]; then
        die "impossible de trouver le script courant"
    fi

    # Très important :
    # on utilise /bin/bash "$SELF" start, pas "$SELF start" directement.
    # Comme ça, même si le fichier n'a pas le bit exécutable, systemd démarre quand même.
    cat > "$SERVICE_FILE" <<EOF_SERVICE
[Unit]
Description=Registry Docker LABO host
After=local-fs.target network-online.target
Wants=network-online.target
RequiresMountsFor=$MNT_ROOT

[Service]
Type=forking
PIDFile=$PID
ExecStart=/bin/bash $SELF start
ExecStop=/bin/bash $SELF stop
ExecReload=/bin/bash $SELF restart
Restart=no
TimeoutStartSec=180
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
EOF_SERVICE
}

install_service() {
    need_root

    say "Installation service systemd : $SERVICE_NAME"

    # Stoppe toute instance manuelle/orpheline avant de laisser systemd prendre la main.
    stop
    sleep 1

    write_service_file

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    systemctl reset-failed "$SERVICE_NAME" 2>/dev/null || true
    systemctl start "$SERVICE_NAME"

    say "OK: service installé et démarré : $SERVICE_NAME"
    systemctl --no-pager status "$SERVICE_NAME" || true
}

remove_service() {
    need_root

    say "Suppression service systemd : $SERVICE_NAME"

    systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload
    systemctl reset-failed "$SERVICE_NAME" 2>/dev/null || true

    say "OK: service supprimé : $SERVICE_NAME"
}

case "${1:-status}" in
    start) start ;;
    stop) stop ;;
    restart) restart ;;
    status) status ;;
    log|logs) show_log ;;
    service|services|install-service|service-install) install_service ;;
    service-remove|remove-service|uninstall-service) remove_service ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|log|logs|service|service-remove}"
        exit 1
        ;;
esac
