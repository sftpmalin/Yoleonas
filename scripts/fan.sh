#!/usr/bin/env bash
#
# yoleo_fans_boot_fix.sh - Yoleo / MSI PRO B760-P DDR4 II
#
# But :
#   Charger réellement le module nct6683 après reboot, avec un service systemd
#   oneshot qui vérifie que les fan*_input apparaissent dans /sys/class/hwmon.
#
# Pourquoi cette version :
#   /etc/modules-load.d peut charger le module trop tôt ou échouer sans rattrapage.
#   Cette version installe un service yoleo-fans.service lancé au boot, après
#   systemd-modules-load et avant/pendant le démarrage normal multi-user.
#
# Utilisation :
#   ./yoleo_fans_boot_fix.sh --install   Installe le service de chargement au boot
#   ./yoleo_fans_boot_fix.sh --remove    Supprime le service
#   ./yoleo_fans_boot_fix.sh --status    Affiche état, logs et ventilateurs détectés
#   ./yoleo_fans_boot_fix.sh --load      Charge/teste maintenant sans installer
#
# Notes :
#   - N'installe aucun paquet APT.
#   - Ne modifie aucun PWM.
#   - Lecture seule : fan*_input et pwm* sont seulement affichés.
#   - Remplace l'ancien fichier /etc/modules-load.d/yoleo-fans.conf par un service
#     plus fiable, pour éviter un simple chargement trop tôt au boot.
#

set -Eeuo pipefail

MODULE="${YOLEO_FAN_MODULE:-nct6683}"
UNIT_NAME="yoleo-fans.service"
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}"
HELPER_PATH="/usr/local/sbin/yoleo-fans-load"
OLD_MODULES_LOAD="/etc/modules-load.d/yoleo-fans.conf"

log() {
  printf '[yoleo-fans] %s\n' "$*"
}

usage() {
  sed -n '2,38p' "$0"
}

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "Erreur : lance ce script en root."
    exit 1
  fi
}

module_present() {
  modinfo "$MODULE" >/dev/null 2>&1
}

module_loaded() {
  lsmod | awk '{print $1}' | grep -qx "$MODULE"
}

show_hwmon() {
  echo "===== HWMON NCT668x ====="
  local found=0

  shopt -s nullglob
  for h in /sys/class/hwmon/hwmon*; do
    [ -e "$h/name" ] || continue
    local name
    name="$(cat "$h/name" 2>/dev/null || true)"

    case "$name" in
      nct6687|nct6683|nct668*)
        found=1
        echo
        echo "$h : $name"

        local any=0
        for f in "$h"/fan*_input "$h"/pwm*; do
          [ -e "$f" ] || continue
          any=1
          local perm val
          perm="$(stat -c '%A %a %U:%G' "$f" 2>/dev/null || true)"
          val="$(cat "$f" 2>/dev/null || true)"
          printf '%-18s %-24s valeur=%s\n' "$(basename "$f")" "$perm" "$val"
        done

        if [ "$any" -eq 0 ]; then
          echo "Info : puce détectée, mais aucun fan*_input/pwm* visible."
        fi
        ;;
    esac
  done
  shopt -u nullglob

  if [ "$found" -eq 0 ]; then
    echo "Aucun hwmon nct668x visible."
    return 1
  fi

  return 0
}

wait_for_hwmon() {
  local seconds="${1:-20}"
  local i

  for ((i=1; i<=seconds; i++)); do
    if show_hwmon >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done

  return 1
}

load_now() {
  need_root

  log "Module demandé : $MODULE"

  if ! module_present; then
    echo "Erreur : module $MODULE introuvable dans ce noyau."
    echo "Le noyau courant ne fournit pas ce module."
    exit 1
  fi

  log "udevadm settle"
  udevadm settle 2>/dev/null || true

  if module_loaded; then
    log "$MODULE déjà chargé, vérification hwmon..."
    if wait_for_hwmon 3; then
      show_hwmon
      exit 0
    fi

    log "$MODULE est chargé mais aucun hwmon nct668x n'est visible."
    log "Tentative de rechargement du module..."
    if modprobe -r "$MODULE" 2>/dev/null; then
      sleep 1
    else
      log "Impossible de décharger $MODULE, on tente quand même un modprobe."
    fi
  fi

  log "Chargement : modprobe $MODULE"
  modprobe "$MODULE"

  log "Attente apparition /sys/class/hwmon nct668x..."
  if wait_for_hwmon 20; then
    show_hwmon
    log "OK : ventilateurs visibles."
    exit 0
  fi

  echo
  echo "ERREUR : $MODULE chargé, mais aucun hwmon nct668x/fan*_input visible après attente."
  echo
  echo "Diagnostic rapide :"
  echo "- lsmod :"
  lsmod | grep -E "^${MODULE}[[:space:]]" || true
  echo
  echo "- dmesg récent nct668/nct :"
  dmesg -T 2>/dev/null | grep -Ei 'nct668|nct|hwmon' | tail -80 || true
  echo
  exit 2
}

install_service() {
  need_root

  echo "===== Installation service ventilateurs Yoleo ====="
  echo "Module : $MODULE"
  echo "Service : $UNIT_PATH"
  echo "Helper : $HELPER_PATH"
  echo

  if ! module_present; then
    echo "Erreur : module $MODULE introuvable dans ce noyau."
    exit 1
  fi

  cat > "$HELPER_PATH" <<'EOF_HELPER'
#!/usr/bin/env bash
set -Eeuo pipefail

MODULE="${YOLEO_FAN_MODULE:-nct6683}"

log() {
  printf '[yoleo-fans] %s\n' "$*"
}

module_loaded() {
  lsmod | awk '{print $1}' | grep -qx "$MODULE"
}

has_hwmon() {
  shopt -s nullglob
  local h name
  for h in /sys/class/hwmon/hwmon*; do
    [ -e "$h/name" ] || continue
    name="$(cat "$h/name" 2>/dev/null || true)"
    case "$name" in
      nct6687|nct6683|nct668*) shopt -u nullglob; return 0 ;;
    esac
  done
  shopt -u nullglob
  return 1
}

show_hwmon() {
  shopt -s nullglob
  local h f name val perm
  for h in /sys/class/hwmon/hwmon*; do
    [ -e "$h/name" ] || continue
    name="$(cat "$h/name" 2>/dev/null || true)"
    case "$name" in
      nct6687|nct6683|nct668*)
        log "$h : $name"
        for f in "$h"/fan*_input "$h"/pwm*; do
          [ -e "$f" ] || continue
          perm="$(stat -c '%A %a %U:%G' "$f" 2>/dev/null || true)"
          val="$(cat "$f" 2>/dev/null || true)"
          log "$(basename "$f") ${perm} valeur=${val}"
        done
        ;;
    esac
  done
  shopt -u nullglob
}

wait_for_hwmon() {
  local seconds="${1:-20}"
  local i
  for ((i=1; i<=seconds; i++)); do
    if has_hwmon; then
      return 0
    fi
    sleep 1
  done
  return 1
}

case "${1:-load}" in
  load)
    log "Démarrage service. Module=${MODULE}"

    if ! modinfo "$MODULE" >/dev/null 2>&1; then
      log "ERREUR : module ${MODULE} introuvable dans ce noyau."
      exit 1
    fi

    udevadm settle 2>/dev/null || true

    if module_loaded && has_hwmon; then
      log "OK : ${MODULE} déjà chargé et hwmon visible."
      show_hwmon
      exit 0
    fi

    if module_loaded && ! has_hwmon; then
      log "${MODULE} chargé sans hwmon visible : tentative reload."
      modprobe -r "$MODULE" 2>/dev/null || true
      sleep 1
    fi

    log "modprobe ${MODULE}"
    modprobe "$MODULE"

    if wait_for_hwmon 20; then
      log "OK : hwmon nct668x visible."
      show_hwmon
      exit 0
    fi

    log "ERREUR : aucun hwmon nct668x visible après chargement."
    dmesg -T 2>/dev/null | grep -Ei 'nct668|nct|hwmon' | tail -80 || true
    exit 2
    ;;
  *)
    echo "Usage: $0 load"
    exit 1
    ;;
esac
EOF_HELPER

  chmod 0755 "$HELPER_PATH"

  cat > "$UNIT_PATH" <<EOF_UNIT
[Unit]
Description=Yoleo - load NCT668x fan sensors
Documentation=man:modprobe(8)
After=systemd-modules-load.service local-fs.target
Wants=systemd-modules-load.service
Before=multi-user.target

[Service]
Type=oneshot
ExecStart=$HELPER_PATH load
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF_UNIT

  chmod 0644 "$UNIT_PATH"

  if [ -f "$OLD_MODULES_LOAD" ]; then
    mv -f "$OLD_MODULES_LOAD" "${OLD_MODULES_LOAD}.disabled-by-yoleo-fans-service"
    echo "Ancien chargement trop simple désactivé : ${OLD_MODULES_LOAD}.disabled-by-yoleo-fans-service"
  fi

  systemctl daemon-reload
  systemctl enable "$UNIT_NAME"
  systemctl restart "$UNIT_NAME"

  echo
  systemctl --no-pager --full status "$UNIT_NAME" || true

  echo
  echo "===== Vérification actuelle ====="
  show_hwmon || {
    echo
    echo "Le service est installé, mais les ventilateurs ne sont pas visibles maintenant."
    echo "Regarde les logs avec : journalctl -u $UNIT_NAME -b --no-pager"
    exit 2
  }

  echo
  echo "OK : service installé. Au prochain reboot, il chargera $MODULE et vérifiera les fan*_input."
}

remove_service() {
  need_root

  echo "===== Suppression service ventilateurs Yoleo ====="
  echo

  systemctl disable --now "$UNIT_NAME" 2>/dev/null || true
  rm -f "$UNIT_PATH" "$HELPER_PATH"
  systemctl daemon-reload
  systemctl reset-failed "$UNIT_NAME" 2>/dev/null || true

  echo "OK : service supprimé."
  echo
  echo "Note : le module $MODULE peut rester chargé jusqu'au prochain reboot."
}

status_service() {
  echo "===== État ventilateurs Yoleo ====="
  echo "Module : $MODULE"
  echo "Service : $UNIT_NAME"
  echo

  echo "----- systemd -----"
  systemctl --no-pager --full status "$UNIT_NAME" 2>/dev/null || true

  echo
  echo "----- module -----"
  if module_loaded; then
    echo "RAM : $MODULE chargé"
  else
    echo "RAM : $MODULE non chargé"
  fi

  echo
  echo "----- ancien modules-load -----"
  if [ -f "$OLD_MODULES_LOAD" ]; then
    echo "Présent : $OLD_MODULES_LOAD"
    cat "$OLD_MODULES_LOAD" 2>/dev/null || true
  elif [ -f "${OLD_MODULES_LOAD}.disabled-by-yoleo-fans-service" ]; then
    echo "Désactivé : ${OLD_MODULES_LOAD}.disabled-by-yoleo-fans-service"
  else
    echo "Absent : $OLD_MODULES_LOAD"
  fi

  echo
  echo "----- hwmon -----"
  show_hwmon || true

  echo
  echo "----- logs boot courant -----"
  journalctl -u "$UNIT_NAME" -b --no-pager -n 120 2>/dev/null || true
}

case "${1:-}" in
  --install)
    install_service
    ;;
  --remove)
    remove_service
    ;;
  --status)
    status_service
    ;;
  --load)
    load_now
    ;;
  "")
    usage
    ;;
  *)
    echo "Option inconnue : $1"
    echo
    usage
    exit 1
    ;;
esac
