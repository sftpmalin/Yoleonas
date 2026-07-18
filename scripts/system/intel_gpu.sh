#!/usr/bin/env bash
set -Eeuo pipefail

LOG="/root/yoleo_install_intel_uhd770_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

echo "============================================================"
echo " Yoleo / Debian - Installation Intel iGPU i7-13700 / UHD 770"
echo "============================================================"
echo "Log : $LOG"
echo

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERREUR: lance ce script en root."
  exit 1
fi

if [[ -r /etc/os-release ]]; then
  . /etc/os-release
else
  echo "ERREUR: /etc/os-release introuvable."
  exit 1
fi

CODENAME="${VERSION_CODENAME:-trixie}"
echo "[INFO] Distribution detectee : ${PRETTY_NAME:-Debian} / codename=${CODENAME}"
echo

echo "[1/5] Sauvegarde des sources APT..."
mkdir -p /root/yoleo_apt_backup
TS="$(date +%Y%m%d_%H%M%S)"

if [[ -f /etc/apt/sources.list ]]; then
  cp -a /etc/apt/sources.list "/root/yoleo_apt_backup/sources.list.${TS}.bak"
  echo "  Backup: /root/yoleo_apt_backup/sources.list.${TS}.bak"
fi

if compgen -G "/etc/apt/sources.list.d/*.sources" > /dev/null; then
  cp -a /etc/apt/sources.list.d/*.sources /root/yoleo_apt_backup/ || true
  echo "  Backup: /root/yoleo_apt_backup/*.sources"
fi

if compgen -G "/etc/apt/sources.list.d/*.list" > /dev/null; then
  cp -a /etc/apt/sources.list.d/*.list /root/yoleo_apt_backup/ || true
  echo "  Backup: /root/yoleo_apt_backup/*.list"
fi

echo
echo "[2/5] Activation des composants Debian: main contrib non-free non-free-firmware..."

changed=0

# Format Debian moderne Deb822 (*.sources)
if compgen -G "/etc/apt/sources.list.d/*.sources" > /dev/null; then
  for f in /etc/apt/sources.list.d/*.sources; do
    if [[ -s "$f" ]] && grep -qE '^Components:' "$f"; then
      echo "  Correction Deb822: $f"
      sed -i -E 's/^Components:.*/Components: main contrib non-free non-free-firmware/' "$f"
      changed=1
    fi
  done
fi

# Format classique /etc/apt/sources.list
if [[ -s /etc/apt/sources.list ]] && grep -qE '^[[:space:]]*deb[[:space:]]+https?://' /etc/apt/sources.list; then
  echo "  Correction sources.list classique: /etc/apt/sources.list"
  tmp="$(mktemp)"
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ ^[[:space:]]*deb[[:space:]]+https?:// ]]; then
      for comp in main contrib non-free non-free-firmware; do
        if [[ " $line " != *" $comp "* ]]; then
          line="$line $comp"
        fi
      done
    fi
    printf '%s\n' "$line"
  done < /etc/apt/sources.list > "$tmp"
  cat "$tmp" > /etc/apt/sources.list
  rm -f "$tmp"
  changed=1
fi

# Si rien n'a ete trouve, on cree une source Debian propre pour Trixie.
if [[ "$changed" -eq 0 ]]; then
  echo "  Aucune source modifiable trouvee, creation de /etc/apt/sources.list.d/yoleo-debian.sources"
  cat > /etc/apt/sources.list.d/yoleo-debian.sources <<EOF
Types: deb
URIs: http://deb.debian.org/debian
Suites: ${CODENAME} ${CODENAME}-updates
Components: main contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: http://security.debian.org/debian-security
Suites: ${CODENAME}-security
Components: main contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
fi

echo
echo "[INFO] Sources APT apres correction:"
if [[ -f /etc/apt/sources.list ]]; then
  echo "--- /etc/apt/sources.list ---"
  cat /etc/apt/sources.list
fi
if compgen -G "/etc/apt/sources.list.d/*.sources" > /dev/null; then
  echo "--- /etc/apt/sources.list.d/*.sources ---"
  for f in /etc/apt/sources.list.d/*.sources; do
    echo "### $f"
    cat "$f"
  done
fi

echo
echo "[3/5] apt update..."
apt update

echo
echo "[4/5] Installation Intel UHD 770 / VAAPI / outils..."
PKGS=(
  pciutils
  firmware-misc-nonfree
  firmware-intel-graphics
  intel-media-va-driver-non-free
  vainfo
  intel-gpu-tools
  mesa-utils
  mesa-vulkan-drivers
  libva2
  libva-drm2
)

TO_INSTALL=()
MISSING=()

for pkg in "${PKGS[@]}"; do
  if apt-cache show "$pkg" >/dev/null 2>&1; then
    TO_INSTALL+=("$pkg")
  else
    MISSING+=("$pkg")
  fi
done

if [[ "${#MISSING[@]}" -gt 0 ]]; then
  echo
  echo "ATTENTION: paquets introuvables dans tes depots actuels:"
  printf '  - %s\n' "${MISSING[@]}"
  echo
fi

if [[ "${#TO_INSTALL[@]}" -eq 0 ]]; then
  echo "ERREUR: aucun paquet installable trouve. Regarde le log: $LOG"
  exit 1
fi

apt install -y "${TO_INSTALL[@]}"

echo
echo "[5/5] Verification rapide..."
echo
echo "GPU detectes:"
lspci -nn | grep -Ei 'vga|display|3d' || true

echo
echo "Module i915:"
modprobe i915 2>/dev/null || true
lsmod | grep '^i915' || echo "  i915 pas encore charge ou iGPU non initialise."

echo
echo "Peripheriques /dev/dri:"
ls -l /dev/dri 2>/dev/null || echo "  /dev/dri absent pour le moment."

echo
if [[ -e /dev/dri/renderD128 ]]; then
  echo "Test VAAPI DRM:"
  vainfo --display drm --device /dev/dri/renderD128 || true
else
  echo "Pas de /dev/dri/renderD128 pour le moment. Un reboot peut etre necessaire."
fi

echo
echo "============================================================"
echo " TERMINE"
echo "============================================================"
echo "Log complet : $LOG"
echo
echo "Recommande maintenant:"
echo "  reboot"
echo
echo "Apres reboot, verifie avec:"
echo "  lsmod | grep i915"
echo "  ls -l /dev/dri"
echo "  vainfo --display drm --device /dev/dri/renderD128"
echo "  intel_gpu_top"
echo
