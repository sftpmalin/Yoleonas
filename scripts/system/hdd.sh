#!/usr/bin/env bash
set -Eeuo pipefail

# =============================================================================
# hdd.sh - veille + enquête HDD pour Debian NAS
# =============================================================================
#
# Principe :
#   - un seul script : hdd.sh
#   - lancé sans paramètre : affiche l'aide, ne fait rien
#   - pas de mode -install
#   - pas de majuscule
#   - -10 / -20 / -30 / -60 configurent directement la veille au boot
#   - -stop met les disques en veille tout de suite
#   - -services / -top / -fatrace servent à enquêter sur les réveils
#
# Notes :
#   - seuls les disques rotatifs ROTA=1 sont ciblés
#   - nvme/ssd/loop sont ignorés
#   - -status utilise seulement hdparm -C, pas hdparm -I
# =============================================================================

action_raw="${1:-}"
target="${2:-}"

# Nettoyage des paramètres copiés/collés.
action="$(printf '%s' "$action_raw" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
action="${action//–/-}"
action="${action//—/-}"

service_name="hdd-veille.service"
service_file="/etc/systemd/system/${service_name}"
defaults_file="/etc/default/hdd-veille"
hd_idle_defaults="/etc/default/hd-idle"
hd_idle_bin="/usr/sbin/hd-idle"

self_path="$(readlink -f "$0" 2>/dev/null || echo "$0")"

say() {
  echo "[$(date '+%F %T')] $*"
}

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "erreur: lance en root."
    echo "exemple: bash hdd.sh -status"
    exit 1
  fi
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

safe_systemctl() {
  if have_cmd systemctl; then
    systemctl "$@" 2>/dev/null || true
  fi
}

show_help() {
  cat <<'EOF'
hdd.sh - veille + enquête HDD

usage :
  bash hdd.sh -list
      liste les disques et les HDD ciblés

  bash hdd.sh -status
      état léger avec hdparm -C seulement

  bash hdd.sh -stop
      met les HDD en veille immédiatement

  bash hdd.sh -10
  bash hdd.sh -20
  bash hdd.sh -30
  bash hdd.sh -60
      configure la veille automatique avec hd-idle pour ce nombre de minutes
      hd-idle surveille l'inactivité et force la veille des HDD

  bash hdd.sh -off
      désactive hd-idle et désactive aussi le timer hdparm -S

  bash hdd.sh -remove
      supprime le service hdd-veille et désactive hd-idle

  bash hdd.sh -install-hd-idle
      vérifie/installe le paquet hd-idle

  bash hdd.sh -disable-hd-idle
      désactive hd-idle

enquête :
  bash hdd.sh -services
      affiche les services suspects

  bash hdd.sh -tools
      vérifie les outils installés

  bash hdd.sh -top
      lance iotop

  bash hdd.sh -fatrace
      affiche les accès fichiers en direct

  bash hdd.sh -atop
      lance atop

  bash hdd.sh -lsof /mnt/disque1
      fichiers ouverts dans un dossier

  bash hdd.sh -watch
      surveille l'état des HDD toutes les 10 secondes

exemples :
  bash hdd.sh -10
  bash hdd.sh -stop
  bash hdd.sh -status
  bash hdd.sh -services

lancé sans paramètre, ce script affiche seulement cette aide.
EOF
}

ensure_hd_idle_installed() {
  if have_cmd hd-idle || [ -x "$hd_idle_bin" ]; then
    return 0
  fi

  say "hd-idle introuvable, installation du paquet hd-idle..."
  if ! have_cmd apt-get; then
    echo "erreur: apt-get introuvable. installe hd-idle manuellement."
    return 1
  fi

  export DEBIAN_FRONTEND=noninteractive
  apt-get update || true
  apt-get install -y hd-idle || {
    echo "erreur: installation hd-idle impossible."
    return 1
  }
}

hd_idle_cmd() {
  if have_cmd hd-idle; then
    command -v hd-idle
  elif [ -x "$hd_idle_bin" ]; then
    echo "$hd_idle_bin"
  else
    return 1
  fi
}

build_hd_idle_opts() {
  local seconds="$1"
  local opts="-i 0"
  local dev

  while read -r dev; do
    [ -z "$dev" ] && continue
    opts="$opts -a $dev -i $seconds"
  done < <(list_hdd_devs)

  echo "$opts"
}

write_hd_idle_defaults() {
  local minutes="$1"
  local seconds="$2"
  local opts
  opts="$(build_hd_idle_opts "$seconds")"

  cat > "$hd_idle_defaults" <<EOF
# généré par hdd.sh
# hd-idle surveille l'inactivité réelle et force la veille des HDD.
START_HD_IDLE=true
HD_IDLE_OPTS="$opts"
EOF
  chmod 0644 "$hd_idle_defaults"

  cat > "$defaults_file" <<EOF
# généré par hdd.sh
HDD_STANDBY_MODE=hd-idle
HDD_STANDBY_MINUTES=$minutes
HDD_STANDBY_SECONDS=$seconds
HD_IDLE_OPTS="$opts"
SCRIPT_PATH=$self_path
EOF
  chmod 0644 "$defaults_file"
}

enable_hd_idle() {
  safe_systemctl daemon-reload
  safe_systemctl reset-failed hd-idle.service
  safe_systemctl enable hd-idle.service
  safe_systemctl restart hd-idle.service
  safe_systemctl reset-failed hd-idle.service
}

disable_hd_idle() {
  if have_cmd systemctl; then
    if systemctl list-unit-files hd-idle.service >/dev/null 2>&1 || systemctl status hd-idle.service >/dev/null 2>&1; then
      say "désactivation de hd-idle.service..."
      systemctl disable --now hd-idle.service >/dev/null 2>&1 || true
      systemctl reset-failed hd-idle.service >/dev/null 2>&1 || true
    else
      say "hd-idle.service absent ou non déclaré."
    fi
  fi
}

list_hdd_names() {
  lsblk -dn -o NAME,TYPE,ROTA 2>/dev/null | awk '$2=="disk" && $3=="1" {print $1}'
}

list_hdd_devs() {
  local name
  while read -r name; do
    [ -n "$name" ] && [ -b "/dev/$name" ] && echo "/dev/$name"
  done < <(list_hdd_names)
}

count_hdds() {
  list_hdd_devs | wc -l | awk '{print $1}'
}

hdparm_value_for_minutes() {
  local minutes="$1"

  if ! [[ "$minutes" =~ ^[0-9]+$ ]]; then
    echo "erreur: minutes invalide: $minutes" >&2
    exit 1
  fi

  if [ "$minutes" -le 0 ]; then
    echo 0
    return
  fi

  # hdparm -S :
  # 1..240 = multiples de 5 secondes
  # 241..251 = 30 min, 60 min, 90 min, ..., 330 min
  if [ "$minutes" -le 20 ]; then
    echo $((minutes * 12))
    return
  fi

  if [ "$minutes" -le 330 ]; then
    local units=$(( (minutes + 29) / 30 ))
    echo $((240 + units))
    return
  fi

  echo 251
}

show_list() {
  echo "disques détectés :"
  lsblk -o NAME,TYPE,ROTA,SIZE,MODEL,SERIAL,MOUNTPOINTS
  echo
  echo "HDD rotatifs ciblés :"

  local found=0
  local dev
  while read -r dev; do
    [ -z "$dev" ] && continue
    found=1
    echo "  $dev"
  done < <(list_hdd_devs)

  [ "$found" -eq 0 ] && echo "  aucun HDD rotatif détecté"
}

show_hdd_power_states() {
  if ! have_cmd hdparm; then
    echo "hdparm introuvable. paquet: apt install -y hdparm"
    return 0
  fi

  local found=0
  local dev
  while read -r dev; do
    [ -z "$dev" ] && continue
    found=1
    echo "------------------------------------------------------------"
    echo "$dev"
    hdparm -C "$dev" 2>/dev/null || true
  done < <(list_hdd_devs)

  [ "$found" -eq 0 ] && echo "aucun HDD rotatif à interroger."
}

show_status() {
  show_list
  echo
  echo "état hdparm léger :"
  echo "note : seulement hdparm -C, pas hdparm -I."
  show_hdd_power_states

  echo
  echo "service ${service_name} :"
  if have_cmd systemctl; then
    systemctl status "$service_name" --no-pager -l 2>/dev/null | sed -n '1,14p' || echo "service non installé."
  else
    echo "systemctl introuvable."
  fi

  echo
  echo "service hd-idle :"
  if have_cmd systemctl; then
    systemctl status hd-idle.service --no-pager -l 2>/dev/null | sed -n '1,10p' || echo "hd-idle absent ou inactif."
  else
    echo "systemctl introuvable."
  fi

  echo
  echo "configuration :"
  if [ -f "$defaults_file" ]; then
    cat "$defaults_file"
  else
    echo "aucun fichier $defaults_file"
  fi

  echo
  echo "script utilisé par le service au prochain réglage :"
  echo "  $self_path"
}

apply_timer_value() {
  local value="$1"

  if ! have_cmd hdparm; then
    echo "erreur: hdparm introuvable. paquet: apt install -y hdparm"
    return 0
  fi

  local hdd_count
  hdd_count="$(count_hdds)"
  if [ "$hdd_count" -eq 0 ]; then
    say "aucun HDD rotatif détecté. rien à appliquer."
    return 0
  fi

  say "application hdparm -S $value sur les HDD..."
  local dev
  while read -r dev; do
    [ -z "$dev" ] && continue
    echo "  $dev"
    hdparm -S "$value" "$dev" || true
  done < <(list_hdd_devs)

  return 0
}

stop_now() {
  if ! have_cmd hdparm; then
    echo "erreur: hdparm introuvable. paquet: apt install -y hdparm"
    return 0
  fi

  local hdd_count
  hdd_count="$(count_hdds)"
  if [ "$hdd_count" -eq 0 ]; then
    say "aucun HDD rotatif détecté. rien à stopper."
    return 0
  fi

  sync || true
  say "mise en veille immédiate des HDD : hdparm -y"

  local dev
  while read -r dev; do
    [ -z "$dev" ] && continue
    echo "  $dev"
    hdparm -y "$dev" || true
  done < <(list_hdd_devs)

  return 0
}

write_service() {
  cat > "$service_file" <<EOF
[Unit]
Description=Configure HDD standby timers with hdparm
Documentation=man:hdparm(8)
After=multi-user.target
ConditionPathExists=${self_path}

[Service]
Type=oneshot
EnvironmentFile=${defaults_file}
ExecStart=${self_path} -apply
RemainAfterExit=no
TimeoutStartSec=60

[Install]
WantedBy=multi-user.target
EOF

  chmod 0644 "$service_file"
}

configure_sleep_minutes() {
  local minutes="$1"

  if ! [[ "$minutes" =~ ^[0-9]+$ ]]; then
    echo "erreur: minutes invalide: $minutes" >&2
    exit 1
  fi

  if [ "$minutes" -le 0 ]; then
    disable_hd_idle
    cat > "$defaults_file" <<EOF
# généré par hdd.sh
HDD_STANDBY_MODE=off
HDD_STANDBY_MINUTES=0
SCRIPT_PATH=$self_path
EOF
    chmod 0644 "$defaults_file"
    apply_timer_value 0
    say "ok: veille automatique désactivée."
    return 0
  fi

  ensure_hd_idle_installed || exit 1

  local hdd_count
  hdd_count="$(count_hdds)"
  if [ "$hdd_count" -eq 0 ]; then
    say "aucun HDD rotatif détecté. hd-idle non configuré."
    return 0
  fi

  # On coupe le vieux service hdparm pour éviter deux logiques de veille concurrentes.
  safe_systemctl disable --now "$service_name"
  rm -f "$service_file"

  local seconds=$((minutes * 60))
  write_hd_idle_defaults "$minutes" "$seconds"
  enable_hd_idle

  say "ok: veille programmée avec hd-idle : ${minutes} minute(s) / ${seconds} seconde(s)"
  say "service : hd-idle.service"
  say "configuration : $hd_idle_defaults"
  say "options : $(build_hd_idle_opts "$seconds")"
}

apply_from_defaults() {
  local value=""

  disable_hd_idle

  if [ -f "$defaults_file" ]; then
    # shellcheck disable=SC1090
    . "$defaults_file"
    value="${HDD_HDPARM_S:-}"
  fi

  if [ -z "$value" ]; then
    echo "erreur: $defaults_file absent ou HDD_HDPARM_S vide."
    return 0
  fi

  apply_timer_value "$value"
}

remove_service() {
  safe_systemctl disable --now "$service_name"
  rm -f "$service_file" "$defaults_file"
  safe_systemctl daemon-reload
  safe_systemctl reset-failed "$service_name"
  say "ok: service ${service_name} supprimé."
  disable_hd_idle
}

show_tools() {
  echo "outils diagnostic disponibles :"
  for cmd in hdparm hd-idle iotop iotop-c fatrace atop lsof systemctl lsblk fuser watch; do
    if have_cmd "$cmd"; then
      echo "  ok  : $cmd -> $(command -v "$cmd")"
    else
      echo "  abs : $cmd"
    fi
  done

  echo
  echo "paquets utiles si besoin :"
  echo "  apt install -y hdparm hd-idle iotop fatrace atop lsof psmisc procps"
}

show_services() {
  echo "services suspects / utiles pour enquête réveil HDD :"
  echo

  if ! have_cmd systemctl; then
    echo "systemctl introuvable."
    return 0
  fi

  local units=(
    "webmin.service"
    "cockpit.socket"
    "cockpit.service"
    "udisks2.service"
    "smartmontools.service"
    "smartd.service"
    "hd-idle.service"
    "hdd-veille.service"
    "atop.service"
    "sysstat.service"
    "docker.service"
  )

  local unit state enabled
  for unit in "${units[@]}"; do
    if systemctl list-unit-files "$unit" >/dev/null 2>&1 || systemctl status "$unit" >/dev/null 2>&1; then
      state="$(systemctl is-active "$unit" 2>/dev/null || true)"
      enabled="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
      printf "  %-24s active=%-10s enabled=%s\n" "$unit" "${state:-unknown}" "${enabled:-unknown}"
    else
      printf "  %-24s absent\n" "$unit"
    fi
  done

  echo
  echo "tests ciblés, un par un :"
  echo "  systemctl stop webmin"
  echo "  systemctl stop cockpit.socket"
  echo "  systemctl stop udisks2"
  echo "  systemctl stop smartmontools 2>/dev/null || systemctl stop smartd"
}

run_iotop() {
  if have_cmd iotop; then
    exec iotop -oPa
  elif have_cmd iotop-c; then
    exec iotop-c -oPa
  else
    echo "iotop/iotop-c introuvable. paquet: apt install -y iotop"
    exit 1
  fi
}

run_fatrace() {
  if have_cmd fatrace; then
    echo "fatrace démarre. quitter avec ctrl+c."
    exec fatrace -t
  else
    echo "fatrace introuvable. paquet: apt install -y fatrace"
    exit 1
  fi
}

run_atop() {
  if have_cmd atop; then
    exec atop
  else
    echo "atop introuvable. paquet: apt install -y atop"
    exit 1
  fi
}

run_lsof() {
  if [ -z "$target" ]; then
    echo "usage: bash hdd.sh -lsof /mnt/disque1"
    exit 1
  fi

  if ! have_cmd lsof; then
    echo "lsof introuvable. paquet: apt install -y lsof"
    exit 1
  fi

  if [ ! -e "$target" ]; then
    echo "erreur: cible inexistante : $target"
    exit 1
  fi

  echo "attention : lsof +D peut parcourir beaucoup de dossiers."
  echo "cible : $target"
  exec lsof +D "$target"
}

watch_power_states() {
  if ! have_cmd watch; then
    echo "watch introuvable. paquet: apt install -y procps"
    exit 1
  fi

  if ! have_cmd hdparm; then
    echo "hdparm introuvable. paquet: apt install -y hdparm"
    exit 1
  fi

  echo "surveillance des états HDD toutes les 10 secondes. quitter avec ctrl+c."
  watch -n 10 'for d in /dev/sd?; do [ -b "$d" ] || continue; echo -n "$d "; hdparm -C "$d" 2>/dev/null | grep state || true; done'
}

if [ -z "$action" ] || [ "$action" = "-h" ] || [ "$action" = "--help" ] || [ "$action" = "help" ] || [ "$action" = "?" ]; then
  show_help
  exit 0
fi

need_root

case "$action" in
  -list|list|--list)
    show_list
    ;;
  -status|status|--status|-stat|stat|--stat|-statut|statut|--statut)
    show_status
    ;;
  -stop|stop|--stop)
    stop_now
    ;;
  -apply|apply|--apply)
    apply_from_defaults
    ;;
  -off|off|--off)
    configure_sleep_minutes 0
    ;;
  -remove|remove|--remove|-rm|rm|--rm|-delete|delete|--delete)
    remove_service
    ;;
  -install-hd-idle|install-hd-idle|--install-hd-idle|-hd-idle|hd-idle|--hd-idle)
    ensure_hd_idle_installed
    ;;
  -disable-hd-idle|disable-hd-idle|--disable-hd-idle|-no-hd-idle|no-hd-idle|--no-hd-idle)
    disable_hd_idle
    ;;
  -services|services|--services|-service|service|-suspects|suspects|--suspects)
    show_services
    ;;
  -tools|tools|--tools|-check|check|--check)
    show_tools
    ;;
  -top|top|--top)
    run_iotop
    ;;
  -fatrace|fatrace|--fatrace)
    run_fatrace
    ;;
  -atop|atop|--atop)
    run_atop
    ;;
  -lsof|lsof|--lsof)
    run_lsof
    ;;
  -watch|watch|--watch)
    watch_power_states
    ;;
  -[0-9]*|--[0-9]*)
    minutes="${action#-}"
    minutes="${minutes#-}"
    configure_sleep_minutes "$minutes"
    ;;
  *)
    echo "paramètre inconnu reçu : [$action]"
    echo
    show_help
    exit 1
    ;;
esac

exit 0
