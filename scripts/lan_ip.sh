#!/bin/bash
# ============================================================
# IP.sh - Bannière console façon OMV pour Debian
# ============================================================
#
# But :
#   Afficher les interfaces réseau + IP avant le login console,
#   dans /etc/issue, là où tu tapes root.
#
# Usage :
#   sudo bash IP.sh
#   sudo bash IP.sh -install
#   sudo bash IP.sh -update
#   sudo bash IP.sh -show
#   sudo bash IP.sh -remove
#
# Fichiers créés :
#   /usr/local/sbin/update-issue.sh
#   /etc/systemd/system/update-issue.service
#   /etc/issue.backup_IP_YYYYMMDD_HHMMSS
#
# Notes :
#   - Ne touche pas au réseau.
#   - Ne modifie pas SSH.
#   - Ne modifie pas les interfaces.
#   - Écrit seulement /etc/issue.
# ============================================================

set -euo pipefail

SERVICE_FILE="/etc/systemd/system/update-issue.service"
GENERATOR_FILE="/usr/local/sbin/update-issue.sh"
ISSUE_FILE="/etc/issue"

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "ERREUR : lance ce script en root."
        echo "Exemple : sudo bash IP.sh -install"
        exit 1
    fi
}

backup_issue() {
    if [ -f "$ISSUE_FILE" ]; then
        local stamp
        stamp="$(date +%Y%m%d_%H%M%S)"
        cp -a "$ISSUE_FILE" "${ISSUE_FILE}.backup_IP_${stamp}"
        echo "Sauvegarde créée : ${ISSUE_FILE}.backup_IP_${stamp}"
    fi
}

write_generator() {
    cat > "$GENERATOR_FILE" <<'EOF'
#!/bin/bash
set -euo pipefail

ISSUE_FILE="/etc/issue"

hostname_short="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo debian)"
kernel="$(uname -r 2>/dev/null || echo '-')"
date_now="$(date '+%Y-%m-%d %H:%M:%S')"

default_iface="-"
default_gateway="-"

if command -v ip >/dev/null 2>&1; then
    default_line="$(ip route show default 2>/dev/null | head -n 1 || true)"
    if [ -n "$default_line" ]; then
        default_iface="$(echo "$default_line" | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')"
        default_gateway="$(echo "$default_line" | awk '{for(i=1;i<=NF;i++) if($i=="via"){print $(i+1); exit}}')"
        [ -n "$default_iface" ] || default_iface="-"
        [ -n "$default_gateway" ] || default_gateway="-"
    fi
fi

tmp_ips="$(mktemp)"
trap 'rm -f "$tmp_ips"' EXIT

if command -v ip >/dev/null 2>&1; then
    ip -o -4 addr show scope global 2>/dev/null | while read -r num iface fam cidr rest; do
        iface="${iface%%@*}"
        ipaddr="${cidr%%/*}"

        case "$iface" in
            lo|docker*|veth*|virbr*|vnet*|tap*|tun*|br-*)
                continue
                ;;
        esac

        speed="-"
        state="-"

        if [ -r "/sys/class/net/$iface/operstate" ]; then
            state="$(cat "/sys/class/net/$iface/operstate" 2>/dev/null || echo '-')"
        fi

        if [ -r "/sys/class/net/$iface/speed" ]; then
            raw_speed="$(cat "/sys/class/net/$iface/speed" 2>/dev/null || echo '-')"
            case "$raw_speed" in
                ''|-1) speed="-" ;;
                *) speed="${raw_speed} Mb/s" ;;
            esac
        fi

        marker=" "
        if [ "$iface" = "$default_iface" ]; then
            marker="*"
        fi

        printf "  %s %-12s %-15s état=%-7s lien=%s\n" "$marker" "$iface" "$ipaddr" "$state" "$speed" >> "$tmp_ips"
    done
fi

if [ ! -s "$tmp_ips" ]; then
    echo "  Aucune IP IPv4 globale détectée pour le moment." > "$tmp_ips"
fi

main_ip="$(awk '$1=="*" {print $3; exit}' "$tmp_ips" 2>/dev/null || true)"
if [ -z "$main_ip" ]; then
    main_ip="$(awk '{print $3; exit}' "$tmp_ips" 2>/dev/null || true)"
fi
[ -n "$main_ip" ] || main_ip="IP_DU_SERVEUR"

{
    echo "============================================================"
    echo " Debian NAS - ${hostname_short}"
    echo " Kernel : ${kernel}"
    echo " Date   : ${date_now}"
    echo "============================================================"
    echo
    echo "Interface par défaut : ${default_iface}"
    echo "Gateway             : ${default_gateway}"
    echo
    echo "Interfaces réseau :"
    cat "$tmp_ips"
    echo
    echo "Accès utiles :"
    echo "  Flask System : http://${main_ip}:5000"
    echo "  SSH          : ssh root@${main_ip}"
    echo
    echo "Login sur \\l :"
    echo
} > "$ISSUE_FILE"
EOF

    chmod +x "$GENERATOR_FILE"
    echo "Générateur installé : $GENERATOR_FILE"
}

write_service() {
    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Mettre a jour la banniere console /etc/issue avec les IP
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=$GENERATOR_FILE
ExecStartPost=/bin/systemctl try-restart getty@tty1.service

[Install]
WantedBy=multi-user.target
EOF

    echo "Service installé : $SERVICE_FILE"
}

install_all() {
    need_root
    backup_issue
    write_generator
    write_service

    systemctl daemon-reload
    systemctl enable update-issue.service >/dev/null

    "$GENERATOR_FILE"

    echo
    echo "OK : bannière console installée."
    echo "Service activé : update-issue.service"
    echo
    echo "Pour tester maintenant :"
    echo "  cat /etc/issue"
    echo
    echo "Après reboot, l'IP sera affichée avant le login console."
    echo
}

update_now() {
    need_root
    if [ ! -x "$GENERATOR_FILE" ]; then
        echo "ERREUR : générateur absent. Lance d'abord : sudo bash IP.sh -install"
        exit 1
    fi
    "$GENERATOR_FILE"
    systemctl try-restart getty@tty1.service 2>/dev/null || true
    echo "OK : /etc/issue mis à jour."
}

show_issue() {
    if [ -f "$ISSUE_FILE" ]; then
        cat "$ISSUE_FILE"
    else
        echo "/etc/issue introuvable."
    fi
}

remove_all() {
    need_root
    systemctl disable update-issue.service >/dev/null 2>&1 || true
    systemctl stop update-issue.service >/dev/null 2>&1 || true
    rm -f "$SERVICE_FILE"
    rm -f "$GENERATOR_FILE"
    systemctl daemon-reload

    echo "OK : service et générateur supprimés."
    echo "Note : /etc/issue actuel n'a pas été effacé."
    echo "Tu peux restaurer une sauvegarde /etc/issue.backup_IP_* si besoin."
}

usage() {
    echo
    echo "Usage :"
    echo "  sudo bash IP.sh"
    echo "  sudo bash IP.sh -install"
    echo "  sudo bash IP.sh -update"
    echo "  sudo bash IP.sh -show"
    echo "  sudo bash IP.sh -remove"
    echo
}

action="${1:--install}"

case "$action" in
    -install|install)
        install_all
        ;;
    -update|update)
        update_now
        ;;
    -show|show|-status|status)
        show_issue
        ;;
    -remove|remove|-rm)
        remove_all
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        echo "ERREUR : option inconnue : $action"
        usage
        exit 1
        ;;
esac
