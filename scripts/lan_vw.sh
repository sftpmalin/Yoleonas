#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# lan_vw.sh - Réseau LAN libvirt pour VM via vrai bridge Linux
# ============================================================
#
# BUT
#   Créer dans libvirt/virt-manager un réseau général nommé BR0,
#   visible à côté de "default", branché sur le vrai pont Linux br0.
#
# IMPORTANT
#   Ce script NE crée PAS le pont Linux côté OMV.
#   Le pont Linux doit déjà exister côté hôte :
#
#     enp7s0 = port physique, sans IP, master br0
#     br0    = pont Linux, avec l'IP du serveur
#
# CE QUE LE SCRIPT CRÉE
#   Un réseau libvirt :
#
#     <network>
#       <name>BR0</name>
#       <forward mode='bridge'/>
#       <bridge name='br0'/>
#     </network>
#
#   Dans virt-manager :
#     NIC -> Network source -> Virtual network 'BR0'
#
# EXEMPLES
#   sudo bash lan_vw.sh install
#   sudo bash lan_vw.sh status
#   sudo bash lan_vw.sh remove
#   sudo bash lan_vw.sh recreate
#
# Avec nom personnalisé :
#   sudo bash lan_vw.sh install LAN
#
# Avec bridge forcé :
#   sudo bash lan_vw.sh install BR0 br0
#
# ============================================================

ACTION="${1:-install}"
NET_NAME="${2:-BR0}"
BRIDGE_ARG="${3:-auto}"

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "ERREUR : lance ce script en root."
        echo "Exemple : sudo bash lan_vw.sh install"
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
    echo "Configuration bridge/netfilter pour VM bridgées..."
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

is_bridge() {
    local iface="$1"
    [ -d "/sys/class/net/${iface}/bridge" ]
}

iface_ipv4() {
    ip -4 -o addr show dev "$1" 2>/dev/null | awk '{print $4}' | head -n1
}

bridge_ports() {
    local br="$1"
    bridge link 2>/dev/null | awk -v br="$br" '
        $0 ~ "master " br {
            print $2
        }
    ' | sed 's/@.*//' | sed 's/://'
}

detect_default_route_iface() {
    ip -4 route get 1.1.1.1 2>/dev/null | awk '
        {
            for (i=1; i<=NF; i++) {
                if ($i == "dev") {
                    print $(i+1)
                    exit
                }
            }
        }
    '
}

detect_bridge() {
    local iface=""
    local ipaddr=""

    # 1) Priorité au nom classique br0.
    if ip link show br0 >/dev/null 2>&1 && is_bridge br0; then
        ipaddr="$(iface_ipv4 br0 || true)"
        if [ -n "$ipaddr" ]; then
            echo "br0"
            return 0
        fi
    fi

    # 2) Si la route par défaut passe par un bridge, on le prend.
    iface="$(detect_default_route_iface || true)"
    if [ -n "$iface" ] && is_bridge "$iface"; then
        echo "$iface"
        return 0
    fi

    # 3) Sinon, on cherche un bridge UP avec une IPv4, en évitant les bridges internes.
    while read -r name state addr; do
        name="${name%%@*}"

        case "$name" in
            docker0|virbr0|lxcbr0)
                continue
                ;;
        esac

        if is_bridge "$name" && [ "$state" = "UP" ] && [ -n "${addr:-}" ]; then
            echo "$name"
            return 0
        fi
    done < <(ip -br -4 addr | awk '{print $1, $2, $3}')

    echo "ERREUR : aucun vrai bridge LAN détecté." >&2
    echo >&2
    echo "Il faut d'abord créer le pont côté OMV/Linux." >&2
    echo "État attendu :" >&2
    echo "  br0     UP  192.168.1.xxx/24" >&2
    echo "  enp7s0  UP  sans IP, master br0" >&2
    echo >&2
    echo "Commandes de diagnostic :" >&2
    echo "  ip -br addr" >&2
    echo "  bridge link" >&2
    exit 1
}

net_exists() {
    virsh net-info "$NET_NAME" >/dev/null 2>&1
}

net_active() {
    virsh net-info "$NET_NAME" 2>/dev/null | awk -F: '/Active/ {gsub(/^[ \t]+/, "", $2); print $2}' | grep -qi yes
}

net_uses_bridge() {
    local br="$1"
    virsh net-dumpxml "$NET_NAME" 2>/dev/null | grep -q "<bridge name=['\"]${br}['\"]"
}

write_xml() {
    local br="$1"
    local xml="/tmp/${NET_NAME}.xml"

    cat > "$xml" <<XML
<network>
  <name>${NET_NAME}</name>
  <forward mode='bridge'/>
  <bridge name='${br}'/>
</network>
XML

    echo "$xml"
}

show_host_state() {
    echo "============================================================"
    echo "État réseau hôte"
    echo "============================================================"
    ip -br addr
    echo
    echo "============================================================"
    echo "Ports de bridge"
    echo "============================================================"
    bridge link || true
    echo
}

install_net() {
    need_root
    need_cmd ip iproute2
    need_cmd bridge iproute2
    need_cmd virsh libvirt-clients
    fix_bridge_netfilter

    local br="$BRIDGE_ARG"
    if [ "$br" = "auto" ]; then
        br="$(detect_bridge)"
    fi

    if ! ip link show "$br" >/dev/null 2>&1; then
        echo "ERREUR : bridge introuvable : $br"
        echo "Interfaces disponibles :"
        ip -br link
        exit 1
    fi

    if ! is_bridge "$br"; then
        echo "ERREUR : l'interface détectée n'est pas un bridge Linux : $br"
        echo
        echo "Ce script refuse de créer un réseau direct/macvtap sur une carte physique."
        echo "Il faut un vrai pont Linux, exemple : br0."
        echo
        echo "Diagnostic :"
        ip -br addr
        echo
        bridge link || true
        exit 1
    fi

    local ipaddr
    ipaddr="$(iface_ipv4 "$br" || true)"

    if [ -z "$ipaddr" ]; then
        echo "ATTENTION : le bridge $br n'a pas d'IPv4."
        echo "Le réseau libvirt peut être créé, mais vérifie que OMV a bien mis l'IP sur $br."
        echo
    fi

    echo "============================================================"
    echo "Création réseau libvirt VM LAN via vrai bridge"
    echo "============================================================"
    echo "Nom réseau libvirt : $NET_NAME"
    echo "Bridge Linux       : $br"
    echo "IPv4 bridge        : ${ipaddr:-aucune}"
    echo "Ports du bridge    :"
    bridge_ports "$br" | sed 's/^/  - /' || true
    echo

    if net_exists; then
        if net_uses_bridge "$br"; then
            echo "INFO : le réseau $NET_NAME existe déjà et utilise déjà le bridge $br."
        else
            echo "ATTENTION : le réseau $NET_NAME existe mais n'utilise pas le bridge $br."
            echo "Ancien XML :"
            virsh net-dumpxml "$NET_NAME" || true
            echo
            echo "Remplacement automatique par un vrai réseau bridge $br..."
            virsh net-destroy "$NET_NAME" 2>/dev/null || true
            virsh net-undefine "$NET_NAME" 2>/dev/null || true

            local xml
            xml="$(write_xml "$br")"
            virsh net-define "$xml"
        fi
    else
        local xml
        xml="$(write_xml "$br")"
        echo "Définition libvirt : $xml"
        virsh net-define "$xml"
    fi

    if net_active; then
        echo "INFO : réseau déjà actif : $NET_NAME"
    else
        echo "Démarrage réseau : $NET_NAME"
        virsh net-start "$NET_NAME"
    fi

    echo "Activation autostart : $NET_NAME"
    virsh net-autostart "$NET_NAME"

    echo
    echo "État final libvirt :"
    virsh net-list --all
    echo
    echo "XML final :"
    virsh net-dumpxml "$NET_NAME"

    echo
    echo "OK : réseau VM LAN prêt."
    echo
    echo "Dans virt-manager :"
    echo "  NIC / Carte réseau"
    echo "  Source réseau : Réseau virtuel '${NET_NAME}'"
    echo "  Modèle        : virtio"
    echo
    echo "La VM doit recevoir une IP LAN de ta box."
}

remove_net() {
    need_root
    need_cmd virsh libvirt-clients

    echo "Suppression réseau libvirt : $NET_NAME"

    if virsh net-info "$NET_NAME" >/dev/null 2>&1; then
        virsh net-destroy "$NET_NAME" 2>/dev/null || true
        virsh net-undefine "$NET_NAME" 2>/dev/null || true
        echo "OK : réseau supprimé : $NET_NAME"
    else
        echo "INFO : réseau introuvable : $NET_NAME"
    fi

    echo
    virsh net-list --all
}

status_net() {
    need_cmd ip iproute2
    need_cmd bridge iproute2
    need_cmd virsh libvirt-clients

    show_host_state

    echo "============================================================"
    echo "Réseaux libvirt"
    echo "============================================================"
    virsh net-list --all
    echo

    echo "============================================================"
    echo "XML réseau : $NET_NAME"
    echo "============================================================"
    virsh net-dumpxml "$NET_NAME" 2>/dev/null || echo "Réseau introuvable : $NET_NAME"
}

case "$ACTION" in
    install|-install|start|-start|create|-create)
        install_net
        ;;
    recreate|-recreate|force|-force)
        remove_net
        install_net
        ;;
    remove|-remove|delete|-delete|rm|-rm)
        remove_net
        ;;
    status|-status|list|-list)
        status_net
        ;;
    *)
        echo "Usage :"
        echo "  sudo bash lan_vw.sh install"
        echo "  sudo bash lan_vw.sh install BR0"
        echo "  sudo bash lan_vw.sh install BR0 br0"
        echo "  sudo bash lan_vw.sh status"
        echo "  sudo bash lan_vw.sh remove"
        echo
        echo "Arguments :"
        echo "  action      : install | recreate | remove | status"
        echo "  nom réseau  : défaut = BR0"
        echo "  bridge      : défaut = auto"
        exit 1
        ;;
esac
