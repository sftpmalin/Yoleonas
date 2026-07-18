#!/usr/bin/env bash
set -Euo pipefail

# =============================================================================
# install.sh - Menu installation Debian PURE NAS maison
# =============================================================================
#
# Objectif :
#   Un seul script avec menu pour installer les blocs indépendamment :
#     1) Base Debian NAS sans dépendance GPU
#     2) WebAdmin / Webmin
#     3) Cockpit port 9090 + KVM/libvirt
#     4) Outils NVIDIA Docker seulement si le driver NVIDIA est déjà fonctionnel
#     5) Statut / diagnostic
#     6) Désinstaller Cockpit
#     7) Désinstaller Webmin
#     8) Désinstaller outils NVIDIA Docker
#
# Principe important :
#   - L'installation de base ne dépend PAS de NVIDIA.
#   - Le script NVIDIA n'installe PAS le driver NVIDIA hôte.
#   - Le script NVIDIA s'arrête proprement si nvidia-smi ne fonctionne pas déjà.
#   - Les erreurs APT sont enregistrées mais ne bloquent pas toute la suite.
#
# Usage interactif :
#   sudo bash install.sh
#
# Usage direct :
#   sudo bash install.sh -base
#   sudo bash install.sh -webadmin
#   sudo bash install.sh -cockpit
#   sudo bash install.sh -nvidia
#   sudo bash install.sh -gpu
#   sudo bash install.sh -nvidia-driver-580
#   sudo bash install.sh -nvidia-driver-595
#   sudo bash install.sh -intel-gpu
#   sudo bash install.sh -status
#   sudo bash install.sh -remove-cockpit
#   sudo bash install.sh -remove-webadmin
#   sudo bash install.sh -remove-nvidia
#
# Usage direct Flask / sans menu interactif :
#   sudo bash install.sh -1       # même action que menu 1
#   sudo bash install.sh -2       # même action que menu 2
#   sudo bash install.sh -3       # même action que menu 3
#   sudo bash install.sh -4       # même action que menu 4
#   sudo bash install.sh -5       # même action que menu 5
#   sudo bash install.sh -6       # même action que menu 6
#   sudo bash install.sh -7       # même action que menu 7
#   sudo bash install.sh -8       # même action que menu 8
#
# Log optionnel pour Flask :
#   sudo bash install.sh -1 --log
#   sudo bash install.sh -1 --log /tmp/install_base.log
#   sudo bash install.sh --log /tmp/install_base.log -1
#
# =============================================================================

export DEBIAN_FRONTEND=noninteractive

STEP=0
SKIPPED_ITEMS=()
FAILED_ITEMS=()
LOG_FILE=""
ACTION_ARGS=()
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
NVIDIA_DIR="${SCRIPT_DIR}/nvidia"
NVIDIA_INSTALLER="${NVIDIA_DIR}/install.sh"
NVIDIA_RUN_580="${NVIDIA_DIR}/NVIDIA-Linux-x86_64-580.95.05.run"
NVIDIA_RUN_595="${NVIDIA_DIR}/NVIDIA-Linux-x86_64-595.71.05-no-compat32.run"

default_log_file() {
  local dir="${INSTALL_LOG_DIR:-/tmp}"

  if ! mkdir -p "$dir" 2>/dev/null; then
    dir="/tmp"
  fi

  echo "${dir}/install_debian_$(date +%Y%m%d_%H%M%S).log"
}

setup_logging() {
  [ -n "${LOG_FILE:-}" ] || return 0

  local log_dir
  log_dir="$(dirname "$LOG_FILE")"

  if ! mkdir -p "$log_dir" 2>/dev/null; then
    echo "ERREUR: impossible de créer le dossier de log: $log_dir" >&2
    exit 1
  fi

  touch "$LOG_FILE" 2>/dev/null || {
    echo "ERREUR: impossible d'écrire le log: $LOG_FILE" >&2
    exit 1
  }

  exec > >(tee -a "$LOG_FILE") 2>&1
  echo "LOG: $LOG_FILE"
}

parse_args() {
  ACTION_ARGS=()

  while [ "$#" -gt 0 ]; do
    case "$1" in
      -log|--log)
        if [ -n "${2:-}" ] && [[ "${2:-}" != -* ]]; then
          LOG_FILE="$2"
          shift 2
        else
          LOG_FILE="$(default_log_file)"
          shift
        fi
        ;;
      -log=*|--log=*)
        LOG_FILE="${1#*=}"
        shift
        ;;
      --)
        shift
        while [ "$#" -gt 0 ]; do
          ACTION_ARGS+=("$1")
          shift
        done
        ;;
      *)
        ACTION_ARGS+=("$1")
        shift
        ;;
    esac
  done
}

STEP=0
SKIPPED_ITEMS=()
FAILED_ITEMS=()

record_skip() {
  SKIPPED_ITEMS+=("$*")
  echo "SKIP: $*" >&2
}

record_fail() {
  FAILED_ITEMS+=("$*")
  echo "FAIL: $*" >&2
}

warn() {
  echo "ATTENTION: $*" >&2
}

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "ERREUR: lance ce script en root."
    echo "Exemple : sudo bash install.sh"
    exit 1
  fi
}

log_step() {
  STEP=$((STEP + 1))
  echo
  echo "============================================================================="
  echo "[$STEP] $*"
  echo "============================================================================="
}

run_cmd() {
  local label="$1"
  shift
  echo "RUN: $label"
  if "$@"; then
    return 0
  fi
  local rc=$?
  record_fail "$label rc=$rc"
  return 0
}

configure_bridge_netfilter_for_vm_bridges() {
  echo "Configuration bridge/netfilter pour VM bridgées..."
  mkdir -p /etc/modules-load.d /etc/sysctl.d
  printf '%s\n' br_netfilter > /etc/modules-load.d/yoleo-br-netfilter.conf
  cat > /etc/sysctl.d/99-yoleo-bridge-vm.conf <<'EOF'
# Laisse les bridges Linux faire leur travail L2 sans passer par les règles
# FORWARD de Docker. Sinon une VM branchée sur br0 peut perdre le DHCP.
net.bridge.bridge-nf-call-iptables = 0
net.bridge.bridge-nf-call-ip6tables = 0
EOF
  modprobe br_netfilter 2>/dev/null || true
  sysctl -w net.bridge.bridge-nf-call-iptables=0 2>/dev/null || true
  sysctl -w net.bridge.bridge-nf-call-ip6tables=0 2>/dev/null || true
}

apt_update_safe() {
  echo "APT update..."
  if ! apt-get update; then
    record_fail "apt-get update"
  fi
}

pkg_installed() {
  dpkg -s "$1" >/dev/null 2>&1
}

pkg_available() {
  # Compatible Debian en français : on force LC_ALL=C pour lire Candidate.
  local pkg="$1"
  local candidate=""

  if pkg_installed "$pkg"; then
    return 0
  fi

  candidate="$(LC_ALL=C apt-cache policy "$pkg" 2>/dev/null | awk '/Candidate:/ {print $2; exit}')"
  [ -n "$candidate" ] && [ "$candidate" != "(none)" ]
}

apt_install_available() {
  local pkg
  for pkg in "$@"; do
    if pkg_installed "$pkg"; then
      echo "APT déjà installé: $pkg"
      continue
    fi

    if ! pkg_available "$pkg"; then
      record_skip "Paquet ignoré car non disponible/installable: $pkg"
      continue
    fi

    echo "APT install: $pkg"
    if ! apt-get install -y "$pkg"; then
      record_fail "apt install $pkg"
    fi
  done
  return 0
}

apt_purge_available() {
  local pkg
  for pkg in "$@"; do
    if ! pkg_installed "$pkg"; then
      echo "APT déjà absent: $pkg"
      continue
    fi

    echo "APT purge: $pkg"
    if ! apt-get purge -y "$pkg"; then
      record_fail "apt purge $pkg"
    fi
  done
  return 0
}

install_first_available() {
  local pkg
  for pkg in "$@"; do
    if pkg_installed "$pkg"; then
      echo "APT déjà installé: $pkg"
      return 0
    fi
    if pkg_available "$pkg"; then
      echo "APT install premier disponible: $pkg"
      if ! apt-get install -y "$pkg"; then
        record_fail "apt install $pkg"
      fi
      return 0
    fi
  done
  record_skip "Aucun de ces paquets n'est disponible/installable: $*"
  return 0
}

get_normal_user() {
  if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER:-}" != "root" ]; then
    echo "$SUDO_USER"
    return 0
  fi
  awk -F: '$3 >= 1000 && $3 < 65534 { print $1; exit }' /etc/passwd || true
}

add_user_to_groups() {
  local normal_user
  normal_user="$(get_normal_user)"

  if [ -z "$normal_user" ]; then
    record_skip "Aucun utilisateur normal détecté pour les groupes sudo/docker/libvirt/kvm"
    return 0
  fi

  echo "Ajout de l'utilisateur '$normal_user' aux groupes utiles si présents..."
  local groups_to_add=()
  local grp
  for grp in sudo docker libvirt kvm render video; do
    if getent group "$grp" >/dev/null 2>&1; then
      groups_to_add+=("$grp")
    fi
  done

  if [ "${#groups_to_add[@]}" -gt 0 ]; then
    if ! usermod -aG "$(IFS=,; echo "${groups_to_add[*]}")" "$normal_user"; then
      record_fail "usermod groupes ${groups_to_add[*]} pour $normal_user"
    else
      echo "OK: $normal_user ajouté à: ${groups_to_add[*]}"
      echo "Info: déconnexion/reconnexion nécessaire pour appliquer les groupes."
    fi
  fi
}

choose_mc_skin() {
  local skin
  for skin in dark modarcon16 gray gotar sand256 default; do
    if [ -f "/usr/share/mc/skins/${skin}.ini" ]; then
      echo "$skin"
      return 0
    fi
  done
  echo "default"
}

set_mc_skin_for_home() {
  local home_dir="$1"
  local owner="$2"
  local skin="$3"
  local conf_dir="${home_dir}/.config/mc"
  local ini="${conf_dir}/ini"

  [ -d "$home_dir" ] || return 0
  mkdir -p "$conf_dir"

  if [ ! -f "$ini" ]; then
    cat > "$ini" <<EOF
[Midnight-Commander]
skin=$skin
EOF
  elif grep -q '^skin=' "$ini"; then
    sed -i "s|^skin=.*|skin=${skin}|" "$ini"
  elif grep -q '^\[Midnight-Commander\]' "$ini"; then
    sed -i "/^\[Midnight-Commander\]/a skin=${skin}" "$ini"
  else
    cat >> "$ini" <<EOF

[Midnight-Commander]
skin=$skin
EOF
  fi

  chown -R "$owner:$owner" "$conf_dir" >/dev/null 2>&1 || true
}

configure_mc_black_theme() {
  command -v mc >/dev/null 2>&1 || return 0

  local skin
  skin="$(choose_mc_skin)"
  echo "Thème MC choisi : $skin"

  set_mc_skin_for_home "/root" "root" "$skin"

  local normal_user user_home
  normal_user="$(get_normal_user)"
  if [ -n "$normal_user" ]; then
    user_home="$(getent passwd "$normal_user" | cut -d: -f6)"
    [ -n "$user_home" ] && set_mc_skin_for_home "$user_home" "$normal_user" "$skin"
  fi

  cat > /etc/profile.d/mc-black-theme.sh <<EOF
# Généré par install.sh
export MC_SKIN="\${MC_SKIN:-$skin}"
alias mc='mc -S "\${MC_SKIN}"'
EOF
  chmod 0644 /etc/profile.d/mc-black-theme.sh
}

docker_compose_available() {
  command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1
}

docker_compose_legacy_available() {
  command -v docker-compose >/dev/null 2>&1 && docker-compose version >/dev/null 2>&1
}

install_docker_compose_tools() {
  echo "Vérification Docker Compose..."

  if docker_compose_available; then
    echo "OK: Docker Compose plugin déjà disponible."
    docker compose version || true
    return 0
  fi

  echo "Installation Docker Compose plugin + Buildx..."
  apt_install_available docker-compose-plugin docker-buildx-plugin

  if docker_compose_available; then
    echo "OK: Docker Compose plugin installé."
    docker compose version || true
    return 0
  fi

  echo "Docker Compose plugin indisponible, tentative fallback docker-compose legacy..."
  apt_install_available docker-compose

  if docker_compose_available; then
    echo "OK: Docker Compose plugin disponible après fallback."
    docker compose version || true
    return 0
  fi

  if docker_compose_legacy_available; then
    echo "OK: docker-compose legacy disponible."
    docker-compose version || true
    return 0
  fi

  record_fail "Docker Compose introuvable après installation. Installer docker-compose-plugin ou docker-compose."
}

install_docker_official() {
  echo "Suppression des anciens paquets Docker conflictuels éventuels..."
  apt-get remove -y docker.io docker-compose docker-doc podman-docker containerd runc >/dev/null 2>&1 || true

  install -m 0755 -d /etc/apt/keyrings

  echo "Ajout clé officielle Docker..."
  if curl -fsSL "https://download.docker.com/linux/debian/gpg" -o /etc/apt/keyrings/docker.asc; then
    chmod a+r /etc/apt/keyrings/docker.asc || true
  else
    record_fail "Téléchargement clé Docker officiel impossible"
  fi

  if [ -f /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
  fi

  local codename="${VERSION_CODENAME:-}"
  local arch
  arch="$(dpkg --print-architecture)"

  if [ -z "$codename" ]; then
    record_fail "VERSION_CODENAME introuvable dans /etc/os-release"
  else
    echo "Dépôt Docker Debian : ${codename} / ${arch}"
    cat > /etc/apt/sources.list.d/docker.sources <<DOCKER_SOURCES
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: ${codename}
Components: stable
Architectures: ${arch}
Signed-By: /etc/apt/keyrings/docker.asc
DOCKER_SOURCES
    apt_update_safe
  fi

  if pkg_available docker-ce; then
    apt_install_available docker-ce docker-ce-cli containerd.io
  else
    record_skip "docker-ce officiel non disponible, fallback docker.io"
    apt_install_available docker.io containerd runc
  fi

  install_docker_compose_tools

  systemctl enable --now docker >/dev/null 2>&1 || record_fail "systemctl enable --now docker"
  systemctl enable --now containerd >/dev/null 2>&1 || true

  if command -v docker >/dev/null 2>&1; then
    docker --version || true
    if docker_compose_available; then
      docker compose version || true
    elif docker_compose_legacy_available; then
      docker-compose version || true
    else
      record_fail "Docker Compose introuvable après installation Docker"
    fi
  else
    record_fail "Docker introuvable après installation"
  fi
}

install_webadmin_webmin() {
  log_step "Installation WebAdmin / Webmin"

  apt_install_available ca-certificates curl gnupg apt-transport-https

  if pkg_installed webmin; then
    echo "Webmin déjà installé."
    systemctl enable --now webmin >/dev/null 2>&1 || true
    echo "Webmin : https://IP_DU_SERVEUR:10000"
    return 0
  fi

  local setup_script="/tmp/webmin-setup-repo.sh"
  echo "Ajout du dépôt officiel Webmin..."
  if ! curl -fsSL "https://raw.githubusercontent.com/webmin/webmin/master/webmin-setup-repo.sh" -o "$setup_script"; then
    record_fail "Téléchargement webmin-setup-repo.sh impossible"
    return 0
  fi

  chmod 0755 "$setup_script" || true

  if ! sh "$setup_script" --force; then
    record_fail "Configuration dépôt Webmin impossible"
    return 0
  fi

  apt_update_safe

  echo "APT install: webmin --install-recommends"
  if ! apt-get install -y --install-recommends webmin; then
    record_fail "apt install webmin"
    return 0
  fi

  systemctl enable --now webmin >/dev/null 2>&1 || record_fail "systemctl enable --now webmin"

  if systemctl is-active --quiet webmin 2>/dev/null; then
    echo "OK: Webmin actif : https://IP_DU_SERVEUR:10000"
  else
    warn "Webmin installé mais service pas confirmé actif. Vérifier : systemctl status webmin"
  fi
}

install_kvm_libvirt_base() {
  apt_install_available \
    qemu-system-x86 qemu-system-gui qemu-utils \
    libvirt-daemon-system libvirt-clients virtinst virt-manager \
    novnc websockify \
    bridge-utils ovmf swtpm swtpm-tools dnsmasq-base

  systemctl enable --now libvirtd >/dev/null 2>&1 || true
  systemctl enable --now virtlogd >/dev/null 2>&1 || true
}

install_cockpit_kvm_console() {
  log_step "Installation Cockpit port 9090 + module KVM/libvirt"

  # Cockpit peut être installé seul, mais pour l'onglet VM il faut libvirt/KVM.
  install_kvm_libvirt_base

  apt_install_available \
    cockpit \
    cockpit-machines \
    libvirt-dbus

  systemctl enable --now cockpit.socket >/dev/null 2>&1 || record_fail "systemctl enable --now cockpit.socket"
  systemctl enable --now libvirtd >/dev/null 2>&1 || true
  systemctl enable --now virtlogd >/dev/null 2>&1 || true

  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi '^Status: active'; then
    ufw allow 9090/tcp >/dev/null 2>&1 || true
  fi

  add_user_to_groups

  if systemctl is-active --quiet cockpit.socket 2>/dev/null; then
    echo "OK: Cockpit actif : https://IP_DU_SERVEUR:9090"
    echo "Module VM/KVM : cockpit-machines"
  else
    warn "Cockpit installé mais socket pas confirmé actif. Vérifier : systemctl status cockpit.socket"
  fi
}

nvidia_driver_ready() {
  command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1
}

install_nvidia_container_toolkit_only() {
  log_step "Installation outils NVIDIA Docker uniquement si driver hôte déjà OK"

  if ! nvidia_driver_ready; then
    record_fail "Driver NVIDIA hôte non fonctionnel: nvidia-smi -L échoue. Installation outils NVIDIA annulée. Installer/corriger le driver d'abord."
    return 0
  fi

  if ! command -v docker >/dev/null 2>&1; then
    record_fail "Docker absent, impossible de configurer NVIDIA Container Toolkit"
    return 0
  fi

  echo "Driver NVIDIA détecté :"
  nvidia-smi -L || true

  apt_install_available curl gnupg ca-certificates

  install -m 0755 -d /usr/share/keyrings /etc/apt/sources.list.d

  echo "Ajout dépôt NVIDIA Container Toolkit..."
  if curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg; then
    chmod 0644 /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg || true
  else
    record_fail "Téléchargement clé NVIDIA Container Toolkit impossible"
    return 0
  fi

  if curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list; then
    echo "Dépôt NVIDIA Container Toolkit ajouté."
  else
    record_fail "Ajout dépôt NVIDIA Container Toolkit impossible"
    return 0
  fi

  apt_update_safe
  apt_install_available nvidia-container-toolkit

  if ! command -v nvidia-ctk >/dev/null 2>&1; then
    record_fail "nvidia-ctk introuvable après installation nvidia-container-toolkit"
    return 0
  fi

  nvidia-ctk runtime configure --runtime=docker || record_fail "nvidia-ctk runtime configure --runtime=docker"
  systemctl restart docker || record_fail "systemctl restart docker après configuration NVIDIA"

  write_nvidia_docker_test_tool

  echo "Contrôle runtime NVIDIA côté Docker :"
  docker info 2>/dev/null | grep -i nvidia || warn "Runtime NVIDIA non visible dans docker info. Vérifier /etc/docker/daemon.json et redémarrage Docker."
  echo "Test disponible : sudo nvidia_docker_test.sh -list"
}

install_nvidia_driver_run_version() {
  local version="$1"
  local run_file=""

  case "$version" in
    580) run_file="$NVIDIA_RUN_580" ;;
    595) run_file="$NVIDIA_RUN_595" ;;
    *)
      record_fail "Version NVIDIA inconnue: $version"
      return 0
      ;;
  esac

  log_step "Installation driver NVIDIA ${version} depuis scripts/nvidia"

  if [ ! -f "$NVIDIA_INSTALLER" ]; then
    record_fail "Installateur NVIDIA introuvable: $NVIDIA_INSTALLER"
    return 0
  fi

  if [ ! -s "$run_file" ]; then
    record_fail "Fichier driver NVIDIA introuvable: $run_file"
    return 0
  fi

  echo "Installateur : $NVIDIA_INSTALLER"
  echo "Driver       : $run_file"
  echo "Note         : l'installateur peut blacklister nouveau et redemarrer pour reprendre automatiquement."

  bash "$NVIDIA_INSTALLER" --file "$run_file" || record_fail "Installation driver NVIDIA ${version}"
}

install_intel_gpu_docker_tools() {
  log_step "Installation pilotes/outils Intel GPU + usage Docker"

  apt_install_available \
    firmware-misc-nonfree \
    intel-microcode \
    intel-gpu-tools \
    libva-utils \
    vainfo \
    intel-media-va-driver \
    intel-media-va-driver-non-free \
    i965-va-driver \
    i965-va-driver-shaders \
    mesa-va-drivers \
    mesa-vulkan-drivers \
    libvulkan1

  add_user_to_groups
  write_intel_docker_test_tool

  echo
  echo "Controle Intel GPU hote :"
  if [ -d /dev/dri ]; then
    ls -l /dev/dri || true
  else
    record_fail "/dev/dri absent: iGPU Intel non visible cote hote"
  fi

  if command -v vainfo >/dev/null 2>&1; then
    vainfo 2>/dev/null | sed -n '1,35p' || warn "vainfo installe mais test VAAPI non concluant."
  fi

  if command -v intel_gpu_top >/dev/null 2>&1; then
    echo "intel_gpu_top present."
  else
    warn "intel_gpu_top absent apres installation."
  fi

  echo
  echo "Docker Intel GPU : pas de runtime special comme NVIDIA."
  echo "Il faut passer /dev/dri au conteneur : --device /dev/dri:/dev/dri --group-add render"
  echo "Test disponible : sudo intel_docker_test.sh"
}

write_intel_docker_test_tool() {
  cat > /usr/local/sbin/intel_docker_test.sh <<'INTEL_DOCKER_TEST'
#!/usr/bin/env bash
set -Euo pipefail

# =============================================================================
# intel_docker_test.sh - aide/test Intel iGPU pour Docker
# =============================================================================
#
# Contrairement a NVIDIA, Intel n'utilise pas de runtime Docker special.
# Le conteneur doit recevoir le peripherique DRM/VAAPI :
#   --device /dev/dri:/dev/dri --group-add render
# =============================================================================

IMAGE="${INTEL_TEST_IMAGE:-debian:stable-slim}"

echo "Hote : /dev/dri"
if [ -d /dev/dri ]; then
  ls -l /dev/dri
else
  echo "ERREUR: /dev/dri absent."
  exit 1
fi

echo
echo "Groupes utiles :"
getent group render || true
getent group video || true

echo
echo "Commande Docker type :"
echo "  docker run --rm --device /dev/dri:/dev/dri --group-add render IMAGE ..."

if command -v docker >/dev/null 2>&1; then
  echo
  echo "Test Docker minimal avec ${IMAGE} :"
  docker run --rm \
    --device /dev/dri:/dev/dri \
    --group-add render \
    "$IMAGE" \
    sh -lc 'ls -l /dev/dri && id'
else
  echo
  echo "Docker absent, test conteneur ignore."
fi
INTEL_DOCKER_TEST

  chmod 0755 /usr/local/sbin/intel_docker_test.sh
}

write_nvidia_docker_test_tool() {
  cat > /usr/local/sbin/nvidia_docker_test.sh <<'NVIDIA_DOCKER_TEST'
#!/usr/bin/env bash
set -Euo pipefail

# =============================================================================
# nvidia_docker_test.sh - test NVIDIA Docker sans utiliser tous les GPU par défaut
# =============================================================================
#
# Usage :
#   sudo nvidia_docker_test.sh -list
#   sudo nvidia_docker_test.sh GPU-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
#
# Important :
#   Ne met pas --gpus all par défaut, pour éviter qu'une GT 1030 sans encodeur
#   soit utilisée par erreur à la place d'une GTX 1050 Ti / RTX / etc.
# =============================================================================

ACTION="${1:-list}"
IMAGE="${NVIDIA_TEST_IMAGE:-nvidia/cuda:12.4.1-base-ubuntu22.04}"

case "$ACTION" in
  -list|list)
    echo "GPU vus par l'hôte :"
    nvidia-smi -L
    echo
    echo "Runtimes Docker NVIDIA :"
    docker info 2>/dev/null | grep -i nvidia || true
    ;;
  GPU-*)
    echo "Test Docker NVIDIA sur GPU précis : $ACTION"
    docker run --rm \
      --gpus "device=${ACTION}" \
      "$IMAGE" \
      nvidia-smi
    ;;
  *)
    echo "Usage :"
    echo "  sudo nvidia_docker_test.sh -list"
    echo "  sudo nvidia_docker_test.sh GPU-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    exit 1
    ;;
esac
NVIDIA_DOCKER_TEST

  chmod 0755 /usr/local/sbin/nvidia_docker_test.sh
}

write_hdd_sleep_tool() {
  cat > /usr/local/sbin/hdd_sleep.sh <<'HDD_SLEEP'
#!/usr/bin/env bash
set -Euo pipefail

# =============================================================================
# hdd_sleep.sh - veille disques pour Debian NAS
# =============================================================================
#
# Usage :
#   sudo hdd_sleep.sh -list
#   sudo hdd_sleep.sh -status
#   sudo hdd_sleep.sh -stop       # met les HDD en veille immédiatement
#   sudo hdd_sleep.sh -30         # programme veille 30 min au boot via systemd
#   sudo hdd_sleep.sh -60         # programme veille 60 min au boot via systemd
#   sudo hdd_sleep.sh -off        # désactive le timer hdparm -S au boot
#   sudo hdd_sleep.sh -remove     # supprime le service systemd
#
# Détection :
#   Par défaut, seuls les disques ROTA=1 sont ciblés.
#   NVMe/SSD sont ignorés.
# =============================================================================

ACTION="${1:-status}"
SERVICE="/etc/systemd/system/hdd-veille.service"
DEFAULTS="/etc/default/hdd-veille"

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "ERREUR: lance en root."
    exit 1
  fi
}

list_hdd_names() {
  lsblk -dn -o NAME,TYPE,ROTA 2>/dev/null | awk '$2=="disk" && $3=="1" {print $1}'
}

list_hdd_devs() {
  local name
  while read -r name; do
    [ -n "$name" ] && echo "/dev/$name"
  done < <(list_hdd_names)
}

hdparm_value_for_minutes() {
  local minutes="$1"

  if ! [[ "$minutes" =~ ^[0-9]+$ ]]; then
    echo "ERREUR: minutes invalide: $minutes" >&2
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
  echo "Disques détectés :"
  lsblk -o NAME,TYPE,ROTA,SIZE,MODEL,SERIAL,MOUNTPOINTS
  echo
  echo "HDD ciblés par hdd_sleep.sh :"
  local dev
  for dev in $(list_hdd_devs); do
    echo "  $dev"
  done
}

show_status() {
  show_list
  echo
  echo "État hdparm :"
  local dev
  for dev in $(list_hdd_devs); do
    echo "------------------------------------------------------------"
    echo "$dev"
    hdparm -C "$dev" 2>/dev/null || true
    hdparm -I "$dev" 2>/dev/null | grep -E 'Advanced power management|level|standby|Power Management' || true
  done

  echo
  echo "Service systemd :"
  systemctl status hdd-veille.service --no-pager -l 2>/dev/null | sed -n '1,14p' || echo "Service non installé."
  echo
  echo "Configuration :"
  if [ -f "$DEFAULTS" ]; then
    cat "$DEFAULTS"
  else
    echo "Aucun fichier $DEFAULTS"
  fi
}

apply_timer_value() {
  local value="$1"
  local dev

  echo "Application hdparm -S $value sur les HDD..."
  for dev in $(list_hdd_devs); do
    echo "  $dev"
    hdparm -S "$value" "$dev" || true
  done
}

stop_now() {
  sync
  echo "Mise en veille immédiate des HDD : hdparm -y"
  local dev
  for dev in $(list_hdd_devs); do
    echo "  $dev"
    hdparm -y "$dev" || true
  done
}

install_service_for_minutes() {
  local minutes="$1"
  local value
  value="$(hdparm_value_for_minutes "$minutes")"

  cat > "$DEFAULTS" <<EOF
# Généré par hdd_sleep.sh
HDD_STANDBY_MINUTES=$minutes
HDD_HDPARM_S=$value
EOF

  cat > "$SERVICE" <<'EOF'
[Unit]
Description=Configure HDD standby timers
After=multi-user.target

[Service]
Type=oneshot
EnvironmentFile=/etc/default/hdd-veille
ExecStart=/usr/local/sbin/hdd_sleep.sh -apply

[Install]
WantedBy=multi-user.target
EOF

  chmod 0644 "$DEFAULTS" "$SERVICE"
  systemctl daemon-reload
  systemctl enable hdd-veille.service >/dev/null
  systemctl restart hdd-veille.service

  echo "OK: veille programmée au boot : ${minutes} minute(s), hdparm -S ${value}"
}

apply_from_defaults() {
  local value=""
  if [ -f "$DEFAULTS" ]; then
    # shellcheck disable=SC1090
    . "$DEFAULTS"
    value="${HDD_HDPARM_S:-}"
  fi

  if [ -z "$value" ]; then
    echo "ERREUR: $DEFAULTS absent ou HDD_HDPARM_S vide."
    exit 1
  fi

  apply_timer_value "$value"
}

remove_service() {
  systemctl disable --now hdd-veille.service >/dev/null 2>&1 || true
  rm -f "$SERVICE" "$DEFAULTS"
  systemctl daemon-reload
  echo "OK: service hdd-veille supprimé."
}

need_root

case "$ACTION" in
  -list|list)
    show_list
    ;;
  -status|status|-stat)
    show_status
    ;;
  -stop|stop)
    stop_now
    ;;
  -apply|apply)
    apply_from_defaults
    ;;
  -off|off)
    install_service_for_minutes 0
    apply_timer_value 0
    ;;
  -remove|remove)
    remove_service
    ;;
  -[0-9]*)
    minutes="${ACTION#-}"
    install_service_for_minutes "$minutes"
    ;;
  *)
    echo "Usage:"
    echo "  sudo hdd_sleep.sh -list"
    echo "  sudo hdd_sleep.sh -status"
    echo "  sudo hdd_sleep.sh -stop"
    echo "  sudo hdd_sleep.sh -30"
    echo "  sudo hdd_sleep.sh -60"
    echo "  sudo hdd_sleep.sh -off"
    echo "  sudo hdd_sleep.sh -remove"
    exit 1
    ;;
esac
HDD_SLEEP

  chmod 0755 /usr/local/sbin/hdd_sleep.sh
}

write_disk_spy_tool() {
  cat > /usr/local/sbin/disk_spy.sh <<'DISK_SPY'
#!/usr/bin/env bash
set -Euo pipefail

# =============================================================================
# disk_spy.sh - outils pour trouver ce qui réveille les disques
# =============================================================================
#
# Usage :
#   sudo disk_spy.sh -top       # iotop processus qui font de l'I/O
#   sudo disk_spy.sh -fatrace   # accès fichiers en direct
#   sudo disk_spy.sh -atop      # vue globale
#   sudo disk_spy.sh -lsof /mnt/disk1
# =============================================================================

ACTION="${1:-help}"

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "ERREUR: lance en root."
    exit 1
  fi
}

need_root

case "$ACTION" in
  -top|top)
    if command -v iotop >/dev/null 2>&1; then
      exec iotop -oPa
    elif command -v iotop-c >/dev/null 2>&1; then
      exec iotop-c -oPa
    else
      echo "iotop/iotop-c introuvable."
      exit 1
    fi
    ;;
  -fatrace|fatrace)
    if command -v fatrace >/dev/null 2>&1; then
      exec fatrace -t
    else
      echo "fatrace introuvable."
      exit 1
    fi
    ;;
  -atop|atop)
    exec atop
    ;;
  -lsof|lsof)
    TARGET="${2:-}"
    if [ -z "$TARGET" ]; then
      echo "Usage: sudo disk_spy.sh -lsof /mnt/disk1"
      exit 1
    fi
    exec lsof +D "$TARGET"
    ;;
  *)
    echo "Usage:"
    echo "  sudo disk_spy.sh -top"
    echo "  sudo disk_spy.sh -fatrace"
    echo "  sudo disk_spy.sh -atop"
    echo "  sudo disk_spy.sh -lsof /mnt/disk1"
    ;;
esac
DISK_SPY

  chmod 0755 /usr/local/sbin/disk_spy.sh
}

install_base_debian_nas() {
  STEP=0
  log_step "Mise à jour APT"
  apt_update_safe

  log_step "Installation outils de base système / terminal"
  apt_install_available \
    ca-certificates curl wget gnupg lsb-release apt-transport-https debian-archive-keyring \
    sudo openssh-server bash-completion locales tzdata \
    build-essential make gcc g++ pkg-config linux-headers-amd64 \
    git nano vim htop mc tmux screen ttyd tree ncdu jq yq rsync dos2unix \
    net-tools iproute2 iputils-ping bind9-dnsutils traceroute ethtool nftables \
    pciutils usbutils lshw dmidecode lsscsi sg3-utils procps psmisc lsof

  systemctl enable --now ssh >/dev/null 2>&1 || systemctl enable --now sshd >/dev/null 2>&1 || true

  log_step "Configuration Midnight Commander thème sombre"
  configure_mc_black_theme

  log_step "Installation Python / Flask via paquets Debian"
  apt_install_available \
    python3 python3-minimal python3-venv python3-pip python3-dev python3-apt python-is-python3 \
    python3-flask python3-yaml python3-requests python3-psutil python3-dotenv \
    python3-netifaces python3-watchdog python3-gunicorn pipx

  log_step "Installation outils archives / compression"
  apt_install_available tar gzip bzip2 xz-utils zip unzip zstd pigz lz4 rclone
  install_first_available 7zip p7zip-full

  log_step "Installation formats partitions / stockage"
  apt_install_available \
    util-linux mount udev parted gdisk fdisk \
    e2fsprogs xfsprogs btrfs-progs dosfstools exfatprogs ntfs-3g \
    f2fs-tools jfsutils nilfs-tools udftools \
    cryptsetup lvm2 mdadm dmsetup \
    acl attr quota quotatool \
    mergerfs fuse3 \
    snapraid zfsutils-linux

  log_step "Installation surveillance disques / énergie / diagnostic réveils"
  apt_install_available \
    smartmontools hdparm sdparm hd-idle nvme-cli \
    powertop iotop iotop-c sysstat atop blktrace fatrace inotify-tools auditd \
    apcupsd lm-sensors

  systemctl enable --now smartmontools >/dev/null 2>&1 || true
  systemctl enable --now sysstat >/dev/null 2>&1 || true
  systemctl enable --now atop >/dev/null 2>&1 || true

  log_step "Installation partages réseau : NFS / Samba / WSDD / clients"
  apt_install_available \
    nfs-kernel-server nfs-common rpcbind \
    samba samba-vfs-modules wsdd2 cifs-utils \
    proftpd-core proftpd-mod-crypto openssh-sftp-server

  systemctl enable --now rpcbind >/dev/null 2>&1 || true
  systemctl enable --now nfs-server >/dev/null 2>&1 || true
  systemctl enable --now nfs-kernel-server >/dev/null 2>&1 || true
  systemctl enable --now smbd >/dev/null 2>&1 || true
  systemctl enable --now wsdd2 >/dev/null 2>&1 || true

  log_step "Installation mDNS / Avahi"
  apt_install_available avahi-daemon avahi-utils libnss-mdns

  if [ -f /etc/nsswitch.conf ]; then
    if grep -q '^hosts:' /etc/nsswitch.conf; then
      if ! grep '^hosts:' /etc/nsswitch.conf | grep -q 'mdns4_minimal'; then
        sed -i 's/^hosts:.*/hosts:          files mdns4_minimal [NOTFOUND=return] dns myhostname mdns4/' /etc/nsswitch.conf
      fi
    fi
  fi

  systemctl enable --now avahi-daemon >/dev/null 2>&1 || true

  log_step "Installation Docker Engine officiel + Compose plugin"
  install_docker_official
  configure_bridge_netfilter_for_vm_bridges

  log_step "Installation QEMU / KVM / libvirt / virt-manager / noVNC"
  install_kvm_libvirt_base
  configure_bridge_netfilter_for_vm_bridges

  log_step "Ajout utilisateur aux groupes utiles"
  add_user_to_groups

  log_step "Création utilitaires hdd_sleep.sh et disk_spy.sh"
  write_hdd_sleep_tool
  write_disk_spy_tool

  log_step "Activation sensors si possible"
  if command -v sensors-detect >/dev/null 2>&1; then
    yes | sensors-detect --auto >/dev/null 2>&1 || true
  fi

  echo
  echo "============================================"
  echo "Installation base Debian NAS terminée."
  echo "Base alignée avec le bloc Flask : rsync, mdadm, BTRFS, ZFS, SnapRAID, Samba/NFS, Avahi, Docker, KVM/libvirt/noVNC."
  echo "Cette base n'a pas installé Webmin, Cockpit ni les outils NVIDIA."
  echo "============================================"
}

remove_cockpit() {
  log_step "Désinstallation Cockpit uniquement"

  systemctl disable --now cockpit.socket >/dev/null 2>&1 || true
  apt_purge_available cockpit-machines cockpit-packagekit cockpit-storaged cockpit-networkmanager cockpit-system cockpit-ws cockpit-bridge cockpit
  apt-get autoremove -y >/dev/null 2>&1 || true
  rm -rf /etc/cockpit /var/lib/cockpit /var/cache/cockpit 2>/dev/null || true
  systemctl daemon-reload >/dev/null 2>&1 || true

  echo "OK: Cockpit supprimé. Libvirt/KVM n'a pas été supprimé."
}

remove_webadmin_webmin() {
  log_step "Désinstallation WebAdmin / Webmin"

  systemctl disable --now webmin >/dev/null 2>&1 || true
  apt_purge_available webmin

  # Nettoyage dépôt Webmin, quel que soit le nom exact créé par le script officiel.
  if [ -d /etc/apt/sources.list.d ]; then
    grep -RIlE 'webmin|download\.webmin|software\.virtualmin' /etc/apt/sources.list.d 2>/dev/null | xargs -r rm -f
  fi
  rm -f /usr/share/keyrings/webmin*.gpg /etc/apt/trusted.gpg.d/webmin*.gpg 2>/dev/null || true
  apt-get autoremove -y >/dev/null 2>&1 || true
  apt_update_safe

  echo "OK: Webmin supprimé."
}

cleanup_nvidia_docker_runtime() {
  local daemon_json="/etc/docker/daemon.json"
  [ -f "$daemon_json" ] || return 0

  if ! command -v python3 >/dev/null 2>&1; then
    warn "python3 absent: nettoyage automatique de /etc/docker/daemon.json ignoré."
    return 0
  fi

  python3 - <<'PY'
import json
from pathlib import Path

p = Path('/etc/docker/daemon.json')
try:
    data = json.loads(p.read_text() or '{}')
except Exception as exc:
    print(f"ATTENTION: impossible de lire {p}: {exc}")
    raise SystemExit(0)

changed = False
if isinstance(data.get('runtimes'), dict) and 'nvidia' in data['runtimes']:
    data['runtimes'].pop('nvidia', None)
    changed = True
    if not data['runtimes']:
        data.pop('runtimes', None)

if data.get('default-runtime') == 'nvidia':
    data.pop('default-runtime', None)
    changed = True

if changed:
    backup = p.with_suffix('.json.bak-before-remove-nvidia')
    backup.write_text(p.read_text())
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')
    print(f"OK: runtime nvidia retiré de {p}, sauvegarde {backup}")
else:
    print("OK: aucun runtime nvidia à retirer de /etc/docker/daemon.json")
PY
}

remove_nvidia_tools() {
  log_step "Désinstallation outils NVIDIA Docker uniquement"

  apt_purge_available \
    nvidia-container-toolkit \
    nvidia-container-toolkit-base \
    libnvidia-container-tools \
    libnvidia-container1 \
    nvidia-container-runtime \
    nvidia-docker2

  rm -f /etc/apt/sources.list.d/nvidia-container-toolkit.list \
        /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
        /usr/local/sbin/nvidia_docker_test.sh 2>/dev/null || true

  cleanup_nvidia_docker_runtime

  apt-get autoremove -y >/dev/null 2>&1 || true
  apt_update_safe
  systemctl restart docker >/dev/null 2>&1 || true

  echo "OK: outils NVIDIA Docker supprimés. Le driver NVIDIA hôte n'a pas été supprimé."
}

service_line() {
  local name="$1"
  if systemctl list-unit-files "$name" >/dev/null 2>&1; then
    if systemctl is-active --quiet "$name" 2>/dev/null; then
      printf '  %-24s actif\n' "$name"
    else
      printf '  %-24s installé mais inactif\n' "$name"
    fi
  else
    printf '  %-24s absent\n' "$name"
  fi
}

show_status() {
  log_step "Statut / diagnostic rapide"

  local ip=""
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -n "$ip" ] || ip="IP_DU_SERVEUR"

  echo "Machine : $(hostname 2>/dev/null || echo inconnue)"
  echo "IP probable : $ip"
  echo

  echo "Services :"
  service_line ssh.service
  service_line docker.service
  service_line libvirtd.service
  service_line virtlogd.service
  service_line cockpit.socket
  service_line webmin.service
  service_line smbd.service
  service_line nfs-server.service
  service_line avahi-daemon.service

  echo
  echo "Accès web possibles :"
  echo "  Webmin  : https://${ip}:10000"
  echo "  Cockpit : https://${ip}:9090"

  echo
  echo "Commandes :"
  if command -v docker >/dev/null 2>&1; then
    echo "  Docker : $(docker --version 2>/dev/null || true)"
    if docker_compose_available; then
      echo "  Compose: $(docker compose version 2>/dev/null || true)"
    elif docker_compose_legacy_available; then
      echo "  Compose: $(docker-compose version 2>/dev/null || true)"
    else
      echo "  Compose: absent"
    fi
  else
    echo "  Docker : absent"
    echo "  Compose: absent"
  fi

  if command -v virsh >/dev/null 2>&1; then
    echo "  virsh  : $(virsh --version 2>/dev/null || true)"
  else
    echo "  virsh  : absent"
  fi

  if command -v nvidia-smi >/dev/null 2>&1; then
    if nvidia-smi -L >/dev/null 2>&1; then
      echo "  NVIDIA : driver OK"
      nvidia-smi -L || true
    else
      echo "  NVIDIA : nvidia-smi présent mais driver non fonctionnel"
    fi
  else
    echo "  NVIDIA : nvidia-smi absent"
  fi

  if command -v nvidia-ctk >/dev/null 2>&1; then
    echo "  NVIDIA Container Toolkit : présent"
  else
    echo "  NVIDIA Container Toolkit : absent"
  fi

  echo
  if [ -d /dev/dri ]; then
    echo "  Intel/iGPU : /dev/dri present"
    if command -v intel_gpu_top >/dev/null 2>&1; then
      echo "  Intel tools: intel_gpu_top present"
    else
      echo "  Intel tools: intel_gpu_top absent"
    fi
  else
    echo "  Intel/iGPU : /dev/dri absent"
  fi

  echo
  echo "Ports à l'écoute :"
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | grep -E ':(22|9090|10000)\b' || true
  else
    echo "  ss absent"
  fi
}

explain_known_notes() {
  echo
  echo "Notes :"
  echo "  - La base n'installe pas Webmin, Cockpit, ni les outils NVIDIA."
  echo "  - Cockpit écoute via cockpit.socket sur le port 9090."
  echo "  - Webmin/WebAdmin écoute par défaut en HTTPS sur le port 10000."
  echo "  - Les outils NVIDIA Docker exigent que nvidia-smi -L fonctionne déjà."
  echo "  - Le driver NVIDIA hôte doit être installé séparément si besoin."
  echo "  - Debian 13/Trixie : dnsutils -> bind9-dnsutils, wsdd -> wsdd2, proftpd-basic -> proftpd-core."
}

show_install_report() {
  echo
  echo "============================================================================="
  echo "RÉSUMÉ"
  echo "============================================================================="

  if [ "${#SKIPPED_ITEMS[@]}" -eq 0 ] && [ "${#FAILED_ITEMS[@]}" -eq 0 ]; then
    echo "✅ Aucune erreur non bloquante enregistrée."
  else
    echo "⚠️ Terminé, mais tout n'a pas été parfait."

    if [ "${#SKIPPED_ITEMS[@]}" -gt 0 ]; then
      echo
      echo "Éléments ignorés / non disponibles :"
      printf '  - %s\n' "${SKIPPED_ITEMS[@]}"
    fi

    if [ "${#FAILED_ITEMS[@]}" -gt 0 ]; then
      echo
      echo "Éléments en erreur non bloquante :"
      printf '  - %s\n' "${FAILED_ITEMS[@]}"
    fi
  fi

  explain_known_notes
}

pause_menu() {
  echo
  read -r -p "Entrée pour revenir au menu... " _ || true
}

show_gpu_menu() {
  clear 2>/dev/null || true
  cat <<'MENU'
=============================================================================
 install.sh - Drivers GPU
=============================================================================

  11) Installer driver NVIDIA 580 (.run local)
  12) Installer driver NVIDIA 595 (.run local)
  13) Installer outils NVIDIA Docker
      Seulement si nvidia-smi fonctionne deja.

  14) Installer Intel GPU + outils Docker
      Intel VAAPI/Mesa/intel-gpu-tools + helper Docker /dev/dri.

  0) Retour

=============================================================================
MENU
}

gpu_menu_loop() {
  while true; do
    show_gpu_menu
    read -r -p "Choix GPU : " choice || choice="0"
    case "$choice" in
      11)
        install_nvidia_driver_run_version 580
        show_install_report
        pause_menu
        ;;
      12)
        install_nvidia_driver_run_version 595
        show_install_report
        pause_menu
        ;;
      13)
        apt_update_safe
        install_nvidia_container_toolkit_only
        show_install_report
        pause_menu
        ;;
      14)
        apt_update_safe
        install_intel_gpu_docker_tools
        show_install_report
        pause_menu
        ;;
      0|r|R|retour|back)
        return 0
        ;;
      *)
        echo "Choix GPU invalide."
        pause_menu
        ;;
    esac
  done
}

show_menu() {
  clear 2>/dev/null || true
  cat <<'MENU'
=============================================================================
 install.sh - Debian PURE NAS / menu installation
=============================================================================

  1) Installer base Debian NAS
     Outils système, SSH, Python/Flask, terminal, stockage, rsync,
     mdadm, BTRFS, ZFS, SnapRAID, Samba/NFS, Avahi,
     Docker, KVM/libvirt/noVNC, MC, outils disques.
     Ne dépend pas de NVIDIA. N'installe pas Webmin/Cockpit.

  2) Installer WebAdmin / Webmin
     Port HTTPS 10000.

  3) Installer Cockpit + KVM
     Port HTTPS 9090 + cockpit-machines + libvirt.

  4) Installer outils NVIDIA Docker
     Seulement si le driver hôte est déjà OK avec nvidia-smi.
     N'installe pas le driver NVIDIA.

  5) Statut / diagnostic

  6) Désinstaller Cockpit
     Supprime Cockpit uniquement, pas libvirt/KVM.

  7) Désinstaller WebAdmin / Webmin

  8) Désinstaller outils NVIDIA Docker
     Supprime le toolkit Docker NVIDIA uniquement, pas le driver hôte.

  9) Menu drivers GPU
     Pilotes NVIDIA 580/595, outils NVIDIA Docker, Intel GPU + Docker.

  11) Installer driver NVIDIA 580 (.run local)
  12) Installer driver NVIDIA 595 (.run local)
  13) Installer outils NVIDIA Docker
  14) Installer Intel GPU + outils Docker

  0) Quitter

=============================================================================
MENU
}

menu_loop() {
  while true; do
    show_menu
    read -r -p "Choix : " choice || choice="0"
    case "$choice" in
      1)
        install_base_debian_nas
        show_install_report
        pause_menu
        ;;
      2)
        apt_update_safe
        install_webadmin_webmin
        show_install_report
        pause_menu
        ;;
      3)
        apt_update_safe
        install_cockpit_kvm_console
        show_install_report
        pause_menu
        ;;
      4)
        apt_update_safe
        install_nvidia_container_toolkit_only
        show_install_report
        pause_menu
        ;;
      5)
        show_status
        pause_menu
        ;;
      6)
        remove_cockpit
        show_install_report
        pause_menu
        ;;
      7)
        remove_webadmin_webmin
        show_install_report
        pause_menu
        ;;
      8)
        remove_nvidia_tools
        show_install_report
        pause_menu
        ;;
      9)
        gpu_menu_loop
        ;;
      11)
        install_nvidia_driver_run_version 580
        show_install_report
        pause_menu
        ;;
      12)
        install_nvidia_driver_run_version 595
        show_install_report
        pause_menu
        ;;
      13)
        apt_update_safe
        install_nvidia_container_toolkit_only
        show_install_report
        pause_menu
        ;;
      14)
        apt_update_safe
        install_intel_gpu_docker_tools
        show_install_report
        pause_menu
        ;;
      0|q|Q|quit|exit)
        echo "Fin."
        exit 0
        ;;
      *)
        echo "Choix invalide."
        pause_menu
        ;;
    esac
  done
}

usage() {
  cat <<'EOF'
Usage interactif :
  sudo bash install.sh

Usage direct par nom :
  sudo bash install.sh -base
  sudo bash install.sh -webadmin
  sudo bash install.sh -cockpit
  sudo bash install.sh -nvidia
  sudo bash install.sh -gpu
  sudo bash install.sh -nvidia-driver-580
  sudo bash install.sh -nvidia-driver-595
  sudo bash install.sh -intel-gpu
  sudo bash install.sh -status
  sudo bash install.sh -remove-cockpit
  sudo bash install.sh -remove-webadmin
  sudo bash install.sh -remove-nvidia

Usage direct Flask / sans menu :
  sudo bash install.sh -1    # menu 1 : Installer base Debian NAS
  sudo bash install.sh -2    # menu 2 : Installer WebAdmin / Webmin
  sudo bash install.sh -3    # menu 3 : Installer Cockpit + KVM
  sudo bash install.sh -4    # menu 4 : Installer outils NVIDIA Docker
  sudo bash install.sh -5    # menu 5 : Statut / diagnostic
  sudo bash install.sh -6    # menu 6 : Désinstaller Cockpit
  sudo bash install.sh -7    # menu 7 : Désinstaller WebAdmin / Webmin
  sudo bash install.sh -8    # menu 8 : Désinstaller outils NVIDIA Docker
  sudo bash install.sh -11   # menu 11 : Installer driver NVIDIA 580
  sudo bash install.sh -12   # menu 12 : Installer driver NVIDIA 595
  sudo bash install.sh -13   # menu 13 : Installer outils NVIDIA Docker
  sudo bash install.sh -14   # menu 14 : Installer Intel GPU + outils Docker

Log optionnel :
  sudo bash install.sh -1 --log
  sudo bash install.sh -1 --log /tmp/install_base.log
  sudo bash install.sh --log /tmp/install_base.log -1
EOF
}

main() {
  parse_args "$@"
  setup_logging
  set -- "${ACTION_ARGS[@]}"

  need_root

  if [ "$#" -gt 1 ]; then
    echo "Option(s) inconnue(s) ou en trop: $*"
    usage
    exit 1
  fi

  case "${1:-}" in
    "")
      menu_loop
      ;;
    -1|1|-base|base)
      install_base_debian_nas
      show_install_report
      ;;
    -2|2|-webadmin|webadmin|-webmin|webmin)
      apt_update_safe
      install_webadmin_webmin
      show_install_report
      ;;
    -3|3|-cockpit|cockpit)
      apt_update_safe
      install_cockpit_kvm_console
      show_install_report
      ;;
    -4|4|-nvidia|nvidia|-nvidia-tools|nvidia-tools)
      apt_update_safe
      install_nvidia_container_toolkit_only
      show_install_report
      ;;
    -5|5|-status|status|-stat|stat)
      show_status
      ;;
    -6|6|-remove-cockpit|remove-cockpit)
      remove_cockpit
      show_install_report
      ;;
    -7|7|-remove-webadmin|remove-webadmin|-remove-webmin|remove-webmin)
      remove_webadmin_webmin
      show_install_report
      ;;
    -8|8|-remove-nvidia|remove-nvidia|-remove-nvidia-tools|remove-nvidia-tools)
      remove_nvidia_tools
      show_install_report
      ;;
    -9|9|-gpu|gpu|-drivers-gpu|drivers-gpu)
      gpu_menu_loop
      ;;
    -11|11|-nvidia-driver-580|nvidia-driver-580|-nvidia-580|nvidia-580)
      install_nvidia_driver_run_version 580
      show_install_report
      ;;
    -12|12|-nvidia-driver-595|nvidia-driver-595|-nvidia-595|nvidia-595)
      install_nvidia_driver_run_version 595
      show_install_report
      ;;
    -13|13|-nvidia-docker|nvidia-docker|-nvidia-tools|nvidia-tools)
      apt_update_safe
      install_nvidia_container_toolkit_only
      show_install_report
      ;;
    -14|14|-intel-gpu|intel-gpu|-intel-docker|intel-docker)
      apt_update_safe
      install_intel_gpu_docker_tools
      show_install_report
      ;;
    -h|--help|help)
      usage
      ;;
    *)
      echo "Option inconnue: $1"
      usage
      exit 1
      ;;
  esac
}

main "$@"
