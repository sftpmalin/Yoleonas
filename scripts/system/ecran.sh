#!/bin/bash
# ============================================================
# ecran.sh - Mise en veille écran console Linux après 1 minute
# ============================================================
#
# But :
#   Éteindre / blanker l'écran de la console locale après inactivité.
#   Une touche clavier réactive l'affichage.
#
# Usage :
#   sudo bash ecran.sh
#   sudo bash ecran.sh -install
#   sudo bash ecran.sh -apply
#   sudo bash ecran.sh -show
#   sudo bash ecran.sh -remove
#
# Ce que ça fait :
#   - crée /etc/default/console-screen-sleep
#   - crée /usr/local/sbin/console-screen-sleep.sh
#   - crée /etc/systemd/system/console-screen-sleep.service
#   - applique setterm sur /dev/tty1 à /dev/tty6
#
# Notes :
#   - Cible la console physique Linux, pas SSH.
#   - Sur une interface graphique X/Wayland, il faut une logique différente.
#   - Selon la carte/écran, le mode powerdown peut couper le signal vidéo
#     ou seulement afficher un écran noir. Sur console pure, c'est le bon réglage.
# ============================================================

set -euo pipefail

CONF_FILE="/etc/default/console-screen-sleep"
GENERATOR="/usr/local/sbin/console-screen-sleep.sh"
SERVICE="/etc/systemd/system/console-screen-sleep.service"

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "ERREUR : lance ce script en root."
        echo "Exemple : sudo bash ecran.sh -install"
        exit 1
    fi
}

write_conf_if_missing() {
    if [ ! -f "$CONF_FILE" ]; then
        cat > "$CONF_FILE" <<'EOF'
# /etc/default/console-screen-sleep
#
# Valeurs en minutes pour setterm.
# 1 = écran en veille après 1 minute.
BLANK_MINUTES=1
POWERDOWN_MINUTES=1

# Consoles locales à régler.
TTY_LIST="1 2 3 4 5 6"
EOF
        echo "Configuration créée : $CONF_FILE"
    else
        echo "Configuration déjà présente : $CONF_FILE"
    fi
}

write_generator() {
    cat > "$GENERATOR" <<'EOF'
#!/bin/bash
set -euo pipefail

CONF_FILE="/etc/default/console-screen-sleep"

BLANK_MINUTES=1
POWERDOWN_MINUTES=1
TTY_LIST="1 2 3 4 5 6"

if [ -f "$CONF_FILE" ]; then
    # shellcheck disable=SC1090
    . "$CONF_FILE"
fi

apply_one_tty() {
    local n="$1"
    local tty="/dev/tty${n}"

    [ -e "$tty" ] || return 0

    # Réglage principal :
    # --blank N     : blank écran après N minutes
    # --powerdown N : demande extinction/powerdown après N minutes
    TERM=linux setterm --blank "$BLANK_MINUTES" --powerdown "$POWERDOWN_MINUTES" < "$tty" > "$tty" 2>/dev/null || true

    # Certains couples kernel/GPU acceptent aussi ce mode VESA.
    TERM=linux setterm --powersave powerdown < "$tty" > "$tty" 2>/dev/null || true
}

for n in $TTY_LIST; do
    apply_one_tty "$n"
done

# Réglage kernel global en secondes si disponible.
# Ce n'est pas toujours modifiable selon le noyau, donc on n'échoue pas.
if [ -w /sys/module/kernel/parameters/consoleblank ]; then
    echo "$((BLANK_MINUTES * 60))" > /sys/module/kernel/parameters/consoleblank 2>/dev/null || true
fi

exit 0
EOF

    chmod +x "$GENERATOR"
    echo "Script système installé : $GENERATOR"
}

write_service() {
    cat > "$SERVICE" <<EOF
[Unit]
Description=Mettre en veille l'ecran console apres inactivite
After=multi-user.target getty.target
Wants=getty.target

[Service]
Type=oneshot
ExecStart=$GENERATOR
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

    echo "Service installé : $SERVICE"
}

install_all() {
    need_root
    write_conf_if_missing
    write_generator
    write_service

    systemctl daemon-reload
    systemctl enable console-screen-sleep.service >/dev/null
    systemctl start console-screen-sleep.service || true

    echo
    echo "OK : mise en veille écran console installée."
    echo
    echo "Réglage actuel :"
    grep -E '^(BLANK_MINUTES|POWERDOWN_MINUTES|TTY_LIST)=' "$CONF_FILE" || true
    echo
    echo "Après 1 minute sans activité sur la console locale, l'écran doit se mettre en veille."
    echo "Une touche clavier réveille l'affichage."
    echo
}

apply_now() {
    need_root
    if [ ! -x "$GENERATOR" ]; then
        echo "ERREUR : $GENERATOR absent."
        echo "Lance d'abord : sudo bash ecran.sh -install"
        exit 1
    fi
    "$GENERATOR"
    echo "OK : réglage appliqué maintenant."
}

show_status() {
    echo
    echo "CONFIGURATION"
    echo "============="
    if [ -f "$CONF_FILE" ]; then
        cat "$CONF_FILE"
    else
        echo "Aucune configuration : $CONF_FILE absent."
    fi

    echo
    echo "SERVICE"
    echo "======="
    systemctl is-enabled console-screen-sleep.service 2>/dev/null || true
    systemctl is-active console-screen-sleep.service 2>/dev/null || true

    echo
    echo "CONSOLEBLANK KERNEL"
    echo "==================="
    if [ -r /sys/module/kernel/parameters/consoleblank ]; then
        echo -n "consoleblank secondes : "
        cat /sys/module/kernel/parameters/consoleblank
    else
        echo "Paramètre consoleblank non visible."
    fi

    echo
}

remove_all() {
    need_root
    systemctl disable console-screen-sleep.service >/dev/null 2>&1 || true
    systemctl stop console-screen-sleep.service >/dev/null 2>&1 || true
    rm -f "$SERVICE"
    rm -f "$GENERATOR"
    systemctl daemon-reload

    echo "OK : service supprimé."
    echo "Note : le fichier de configuration est conservé : $CONF_FILE"
    echo
    echo "Pour désactiver immédiatement le blanking sur la session courante :"
    echo "  setterm --blank 0 --powerdown 0"
    echo
}

usage() {
    echo
    echo "Usage :"
    echo "  sudo bash ecran.sh"
    echo "  sudo bash ecran.sh -install"
    echo "  sudo bash ecran.sh -apply"
    echo "  sudo bash ecran.sh -show"
    echo "  sudo bash ecran.sh -remove"
    echo
}

action="${1:--install}"

case "$action" in
    -install|install)
        install_all
        ;;
    -apply|apply|-update|update)
        apply_now
        ;;
    -show|show|-status|status)
        show_status
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
