#!/bin/bash
set -Eeuo pipefail

# ============================================================
# mdns.sh - Debian LABO local mDNS helper
#
# Commandes :
#   bash mdns.sh start
#   bash mdns.sh stop
#   bash mdns.sh restart
#   bash mdns.sh status
#   bash mdns.sh log
#   bash mdns.sh service
#   bash mdns.sh service-remove
#
# Alias acceptés aussi :
#   bash mdns.sh -start
#   bash mdns.sh -stop
#   bash mdns.sh -stat
#   bash mdns.sh -status
#   bash mdns.sh -remove
#
# Principe chemins :
#   - le script retrouve automatiquement son propre dossier
#   - depuis <base>/scripts, il remonte avec ../ vers <base>
#   - seul CONF_DIR peut être forcé si besoin
#
# Dépendances Debian :
#   avahi-daemon
#   avahi-utils
#   libnss-mdns
# ============================================================

SELF_PATH="$(readlink -f "$0")"
SCRIPT_DIR="$(cd "$(dirname "$SELF_PATH")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

CONF_DIR="${CONF_DIR:-$BASE_DIR/conf}"
LOG_DIR="${LOG_DIR:-$BASE_DIR/logs/mdns}"
RUN_DIR="/var/run/mdns"

CONF="${MDNS_CONF:-$CONF_DIR/mdns.conf}"
LOG="$LOG_DIR/mdns.log"
PID_FILE="$RUN_DIR/mdns-publish.pids"

AVAHI_HOSTS="/etc/avahi/hosts"
RUNTIME_HOSTS="$RUN_DIR/mdns.hosts"

SERVICE_NAME="mdns-labo.service"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME"

mkdir -p "$CONF_DIR" "$LOG_DIR" "$RUN_DIR"

ts() {
    date '+%F %T'
}

say() {
    echo "$(ts) | $*" | tee -a "$LOG"
}

line() {
    say "------------------------------------------------------------"
}

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        say "ERROR: lance ce script en root"
        exit 1
    fi
}

create_default_conf() {
    if [ ! -f "$CONF" ]; then
        cat > "$CONF" <<'EOC'
# Format normal: IP=name.local
# Exemple:
# 192.168.1.104=systeme.local
# 192.168.1.104=flask.local

192.168.1.104=systeme.local
192.168.1.104=flask.local
EOC
        say "OK: config created: $CONF"
    else
        say "OK: existing config kept: $CONF"
    fi
}

trim() {
    echo "$1" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

parse_conf_line() {
    RAW_LINE="$1"
    LINE="${RAW_LINE%%#*}"
    LINE="$(trim "$LINE")"

    PARSED_IP=""
    PARSED_NAME=""
    PARSE_ERROR=""

    [ -z "$LINE" ] && return 1

    if ! echo "$LINE" | grep -q '='; then
        PARSE_ERROR="missing '=' separator, expected IP=name.local"
        return 2
    fi

    PARSED_IP="$(trim "${LINE%%=*}")"
    PARSED_NAME="$(trim "${LINE#*=}")"

    if [ -z "$PARSED_IP" ] || [ -z "$PARSED_NAME" ]; then
        PARSE_ERROR="empty IP or name, expected IP=name.local"
        return 2
    fi

    return 0
}

write_runtime_hosts() {
    line
    say "Generate runtime hosts file for Avahi"

    : > "$RUNTIME_HOSTS"

    while IFS= read -r RAW_LINE || [ -n "$RAW_LINE" ]; do
        PARSE_STATUS=0
        parse_conf_line "$RAW_LINE" || PARSE_STATUS=$?
        [ "$PARSE_STATUS" -eq 1 ] && continue
        [ "$PARSE_STATUS" -ne 0 ] && continue

        echo "$PARSED_IP $PARSED_NAME" >> "$RUNTIME_HOSTS"
    done < "$CONF"

    say "OK: runtime hosts generated: $RUNTIME_HOSTS"
    return 0
}

show_diag() {
    line
    say "Diagnostic environment"
    say "SELF_PATH   = $SELF_PATH"
    say "SCRIPT_DIR  = $SCRIPT_DIR"
    say "BASE_DIR    = $BASE_DIR"
    say "CONF_DIR    = $CONF_DIR"
    say "CONF        = $CONF"
    say "LOG         = $LOG"
    say "RUN_DIR     = $RUN_DIR"
    say "SERVICE     = $SERVICE_NAME"
    say "hostname    = $(hostname 2>/dev/null || true)"
    say "hostname -I = $(hostname -I 2>/dev/null || true)"
    say "default IP  = $(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
    say "default IF  = $(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $5; exit}')"

    say "IPv4 interfaces:"
    ip -br -4 addr 2>/dev/null | tee -a "$LOG" || true

    say "UDP 5353 listeners:"
    ss -lunp 2>/dev/null | grep ':5353' | tee -a "$LOG" || say "No UDP 5353 listener visible"
}

show_conf() {
    line
    say "Config content: $CONF"
    nl -ba "$CONF" | tee -a "$LOG"
}

validate_conf() {
    line
    say "Validate config"

    if [ ! -f "$CONF" ]; then
        say "ERROR: config not found: $CONF"
        exit 1
    fi

    VALID_LINES=0
    ERRORS=0

    while IFS= read -r RAW_LINE || [ -n "$RAW_LINE" ]; do
        PARSE_STATUS=0
        parse_conf_line "$RAW_LINE" || PARSE_STATUS=$?
        [ "$PARSE_STATUS" -eq 1 ] && continue

        if [ "$PARSE_STATUS" -ne 0 ]; then
            say "ERROR: $PARSE_ERROR: $RAW_LINE"
            ERRORS=$((ERRORS + 1))
            continue
        fi

        IP="$PARSED_IP"
        NAME="$PARSED_NAME"

        if ! echo "$IP" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'; then
            say "ERROR: invalid IP: $RAW_LINE"
            ERRORS=$((ERRORS + 1))
            continue
        fi

        OLD_IFS="$IFS"
        IFS='.'
        set -- $IP
        IFS="$OLD_IFS"
        for OCTET in "$@"; do
            if [ "$OCTET" -gt 255 ] 2>/dev/null; then
                say "ERROR: invalid IP octet: $RAW_LINE"
                ERRORS=$((ERRORS + 1))
                continue 2
            fi
        done

        if ! echo "$NAME" | grep -Eq '^[A-Za-z0-9-]+\.local$'; then
            say "ERROR: invalid name, expected name.local: $RAW_LINE"
            ERRORS=$((ERRORS + 1))
            continue
        fi

        VALID_LINES=$((VALID_LINES + 1))
    done < "$CONF"

    if [ "$ERRORS" -gt 0 ]; then
        say "ERROR: config has $ERRORS error(s)"
        exit 1
    fi

    if [ "$VALID_LINES" -eq 0 ]; then
        say "ERROR: config has no valid entries"
        exit 1
    fi

    say "OK: config syntax looks good ($VALID_LINES entries)"
    write_runtime_hosts
}

stop_publishers() {
    line
    say "Stop previous avahi-publish-address processes started by this script"

    if [ -f "$PID_FILE" ]; then
        while IFS= read -r PID; do
            [ -z "$PID" ] && continue
            if kill -0 "$PID" 2>/dev/null; then
                say "Stopping avahi-publish-address PID $PID"
                kill "$PID" 2>/dev/null || true
            fi
        done < "$PID_FILE"
        rm -f "$PID_FILE"
    else
        say "No publisher PID file"
    fi

    # Sécurité : nettoie uniquement les publications qui correspondent au conf actuel.
    if [ -f "$CONF" ]; then
        while IFS= read -r RAW_LINE || [ -n "$RAW_LINE" ]; do
            PARSE_STATUS=0
            parse_conf_line "$RAW_LINE" || PARSE_STATUS=$?
            [ "$PARSE_STATUS" -eq 1 ] && continue
            [ "$PARSE_STATUS" -ne 0 ] && continue

            pkill -f "avahi-publish-address[[:space:]]+$PARSED_NAME[[:space:]]+$PARSED_IP" 2>/dev/null || true
        done < "$CONF"
    fi
}

restart_avahi_daemon() {
    if command -v systemctl >/dev/null 2>&1; then
        say "Restart avahi: systemctl restart avahi-daemon"
        systemctl restart avahi-daemon 2>&1 | tee -a "$LOG"
    elif command -v service >/dev/null 2>&1; then
        say "Restart avahi: service avahi-daemon restart"
        service avahi-daemon restart 2>&1 | tee -a "$LOG"
    else
        say "No systemctl/service found, try HUP avahi-daemon"
        pkill -HUP avahi-daemon 2>/dev/null || true
    fi
}

ensure_avahi_available() {
    if ! command -v avahi-daemon >/dev/null 2>&1; then
        say "ERROR: avahi-daemon introuvable"
        say "Installe : apt install avahi-daemon avahi-utils libnss-mdns"
        exit 1
    fi

    if ! command -v avahi-publish-address >/dev/null 2>&1; then
        say "ERROR: avahi-publish-address introuvable"
        say "Installe : apt install avahi-utils"
        exit 1
    fi

    if pgrep avahi-daemon >/dev/null 2>&1; then
        return 0
    fi

    say "Avahi daemon not active yet, trying to start it"

    if command -v systemctl >/dev/null 2>&1; then
        systemctl enable --now avahi-daemon 2>&1 | tee -a "$LOG" || true
    elif command -v service >/dev/null 2>&1; then
        service avahi-daemon start 2>&1 | tee -a "$LOG" || true
    fi

    sleep 1

    if ! pgrep avahi-daemon >/dev/null 2>&1; then
        say "ERROR: avahi-daemon is not active and could not be started"
        exit 1
    fi
}

publish_with_avahi() {
    line
    say "Publish local names with Debian Avahi"

    ensure_avahi_available

    mkdir -p /etc/avahi

    if [ -f "$AVAHI_HOSTS" ]; then
        cp -a "$AVAHI_HOSTS" "$AVAHI_HOSTS.bak.$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
    fi

    cp "$RUNTIME_HOSTS" "$AVAHI_HOSTS"
    say "OK: copied $RUNTIME_HOSTS to $AVAHI_HOSTS"

    restart_avahi_daemon
    sleep 1

    line
    say "Start explicit avahi-publish-address entries"
    : > "$PID_FILE"

    while IFS= read -r RAW_LINE || [ -n "$RAW_LINE" ]; do
        PARSE_STATUS=0
        parse_conf_line "$RAW_LINE" || PARSE_STATUS=$?
        [ "$PARSE_STATUS" -eq 1 ] && continue
        [ "$PARSE_STATUS" -ne 0 ] && continue

        IP="$PARSED_IP"
        NAME="$PARSED_NAME"

        say "Publish: $NAME -> $IP"
        nohup avahi-publish-address "$NAME" "$IP" >> "$LOG" 2>&1 &
        echo $! >> "$PID_FILE"
    done < "$CONF"

    sleep 1
    say "Active publisher PIDs:"
    cat "$PID_FILE" | tee -a "$LOG" || true

    line
    say "UDP 5353 after Avahi setup:"
    ss -lunp 2>/dev/null | grep ':5353' | tee -a "$LOG" || say "No UDP 5353 listener visible"

    say "DONE: Avahi setup finished"
    say "Test from Windows: ipconfig /flushdns ; ping systeme.local"
    return 0
}

start() {
    line
    say "Start local mDNS"
    create_default_conf
    stop_publishers
    show_diag
    show_conf
    validate_conf
    publish_with_avahi
}

stop() {
    stop_publishers
}

restart() {
    stop
    start
}

status() {
    line
    say "mDNS status"
    say "Self path    : $SELF_PATH"
    say "Config used  : $CONF"
    say "Runtime hosts: $RUNTIME_HOSTS"
    say "Log file     : $LOG"
    say "PID file     : $PID_FILE"
    say "Service      : $SERVICE_NAME"

    say "Avahi daemon:"
    pgrep -a avahi-daemon | tee -a "$LOG" || say "avahi-daemon not active"

    say "avahi-publish-address processes:"
    if [ -f "$PID_FILE" ]; then
        while IFS= read -r PID; do
            [ -z "$PID" ] && continue
            if kill -0 "$PID" 2>/dev/null; then
                ps -fp "$PID" | tee -a "$LOG"
            else
                say "dead publisher PID in file: $PID"
            fi
        done < "$PID_FILE"
    else
        say "no PID file for publishers started by this script"
    fi

    say "UDP 5353:"
    ss -lunp 2>/dev/null | grep ':5353' | tee -a "$LOG" || say "No UDP 5353 listener visible"

    say "Systemd service:"
    systemctl --no-pager --quiet is-enabled "$SERVICE_NAME" >/dev/null 2>&1 && say "Service enabled" || say "Service disabled/non installé"
    systemctl --no-pager --quiet is-active "$SERVICE_NAME" >/dev/null 2>&1 && say "Systemd active" || say "Systemd inactive/non installé"

    show_conf
}

install_service() {
    need_root

    say "Installation service systemd : $SERVICE_NAME"
    say "Script utilisé par le service : $SELF_PATH"

    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=mDNS local names LABO
After=network-online.target avahi-daemon.service
Wants=network-online.target avahi-daemon.service

[Service]
Type=oneshot
ExecStart=/bin/bash $SELF_PATH start
ExecStop=/bin/bash $SELF_PATH stop
ExecReload=/bin/bash $SELF_PATH restart
RemainAfterExit=yes
TimeoutStartSec=120
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    systemctl restart "$SERVICE_NAME"

    say "OK: service installé et lancé : $SERVICE_NAME"
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

ACTION="${1:-start}"
case "$ACTION" in
    start|-start|--start)
        start
        ;;
    restart|-restart|--restart)
        restart
        ;;
    stop|-stop|--stop)
        stop
        ;;
    status|stat|-status|--status|-stat|--stat)
        status
        ;;
    log|logs|-log|--log|-logs|--logs)
        touch "$LOG"
        tail -f "$LOG"
        ;;
    service|services|install-service|service-install|-service|--service)
        install_service
        ;;
    service-remove|remove-service|uninstall-service|-remove|--remove|-service-remove|--service-remove)
        remove_service
        ;;
    *)
        echo "Usage: bash $0 {start|restart|stop|status|log|service|service-remove}"
        echo "Alias acceptés : -start, -stop, -stat, -status, -remove"
        exit 1
        ;;
esac
