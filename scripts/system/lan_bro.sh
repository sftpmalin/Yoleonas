#!/usr/bin/env bash
set -euo pipefail

# BR0 - Gestion simple d'un bridge br0 pour Debian pure
# Auteur: généré pour Yoan
#
# Commandes:
#   BR0 -show
#   BR0 -statut
#   BR0 -install
#   BR0 -remove
#
# Philosophie:
#   - La carte physique devient un simple port du bridge.
#   - br0 devient l'interface réseau principale de Linux.
#   - br0 reprend la MAC de la carte physique pour éviter la loterie DHCP.
#   - -remove restaure la configuration sauvegardée si possible.
#
# Fichiers:
#   /root/.BR0/state.conf
#   /root/.BR0/backups/

APP_DIR="/root/.BR0"
BACKUP_ROOT="$APP_DIR/backups"
STATE_FILE="$APP_DIR/state.conf"
MODE="${1:-}"

say() {
  echo "[BR0] $*"
}

die() {
  echo "[BR0][ERREUR] $*" >&2
  exit 1
}

need_root() {
  [ "$(id -u)" -eq 0 ] || die "Lance ce script en root."
}

usage() {
  cat <<'USAGE'
Usage:
  BR0 -show
  BR0 -statut
  BR0 -install
  BR0 -remove

Commandes:
  -show     Affiche la détection réseau sans modifier.
  -statut   Alias de -show, avec état br0 si présent.
  -install  Installe/configure br0, force la MAC physique sur br0, puis reboot.
  -remove   Supprime br0 et restaure le réseau classique, puis reboot.

Principe:
  Avant:
    carte physique = IP du serveur

  Après -install:
    carte physique = port sans IP
    br0            = IP du serveur + MAC de la carte physique

  Après -remove:
    carte physique = IP du serveur
    br0 supprimé de la configuration persistante
USAGE
}

ensure_dirs() {
  mkdir -p "$APP_DIR" "$BACKUP_ROOT"
}

detect_default_dev() {
  ip -4 route show default 2>/dev/null | awk '
    NR==1 {
      for (i=1; i<=NF; i++) {
        if ($i == "dev") {
          print $(i+1)
          exit
        }
      }
    }'
}

detect_gateway() {
  ip -4 route show default 2>/dev/null | awk '
    NR==1 {
      for (i=1; i<=NF; i++) {
        if ($i == "via") {
          print $(i+1)
          exit
        }
      }
    }'
}

is_bridge() {
  local dev="$1"
  [ -d "/sys/class/net/$dev/brif" ]
}

bridge_first_port() {
  local br="$1"
  find "/sys/class/net/$br/brif" -mindepth 1 -maxdepth 1 2>/dev/null | head -n1 | xargs -r basename
}

detect_physical_from_route() {
  local dev="$1"

  if [ -z "$dev" ]; then
    return 1
  fi

  if is_bridge "$dev"; then
    bridge_first_port "$dev"
  else
    echo "$dev"
  fi
}

detect_dns() {
  local dns
  dns="$(awk '/^nameserver / {print $2}' /etc/resolv.conf 2>/dev/null | xargs || true)"

  if [ -n "$dns" ]; then
    echo "$dns"
    return
  fi

  if [ -n "${GW:-}" ]; then
    echo "$GW 1.1.1.1"
  else
    echo "1.1.1.1 8.8.8.8"
  fi
}

detect_current_ip_cidr() {
  local dev="$1"
  ip -4 addr show dev "$dev" scope global 2>/dev/null | awk '/inet / {print $2; exit}'
}

detect_all() {
  DEV="$(detect_default_dev || true)"
  GW="$(detect_gateway || true)"

  [ -n "$DEV" ] || die "Aucune interface avec route par défaut détectée."

  PHY="$(detect_physical_from_route "$DEV" || true)"
  [ -n "$PHY" ] || die "Impossible de retrouver la carte physique depuis $DEV."
  [ -d "/sys/class/net/$PHY" ] || die "Carte physique introuvable: $PHY"

  MAC="$(cat "/sys/class/net/$PHY/address")"

  IP_CIDR="$(detect_current_ip_cidr "$DEV" || true)"
  if [ -z "${IP_CIDR:-}" ] && [ "$PHY" != "$DEV" ]; then
    IP_CIDR="$(detect_current_ip_cidr "$PHY" || true)"
  fi

  DNS="$(detect_dns)"

  if [ -z "${GW:-}" ]; then
    GW="192.168.1.254"
  fi
}

show_status() {
  detect_all

  echo "=== Détection réseau ==="
  echo "Interface route défaut : $DEV"
  echo "Interface physique     : $PHY"
  echo "MAC physique           : $MAC"
  echo "IP actuelle            : ${IP_CIDR:-non détectée}"
  echo "Gateway                : $GW"
  echo "DNS                    : $DNS"
  echo

  echo "=== Interfaces ==="
  ip -br link show | sed 's/^/  /'
  echo

  echo "=== IP ==="
  ip -br -4 addr show | sed 's/^/  /'
  echo

  echo "=== Routes ==="
  ip -4 route show | sed 's/^/  /'
  echo

  echo "=== Bridge ==="
  if ip link show br0 >/dev/null 2>&1; then
    echo "br0 existe."
    echo "MAC br0: $(cat /sys/class/net/br0/address 2>/dev/null || echo '?')"
    bridge link show 2>/dev/null | sed 's/^/  /' || true
  else
    echo "br0 n'existe pas actuellement."
  fi
  echo

  echo "=== Etat BR0 ==="
  if [ -f "$STATE_FILE" ]; then
    echo "State file: $STATE_FILE"
    sed 's/^/  /' "$STATE_FILE"
  else
    echo "Aucun état BR0 enregistré."
  fi
}

install_packages() {
  say "Installation des paquets nécessaires..."
  apt update
  DEBIAN_FRONTEND=noninteractive apt install -y ifupdown bridge-utils iproute2
}

fix_bridge_netfilter() {
  say "Configuration bridge/netfilter pour laisser passer le DHCP des VM..."
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

backup_network_config() {
  local ts backup_dir
  ts="$(date +%Y%m%d-%H%M%S)"
  backup_dir="$BACKUP_ROOT/$ts"
  mkdir -p "$backup_dir"

  [ -f /etc/network/interfaces ] && cp -a /etc/network/interfaces "$backup_dir/interfaces" || true
  [ -d /etc/network/interfaces.d ] && cp -a /etc/network/interfaces.d "$backup_dir/interfaces.d" || true
  [ -f /etc/resolv.conf ] && cp -a /etc/resolv.conf "$backup_dir/resolv.conf" || true

  echo "$backup_dir"
}

write_state() {
  cat > "$STATE_FILE" <<EOF_STATE
PHY="$PHY"
DEV_BEFORE="$DEV"
MAC="$MAC"
IP_CIDR="$IP_CIDR"
GW="$GW"
DNS="$DNS"
BACKUP_DIR="$BACKUP_DIR"
INSTALLED_AT="$(date -Is)"
EOF_STATE
  chmod 600 "$STATE_FILE"
}

disable_interfaces_d() {
  mkdir -p /etc/network/interfaces.d
  shopt -s nullglob
  for f in /etc/network/interfaces.d/*; do
    [ -f "$f" ] || continue
    case "$f" in
      *.disabled-by-BR0) continue ;;
    esac
    mv "$f" "$f.disabled-by-BR0"
  done
  shopt -u nullglob
}

write_br0_config() {
  [ -n "${IP_CIDR:-}" ] || die "IP actuelle non détectée. Impossible de créer une config statique propre."

  cat > /etc/network/interfaces <<EOF_IFACE
# Généré par /usr/local/bin/BR0
# Sauvegarde précédente: $BACKUP_DIR

auto lo
iface lo inet loopback

# Carte physique: simple port du bridge, sans IP.
auto $PHY
iface $PHY inet manual

# br0: interface principale de Linux.
# La MAC est forcée sur la MAC physique pour éviter un changement d'IP côté box/DHCP.
auto br0
iface br0 inet static
    address $IP_CIDR
    gateway $GW
    dns-nameservers $DNS
    bridge_ports $PHY
    bridge_stp off
    bridge_fd 0
    bridge_maxwait 0
    hwaddress ether $MAC
EOF_IFACE
}

install_br0() {
  need_root
  ensure_dirs
  detect_all

  say "Détection:"
  say "  Interface actuelle : $DEV"
  say "  Carte physique     : $PHY"
  say "  MAC physique       : $MAC"
  say "  IP                 : ${IP_CIDR:-non détectée}"
  say "  Gateway            : $GW"
  say "  DNS                : $DNS"

  if [ "$DEV" = "br0" ]; then
    say "La route par défaut passe déjà par br0. Je réécris une configuration propre."
  fi

  install_packages

  BACKUP_DIR="$(backup_network_config)"
  say "Sauvegarde créée: $BACKUP_DIR"

  disable_interfaces_d
  modprobe bridge || true
  fix_bridge_netfilter

  write_br0_config
  write_state

  say "Configuration br0 écrite dans /etc/network/interfaces"
  say "br0 prendra la MAC: $MAC"
  say "Redémarrage..."
  reboot
}

restore_backup_if_possible() {
  if [ -f "$STATE_FILE" ]; then
    # shellcheck disable=SC1090
    . "$STATE_FILE"
    if [ -n "${BACKUP_DIR:-}" ] && [ -f "$BACKUP_DIR/interfaces" ]; then
      say "Restauration de la sauvegarde: $BACKUP_DIR"
      cp -a "$BACKUP_DIR/interfaces" /etc/network/interfaces

      if [ -d "$BACKUP_DIR/interfaces.d" ]; then
        rm -rf /etc/network/interfaces.d
        cp -a "$BACKUP_DIR/interfaces.d" /etc/network/interfaces.d
      else
        mkdir -p /etc/network/interfaces.d
      fi

      [ -f "$BACKUP_DIR/resolv.conf" ] && cp -a "$BACKUP_DIR/resolv.conf" /etc/resolv.conf 2>/dev/null || true
      return 0
    fi
  fi

  return 1
}

write_classic_config_from_state_or_detect() {
  local phy mac ip_cidr gw dns

  if [ -f "$STATE_FILE" ]; then
    # shellcheck disable=SC1090
    . "$STATE_FILE"
    phy="${PHY:-}"
    mac="${MAC:-}"
    ip_cidr="${IP_CIDR:-}"
    gw="${GW:-}"
    dns="${DNS:-}"
  else
    detect_all
    phy="$PHY"
    mac="$MAC"
    ip_cidr="$IP_CIDR"
    gw="$GW"
    dns="$DNS"
  fi

  [ -n "$phy" ] || die "Impossible de déterminer la carte physique pour restaurer."
  [ -n "$ip_cidr" ] || die "Impossible de déterminer l'IP à restaurer."

  cat > /etc/network/interfaces <<EOF_IFACE
# Généré par /usr/local/bin/BR0 -remove
# Retour réseau classique: IP directement sur la carte physique.

auto lo
iface lo inet loopback

auto $phy
iface $phy inet static
    address $ip_cidr
    gateway $gw
    dns-nameservers $dns
    hwaddress ether $mac
EOF_IFACE

  mkdir -p /etc/network/interfaces.d
}

remove_br0() {
  need_root
  ensure_dirs

  if restore_backup_if_possible; then
    say "Configuration d'origine restaurée."
  else
    say "Aucune sauvegarde exploitable. Génération d'une configuration classique."
    write_classic_config_from_state_or_detect
  fi

  if [ -f "$STATE_FILE" ]; then
    mv "$STATE_FILE" "$STATE_FILE.removed-$(date +%Y%m%d-%H%M%S)"
  fi

  say "Mode réseau classique restauré dans /etc/network/interfaces"
  say "Redémarrage..."
  reboot
}

case "$MODE" in
  -show|-statut)
    need_root
    show_status
    ;;

  -install)
    install_br0
    ;;

  -remove)
    remove_br0
    ;;

  -h|--help|help|"")
    usage
    ;;

  *)
    usage
    die "Commande inconnue: $MODE"
    ;;
esac
