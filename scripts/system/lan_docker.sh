#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# lan-direct-docker.sh - Réseau Docker br0 en ipvlan L2
# ============================================================
#
# BUT
#   Créer le réseau Docker "br0" directement sur la vraie carte LAN,
#   sans passer par l'interface OMV.
#
# PRINCIPE
#   1. Détecte automatiquement la carte réseau de la route par défaut.
#   2. Détecte l'IP/CIDR de cette carte.
#   3. Calcule le vrai subnet LAN, ex: 192.168.1.0/24.
#   4. Détecte la gateway, ex: 192.168.1.254.
#   5. Crée le réseau Docker br0 en ipvlan L2.
#
# EXEMPLES
#   sudo bash lan-direct-docker.sh install
#   sudo bash lan-direct-docker.sh status
#   sudo bash lan-direct-docker.sh remove
#
# Avec interface forcée :
#   sudo bash lan-direct-docker.sh install enp1s0
#
# Vérification :
#   docker network ls
#   docker network inspect br0
#
# Dans les YAML :
#   networks:
#     br0:
#       external: true
#
# ============================================================

ACTION="${1:-install}"
IFACE_ARG="${2:-auto}"
NET_NAME="br0"

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "ERREUR : lance ce script en root."
        echo "Exemple : sudo bash lan-direct-docker.sh install"
        exit 1
    fi
}

need_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "ERREUR : commande introuvable : $1"
        [ -n "${2:-}" ] && echo "Installe si besoin : apt install -y $2"
        exit 1
    fi
}

fix_bridge_netfilter() {
    echo "Configuration bridge/netfilter pour Docker + VM bridgées..."
    mkdir -p /etc/modules-load.d /etc/sysctl.d
    printf '%s\n' br_netfilter > /etc/modules-load.d/yoleo-br-netfilter.conf
    cat > /etc/sysctl.d/99-yoleo-bridge-vm.conf <<'EOF_SYSCTL'
# Docker met souvent FORWARD en DROP. Avec bridge-nf active, le DHCP des VM
# bridgées sur br0 peut être filtré. On garde br0 en pur switch L2.
net.bridge.bridge-nf-call-iptables = 0
net.bridge.bridge-nf-call-ip6tables = 0
EOF_SYSCTL
    modprobe br_netfilter 2>/dev/null || true
    sysctl -w net.bridge.bridge-nf-call-iptables=0 2>/dev/null || true
    sysctl -w net.bridge.bridge-nf-call-ip6tables=0 2>/dev/null || true
}

detect_default_iface() {
    local iface=""

    iface="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '
        {
            for (i=1; i<=NF; i++) {
                if ($i == "dev") {
                    print $(i+1)
                    exit
                }
            }
        }
    ')"

    if [ -z "$iface" ]; then
        iface="$(ip -4 route show default 2>/dev/null | awk '
            {
                for (i=1; i<=NF; i++) {
                    if ($i == "dev") {
                        print $(i+1)
                        exit
                    }
                }
            }
        ')"
    fi

    if [ -z "$iface" ]; then
        echo "ERREUR : impossible de détecter la vraie carte réseau."
        echo "Commandes utiles : ip -br addr ; ip route"
        exit 1
    fi

    echo "$iface"
}

detect_gateway() {
    local iface="$1"
    local gw=""

    gw="$(ip -4 route show default dev "$iface" 2>/dev/null | awk '
        {
            for (i=1; i<=NF; i++) {
                if ($i == "via") {
                    print $(i+1)
                    exit
                }
            }
        }
    ')"

    if [ -z "$gw" ]; then
        gw="$(ip -4 route show default 2>/dev/null | awk '
            {
                for (i=1; i<=NF; i++) {
                    if ($i == "via") {
                        print $(i+1)
                        exit
                    }
                }
            }
        ')"
    fi

    if [ -z "$gw" ]; then
        echo "ERREUR : gateway introuvable."
        echo "Commande utile : ip route"
        exit 1
    fi

    echo "$gw"
}

detect_iface_cidr() {
    local iface="$1"
    local cidr=""

    cidr="$(ip -4 -o addr show dev "$iface" scope global 2>/dev/null | awk '{print $4; exit}')"

    if [ -z "$cidr" ]; then
        echo "ERREUR : aucune IPv4 globale trouvée sur $iface."
        echo "Commande utile : ip -br addr"
        exit 1
    fi

    echo "$cidr"
}

cidr_to_subnet() {
    local cidr="$1"

    python3 - "$cidr" <<'PY'
import ipaddress
import sys

cidr = sys.argv[1]
iface = ipaddress.ip_interface(cidr)
print(str(iface.network))
PY
}

network_exists() {
    docker network inspect "$NET_NAME" >/dev/null 2>&1
}

network_has_containers() {
    local count
    count="$(docker network inspect "$NET_NAME" --format '{{len .Containers}}' 2>/dev/null || echo 0)"
    [ "${count:-0}" != "0" ]
}

show_status() {
    need_cmd docker docker.io

    echo "============================================================"
    echo "Réseaux Docker"
    echo "============================================================"
    docker network ls
    echo

    echo "============================================================"
    echo "Inspection réseau Docker : $NET_NAME"
    echo "============================================================"
    if docker network inspect "$NET_NAME" >/dev/null 2>&1; then
        docker network inspect "$NET_NAME" --format \
'Nom={{.Name}}
Driver={{.Driver}}
Parent={{index .Options "parent"}}
Mode={{index .Options "ipvlan_mode"}}
Subnet={{(index .IPAM.Config 0).Subnet}}
Gateway={{(index .IPAM.Config 0).Gateway}}
Containers={{len .Containers}}'
    else
        echo "Réseau Docker absent : $NET_NAME"
    fi
    echo

    echo "============================================================"
    echo "Réseau hôte"
    echo "============================================================"
    ip -br addr
    echo
    ip route
}

install_network() {
    need_root
    need_cmd ip iproute2
    need_cmd docker docker.io
    need_cmd python3 python3
    fix_bridge_netfilter

    local iface="$IFACE_ARG"
    if [ "$iface" = "auto" ]; then
        iface="$(detect_default_iface)"
    fi

    if ! ip link show "$iface" >/dev/null 2>&1; then
        echo "ERREUR : interface introuvable : $iface"
        echo "Interfaces disponibles :"
        ip -br link
        exit 1
    fi

    local cidr
    local subnet
    local gateway

    cidr="$(detect_iface_cidr "$iface")"
    subnet="$(cidr_to_subnet "$cidr")"
    gateway="$(detect_gateway "$iface")"

    echo "============================================================"
    echo "Création réseau Docker br0 ipvlan"
    echo "============================================================"
    echo "Nom réseau      : $NET_NAME"
    echo "Interface       : $iface"
    echo "IP interface    : $cidr"
    echo "Subnet Docker   : $subnet"
    echo "Gateway Docker  : $gateway"
    echo "Driver          : ipvlan"
    echo "Mode            : l2"
    echo

    if network_exists; then
        echo "INFO : le réseau Docker $NET_NAME existe déjà."
        echo
        docker network inspect "$NET_NAME" --format \
'Driver={{.Driver}}
Parent={{index .Options "parent"}}
Mode={{index .Options "ipvlan_mode"}}
Subnet={{(index .IPAM.Config 0).Subnet}}
Gateway={{(index .IPAM.Config 0).Gateway}}
Containers={{len .Containers}}'
        echo
        echo "Si tu veux le refaire :"
        echo "  sudo bash lan-direct-docker.sh recreate"
        exit 0
    fi

    docker network create \
        -d ipvlan \
        --subnet="$subnet" \
        --gateway="$gateway" \
        -o parent="$iface" \
        -o ipvlan_mode=l2 \
        "$NET_NAME"

    echo
    echo "OK : réseau Docker créé."
    echo
    docker network inspect "$NET_NAME" --format \
'Nom={{.Name}}
Driver={{.Driver}}
Parent={{index .Options "parent"}}
Mode={{index .Options "ipvlan_mode"}}
Subnet={{(index .IPAM.Config 0).Subnet}}
Gateway={{(index .IPAM.Config 0).Gateway}}'

    echo
    echo "Dans tes YAML, garde :"
    echo "networks:"
    echo "  br0:"
    echo "    external: true"
}

remove_network() {
    need_root
    need_cmd docker docker.io

    if ! network_exists; then
        echo "INFO : réseau Docker absent : $NET_NAME"
        docker network ls
        exit 0
    fi

    if network_has_containers; then
        echo "ERREUR : le réseau $NET_NAME est utilisé par des containers."
        echo "Arrête/supprime d'abord les containers branchés dessus."
        echo
        docker network inspect "$NET_NAME" --format '{{json .Containers}}'
        exit 1
    fi

    docker network rm "$NET_NAME"
    echo "OK : réseau Docker supprimé : $NET_NAME"
    docker network ls
}

recreate_network() {
    remove_network || exit 1
    install_network
}

case "$ACTION" in
    install|-install|start|-start|create|-create)
        install_network
        ;;
    recreate|-recreate|force|-force)
        recreate_network
        ;;
    remove|-remove|delete|-delete|rm|-rm)
        remove_network
        ;;
    status|-status|list|-list)
        show_status
        ;;
    *)
        echo "Usage :"
        echo "  sudo bash lan-direct-docker.sh install"
        echo "  sudo bash lan-direct-docker.sh install enp1s0"
        echo "  sudo bash lan-direct-docker.sh status"
        echo "  sudo bash lan-direct-docker.sh remove"
        echo "  sudo bash lan-direct-docker.sh recreate"
        echo
        echo "Note : ce script crée le réseau Docker nommé br0 en ipvlan L2."
        exit 1
        ;;
esac
