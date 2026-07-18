#!/usr/bin/env bash
set -euo pipefail

# Donne a l'hote Debian une patte dans le reseau Docker macvlan.
# Cas cible du labo :
#   - parent Linux : br0
#   - reseau Docker : br0
#   - IP hote macvlan : 192.168.1.253/32
#
# Usage :
#   /fileo/yoleo-macvlan-host.sh up
#   /fileo/yoleo-macvlan-host.sh status
#   /fileo/yoleo-macvlan-host.sh down
#   /fileo/yoleo-macvlan-host.sh install-systemd

DOCKER_NETWORK="${DOCKER_NETWORK:-br0}"
PARENT_IF="${PARENT_IF:-br0}"
SHIM_IF="${SHIM_IF:-mv-host}"
SHIM_IP_CIDR="${SHIM_IP_CIDR:-192.168.1.253/32}"
MACVLAN_MODE="${MACVLAN_MODE:-bridge}"
SERVICE_NAME="${SERVICE_NAME:-yoleo-macvlan-host.service}"
SCRIPT_PATH="${SCRIPT_PATH:-/fileo/yoleo-macvlan-host.sh}"

die() {
  echo "ERREUR: $*" >&2
  exit 1
}

need_root() {
  [ "$(id -u)" -eq 0 ] || die "lance ce script en root"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "commande introuvable: $1"
}

shim_ip() {
  printf '%s\n' "${SHIM_IP_CIDR%%/*}"
}

parent_ip() {
  ip -4 addr show "$PARENT_IF" | awk '/inet / { split($2, ip, "/"); print ip[1]; exit }'
}

container_rows() {
  docker network inspect -f '{{range .Containers}}{{println .Name .IPv4Address}}{{end}}' "$DOCKER_NETWORK" \
    | awk 'NF >= 2 { split($2, ip, "/"); if (ip[1] != "") print $1 "\t" ip[1] }' \
    | sort -u
}

container_ips() {
  container_rows | awk '{print $2}' | sort -u
}

container_names() {
  container_rows | awk '{print $1}' | sort -u
}

ensure_base() {
  need_root
  need_cmd ip
  need_cmd docker
  ip link show "$PARENT_IF" >/dev/null 2>&1 || die "interface parente introuvable: $PARENT_IF"
  docker network inspect "$DOCKER_NETWORK" >/dev/null 2>&1 || die "reseau Docker introuvable: $DOCKER_NETWORK"
}

ensure_shim() {
  if ! ip link show "$SHIM_IF" >/dev/null 2>&1; then
    ip link add "$SHIM_IF" link "$PARENT_IF" type macvlan mode "$MACVLAN_MODE"
  fi

  ip -4 addr flush dev "$SHIM_IF" >/dev/null 2>&1 || true
  ip addr add "$SHIM_IP_CIDR" dev "$SHIM_IF"
  ip link set "$SHIM_IF" up
}

clear_owned_routes() {
  ip -4 route show dev "$SHIM_IF" | awk '{print $1}' | while read -r route; do
    [ -n "$route" ] || continue
    [ "$route" = "default" ] && continue
    ip route del "$route" dev "$SHIM_IF" >/dev/null 2>&1 || true
  done
}

cmd_up() {
  ensure_base
  ensure_shim
  clear_owned_routes

  ips="$(container_ips)"
  if [ -z "$ips" ]; then
    echo "OK: $SHIM_IF est pret en $(shim_ip), mais aucun conteneur n'est attache a $DOCKER_NETWORK."
    return 0
  fi

  echo "$ips" | while read -r ip; do
    [ -n "$ip" ] || continue
    ip route replace "$ip/32" dev "$SHIM_IF"
  done

  echo "OK: hote <-> Docker active via $SHIM_IF ($(shim_ip))."
  echo "Routes Docker:"
  ip -4 route show dev "$SHIM_IF"
}

cmd_down() {
  need_root
  need_cmd ip
  if ip link show "$SHIM_IF" >/dev/null 2>&1; then
    ip link del "$SHIM_IF"
    echo "OK: $SHIM_IF supprime."
  else
    echo "OK: $SHIM_IF n'existe pas."
  fi
}

cmd_status() {
  need_cmd ip
  echo "Interface:"
  ip -br addr show "$SHIM_IF" 2>/dev/null || echo "$SHIM_IF absent"
  echo
  echo "Routes via $SHIM_IF:"
  ip -4 route show dev "$SHIM_IF" 2>/dev/null || true
  echo
  if command -v docker >/dev/null 2>&1 && docker network inspect "$DOCKER_NETWORK" >/dev/null 2>&1; then
    echo "Conteneurs sur $DOCKER_NETWORK:"
    container_rows || true
  fi
}

cmd_test() {
  ensure_base
  need_cmd ping

  ips="$(container_ips)"
  [ -n "$ips" ] || die "aucun conteneur a tester sur $DOCKER_NETWORK"

  echo "Test hote -> conteneurs:"
  echo "$ips" | while read -r ip; do
    [ -n "$ip" ] || continue
    ping -c 1 -W 1 "$ip"
  done

  if command -v nsenter >/dev/null 2>&1; then
    echo
    main_ip="$(parent_ip || true)"
    echo "Test conteneurs -> hote ($(shim_ip) et ${main_ip:-IP principale inconnue}):"
    container_names | while read -r name; do
      [ -n "$name" ] || continue
      pid="$(docker inspect -f '{{.State.Pid}}' "$name")"
      echo "== $name =="
      nsenter -t "$pid" -n ping -c 1 -W 1 "$(shim_ip)"
      if [ -n "$main_ip" ] && [ "$main_ip" != "$(shim_ip)" ]; then
        nsenter -t "$pid" -n ping -c 1 -W 1 "$main_ip"
      fi
    done
  fi
}

cmd_install_systemd() {
  need_root
  need_cmd systemctl
  [ -f "$SCRIPT_PATH" ] || die "script introuvable: $SCRIPT_PATH"

  cat >"/etc/systemd/system/$SERVICE_NAME" <<EOF
[Unit]
Description=Yoleo macvlan host access shim
After=network-online.target docker.service
Wants=network-online.target docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=DOCKER_NETWORK=$DOCKER_NETWORK
Environment=PARENT_IF=$PARENT_IF
Environment=SHIM_IF=$SHIM_IF
Environment=SHIM_IP_CIDR=$SHIM_IP_CIDR
Environment=MACVLAN_MODE=$MACVLAN_MODE
ExecStart=$SCRIPT_PATH up
ExecStop=$SCRIPT_PATH down

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"
  echo "OK: service installe et active: $SERVICE_NAME"
  echo "Demarrage manuel: systemctl start $SERVICE_NAME"
}

usage() {
  sed -n '2,18p' "$0"
}

case "${1:-status}" in
  up) cmd_up ;;
  down) cmd_down ;;
  status) cmd_status ;;
  test) cmd_test ;;
  install-systemd) cmd_install_systemd ;;
  *) usage; exit 2 ;;
esac
