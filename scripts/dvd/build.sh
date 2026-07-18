#!/bin/bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
DEBIAN_VERSION="${DEBIAN_VERSION:-13.4.0}"
DEBIAN_ARCH="${DEBIAN_ARCH:-amd64}"
ISO_DIR="${ISO_DIR:-${SCRIPT_DIR}/iso}"
ISO_NAME="${ISO_NAME:-debian-${DEBIAN_VERSION}-${DEBIAN_ARCH}-netinst.iso}"
DEFAULT_SRC_ISO="${ISO_DIR}/${ISO_NAME}"
SRC_ISO="${SRC_ISO:-$DEFAULT_SRC_ISO}"
NAS_DIR="${NAS_DIR:-${SCRIPT_DIR}/nas}"
BUILD_ROOT="${BUILD_ROOT:-${SCRIPT_DIR}/iso_work}"
OUT_DIR="${OUT_DIR:-${SCRIPT_DIR}/out}"
WORK_DIR="${BUILD_ROOT}/gen1-current"
OUT_ISO="${OUT_ISO:-${OUT_DIR}/debian-${DEBIAN_VERSION}-${DEBIAN_ARCH}-nas-gen1.iso}"
BUILD_DEMO_RAM="${BUILD_DEMO_RAM:-1}"
BUILD_DEMO_SCRIPT="${BUILD_DEMO_SCRIPT:-${SCRIPT_DIR}/build_demo.sh}"
COPY_TO_TOWER="${COPY_TO_TOWER:-1}"
COPY_DEMO_TO_TOWER="${COPY_DEMO_TO_TOWER:-1}"
NAS_INSTALL_DIR="${NAS_INSTALL_DIR:-/yoleo}"
NAS_INSTALL_DIR="/${NAS_INSTALL_DIR#/}"
NAS_INSTALL_DIR="${NAS_INSTALL_DIR%/}"
[ -n "$NAS_INSTALL_DIR" ] && [ "$NAS_INSTALL_DIR" != "/" ] || { echo "ERREUR: NAS_INSTALL_DIR invalide: ${NAS_INSTALL_DIR}" >&2; exit 1; }

PRESEED_PACKAGES="
openssh-server sudo curl ca-certificates gnupg lsb-release iproute2 net-tools
python3 python3-venv python3-pip rsync bash-completion locales tzdata cron
"

NAS_PACKAGES="
ca-certificates curl wget gnupg lsb-release apt-transport-https debian-archive-keyring
sudo openssh-server bash-completion locales tzdata
build-essential make gcc g++ pkg-config
git nano vim htop mc tmux screen tree ncdu jq yq rsync dos2unix
net-tools iproute2 iputils-ping bind9-dnsutils traceroute ethtool
pciutils usbutils lshw dmidecode lsscsi sg3-utils procps psmisc lsof
python3 python3-minimal python3-venv python3-pip python3-dev python3-apt python-is-python3
python3-flask python3-yaml python3-requests python3-psutil python3-dotenv
python3-netifaces python3-watchdog python3-docker python3-paramiko python3-gunicorn gunicorn pipx
tar gzip bzip2 xz-utils zip unzip zstd pigz lz4 rclone 7zip
util-linux mount udev parted gdisk fdisk
e2fsprogs xfsprogs btrfs-progs dosfstools exfatprogs ntfs-3g
f2fs-tools jfsutils nilfs-tools udftools
cryptsetup lvm2 mdadm dmsetup
acl attr quota quotatool
mergerfs fuse3
snapraid
smartmontools hdparm sdparm hd-idle nvme-cli
powertop iotop iotop-c sysstat atop blktrace fatrace inotify-tools auditd
apcupsd lm-sensors
nfs-kernel-server nfs-common rpcbind
samba samba-vfs-modules wsdd2 cifs-utils
proftpd-core proftpd-mod-crypto openssh-sftp-server
avahi-daemon avahi-utils libnss-mdns
docker.io containerd runc
docker-compose docker-buildx
qemu-system-x86 qemu-system-gui qemu-utils
libvirt-daemon-system libvirt-clients virtinst virt-manager
novnc websockify
bridge-utils ovmf swtpm swtpm-tools dnsmasq-base
"

need_file() {
    [ -f "$1" ] || { echo "ERREUR: fichier introuvable: $1" >&2; exit 1; }
}

need_dir() {
    [ -d "$1" ] || { echo "ERREUR: dossier introuvable: $1" >&2; exit 1; }
}

download_src_iso() {
    local target="$1"
    local tmp="${target}.part"
    local urls=()
    local url

    if [ -n "${ISO_URL:-}" ]; then
        urls+=("$ISO_URL")
    else
        urls+=(
            "https://cdimage.debian.org/cdimage/archive/${DEBIAN_VERSION}/${DEBIAN_ARCH}/iso-cd/${ISO_NAME}"
            "https://cdimage.debian.org/debian-cd/current/${DEBIAN_ARCH}/iso-cd/${ISO_NAME}"
            "https://cdimage.debian.org/debian-cd/${DEBIAN_VERSION}/${DEBIAN_ARCH}/iso-cd/${ISO_NAME}"
        )
    fi

    mkdir -p "$(dirname "$target")"
    rm -f "$tmp"

    for url in "${urls[@]}"; do
        echo "ISO introuvable, telechargement: $url"
        if command -v curl >/dev/null 2>&1; then
            if curl -fL --retry 3 --connect-timeout 20 -o "$tmp" "$url"; then
                mv "$tmp" "$target"
                return 0
            fi
        elif command -v wget >/dev/null 2>&1; then
            if wget -O "$tmp" "$url"; then
                mv "$tmp" "$target"
                return 0
            fi
        else
            echo "ERREUR: curl ou wget requis pour telecharger l'ISO" >&2
            return 1
        fi
        rm -f "$tmp"
    done

    echo "ERREUR: impossible de telecharger ${ISO_NAME}" >&2
    return 1
}

ensure_src_iso() {
    if [ -f "$SRC_ISO" ]; then
        return 0
    fi

    if [ "$SRC_ISO" != "$DEFAULT_SRC_ISO" ]; then
        echo "WARN: ISO introuvable: $SRC_ISO" >&2
        echo "WARN: fallback vers le dossier ISO local: $DEFAULT_SRC_ISO" >&2
        SRC_ISO="$DEFAULT_SRC_ISO"
        [ -f "$SRC_ISO" ] && return 0
    fi

    download_src_iso "$SRC_ISO"
}

ensure_src_iso
need_dir "$NAS_DIR"
command -v xorriso >/dev/null 2>&1 || { echo "ERREUR: xorriso absent" >&2; exit 1; }

# IMPORTANT: on garde volontairement PRESEED_PACKAGES + NAS_PACKAGES ici.
# Le gros APT doit se faire dans l'installateur Debian, avant le premier reboot.
PRESEED_PACKAGE_LINE="$(printf '%s\n%s\n' "$PRESEED_PACKAGES" "$NAS_PACKAGES" | tr '\n' ' ' | xargs -n1 | awk '!seen[$0]++' | xargs)"
BOOT_PARAMS="auto=true priority=high locale=fr_FR.UTF-8 language=fr country=FR keymap=fr-latin9 console-keymaps-at/keymap=fr-latin9 debian-installer/keymap=fr-latin9 kbd-chooser/method=fr-latin9 keyboard-configuration/xkb-keymap=fr keyboard-configuration/layout=French keyboard-configuration/layoutcode=fr pkgsel/update-policy=none unattended-upgrades/enable_auto_updates=false file=/preseed.cfg preseed/file=/preseed.cfg"
INSTALL_VIDEO_PARAMS="vga=normal fb=false debian-installer/framebuffer=false nomodeset"

rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR" "$OUT_DIR"

cat > "$WORK_DIR/preseed.cfg" <<EOF
### Debian NAS gen1 - preseed partiel
### Francais auto, comptes et disque demandes a l'utilisateur.

d-i debian-installer/locale string fr_FR.UTF-8
d-i debian-installer/language string fr
d-i debian-installer/country string FR
d-i localechooser/supported-locales multiselect fr_FR.UTF-8
d-i keyboard-configuration/xkb-keymap select fr
d-i keyboard-configuration/modelcode string pc105
d-i keyboard-configuration/layout select French
d-i keyboard-configuration/layout string French
d-i keyboard-configuration/layoutcode string fr
d-i keyboard-configuration/variant select French
d-i keyboard-configuration/variant string French
d-i keyboard-configuration/variantcode string
d-i keyboard-configuration/toggle select No toggling
d-i console-setup/ask_detect boolean false
d-i console-setup/layoutcode string fr
d-i console-setup/variantcode string
d-i console-keymaps-at/keymap select fr
d-i console-keymaps-at/keymap select fr-latin9
d-i debian-installer/keymap select fr
d-i kbd-chooser/method select French
d-i kbd-chooser/method select fr-latin9

d-i netcfg/choose_interface select auto
d-i netcfg/get_hostname string Yoleo
d-i netcfg/get_domain string local

d-i clock-setup/utc boolean true
d-i time/zone string Europe/Paris
d-i clock-setup/ntp boolean true

### Comptes : Debian demande uniquement le mot de passe root, pas de compte utilisateur.
d-i passwd/root-login boolean true
d-i passwd/make-user boolean false

### Partitionnement : Debian demandera le disque, puis utilisera le guide standard.
d-i partman-auto/method string regular
d-i partman-auto/choose_recipe select atomic
d-i partman/default_filesystem string ext4
d-i partman-partitioning/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true

### Miroir, sources et questions inutiles.
d-i mirror/country string manual
d-i mirror/http/hostname string deb.debian.org
d-i mirror/http/directory string /debian
d-i mirror/http/proxy string
d-i apt-setup/use_mirror boolean true
d-i apt-setup/non-free-firmware boolean true
d-i apt-setup/contrib boolean true
d-i apt-setup/cdrom/set-first boolean false
d-i apt-setup/cdrom/set-next boolean false
d-i apt-setup/cdrom/set-failed boolean false
d-i apt-setup/disable-cdrom-entries boolean true

tasksel tasksel/first multiselect standard, ssh-server
d-i pkgsel/include string ${PRESEED_PACKAGE_LINE}
d-i pkgsel/upgrade select none
d-i pkgsel/update-policy select none
# Evite l'ecran "Gestion des mises a jour" / unattended-upgrades pendant pkgsel.
d-i pkgsel/update-policy seen true
unattended-upgrades unattended-upgrades/enable_auto_updates boolean false
popularity-contest popularity-contest/participate boolean false
d-i popularity-contest/participate boolean false

### GRUB : installation sur le disque principal choisi par l'installateur.
d-i grub-installer/only_debian boolean true
d-i grub-installer/with_other_os boolean true
d-i grub-installer/bootdev string default

d-i finish-install/reboot_in_progress note

### Copie NAS + preparation du premier boot.
d-i preseed/late_command string /bin/sh /cdrom/nas_late_command.sh
EOF

cat > "$WORK_DIR/nas_late_command.sh" <<'EOF'
#!/bin/sh
set -eu

LOG="/target/root/nas-late-command.log"
exec >>"$LOG" 2>&1

echo "========== NAS late_command $(date) =========="
NAS_INSTALL_DIR="__NAS_INSTALL_DIR__"
TARGET_NAS_DIR="/target${NAS_INSTALL_DIR}"

echo "Copie de /cdrom/nas vers ${TARGET_NAS_DIR}"
mkdir -p "$TARGET_NAS_DIR"
cp -a /cdrom/nas/. "$TARGET_NAS_DIR/"
chmod +x "${TARGET_NAS_DIR}/system/system.sh" "${TARGET_NAS_DIR}/scripts/lan_ip.sh" 2>/dev/null || true

mkdir -p /target/etc/systemd/system-generators
ln -sf /dev/null /target/etc/systemd/system-generators/systemd-ssh-generator
mkdir -p /target/etc/systemd/user-generators
ln -sf /dev/null /target/etc/systemd/user-generators/systemd-ssh-generator

mkdir -p /target/etc/ssh/sshd_config.d
if [ -f /target/etc/ssh/sshd_config ] && ! grep -Eq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' /target/etc/ssh/sshd_config; then
    sed -i '1i Include /etc/ssh/sshd_config.d/*.conf' /target/etc/ssh/sshd_config
fi
cat > /target/etc/ssh/sshd_config.d/99-yoleo-root-login.conf <<'EOS'
# Yoleo NAS OS: acces root direct, comme OMV/labo local.
PermitRootLogin yes
PasswordAuthentication yes
KbdInteractiveAuthentication yes
PubkeyAuthentication yes
EOS

cat > /target/etc/default/keyboard <<'EOS'
XKBMODEL="pc105"
XKBLAYOUT="fr"
XKBVARIANT=""
XKBOPTIONS=""
BACKSPACE="guess"
EOS

echo "Activation du boot verbeux type NAS"
mkdir -p /target/etc/default/grub.d
cat > /target/etc/default/grub.d/99-yoleo-verbose-boot.cfg <<'EOS'
# Yoleo NAS OS: afficher les services au demarrage.
# On evite le boot silencieux, sinon en VNC/console on croit que la machine est bloquee.
GRUB_CMDLINE_LINUX_DEFAULT="loglevel=4 printk.time=1"
GRUB_CMDLINE_LINUX="systemd.show_status=1"
EOS

mkdir -p /target/etc/systemd/system.conf.d
cat > /target/etc/systemd/system.conf.d/99-yoleo-verbose-boot.conf <<'EOS'
[Manager]
ShowStatus=yes
LogLevel=info
EOS

if [ -f /target/etc/default/grub ]; then
    sed -i \
        -e 's/[[:space:]]quiet\([[:space:]]\|"\)/\1/g' \
        -e 's/[[:space:]]splash\([[:space:]]\|"\)/\1/g' \
        /target/etc/default/grub || true
fi
in-target update-grub >/dev/null 2>&1 || true

mkdir -p /target/etc/systemd/network
cat > /target/etc/systemd/network/20-yoleo-dhcp.network <<'EOS'
[Match]
Name=en* eth*
Type=ether

[Network]
DHCP=ipv4
IPv6AcceptRA=yes

[DHCP]
RouteMetric=100
EOS
in-target systemctl enable systemd-networkd.service >/dev/null 2>&1 || true
in-target systemctl enable systemd-networkd-wait-online.service >/dev/null 2>&1 || true

mkdir -p /target/usr/local/sbin
cat > /target/usr/local/sbin/nas-update-issue.sh <<'EOS'
#!/bin/bash
set -euo pipefail

ISSUE_FILE="/etc/issue"
NAS_INSTALL_DIR="__NAS_INSTALL_DIR__"
host="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo Yoleo)"
iface="-"
gateway="-"
ipaddr=""
all_ips="-"
port="12345"

is_bad_iface() {
    case "${1:-}" in
        ""|"-"|lo|docker*|br-*|veth*|virbr*|tun*|tap*) return 0 ;;
        *) return 1 ;;
    esac
}

is_bad_ip() {
    case "${1:-}" in
        ""|127.*|169.254.*) return 0 ;;
        *) return 1 ;;
    esac
}

detect_network() {
    local default_line route_get candidate route_iface route_ip route_gateway
    iface="-"
    gateway="-"
    ipaddr=""
    all_ips="-"

    command -v ip >/dev/null 2>&1 || return 0

    all_ips="$(ip -o -4 addr show scope global 2>/dev/null | awk '
        function bad_iface(i) { return (i == "lo" || i ~ /^docker/ || i ~ /^br-/ || i ~ /^veth/ || i ~ /^virbr/ || i ~ /^tun/ || i ~ /^tap/) }
        {
            split($4, a, "/")
            if (!bad_iface($2) && a[1] !~ /^127\./ && a[1] !~ /^169\.254\./) {
                if (out != "") out = out "  "
                out = out $2 "=" a[1]
            }
        }
        END { print out }
    ')"
    [ -n "$all_ips" ] || all_ips="-"

    route_get="$(ip -4 route get 1.1.1.1 2>/dev/null | head -n 1 || true)"
    if [ -n "$route_get" ]; then
        route_iface="$(echo "$route_get" | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')"
        route_ip="$(echo "$route_get" | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
        route_gateway="$(echo "$route_get" | awk '{for(i=1;i<=NF;i++) if($i=="via"){print $(i+1); exit}}')"
        if ! is_bad_iface "$route_iface" && ! is_bad_ip "$route_ip"; then
            iface="$route_iface"
            ipaddr="$route_ip"
            gateway="${route_gateway:-}"
        fi
    fi

    default_line="$(ip -4 route show default 2>/dev/null | head -n 1 || true)"
    if [ -n "$default_line" ]; then
        [ -n "$gateway" ] && [ "$gateway" != "-" ] || gateway="$(echo "$default_line" | awk '{for(i=1;i<=NF;i++) if($i=="via"){print $(i+1); exit}}')"
        if [ -z "$ipaddr" ]; then
            route_iface="$(echo "$default_line" | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')"
            if ! is_bad_iface "$route_iface"; then
                route_ip="$(ip -o -4 addr show dev "$route_iface" scope global 2>/dev/null | awk '{print $4; exit}' | cut -d/ -f1)"
                if ! is_bad_ip "$route_ip"; then
                    iface="$route_iface"
                    ipaddr="$route_ip"
                fi
            fi
        fi
    fi

    if [ -z "$ipaddr" ]; then
        candidate="$(ip -o -4 addr show scope global 2>/dev/null | awk '
            function bad_iface(i) { return (i == "lo" || i ~ /^docker/ || i ~ /^br-/ || i ~ /^veth/ || i ~ /^virbr/ || i ~ /^tun/ || i ~ /^tap/) }
            {
                split($4, a, "/")
                if (!bad_iface($2) && a[1] !~ /^127\./ && a[1] !~ /^169\.254\./) {
                    print $2, a[1]
                    exit
                }
            }
        ')"
        if [ -n "$candidate" ]; then
            iface="${candidate%% *}"
            ipaddr="${candidate##* }"
        fi
    fi
}

for _try in $(seq 1 30); do
    detect_network
    if [ -n "$ipaddr" ]; then
        break
    fi
    sleep 2
done

[ -n "$ipaddr" ] || ipaddr="IP_EN_ATTENTE"
[ -n "$iface" ] || iface="-"
[ -n "$gateway" ] || gateway="-"
[ -n "$all_ips" ] || all_ips="-"

for env_file in "${NAS_INSTALL_DIR}/conf/flask_system.env" "${NAS_INSTALL_DIR}/system/conf/app.conf"; do
    if [ -f "$env_file" ]; then
        env_port="$(awk -F= '/^[[:space:]]*PORT[[:space:]]*=/ {gsub(/[[:space:]"\047]/, "", $2); print $2; exit}' "$env_file" 2>/dev/null || true)"
        if [ -n "${env_port:-}" ]; then
            port="$env_port"
            break
        fi
    fi
done

{
    echo "============================================================"
    echo " Yoleo Nas OS - ${host}"
    echo "============================================================"
    echo
    echo "Interface    : ${iface}"
    echo "Gateway      : ${gateway}"
    echo "IP IPv4      : ${ipaddr}"
    echo "IPv4 LAN     : ${all_ips}"
    echo "Yoleo Nas OS : http://${ipaddr}:${port}"
    echo "SSH          : ssh root@${ipaddr}"
    echo
    echo "Login sur \\l :"
    echo
} > "$ISSUE_FILE"
EOS
chmod +x /target/usr/local/sbin/nas-update-issue.sh

cat > /target/etc/systemd/system/nas-update-issue.service <<'EOS'
[Unit]
Description=Mettre a jour la banniere console NAS avec la vraie IP
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStartPre=/bin/sleep 3
ExecStart=/usr/local/sbin/nas-update-issue.sh
ExecStartPost=/bin/systemctl try-restart getty@tty1.service

[Install]
WantedBy=multi-user.target
EOS

mkdir -p /target/etc/systemd/system/multi-user.target.wants
rm -f /target/etc/systemd/system/multi-user.target.wants/nas-update-issue.service

cat > /target/etc/issue <<'EOS'
============================================================
 Yoleo Nas OS
============================================================

IP IPv4      : IP_EN_ATTENTE
Yoleo Nas OS : http://IP_EN_ATTENTE:PORT_EN_ATTENTE
SSH          : ssh root@IP_EN_ATTENTE

Login sur \l :

EOS

cat > /target/root/nas-firstboot-install.sh <<'EOS'
#!/bin/bash
set -Eeuo pipefail

LOG="/root/nas-firstboot-install.log"
exec >>"$LOG" 2>&1
NAS_INSTALL_DIR="__NAS_INSTALL_DIR__"

echo "========== NAS first boot install $(date) =========="

if [ -f /root/nas-firstboot-install.done ]; then
    echo "Installation deja terminee, sortie."
    exit 0
fi

LOCK_DIR="/run/nas-firstboot-install.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "Installation deja en cours, sortie."
    exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

if [ ! -d "$NAS_INSTALL_DIR" ]; then
    echo "ERREUR: ${NAS_INSTALL_DIR} introuvable"
    exit 1
fi

chmod +x "${NAS_INSTALL_DIR}/scripts/lan_ip.sh" "${NAS_INSTALL_DIR}/system/system.sh" 2>/dev/null || true

set_install_issue() {
    local step="${1:-Installation NAS en cours}"
    local detail="${2:-Veuillez patienter.}"
    local host ipaddr
    host="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo Yoleo)"
    ipaddr="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}' || true)"
    [ -n "$ipaddr" ] || ipaddr="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    [ -n "$ipaddr" ] || ipaddr="IP_EN_ATTENTE"

    cat > /etc/issue <<EOISSUE
============================================================
 Yoleo Nas OS - ${host}
============================================================

Installation initiale en cours...
${step}
${detail}

IP IPv4      : ${ipaddr}
Yoleo Nas OS : installation en cours, port 12345
SSH          : ssh root@${ipaddr}
Log          : /root/nas-firstboot-install.log

Veuillez patienter. La machine redemarrera automatiquement.

Login sur \l :

EOISSUE
    systemctl try-restart getty@tty1.service >/dev/null 2>&1 || true
}

configure_root_ssh() {
    mkdir -p /etc/ssh/sshd_config.d
    if [ -f /etc/ssh/sshd_config ] && ! grep -Eq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config; then
        sed -i '1i Include /etc/ssh/sshd_config.d/*.conf' /etc/ssh/sshd_config
    fi
    cat > /etc/ssh/sshd_config.d/99-yoleo-root-login.conf <<'EOC'
# Yoleo NAS OS: acces root direct, comme OMV/labo local.
PermitRootLogin yes
PasswordAuthentication yes
KbdInteractiveAuthentication yes
PubkeyAuthentication yes
EOC
}

write_flask_fallback_service() {
    local app_dir="${NAS_INSTALL_DIR}/system"
    local conf_dir="${NAS_INSTALL_DIR}/conf"
    local env_file="${conf_dir}/flask_system.env"
    local service_file="/etc/systemd/system/flask-system.service"
    local log_dir="/var/log/flask-system"
    local log_file="${log_dir}/flask_system.log"
    local access_log="${log_dir}/flask_system_access.log"

    mkdir -p "$conf_dir" "$log_dir"
    if [ ! -f "$env_file" ]; then
        cat > "$env_file" <<EOENV
# Config System Flask hote
DOCKERS_DIR=${NAS_INSTALL_DIR}
APP_DIR=${app_dir}
APP_FILE=${app_dir}/app.py
APP_MODULE=app:app
VENV_DIR=${app_dir}/.venv
REQ_FILE=${app_dir}/requirements.txt
CONF_DIR=../conf
SECRET_FILE=../conf/flask_system.secret_key
LOG_DIR=${log_dir}
LOG_FILE=${log_file}
ACCESS_LOG=${access_log}
PID_FILE=/run/flask_system.pid
HOST=0.0.0.0
PORT=12345
WORKERS=2
THREADS=4
TIMEOUT=120
EOENV
    fi

    cat > "$service_file" <<EOUNIT
[Unit]
Description=System Flask host service
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=${app_dir}
Environment=PYTHONUNBUFFERED=1
Environment=NAS_CONF_DIR=${conf_dir}
Environment=DOCKERS_DIR=${NAS_INSTALL_DIR}
ExecStart=/usr/bin/python3 -m gunicorn --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:12345 --pid /run/flask_system.pid --access-logfile ${access_log} --error-logfile - app:app
Restart=always
RestartSec=3
KillSignal=SIGTERM
TimeoutStopSec=30
StandardOutput=append:${log_file}
StandardError=append:${log_file}

[Install]
WantedBy=multi-user.target
EOUNIT

    systemctl daemon-reload
    systemctl enable flask-system.service
}

install_flask_system() {
    echo "Installation du Flask NAS via system.sh..."
    if bash "${NAS_INSTALL_DIR}/system/system.sh" -install; then
        return 0
    fi

    echo "WARN: system.sh -install a echoue avant service systemd, installation du service Flask de secours."
    write_flask_fallback_service
    systemctl restart flask-system.service || true
    sleep 5
    if systemctl is-active --quiet flask-system.service; then
        echo "OK: service Flask actif via service de secours."
        return 0
    fi

    echo "ERREUR: Flask non actif apres system.sh et service de secours."
    systemctl status flask-system.service --no-pager -l || true
    return 1
}

set_install_issue "Etape 1/4 : preparation" "Configuration console, SSH root et banniere reseau."
echo "Installation immediate de la banniere IP console..."
systemctl disable --now update-issue.service nas-update-issue.service >/dev/null 2>&1 || true

set_install_issue "Etape 2/4 : activation des services" "SSH, Docker, Samba, NFS et services systeme sont actives."
configure_root_ssh
systemctl enable --now ssh >/dev/null 2>&1 || systemctl enable --now sshd >/dev/null 2>&1 || true
systemctl enable --now smartmontools >/dev/null 2>&1 || true
systemctl enable --now sysstat >/dev/null 2>&1 || true
systemctl enable --now atop >/dev/null 2>&1 || true
systemctl enable --now rpcbind >/dev/null 2>&1 || true
systemctl enable --now nfs-server >/dev/null 2>&1 || true
systemctl enable --now nfs-kernel-server >/dev/null 2>&1 || true
systemctl enable --now smbd >/dev/null 2>&1 || true
systemctl enable --now wsdd2 >/dev/null 2>&1 || true
systemctl enable --now avahi-daemon >/dev/null 2>&1 || true
systemctl enable --now docker >/dev/null 2>&1 || true
systemctl enable --now containerd >/dev/null 2>&1 || true
systemctl enable --now libvirtd >/dev/null 2>&1 || true
systemctl enable --now virtlogd >/dev/null 2>&1 || true
systemctl disable --now hd-idle.service >/dev/null 2>&1 || true
systemctl reset-failed hd-idle.service >/dev/null 2>&1 || true

if [ -f /etc/nsswitch.conf ] && grep -q '^hosts:' /etc/nsswitch.conf; then
    if ! grep '^hosts:' /etc/nsswitch.conf | grep -q 'mdns4_minimal'; then
        sed -i 's/^hosts:.*/hosts:          files mdns4_minimal [NOTFOUND=return] dns myhostname mdns4/' /etc/nsswitch.conf
    fi
fi

set_install_issue "Etape 3/4 : installation du Flask" "Execution de system.sh -install et service web sur le port 12345."
install_flask_system
set_install_issue "Etape 4/4 : finalisation" "Rafraichissement de la banniere et preparation du redemarrage."
systemctl enable nas-update-issue.service >/dev/null 2>&1 || true
/usr/local/sbin/nas-update-issue.sh || true
systemctl try-restart getty@tty1.service >/dev/null 2>&1 || true

touch /root/nas-firstboot-install.done
systemctl disable nas-firstboot-install.service >/dev/null 2>&1 || true
rm -f /etc/cron.d/nas-firstboot-install
rm -f /etc/systemd/system/nas-firstboot-install.service
rm -f /etc/systemd/system/multi-user.target.wants/nas-firstboot-install.service
systemctl daemon-reload || true

echo "OK: installation NAS premier boot terminee $(date)"
echo "Reboot automatique pour rafraichir la banniere IP et les services."
systemctl reboot
EOS

chmod +x /target/root/nas-firstboot-install.sh

cat > /target/etc/systemd/system/nas-firstboot-install.service <<'EOS'
[Unit]
Description=Installation initiale du NAS Flask
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/bash /root/nas-firstboot-install.sh
RemainAfterExit=no
Restart=on-failure
RestartSec=60
TimeoutStartSec=0
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
EOS

chroot /target /bin/systemctl enable nas-firstboot-install.service || true

mkdir -p /target/etc/systemd/system/multi-user.target.wants
ln -sf ../nas-firstboot-install.service /target/etc/systemd/system/multi-user.target.wants/nas-firstboot-install.service
mkdir -p /target/etc/systemd/system/default.target.wants
ln -sf ../nas-firstboot-install.service /target/etc/systemd/system/default.target.wants/nas-firstboot-install.service

echo "OK: NAS copie et firstboot active"
echo "Logs futurs: /root/nas-firstboot-install.log"
EOF
sed -i "s#__NAS_INSTALL_DIR__#${NAS_INSTALL_DIR}#g" "$WORK_DIR/nas_late_command.sh"
chmod +x "$WORK_DIR/nas_late_command.sh"

xorriso -osirrox on -indev "$SRC_ISO" \
    -extract /isolinux/isolinux.cfg "$WORK_DIR/isolinux.original.cfg" \
    -extract /boot/grub/grub.cfg "$WORK_DIR/grub.original.cfg" \
    -extract /isolinux/menu.cfg "$WORK_DIR/menu.original.cfg" \
    -extract /isolinux/gtk.cfg "$WORK_DIR/gtk.original.cfg" \
    -extract /install.amd/initrd.gz "$WORK_DIR/initrd.original.gz" \
    >/dev/null 2>&1

INITRD_DIR="$WORK_DIR/initrd-tree"
mkdir -p "$INITRD_DIR"
gzip -dc "$WORK_DIR/initrd.original.gz" | (cd "$INITRD_DIR" && cpio -id --quiet)
cp "$WORK_DIR/preseed.cfg" "$INITRD_DIR/preseed.cfg"
(cd "$INITRD_DIR" && find . | cpio -o -H newc --quiet | gzip -9 > "$WORK_DIR/initrd.gz")

{
    echo "set default=0"
    echo "set timeout=5"
    echo "terminal_output console"
    echo "set gfxpayload=text"
    echo "set theme="
    echo "set color_normal=white/black"
    echo "set color_highlight=black/light-gray"
    echo
    echo "menuentry --hotkey=d 'Demarrer sur le disque dur' {"
    echo "    if [ \"\${grub_platform}\" = \"efi\" ]; then"
    echo "        exit"
    echo "    fi"
    echo "    insmod chain"
    echo "    set root=(hd0)"
    echo "    chainloader +1"
    echo "    boot"
    echo "}"
    echo
    echo "menuentry --hotkey=n 'Installer Debian NAS gen1' {"
    echo "    set background_color=black"
    echo "    linux    /install.amd/vmlinuz ${BOOT_PARAMS} ${INSTALL_VIDEO_PARAMS} ---"
    echo "    initrd   /install.amd/initrd.gz"
    echo "}"
    echo
} > "$WORK_DIR/grub.cfg"

cat > "$WORK_DIR/nas.cfg" <<EOF
label hddboot
    menu label ^Demarrer sur le disque dur
    menu default
    localboot 0x80

label nasinstall
    menu label ^Installer Debian NAS gen1
    kernel /install.amd/vmlinuz
    append ${BOOT_PARAMS} ${INSTALL_VIDEO_PARAMS} initrd=/install.amd/initrd.gz ---
EOF

{
    echo "include nas.cfg"
} > "$WORK_DIR/menu.cfg"

: > "$WORK_DIR/gtk.cfg"
awk '
    BEGIN { seen_timeout=0; seen_ontimeout=0 }
    /^[[:space:]]*(default|ui)[[:space:]]+vesamenu\.c32/ {
        sub(/vesamenu\.c32/, "menu.c32")
        print
        next
    }
    /^[[:space:]]*timeout[[:space:]]+/ {
        print "timeout 50"
        seen_timeout=1
        next
    }
    /^[[:space:]]*ontimeout[[:space:]]+/ {
        print "ontimeout hddboot"
        seen_ontimeout=1
        next
    }
    { print }
    END {
        if (!seen_timeout) print "timeout 50"
        if (!seen_ontimeout) print "ontimeout hddboot"
    }
' "$WORK_DIR/isolinux.original.cfg" > "$WORK_DIR/isolinux.cfg"

echo "Construction ISO: $OUT_ISO"
rm -f "$OUT_ISO" "$OUT_ISO.sha256"
xorriso -indev "$SRC_ISO" \
    -outdev "$OUT_ISO" \
    -rm_r /install.amd/gtk -- \
    -map "$WORK_DIR/preseed.cfg" /preseed.cfg \
    -map "$WORK_DIR/nas_late_command.sh" /nas_late_command.sh \
    -map "$WORK_DIR/grub.cfg" /boot/grub/grub.cfg \
    -map "$WORK_DIR/isolinux.cfg" /isolinux/isolinux.cfg \
    -map "$WORK_DIR/menu.cfg" /isolinux/menu.cfg \
    -map "$WORK_DIR/nas.cfg" /isolinux/nas.cfg \
    -map "$WORK_DIR/gtk.cfg" /isolinux/gtk.cfg \
    -map "$WORK_DIR/initrd.gz" /install.amd/initrd.gz \
    -map "$NAS_DIR" /nas \
    -boot_image any replay \
    -volid "DEBIAN_NAS_GEN1" \
    -padding 0 \
    -compliance no_emul_toc \
    -commit

sha256sum "$OUT_ISO" | tee "$OUT_ISO.sha256"
echo "$OUT_ISO" > "${OUT_DIR}/latest-nas-gen1.txt"

echo
echo "OK ISO creee"
ls -lh "$OUT_ISO" "$OUT_ISO.sha256"
echo "latest: ${OUT_DIR}/latest-nas-gen1.txt"

if [ "$COPY_TO_TOWER" = "1" ]; then
    echo
    echo "Copie ISO vers tower.local..."
    rsync -avh --progress -e "ssh -i /root/.ssh/tower" \
        "$OUT_ISO" \
        root@tower.local:'/mnt/user/Yoan/Soft/ISOs/Linux/'
else
    echo
    echo "COPY_TO_TOWER=0: copie ISO vers tower.local ignoree."
fi

if [ "$BUILD_DEMO_RAM" = "1" ]; then
    echo
    echo "Construction ISO demo RAM Yoleo..."
    if [ ! -x "$BUILD_DEMO_SCRIPT" ]; then
        echo "ERREUR: script demo introuvable ou non executable: $BUILD_DEMO_SCRIPT" >&2
        exit 1
    fi

    SRC_ISO="$SRC_ISO" \
    NAS_DIR="$NAS_DIR" \
    BUILD_ROOT="$BUILD_ROOT" \
    OUT_DIR="$OUT_DIR" \
    INSTALL_WORK_DIR="$WORK_DIR" \
    COPY_TO_TOWER="$COPY_DEMO_TO_TOWER" \
    bash "$BUILD_DEMO_SCRIPT"
else
    echo
    echo "BUILD_DEMO_RAM=0: construction ISO demo RAM ignoree."
fi
