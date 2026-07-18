#!/bin/bash
set -Eeuo pipefail

# ============================================================
# Build demo RAM Yoleo
#
# Cree un ISO separe avec une entree non defaut "Demo Yoleo".
# La demo boote en live RAM avec live-boot + SquashFS, sans installer.
# Le dossier applicatif garde ses chemins relatifs habituels (../conf, etc.).
# ============================================================

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
WORK_DIR="${BUILD_ROOT}/demo-current"
INSTALL_WORK_DIR="${INSTALL_WORK_DIR:-${BUILD_ROOT}/gen1-current}"
LIVE_ROOT="${BUILD_ROOT}/demo-rootfs"
OUT_ISO="${OUT_ISO:-${OUT_DIR}/debian-${DEBIAN_VERSION}-${DEBIAN_ARCH}-yoleo-demo-ram.iso}"
LATEST_FILE="${LATEST_FILE:-${OUT_DIR}/latest-yoleo-demo-ram.txt}"

DEBIAN_SUITE="${DEBIAN_SUITE:-trixie}"
DEBIAN_MIRROR="${DEBIAN_MIRROR:-http://deb.debian.org/debian}"
DEMO_HOSTNAME="${DEMO_HOSTNAME:-Yoleo}"
DEMO_INSTALL_DIR="${DEMO_INSTALL_DIR:-/yoleo}"
DEMO_INSTALL_DIR="/${DEMO_INSTALL_DIR#/}"
DEMO_INSTALL_DIR="${DEMO_INSTALL_DIR%/}"
[ -n "$DEMO_INSTALL_DIR" ] && [ "$DEMO_INSTALL_DIR" != "/" ] || {
    echo "ERREUR: DEMO_INSTALL_DIR invalide: ${DEMO_INSTALL_DIR}" >&2
    exit 1
}

REUSE_LIVE_ROOTFS="${REUSE_LIVE_ROOTFS:-0}"
AUTO_INSTALL_TOOLS="${AUTO_INSTALL_TOOLS:-1}"
COPY_TO_TOWER="${COPY_TO_TOWER:-0}"

LIVE_BASE_PACKAGES="
zstd
linux-image-amd64 live-boot live-config systemd-sysv dbus
locales console-setup keyboard-configuration kbd
ifupdown isc-dhcp-client iproute2 net-tools
openssh-server sudo cron rsync ca-certificates curl wget gnupg lsb-release
python3 python3-full python3-minimal python3-venv python3-pip python3-dev python3-apt python-is-python3
python3-flask python3-yaml python3-requests python3-psutil python3-dotenv
python3-netifaces python3-watchdog python3-docker python3-paramiko python3-gunicorn gunicorn pipx
bash-completion nano vim htop mc tmux screen tree ncdu jq yq dos2unix
"

NAS_PACKAGES="
apt-transport-https debian-archive-keyring
build-essential make gcc g++ pkg-config
iputils-ping bind9-dnsutils traceroute ethtool
pciutils usbutils lshw dmidecode lsscsi sg3-utils procps psmisc lsof
tar gzip bzip2 xz-utils zip unzip zstd pigz lz4 rclone 7zip p7zip-full
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
docker.io containerd runc docker-compose-plugin docker-buildx-plugin docker-compose
qemu-system-x86 qemu-utils
libvirt-daemon-system libvirt-clients
novnc websockify
bridge-utils ovmf swtpm swtpm-tools dnsmasq-base
"

INSTALL_BOOT_PARAMS="auto=true priority=high locale=fr_FR.UTF-8 language=fr country=FR keymap=fr-latin9 console-keymaps-at/keymap=fr-latin9 debian-installer/keymap=fr-latin9 kbd-chooser/method=fr-latin9 keyboard-configuration/xkb-keymap=fr keyboard-configuration/layout=French keyboard-configuration/layoutcode=fr pkgsel/update-policy=none unattended-upgrades/enable_auto_updates=false file=/preseed.cfg preseed/file=/preseed.cfg"
INSTALL_VIDEO_PARAMS="vga=normal fb=false debian-installer/framebuffer=false nomodeset"
LIVE_BOOT_PARAMS="boot=live components toram=filesystem.squashfs hostname=${DEMO_HOSTNAME} username=root locales=fr_FR.UTF-8 keyboard-layouts=fr utc=yes noeject"

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

install_host_tools() {
    local missing=()
    local cmd pkg

    for cmd in xorriso rsync debootstrap mksquashfs cpio gzip awk sed sha256sum; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done

    [ "${#missing[@]}" -eq 0 ] && return 0
    [ "$AUTO_INSTALL_TOOLS" = "1" ] || {
        echo "ERREUR: outils absents: ${missing[*]}" >&2
        exit 1
    }
    command -v apt-get >/dev/null 2>&1 || {
        echo "ERREUR: apt-get absent, impossible d'installer: ${missing[*]}" >&2
        exit 1
    }

    echo "Installation outils build live manquants: ${missing[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get update

    local packages=()
    for cmd in "${missing[@]}"; do
        case "$cmd" in
            mksquashfs) pkg="squashfs-tools" ;;
            *) pkg="$cmd" ;;
        esac
        packages+=("$pkg")
    done

    DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"
}

cleanup_mounts() {
    local mp
    for mp in "$LIVE_ROOT/dev/pts" "$LIVE_ROOT/dev" "$LIVE_ROOT/proc" "$LIVE_ROOT/sys" "$LIVE_ROOT/run"; do
        if mountpoint -q "$mp" 2>/dev/null; then
            umount -lf "$mp" || true
        fi
    done
}

mount_live_root() {
    mkdir -p "$LIVE_ROOT/dev" "$LIVE_ROOT/dev/pts" "$LIVE_ROOT/proc" "$LIVE_ROOT/sys" "$LIVE_ROOT/run"
    mountpoint -q "$LIVE_ROOT/dev" || mount --bind /dev "$LIVE_ROOT/dev"
    mountpoint -q "$LIVE_ROOT/dev/pts" || mount --bind /dev/pts "$LIVE_ROOT/dev/pts"
    mountpoint -q "$LIVE_ROOT/proc" || mount -t proc proc "$LIVE_ROOT/proc"
    mountpoint -q "$LIVE_ROOT/sys" || mount -t sysfs sysfs "$LIVE_ROOT/sys"
    mountpoint -q "$LIVE_ROOT/run" || mount --bind /run "$LIVE_ROOT/run"
}

write_live_package_list() {
    mkdir -p "$LIVE_ROOT/root"
    printf '%s\n%s\n' "$LIVE_BASE_PACKAGES" "$NAS_PACKAGES" \
        | tr ' ' '\n' \
        | sed '/^[[:space:]]*$/d' \
        | awk '!seen[$0]++' \
        > "$LIVE_ROOT/root/yoleo-live-packages.txt"
}

install_live_packages() {
    chroot "$LIVE_ROOT" /bin/bash <<'EOS'
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
export APT_LISTCHANGES_FRONTEND=none

record() { echo "[live-packages] $*"; }

pkg_installed() {
    dpkg -s "$1" >/dev/null 2>&1
}

pkg_available() {
    local candidate
    candidate="$(LC_ALL=C apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/ {print $2; exit}')"
    [ -n "$candidate" ] && [ "$candidate" != "(none)" ]
}

apt-get update
dpkg --configure -a || true
apt-get -f install -y || true

while read -r pkg; do
    [ -n "$pkg" ] || continue
    if pkg_installed "$pkg"; then
        record "deja installe: $pkg"
        continue
    fi
    if ! pkg_available "$pkg"; then
        record "skip indisponible: $pkg"
        continue
    fi
    record "install: $pkg"
    apt-get install --no-install-recommends -y "$pkg" || {
        record "warn echec: $pkg, tentative reparation"
        dpkg --configure -a || true
        apt-get -f install -y || true
        apt-get install --no-install-recommends -y "$pkg" || record "fail definitif: $pkg"
    }
done < /root/yoleo-live-packages.txt

dpkg --configure -a || true
apt-get -f install -y || true
apt-get clean
rm -rf /var/lib/apt/lists/*
EOS
}

write_live_demo_config() {
    mkdir -p "$LIVE_ROOT${DEMO_INSTALL_DIR}"
    rsync -a --delete "$NAS_DIR"/ "$LIVE_ROOT${DEMO_INSTALL_DIR}/"
    chmod +x "$LIVE_ROOT${DEMO_INSTALL_DIR}/system/system.sh" "$LIVE_ROOT${DEMO_INSTALL_DIR}/scripts/lan_ip.sh" 2>/dev/null || true

    cat > "$LIVE_ROOT/etc/hostname" <<EOF
${DEMO_HOSTNAME}
EOF

    cat > "$LIVE_ROOT/etc/hosts" <<EOF
127.0.0.1       localhost
127.0.1.1       ${DEMO_HOSTNAME}.local ${DEMO_HOSTNAME}
EOF

    mkdir -p "$LIVE_ROOT/etc/systemd/network"
    cat > "$LIVE_ROOT/etc/systemd/network/20-yoleo-wired.network" <<'EOF'
[Match]
Name=e* en* eth*

[Network]
DHCP=yes
IPv6AcceptRA=yes
EOF

    mkdir -p "$LIVE_ROOT/etc/ssh/sshd_config.d"
    cat > "$LIVE_ROOT/etc/ssh/sshd_config.d/99-yoleo-demo-root.conf" <<'EOF'
PermitRootLogin yes
PasswordAuthentication yes
EOF

    mkdir -p \
        "$LIVE_ROOT/etc/systemd/system/getty@tty1.service.d" \
        "$LIVE_ROOT/etc/systemd/system/serial-getty@ttyS0.service.d"
    cat > "$LIVE_ROOT/etc/systemd/system/getty@tty1.service.d/override.conf" <<'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear %I $TERM
EOF
    cat > "$LIVE_ROOT/etc/systemd/system/serial-getty@ttyS0.service.d/override.conf" <<'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear %I $TERM
EOF

    cat > "$LIVE_ROOT/usr/local/sbin/yoleo-demo-firstboot.sh" <<'EOF'
#!/bin/bash
set -Eeuo pipefail

DEMO_HOSTNAME="__DEMO_HOSTNAME__"
DEMO_INSTALL_DIR="__DEMO_INSTALL_DIR__"
LOG="/var/log/yoleo-demo-firstboot.log"
exec >>"$LOG" 2>&1

echo "========== Yoleo demo first boot $(date) =========="

hostnamectl set-hostname "$DEMO_HOSTNAME" 2>/dev/null || hostname "$DEMO_HOSTNAME" || true
echo "root:yoleo" | chpasswd || true

mkdir -p /mnt/user /mnt/user0 /mnt/cache /var/log/yoleo /var/lib/yoleo /run/yoleo

systemctl enable --now ssh >/dev/null 2>&1 || systemctl enable --now sshd >/dev/null 2>&1 || true
    systemctl enable --now systemd-networkd >/dev/null 2>&1 || true
    systemctl enable --now systemd-resolved >/dev/null 2>&1 || true
systemctl enable --now docker >/dev/null 2>&1 || true
systemctl enable --now containerd >/dev/null 2>&1 || true
systemctl enable --now avahi-daemon >/dev/null 2>&1 || true
systemctl disable --now hd-idle.service >/dev/null 2>&1 || true

if ! getent hosts deb.debian.org >/dev/null 2>&1; then
    rm -f /etc/resolv.conf
    {
        echo "nameserver 1.1.1.1"
        echo "nameserver 8.8.8.8"
    } > /etc/resolv.conf
fi

if [ -x "${DEMO_INSTALL_DIR}/system/system.sh" ]; then
    echo "Demarrage Yoleo depuis ${DEMO_INSTALL_DIR}/system"
    cd "${DEMO_INSTALL_DIR}/system"
    SKIP_APT_DEPS=1 PIP_OFFLINE=auto bash ./system.sh -install || SKIP_APT_DEPS=1 PIP_OFFLINE=auto bash ./system.sh -restart || true
else
    echo "WARN: system.sh introuvable dans ${DEMO_INSTALL_DIR}/system"
fi

ipaddr="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}' || true)"
[ -n "$ipaddr" ] || ipaddr="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
[ -n "$ipaddr" ] || ipaddr="IP_EN_ATTENTE"

echo "Etat flask-system apres firstboot:"
systemctl --no-pager --full status flask-system.service || true
ss -lntp | grep ':12345' || true

cat > /etc/issue <<EOISSUE
============================================================
 Yoleo NAS OS - Demo RAM
============================================================

Mode         : demo live, rien n'est installe sur disque
Hostname     : ${DEMO_HOSTNAME}
Interface    : http://${ipaddr}:12345
SSH          : ssh root@${ipaddr}
Mot de passe : yoleo

Login sur \l :
EOISSUE

systemctl try-restart getty@tty1.service >/dev/null 2>&1 || true
echo "OK demo first boot $(date)"
EOF
    sed -i "s#__DEMO_HOSTNAME__#${DEMO_HOSTNAME}#g; s#__DEMO_INSTALL_DIR__#${DEMO_INSTALL_DIR}#g" "$LIVE_ROOT/usr/local/sbin/yoleo-demo-firstboot.sh"
    chmod +x "$LIVE_ROOT/usr/local/sbin/yoleo-demo-firstboot.sh"

    cat > "$LIVE_ROOT/etc/systemd/system/yoleo-demo-firstboot.service" <<'EOF'
[Unit]
Description=Initialisation Yoleo demo RAM
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/yoleo-demo-firstboot.sh

[Install]
WantedBy=multi-user.target
EOF

    chroot "$LIVE_ROOT" /bin/bash -c "systemctl enable ssh yoleo-demo-firstboot.service >/dev/null 2>&1 || true"
    chroot "$LIVE_ROOT" /bin/bash -c "systemctl set-default multi-user.target >/dev/null 2>&1 || true"
    : > "$LIVE_ROOT/etc/machine-id"
}

build_live_rootfs() {
    if [ "$REUSE_LIVE_ROOTFS" = "1" ] && [ -x "$LIVE_ROOT/bin/bash" ]; then
        echo "Reuse rootfs live: $LIVE_ROOT"
    else
        echo "Creation rootfs live: $LIVE_ROOT"
        cleanup_mounts
        rm -rf "$LIVE_ROOT"
        debootstrap --arch=amd64 --variant=minbase \
            --include=ca-certificates,apt,systemd-sysv \
            "$DEBIAN_SUITE" "$LIVE_ROOT" "$DEBIAN_MIRROR"
    fi

    cat > "$LIVE_ROOT/etc/apt/sources.list" <<EOF
deb ${DEBIAN_MIRROR} ${DEBIAN_SUITE} main contrib non-free-firmware
deb http://security.debian.org/debian-security ${DEBIAN_SUITE}-security main contrib non-free-firmware
deb ${DEBIAN_MIRROR} ${DEBIAN_SUITE}-updates main contrib non-free-firmware
EOF

    cp /etc/resolv.conf "$LIVE_ROOT/etc/resolv.conf"
    mount_live_root
    write_live_package_list
    install_live_packages
    write_live_demo_config
    cleanup_mounts
}

copy_installer_artifacts() {
    need_file "$INSTALL_WORK_DIR/preseed.cfg"
    need_file "$INSTALL_WORK_DIR/nas_late_command.sh"
    need_file "$INSTALL_WORK_DIR/initrd.gz"

    cp "$INSTALL_WORK_DIR/preseed.cfg" "$WORK_DIR/preseed.cfg"
    cp "$INSTALL_WORK_DIR/nas_late_command.sh" "$WORK_DIR/nas_late_command.sh"
    cp "$INSTALL_WORK_DIR/initrd.gz" "$WORK_DIR/install-initrd.gz"
}

build_live_artifacts() {
    local kernel initrd

    mkdir -p "$WORK_DIR/live"
    kernel="$(find "$LIVE_ROOT/boot" -maxdepth 1 -type f -name 'vmlinuz-*' | sort -V | tail -n 1)"
    [ -n "$kernel" ] || { echo "ERREUR: noyau live introuvable dans $LIVE_ROOT/boot" >&2; exit 1; }
    initrd="${kernel/vmlinuz-/initrd.img-}"
    [ -f "$initrd" ] || { echo "ERREUR: initrd live introuvable: $initrd" >&2; exit 1; }

    cp "$kernel" "$WORK_DIR/live/vmlinuz"
    cp "$initrd" "$WORK_DIR/live/initrd.img"

    rm -f "$WORK_DIR/live/filesystem.squashfs"
    mksquashfs "$LIVE_ROOT" "$WORK_DIR/live/filesystem.squashfs" \
        -comp xz -b 1M -noappend \
        -e boot tmp var/tmp var/cache/apt/archives
}

write_boot_menus() {
    xorriso -osirrox on -indev "$SRC_ISO" \
        -extract /isolinux/isolinux.cfg "$WORK_DIR/isolinux.original.cfg" \
        -extract /boot/grub/grub.cfg "$WORK_DIR/grub.original.cfg" \
        >/dev/null 2>&1

cat > "$WORK_DIR/grub.cfg" <<EOF
set default=0
set timeout=5
terminal_output console
set gfxpayload=text
set theme=
set color_normal=white/black
set color_highlight=black/light-gray

menuentry --hotkey=d 'Demarrer sur le disque dur' {
    if [ "\${grub_platform}" = "efi" ]; then
        exit
    fi
    insmod chain
    set root=(hd0)
    chainloader +1
    boot
}

menuentry --hotkey=y 'Demo Yoleo - live RAM' {
    set background_color=black
    linux    /live/vmlinuz ${LIVE_BOOT_PARAMS} ---
    initrd   /live/initrd.img
}

menuentry --hotkey=n 'Installer Yoleo NAS' {
    set background_color=black
    linux    /install.amd/vmlinuz ${INSTALL_BOOT_PARAMS} ${INSTALL_VIDEO_PARAMS} ---
    initrd   /install.amd/initrd.gz
}

submenu --hotkey=o 'Options Debian utiles ...' {
    menuentry 'Expert install texte' {
        set background_color=black
        linux    /install.amd/vmlinuz priority=low ${INSTALL_VIDEO_PARAMS} ---
        initrd   /install.amd/initrd.gz
    }
    menuentry 'Rescue mode texte' {
        set background_color=black
        linux    /install.amd/vmlinuz ${INSTALL_VIDEO_PARAMS} rescue/enable=true ---
        initrd   /install.amd/initrd.gz
    }
    menuentry 'Automated install Debian' {
        set background_color=black
        linux    /install.amd/vmlinuz auto=true priority=critical ${INSTALL_VIDEO_PARAMS} ---
        initrd   /install.amd/initrd.gz
    }
}
EOF

    cat > "$WORK_DIR/nas.cfg" <<EOF
label hddboot
    menu label ^Demarrer sur le disque dur
    menu default
    localboot 0x80

label yoleodemo
    menu label ^Demo Yoleo - live RAM
    kernel /live/vmlinuz
    append initrd=/live/initrd.img ${LIVE_BOOT_PARAMS} ---

label nasinstall
    menu label ^Installer Yoleo NAS
    kernel /install.amd/vmlinuz
    append ${INSTALL_BOOT_PARAMS} ${INSTALL_VIDEO_PARAMS} initrd=/install.amd/initrd.gz ---

menu begin useful
    menu label ^Options Debian utiles
    menu title Options Debian utiles
    include stdmenu.cfg
    label mainmenu
        menu label ^Retour..
        menu exit
    label expert
        menu label Expert install texte
        kernel /install.amd/vmlinuz
        append priority=low ${INSTALL_VIDEO_PARAMS} initrd=/install.amd/initrd.gz ---
    label rescue
        menu label Rescue mode texte
        kernel /install.amd/vmlinuz
        append ${INSTALL_VIDEO_PARAMS} rescue/enable=true initrd=/install.amd/initrd.gz ---
    label auto
        menu label Automated install Debian
        kernel /install.amd/vmlinuz
        append auto=true priority=critical ${INSTALL_VIDEO_PARAMS} initrd=/install.amd/initrd.gz ---
menu end

label help
    menu label ^Help
    text help
   Display help screens; type 'menu' at boot prompt to return to this menu
    endtext
    config prompt.cfg
EOF

    cat > "$WORK_DIR/menu.cfg" <<'EOF'
menu hshift 4
menu width 70
menu title Yoleo NAS OS
include stdmenu.cfg
include nas.cfg
EOF

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
}

build_iso() {
    echo "Construction ISO demo: $OUT_ISO"
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
        -map "$WORK_DIR/install-initrd.gz" /install.amd/initrd.gz \
        -map "$WORK_DIR/live/vmlinuz" /live/vmlinuz \
        -map "$WORK_DIR/live/initrd.img" /live/initrd.img \
        -map "$WORK_DIR/live/filesystem.squashfs" /live/filesystem.squashfs \
        -map "$NAS_DIR" /nas \
        -boot_image any replay \
        -volid "YOLEO_DEMO_RAM" \
        -padding 0 \
        -compliance no_emul_toc \
        -commit

    sha256sum "$OUT_ISO" | tee "$OUT_ISO.sha256"
    echo "$OUT_ISO" > "$LATEST_FILE"

    echo
    echo "OK ISO demo cree"
    ls -lh "$OUT_ISO" "$OUT_ISO.sha256"
    echo "latest: $LATEST_FILE"

    if [ "$COPY_TO_TOWER" = "1" ]; then
        echo
        echo "Copie ISO demo vers tower.local..."
        rsync -avh --progress -e "ssh -i /root/.ssh/tower" \
            "$OUT_ISO" \
            root@tower.local:'/mnt/user/Yoan/Soft/ISOs/Linux/'
    fi
}

main() {
    ensure_src_iso
    need_dir "$NAS_DIR"
    need_dir "$INSTALL_WORK_DIR"
    install_host_tools

    cleanup_mounts
    trap cleanup_mounts EXIT

    rm -rf "$WORK_DIR"
    mkdir -p "$WORK_DIR" "$OUT_DIR"

    copy_installer_artifacts
    build_live_rootfs
    build_live_artifacts
    write_boot_menus
    build_iso
}

main "$@"
