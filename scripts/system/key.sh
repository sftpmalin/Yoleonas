#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

ROOT_USER="root"
YOAN_USER="yoan"
ROOT_KEY_SRC="$SCRIPT_DIR/../key_pub/tower.pub"
YOAN_KEY_SRC="$SCRIPT_DIR/../key_pub/yoan.pub"
INSTALLED_KEYS=0

echo "Dossier du script : $SCRIPT_DIR"
echo "Cle publique root : $ROOT_KEY_SRC"
echo "Cle publique yoan : $YOAN_KEY_SRC"
echo

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Erreur : lance le script en root."
    echo "Exemple : sudo bash key.sh"
    exit 1
fi

check_pub_key() {
    local key="$1"

    if [[ ! -f "$key" ]]; then
        echo "Erreur : cle introuvable : $key"
        exit 1
    fi

    if [[ ! -s "$key" ]]; then
        echo "Erreur : cle vide : $key"
        exit 1
    fi

    if ! grep -qE '^(ssh-rsa|ssh-ed25519|ecdsa-sha2-|sk-ssh-|sk-ecdsa-)' "$key"; then
        echo "Erreur : ce fichier ne ressemble pas a une cle publique SSH : $key"
        exit 1
    fi
}

user_home() {
    local user="$1"
    getent passwd "$user" | cut -d: -f6
}

install_key() {
    local src="$1"
    local owner="$2"
    local home_dir="$3"
    local ssh_dir="${home_dir}/.ssh"

    echo "Installation de $src vers $ssh_dir/authorized_keys"

    mkdir -p "$ssh_dir"
    chmod 700 "$ssh_dir"
    chown "$owner:" "$ssh_dir"

    if [[ -f "$ssh_dir/authorized_keys" ]]; then
        cp -a "$ssh_dir/authorized_keys" "$ssh_dir/authorized_keys.bak.$(date +%Y%m%d-%H%M%S)"
        echo "Sauvegarde de l'ancien authorized_keys creee."
    fi

    cp "$src" "$ssh_dir/authorized_keys"
    chmod 600 "$ssh_dir/authorized_keys"
    chown "$owner:" "$ssh_dir/authorized_keys"

    echo "OK : $ssh_dir/authorized_keys"
    ls -la "$ssh_dir"
    echo
}

install_user_key_if_present() {
    local user="$1"
    local key_src="$2"
    local home_dir

    if ! id "$user" >/dev/null 2>&1; then
        echo "Attention : l'utilisateur $user n'existe pas, cle ignoree."
        return 0
    fi

    home_dir="$(user_home "$user")"
    if [[ -z "$home_dir" || "$home_dir" == "/" ]]; then
        echo "Attention : home invalide pour $user, cle ignoree."
        return 0
    fi

    check_pub_key "$key_src"
    install_key "$key_src" "$user" "$home_dir"
    INSTALLED_KEYS=$((INSTALLED_KEYS + 1))
}

install_user_key_if_present "$ROOT_USER" "$ROOT_KEY_SRC"
install_user_key_if_present "$YOAN_USER" "$YOAN_KEY_SRC"

if [[ "$INSTALLED_KEYS" -eq 0 ]]; then
    echo "Attention : aucune cle publique installee, aucun utilisateur cible n'existe."
else
    echo "Termine : $INSTALLED_KEYS cle(s) publique(s) installee(s)."
fi
