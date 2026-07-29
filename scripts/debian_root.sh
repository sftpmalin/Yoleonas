#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# debian_root.sh - "root aimable" pour Debian / OMV / VM labo
# ============================================================
#
# BUT
#   Activer ou désactiver proprement la connexion SSH directe en root.
#
# IMPORTANT
#   Si tu lances le script sans paramètre, il ne fait RIEN.
#   Il affiche seulement l'aide pour éviter une activation root par erreur.
#
# USAGE
#   sudo bash debian_root.sh -install
#   sudo bash debian_root.sh -status
#   sudo bash debian_root.sh -remove
#
# MODES
#   -install : autorise root en SSH avec mot de passe + clé SSH
#   -status  : affiche l'état SSH root actuel
#   -remove  : désactive root SSH direct
#
# NOTE
#   Pour une VM de test, -install est pratique.
#   Pour un vrai serveur stable/exposé, utilise -remove.
#
# ============================================================

ACTION="${1:-}"

CONF_DIR="/etc/ssh/sshd_config.d"
DROPIN="${CONF_DIR}/99-root-aimable.conf"
MAIN_CONF="/etc/ssh/sshd_config"
SSHD_BIN="/usr/sbin/sshd"

usage() {
    cat <<'USAGE'
============================================================
debian_root.sh - gestion SSH root Debian
============================================================

Ce script ne fait rien sans paramètre.
Relance-le avec une option :

  sudo bash debian_root.sh -install   # active root SSH avec mot de passe
  sudo bash debian_root.sh -status    # affiche l'état SSH root
  sudo bash debian_root.sh -remove    # désactive root SSH direct

Alias acceptés :
  install / -install
  status  / -status / statut / -statut
  remove  / -remove

Conseil :
  - Phase installation/labo : -install
  - Serveur stable/exposé   : -remove

USAGE
}

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "ERREUR : lance ce script en root."
        echo "Exemple : sudo bash debian_root.sh -status"
        exit 1
    fi
}

find_sshd() {
    if [ -x "$SSHD_BIN" ]; then
        return 0
    fi

    if command -v sshd >/dev/null 2>&1; then
        SSHD_BIN="$(command -v sshd)"
        return 0
    fi

    return 1
}

install_ssh_server() {
    if ! dpkg -s openssh-server >/dev/null 2>&1; then
        echo "Installation openssh-server..."
        apt-get update
        apt-get install -y openssh-server
    else
        echo "openssh-server déjà installé."
    fi

    if ! find_sshd; then
        echo "ERREUR : sshd introuvable après installation openssh-server."
        exit 1
    fi
}

backup_main_conf() {
    local stamp backup_dir
    stamp="$(date +%Y%m%d-%H%M%S)"
    backup_dir="/root/ssh-root-aimable-backup-${stamp}"
    mkdir -p "$backup_dir"

    if [ -f "$MAIN_CONF" ]; then
        cp -a "$MAIN_CONF" "$backup_dir/sshd_config"
        echo "Backup créé : $backup_dir/sshd_config"
    fi

    if [ -d "$CONF_DIR" ]; then
        cp -a "$CONF_DIR" "$backup_dir/sshd_config.d"
        echo "Backup créé : $backup_dir/sshd_config.d"
    fi
}

ensure_include_dir() {
    mkdir -p "$CONF_DIR"

    if [ ! -f "$MAIN_CONF" ]; then
        echo "ERREUR : $MAIN_CONF introuvable."
        exit 1
    fi

    if ! grep -Eq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' "$MAIN_CONF"; then
        echo "Ajout Include /etc/ssh/sshd_config.d/*.conf dans sshd_config..."
        cp -a "$MAIN_CONF" "${MAIN_CONF}.before-include-root-aimable"
        sed -i '1i Include /etc/ssh/sshd_config.d/*.conf' "$MAIN_CONF"
    fi
}

comment_conflicting_main_options() {
    # OpenSSH garde généralement la première valeur lue.
    # On commente donc les réglages concurrents du fichier principal pour éviter les surprises.
    sed -i \
        -e 's/^[[:space:]]*PermitRootLogin[[:space:]].*/# &  # disabled by root-aimable/' \
        -e 's/^[[:space:]]*PasswordAuthentication[[:space:]].*/# &  # disabled by root-aimable/' \
        -e 's/^[[:space:]]*PubkeyAuthentication[[:space:]].*/# &  # disabled by root-aimable/' \
        "$MAIN_CONF"
}

write_install_mode() {
    cat > "$DROPIN" <<'DROPIN_INSTALL'
# 99-root-aimable.conf
# Géré par debian_root.sh
# Mode installation/labo : root autorisé en SSH avec mot de passe et clé SSH.

PermitRootLogin yes
PasswordAuthentication yes
PubkeyAuthentication yes
DROPIN_INSTALL

    chmod 0644 "$DROPIN"
}

write_remove_mode() {
    cat > "$DROPIN" <<'DROPIN_REMOVE'
# 99-root-aimable.conf
# Géré par debian_root.sh
# Mode serveur stable : root interdit en connexion SSH directe.
# Même une clé SSH root valide ne permet plus d'ouvrir une session root directe.

PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
DROPIN_REMOVE

    chmod 0644 "$DROPIN"
}

test_and_reload_ssh() {
    echo "Test configuration SSH..."
    "$SSHD_BIN" -t -f "$MAIN_CONF"

    echo "Redémarrage/rechargement SSH..."
    systemctl enable --now ssh >/dev/null 2>&1 || true
    systemctl reload ssh 2>/dev/null || \
    systemctl restart ssh 2>/dev/null || \
    systemctl reload sshd 2>/dev/null || \
    systemctl restart sshd

    echo "SSH actif :"
    systemctl is-active ssh 2>/dev/null || systemctl is-active sshd 2>/dev/null || true
}

set_root_password_interactive() {
    echo
    echo "Mot de passe root :"
    echo "Si root n'a pas encore de mot de passe connu, définis-le maintenant."
    echo
    passwd root
}

show_status() {
    echo "============================================================"
    echo "État SSH root"
    echo "============================================================"
    echo

    echo "--- Drop-in ---"
    if [ -f "$DROPIN" ]; then
        cat "$DROPIN"
    else
        echo "Aucun fichier : $DROPIN"
    fi

    echo
    echo "--- Valeurs effectives sshd -T ---"
    if find_sshd && [ -f "$MAIN_CONF" ]; then
        "$SSHD_BIN" -T -f "$MAIN_CONF" 2>/dev/null | grep -E '^(port|permitrootlogin|passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication) ' || true
    else
        echo "sshd ou $MAIN_CONF introuvable. openssh-server n'est peut-être pas installé."
    fi

    echo
    echo "--- Service SSH ---"
    systemctl status ssh --no-pager -l 2>/dev/null | sed -n '1,8p' || \
    systemctl status sshd --no-pager -l 2>/dev/null | sed -n '1,8p' || \
    echo "Service SSH introuvable ou inactif."

    echo
    echo "--- IP machine ---"
    hostname -I || true

    echo
    echo "--- Rappel commandes ---"
    echo "  sudo bash debian_root.sh -install"
    echo "  sudo bash debian_root.sh -status"
    echo "  sudo bash debian_root.sh -remove"
}

install_mode() {
    need_root
    install_ssh_server
    backup_main_conf
    ensure_include_dir
    comment_conflicting_main_options
    write_install_mode
    test_and_reload_ssh
    set_root_password_interactive

    echo
    echo "OK : root SSH avec mot de passe est activé."
    echo "Test depuis une autre machine :"
    echo "  ssh root@IP_DEBIAN"
}

remove_mode() {
    need_root
    install_ssh_server
    backup_main_conf
    ensure_include_dir
    comment_conflicting_main_options
    write_remove_mode
    test_and_reload_ssh

    echo
    echo "OK : root SSH direct est désactivé."
    echo "Test attendu :"
    echo "  ssh root@IP_DEBIAN   => refusé"
    echo "  ssh utilisateur@IP   => OK si clé/accès configuré"
    echo
    echo "Important : garde ta session actuelle ouverte et teste une nouvelle connexion avec ton utilisateur normal."
}

case "$ACTION" in
    "")
        usage
        exit 0
        ;;
    -install|install|enable|-enable|root|aimable)
        install_mode
        ;;
    -remove|remove|disable|-disable|safe|-safe)
        remove_mode
        ;;
    -status|status|-stat|stat|-statut|statut|-etat|etat|-état|état|-list|list)
        show_status
        ;;
    -h|--help|help)
        usage
        exit 0
        ;;
    *)
        echo "ERREUR : option inconnue : $ACTION"
        echo
        usage
        exit 1
        ;;
esac
