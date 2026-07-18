def _admin_clean(text: str, limit: int = SYSTEM_ADMIN_MAX_OUTPUT) -> str:
    text = text or ""
    return text[-limit:] if len(text) > limit else text


def _admin_is_root() -> bool:
    try:
        return os.geteuid() == 0
    except Exception:
        return False


def _admin_sh(script: str, timeout: int = SYSTEM_ADMIN_TIMEOUT) -> Tuple[int, str]:
    env = os.environ.copy()
    env.setdefault("DEBIAN_FRONTEND", "noninteractive")
    try:
        p = subprocess.run(
            ["bash", "-lc", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )
        return p.returncode, _admin_clean(p.stdout or "")
    except subprocess.TimeoutExpired as exc:
        out = ""
        try:
            out = (exc.stdout or "") + (exc.stderr or "")
        except Exception:
            pass
        return 124, _admin_clean(out or "Timeout")
    except Exception as exc:
        return 1, str(exc)


def _admin_need_root() -> Tuple[bool, str]:
    if _admin_is_root():
        return True, ""
    return False, "Flask doit tourner en root pour modifier le système hôte."


def _admin_project_root() -> str:
    candidates = []
    try:
        if loaded_config:
            candidates.append(os.path.dirname(os.path.dirname(os.path.abspath(loaded_config))))
    except Exception:
        pass
    candidates.extend(_project_root_candidates())
    for c in _unique_existing_order(candidates):
        if os.path.isdir(os.path.join(c, "scripts")) or os.path.isdir(os.path.join(c, "system")):
            return c
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _admin_script_cmd(relative_path: str, *args: str, env: Optional[Dict[str, str]] = None) -> str:
    root = shlex.quote(_admin_project_root())
    script_rel = str(relative_path).strip().replace("\\", "/").lstrip("/")
    arg_text = " ".join(shlex.quote(str(arg)) for arg in args)
    env_lines = ""
    for key, value in (env or {}).items():
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key)):
            env_lines += f"export {key}={shlex.quote(str(value))}\n"
    return f'''
set -Euo pipefail
root={root}
script="$root/{script_rel}"
[ -f "$script" ] || {{ echo "Script introuvable: $script"; exit 1; }}
chmod +x "$script" 2>/dev/null || true
{env_lines}bash "$script" {arg_text}
'''


def _admin_nvidia_status_cmd(version_prefix: str) -> str:
    prefix = str(version_prefix).strip()
    return f'''
set -Euo pipefail
command -v nvidia-smi >/dev/null
version="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n1 | tr -d '[:space:]')"
[ -n "$version" ] || {{ echo "Driver NVIDIA introuvable"; exit 1; }}
echo "Driver NVIDIA actif: $version"
case "$version" in
  {shlex.quote(prefix)}.*) exit 0 ;;
  *) echo "Version attendue: {prefix}.x"; exit 1 ;;
esac
'''


def _admin_docker_host_install_cmd() -> str:
    return _admin_script_cmd(
        "scripts/system/lan_docker_host.sh",
        "install-systemd",
        env={"SCRIPT_PATH": f"{_admin_project_root().replace(os.sep, '/')}/scripts/system/lan_docker_host.sh"},
    ) + '''
systemctl start yoleo-macvlan-host.service
systemctl status yoleo-macvlan-host.service --no-pager -l | sed -n '1,18p' || true
'''


def _admin_docker_host_remove_cmd() -> str:
    return _admin_script_cmd("scripts/system/lan_docker_host.sh", "down") + '''
systemctl disable --now yoleo-macvlan-host.service 2>/dev/null || true
rm -f /etc/systemd/system/yoleo-macvlan-host.service
systemctl daemon-reload
echo "OK: Docker LAN host supprime."
'''


def _admin_status_from_rc(rc: int, detail: str = "") -> Dict[str, Any]:
    installed = rc == 0
    return {
        "installed": installed,
        "status": "installed" if installed else "missing",
        "label": "Installé" if installed else "Non installé",
        "detail": (detail or "").strip() or ("OK" if installed else "Absent"),
    }


SETUP_CONF_NAME = "setup.conf"
SETUP_CACHE_INSTALLED = "installed"
SETUP_CACHE_MISSING = "missing"


def _setup_conf_path() -> str:
    return nas_conf_file(SETUP_CONF_NAME)


def _setup_cache_load() -> Dict[str, str]:
    path = _setup_conf_path()
    cache: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle.read().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().lower()
                if re.fullmatch(r"[A-Za-z0-9_.-]+", key):
                    cache[key] = value
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return cache


def _setup_cache_save(cache: Dict[str, str]) -> None:
    path = _setup_conf_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write("# Cache des statuts de la page Système > Installation.\n")
            handle.write("# Généré automatiquement : installed / missing.\n")
            for key in sorted(cache.keys(), key=str.lower):
                value = str(cache.get(key) or SETUP_CACHE_MISSING).strip().lower()
                if value not in {SETUP_CACHE_INSTALLED, SETUP_CACHE_MISSING}:
                    value = SETUP_CACHE_MISSING
                handle.write(f"{key}={value}\n")
        os.replace(tmp, path)
    except Exception:
        pass


def _setup_cache_set(feature_id: str, installed: bool) -> None:
    clean = str(feature_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", clean):
        return
    cache = _setup_cache_load()
    cache[clean] = SETUP_CACHE_INSTALLED if installed else SETUP_CACHE_MISSING
    _setup_cache_save(cache)


def _admin_run_status(cmd: str) -> Dict[str, Any]:
    rc, out = _admin_sh(cmd, timeout=25)
    return _admin_status_from_rc(rc, out)


def _admin_feature_payload(feature: Dict[str, Any], setup_cache: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    feature_id = str(feature.get("id") or "")
    status = _admin_run_status(feature.get("status_cmd", "false"))
    cache = setup_cache if setup_cache is not None else _setup_cache_load()
    cached_value = str(cache.get(feature_id, "")).strip().lower()
    use_cache_override = bool(feature.get("cache_status"))

    if status.get("installed"):
        cache[feature_id] = SETUP_CACHE_INSTALLED
    elif use_cache_override and cached_value == SETUP_CACHE_INSTALLED:
        status = {
            **status,
            "installed": True,
            "status": "installed",
            "label": "Installé",
            "detail": "Mémorisé dans setup.conf : installé. " + str(status.get("detail") or ""),
        }
    else:
        cache[feature_id] = SETUP_CACHE_MISSING

    return {
        "id": feature.get("id"),
        "category": feature.get("category", ""),
        "title": feature.get("title", ""),
        "description": feature.get("description", ""),
        "removable": bool(feature.get("remove_cmd")),
        "danger": bool(feature.get("danger")),
        "cache_status": use_cache_override,
        **status,
    }


def _admin_status_rows() -> List[Dict[str, Any]]:
    cache = _setup_cache_load()
    rows = [_admin_feature_payload(feature, cache) for feature in _admin_features()]
    _setup_cache_save(cache)
    return rows


def _admin_run_action(feature: Dict[str, Any], action: str) -> Tuple[bool, str]:
    ok, msg = _admin_need_root()
    if not ok:
        return False, msg
    key = "install_cmd" if action in {"install", "enable", "on"} else "remove_cmd"
    cmd = feature.get(key)
    if not cmd:
        return False, "Action non disponible pour ce bloc."
    rc, out = _admin_sh(cmd, timeout=int(feature.get("timeout", SYSTEM_ADMIN_TIMEOUT)))
    success = rc == 0
    if success:
        _setup_cache_set(str(feature.get("id") or ""), action in {"install", "enable", "on"})
    return success, out or ("OK" if rc == 0 else f"Erreur rc={rc}")


BASE_INSTALL_CMD = r'''
set -Euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
  ca-certificates curl wget gnupg lsb-release apt-transport-https debian-archive-keyring \
  sudo openssh-server bash-completion locales tzdata \
  build-essential make gcc g++ pkg-config linux-headers-amd64 \
  git nano vim htop mc tmux screen tree ncdu jq yq rsync dos2unix \
  net-tools iproute2 iputils-ping bind9-dnsutils traceroute ethtool nftables \
  pciutils usbutils lshw dmidecode lsscsi sg3-utils procps psmisc lsof \
  python3 python3-minimal python3-venv python3-pip python3-dev python3-apt python-is-python3 \
  python3-flask python3-yaml python3-requests python3-psutil python3-dotenv python3-netifaces python3-watchdog python3-gunicorn pipx \
  tar gzip bzip2 xz-utils zip unzip zstd pigz lz4 rclone p7zip-full \
  util-linux mount udev parted gdisk fdisk e2fsprogs xfsprogs btrfs-progs dosfstools exfatprogs ntfs-3g \
  f2fs-tools jfsutils nilfs-tools udftools cryptsetup lvm2 mdadm dmsetup acl attr quota quotatool mergerfs fuse3 \
  smartmontools hdparm sdparm hd-idle nvme-cli powertop iotop sysstat atop blktrace fatrace inotify-tools auditd apcupsd lm-sensors \
  nfs-kernel-server nfs-common rpcbind samba samba-vfs-modules wsdd2 cifs-utils proftpd-core proftpd-mod-crypto openssh-sftp-server \
  avahi-daemon avahi-utils libnss-mdns \
  qemu-system-x86 qemu-system-gui qemu-utils libvirt-daemon-system libvirt-clients virtinst virt-manager novnc websockify bridge-utils ovmf swtpm swtpm-tools dnsmasq-base || true
# Outils stockage NAS optionnels/avancés. Ils restent dans le bloc "base" pour éviter
# de réimplémenter des contrôles d'installation dans RAID, SnapRAID, Cache, etc.
# ZFS peut dépendre des dépôts contrib/non-free-firmware selon la Debian utilisée :
# en cas d'indisponibilité, l'installation continue, mais le statut de la base NAS
# ne doit pas rester bloqué en "Non installé" uniquement à cause de ZFS.
apt-get install -y snapraid || true
apt-get install -y zfsutils-linux || true

# Docker peut déjà être présent sans le plugin Compose. Dans ce cas l'ancien test
# "if ! command -v docker" sautait l'installation de docker-compose-plugin, puis
# l'action Docker/Compose échouait avec "Docker Compose est introuvable".
apt-get install -y docker.io containerd runc || true
apt-get install -y docker-compose-plugin docker-buildx-plugin || true
if ! docker compose version >/dev/null 2>&1 && ! command -v docker-compose >/dev/null 2>&1; then
  apt-get install -y docker-compose || true
fi
mkdir -p /etc/modules-load.d /etc/sysctl.d
printf '%s\n' br_netfilter > /etc/modules-load.d/yoleo-br-netfilter.conf
cat > /etc/sysctl.d/99-yoleo-bridge-vm.conf <<'EOF_SYSCTL'
net.bridge.bridge-nf-call-iptables = 0
net.bridge.bridge-nf-call-ip6tables = 0
EOF_SYSCTL
modprobe br_netfilter 2>/dev/null || true
sysctl -w net.bridge.bridge-nf-call-iptables=0 2>/dev/null || true
sysctl -w net.bridge.bridge-nf-call-ip6tables=0 2>/dev/null || true
systemctl enable --now ssh 2>/dev/null || systemctl enable --now sshd 2>/dev/null || true
systemctl enable --now docker containerd 2>/dev/null || true
systemctl enable --now libvirtd virtlogd 2>/dev/null || true
systemctl enable --now rpcbind nfs-server nfs-kernel-server smbd wsdd2 avahi-daemon 2>/dev/null || true
systemctl enable --now smartmontools sysstat atop 2>/dev/null || true
if [ -f /etc/nsswitch.conf ] && grep -q '^hosts:' /etc/nsswitch.conf && ! grep '^hosts:' /etc/nsswitch.conf | grep -q mdns4_minimal; then
  cp -a /etc/nsswitch.conf "/etc/nsswitch.conf.bak-flask-system-$(date +%Y%m%d-%H%M%S)"
  sed -i 's/^hosts:.*/hosts:          files mdns4_minimal [NOTFOUND=return] dns myhostname mdns4/' /etc/nsswitch.conf
fi
normal_user="${SUDO_USER:-}"
[ -n "$normal_user" ] && [ "$normal_user" != root ] || normal_user="$(awk -F: '$3 >= 1000 && $3 < 65534 {print $1; exit}' /etc/passwd || true)"
if [ -n "$normal_user" ]; then
  groups=""
  for g in sudo docker libvirt kvm render video; do getent group "$g" >/dev/null && groups="${groups}${groups:+,}$g"; done
  [ -n "$groups" ] && usermod -aG "$groups" "$normal_user" || true
fi
cat > /usr/local/sbin/disk_spy.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "${1:-help}" in
  -top|top) iotop -oPa || true ;;
  -fatrace|fatrace) fatrace || true ;;
  -atop|atop) atop || true ;;
  -lsof|lsof) shift || true; lsof "${1:-/mnt}" || true ;;
  *) echo "Usage: disk_spy.sh -top|-fatrace|-atop|-lsof /mnt/disk1" ;;
esac
EOF
chmod 0755 /usr/local/sbin/disk_spy.sh
cat > /usr/local/sbin/hdd_sleep.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
ACTION="${1:-help}"; DEFAULTS=/etc/default/hdd-veille; SERVICE=/etc/systemd/system/hdd-veille.service
need_root(){ [ "$(id -u)" -eq 0 ] || { echo "ERREUR: lance en root."; exit 1; }; }
value_for_minutes(){ local m="$1"; if [ "$m" -eq 0 ]; then echo 0; elif [ "$m" -le 20 ]; then echo "$((m*12))"; else echo "$((m/30+240))"; fi; }
apply_value(){ local v="$1"; for d in /dev/sd?; do [ -b "$d" ] && hdparm -S "$v" "$d" || true; done; }
case "$ACTION" in
  -status|status) systemctl status hdd-veille.service --no-pager -l 2>/dev/null || true; for d in /dev/sd?; do [ -b "$d" ] && hdparm -C "$d" || true; done ;;
  -apply|apply) need_root; . "$DEFAULTS"; apply_value "${HDD_HDPARM_S:-0}" ;;
  -off|off) need_root; echo -e "HDD_STANDBY_MINUTES=0\nHDD_HDPARM_S=0" > "$DEFAULTS"; apply_value 0 ;;
  -remove|remove) need_root; systemctl disable --now hdd-veille.service >/dev/null 2>&1 || true; rm -f "$SERVICE" "$DEFAULTS"; systemctl daemon-reload ;;
  -[0-9]*) need_root; m="${ACTION#-}"; v="$(value_for_minutes "$m")"; echo -e "HDD_STANDBY_MINUTES=$m\nHDD_HDPARM_S=$v" > "$DEFAULTS"; cat > "$SERVICE" <<EOS
[Unit]
Description=Configure HDD standby timers
After=multi-user.target
[Service]
Type=oneshot
EnvironmentFile=/etc/default/hdd-veille
ExecStart=/usr/local/sbin/hdd_sleep.sh -apply
[Install]
WantedBy=multi-user.target
EOS
systemctl daemon-reload; systemctl enable --now hdd-veille.service; apply_value "$v" ;;
  *) echo "Usage: hdd_sleep.sh -status|-apply|-30|-60|-off|-remove" ;;
esac
EOF
chmod 0755 /usr/local/sbin/hdd_sleep.sh
echo "OK: base Debian NAS installée / contrôlée."
'''

ROOT_SSH_INSTALL_CMD = r'''
set -Euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y openssh-server
mkdir -p /etc/ssh/sshd_config.d
stamp="$(date +%Y%m%d-%H%M%S)"
[ -f /etc/ssh/sshd_config ] && cp -a /etc/ssh/sshd_config "/etc/ssh/sshd_config.bak-root-aimable-$stamp"
if [ -f /etc/ssh/sshd_config ] && ! grep -Eq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config; then
  sed -i '1i Include /etc/ssh/sshd_config.d/*.conf' /etc/ssh/sshd_config
fi
if [ -f /etc/ssh/sshd_config ]; then
  sed -i -e 's/^[[:space:]]*PermitRootLogin[[:space:]].*/# &  # disabled by Flask root SSH/' \
         -e 's/^[[:space:]]*PasswordAuthentication[[:space:]].*/# &  # disabled by Flask root SSH/' \
         -e 's/^[[:space:]]*PubkeyAuthentication[[:space:]].*/# &  # disabled by Flask root SSH/' /etc/ssh/sshd_config
fi
cat > /etc/ssh/sshd_config.d/99-root-aimable.conf <<'EOF'
# Géré par Flask System
PermitRootLogin yes
PasswordAuthentication yes
PubkeyAuthentication yes
EOF
chmod 0644 /etc/ssh/sshd_config.d/99-root-aimable.conf
sshd_bin="$(command -v sshd || echo /usr/sbin/sshd)"
[ -x "$sshd_bin" ] && "$sshd_bin" -t -f /etc/ssh/sshd_config
systemctl enable --now ssh 2>/dev/null || systemctl enable --now sshd 2>/dev/null || true
systemctl reload ssh 2>/dev/null || systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
echo "OK: root SSH activé. Si besoin, définir le mot de passe avec passwd root."
'''

ROOT_SSH_REMOVE_CMD = r'''
set -Euo pipefail
mkdir -p /etc/ssh/sshd_config.d
[ -f /etc/ssh/sshd_config.d/99-root-aimable.conf ] && cp -a /etc/ssh/sshd_config.d/99-root-aimable.conf "/etc/ssh/sshd_config.d/99-root-aimable.conf.bak-flask-system-$(date +%Y%m%d-%H%M%S)"
cat > /etc/ssh/sshd_config.d/99-root-aimable.conf <<'EOF'
# Géré par Flask System
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
EOF
chmod 0644 /etc/ssh/sshd_config.d/99-root-aimable.conf
sshd_bin="$(command -v sshd || echo /usr/sbin/sshd)"
[ -x "$sshd_bin" ] && "$sshd_bin" -t -f /etc/ssh/sshd_config
systemctl reload ssh 2>/dev/null || systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
echo "OK: root SSH direct désactivé."
'''

WEBMIN_INSTALL_CMD = r'''
set -Euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl gnupg apt-transport-https
if ! dpkg -s webmin >/dev/null 2>&1; then
  curl -fsSL https://raw.githubusercontent.com/webmin/webmin/master/webmin-setup-repo.sh -o /tmp/webmin-setup-repo.sh
  chmod 0755 /tmp/webmin-setup-repo.sh
  sh /tmp/webmin-setup-repo.sh --force
  apt-get update
  apt-get install -y --install-recommends webmin
fi
systemctl enable --now webmin
echo "OK: Webmin actif sur https://IP_DU_SERVEUR:10000"
'''

COCKPIT_INSTALL_CMD = r'''
set -Euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y qemu-system-x86 qemu-system-gui qemu-utils libvirt-daemon-system libvirt-clients virtinst virt-manager novnc websockify bridge-utils ovmf swtpm swtpm-tools dnsmasq-base cockpit cockpit-machines libvirt-dbus
systemctl enable --now cockpit.socket libvirtd virtlogd || true
normal_user="${SUDO_USER:-}"
[ -n "$normal_user" ] && [ "$normal_user" != root ] || normal_user="$(awk -F: '$3 >= 1000 && $3 < 65534 {print $1; exit}' /etc/passwd || true)"
if [ -n "$normal_user" ]; then
  groups=""
  for g in sudo docker libvirt kvm render video; do getent group "$g" >/dev/null && groups="${groups}${groups:+,}$g"; done
  [ -n "$groups" ] && usermod -aG "$groups" "$normal_user" || true
fi
echo "OK: Cockpit/KVM actif sur https://IP_DU_SERVEUR:9090"
'''

NVIDIA_TOOLS_INSTALL_CMD = r'''
set -Euo pipefail
export DEBIAN_FRONTEND=noninteractive
command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null
command -v docker >/dev/null
apt-get install -y curl gnupg ca-certificates
install -m 0755 -d /usr/share/keyrings /etc/apt/sources.list.d
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
chmod 0644 /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' > /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update
apt-get install -y nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker
echo "OK: NVIDIA Container Toolkit installé."
'''

INTEL_GPU_INSTALL_CMD = r'''
set -Euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y firmware-misc-nonfree intel-microcode intel-gpu-tools libva-utils vainfo intel-media-va-driver intel-media-va-driver-non-free i965-va-driver i965-va-driver-shaders mesa-va-drivers mesa-vulkan-drivers libvulkan1 || true
normal_user="${SUDO_USER:-}"
[ -n "$normal_user" ] && [ "$normal_user" != root ] || normal_user="$(awk -F: '$3 >= 1000 && $3 < 65534 {print $1; exit}' /etc/passwd || true)"
[ -n "$normal_user" ] && usermod -aG render,video "$normal_user" 2>/dev/null || true
cat > /usr/local/sbin/intel_docker_test.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
docker run --rm --device /dev/dri:/dev/dri --group-add render linuxserver/ffmpeg:latest -hide_banner -hwaccels || true
EOF
chmod 0755 /usr/local/sbin/intel_docker_test.sh
ls -l /dev/dri 2>/dev/null || true
vainfo 2>/dev/null | sed -n '1,25p' || true
echo "OK: outils Intel GPU installés."
'''

SCREEN_SLEEP_INSTALL_CMD = r'''
set -Euo pipefail
cat > /etc/default/console-screen-sleep <<'EOF'
BLANK_MINUTES=1
POWERDOWN_MINUTES=1
TTY_LIST="1 2 3 4 5 6"
EOF
cat > /usr/local/sbin/console-screen-sleep.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
CONF_FILE=/etc/default/console-screen-sleep
BLANK_MINUTES=1; POWERDOWN_MINUTES=1; TTY_LIST="1 2 3 4 5 6"
[ -f "$CONF_FILE" ] && . "$CONF_FILE"
for n in $TTY_LIST; do
  tty="/dev/tty${n}"; [ -e "$tty" ] || continue
  TERM=linux setterm --blank "$BLANK_MINUTES" --powerdown "$POWERDOWN_MINUTES" < "$tty" > "$tty" 2>/dev/null || true
  TERM=linux setterm --powersave powerdown < "$tty" > "$tty" 2>/dev/null || true
done
[ -w /sys/module/kernel/parameters/consoleblank ] && echo "$((BLANK_MINUTES * 60))" > /sys/module/kernel/parameters/consoleblank 2>/dev/null || true
EOF
chmod 0755 /usr/local/sbin/console-screen-sleep.sh
cat > /etc/systemd/system/console-screen-sleep.service <<'EOF'
[Unit]
Description=Mettre en veille l'ecran console apres inactivite
After=multi-user.target getty.target
Wants=getty.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/console-screen-sleep.sh
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now console-screen-sleep.service
echo "OK: veille écran console installée."
'''

ROOT_COLOR_INSTALL_CMD = r'''
set -Euo pipefail
[ -f /root/.bashrc ] && cp -a /root/.bashrc "/root/.bashrc.bak-flask-system-$(date +%Y%m%d-%H%M%S)" || true
cat > /root/.bashrc <<'EOF'
# Alias menu principal
alias menu='bash /dockers/scripts/menu.sh'

# Alias de base
alias ls='ls --color=auto'
alias ll='ls -lah --color=auto'

# Prompt cyan
PS1='\[\e[1;36m\]\u@\h:\w\$ \[\e[0m\]'

# Couleurs fichiers
export LS_COLORS=$LS_COLORS:'*.sh=01;32:*.yml=01;35:*.yaml=01;35:*.conf=01;33:*.log=00;31:'
EOF
echo '[[ -f ~/.bashrc ]] && . ~/.bashrc' > /root/.bash_profile
echo "OK: couleur session root installée."
'''

ISSUE_INSTALL_CMD = r'''
set -Euo pipefail
[ -f /etc/issue ] && cp -a /etc/issue "/etc/issue.bak-flask-system-$(date +%Y%m%d-%H%M%S)" || true
cat > /usr/local/sbin/update-issue.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
ISSUE_FILE=/etc/issue
host="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo debian)"
kernel="$(uname -r 2>/dev/null || echo '-')"
date_now="$(date '+%Y-%m-%d %H:%M:%S')"
default_line="$(ip route show default 2>/dev/null | head -n 1 || true)"
default_iface="$(echo "$default_line" | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')"
default_gateway="$(echo "$default_line" | awk '{for(i=1;i<=NF;i++) if($i=="via"){print $(i+1); exit}}')"
[ -n "$default_iface" ] || default_iface="-"; [ -n "$default_gateway" ] || default_gateway="-"
tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
ip -o -4 addr show scope global 2>/dev/null | while read -r n iface fam cidr rest; do
  iface="${iface%%@*}"; ipaddr="${cidr%%/*}"
  case "$iface" in lo|docker*|veth*|virbr*|vnet*|tap*|tun*|br-*) continue ;; esac
  state="$(cat "/sys/class/net/$iface/operstate" 2>/dev/null || echo '-')"
  speed="$(cat "/sys/class/net/$iface/speed" 2>/dev/null || echo '-')"; [ "$speed" = "-1" ] && speed="-"
  marker=" "; [ "$iface" = "$default_iface" ] && marker="*"
  printf "  %s %-12s %-15s état=%-7s lien=%s Mb/s\n" "$marker" "$iface" "$ipaddr" "$state" "$speed" >> "$tmp"
done
[ -s "$tmp" ] || echo "  Aucune IP IPv4 globale détectée." > "$tmp"
main_ip="$(awk '$1=="*" {print $3; exit}' "$tmp" 2>/dev/null || true)"; [ -n "$main_ip" ] || main_ip="$(awk '{print $3; exit}' "$tmp" 2>/dev/null || true)"; [ -n "$main_ip" ] || main_ip=IP_DU_SERVEUR
{
 echo "============================================================"
 echo " Debian NAS - ${host}"
 echo " Kernel : ${kernel}"
 echo " Date   : ${date_now}"
 echo "============================================================"
 echo; echo "Interface par défaut : ${default_iface}"; echo "Gateway             : ${default_gateway}"; echo
 echo "Interfaces réseau :"; cat "$tmp"; echo
 echo "Accès utiles :"; echo "  Flask System : http://${main_ip}:5000"; echo "  SSH          : ssh root@${main_ip}"; echo
 echo "Login sur \\l :"; echo
} > "$ISSUE_FILE"
EOF
chmod 0755 /usr/local/sbin/update-issue.sh
cat > /etc/systemd/system/update-issue.service <<'EOF'
[Unit]
Description=Mettre a jour la banniere console /etc/issue avec les IP
Wants=network-online.target
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/update-issue.sh
ExecStartPost=/bin/systemctl try-restart getty@tty1.service
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now update-issue.service
echo "OK: bannière IP installée."
'''

LINUX_BR0_INSTALL_CMD = r'''
set -Euo pipefail
export DEBIAN_FRONTEND=noninteractive
mkdir -p /etc/modules-load.d /etc/sysctl.d
printf '%s\n' br_netfilter > /etc/modules-load.d/yoleo-br-netfilter.conf
cat > /etc/sysctl.d/99-yoleo-bridge-vm.conf <<'EOF_SYSCTL'
net.bridge.bridge-nf-call-iptables = 0
net.bridge.bridge-nf-call-ip6tables = 0
EOF_SYSCTL
modprobe br_netfilter 2>/dev/null || true
sysctl -w net.bridge.bridge-nf-call-iptables=0 2>/dev/null || true
sysctl -w net.bridge.bridge-nf-call-ip6tables=0 2>/dev/null || true
apt-get update
apt-get install -y ifupdown bridge-utils iproute2
mkdir -p /root/.BR0/backups
ts="$(date +%Y%m%d-%H%M%S)"; backup="/root/.BR0/backups/$ts"; mkdir -p "$backup"
[ -f /etc/network/interfaces ] && cp -a /etc/network/interfaces "$backup/interfaces" || true
[ -d /etc/network/interfaces.d ] && cp -a /etc/network/interfaces.d "$backup/interfaces.d" || true
[ -f /etc/resolv.conf ] && cp -a /etc/resolv.conf "$backup/resolv.conf" || true
dev="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')"
[ -n "$dev" ] || { echo "ERREUR: route défaut introuvable"; exit 1; }
if [ -d "/sys/class/net/$dev/brif" ]; then phy="$(find "/sys/class/net/$dev/brif" -mindepth 1 -maxdepth 1 | head -n1 | xargs -r basename)"; else phy="$dev"; fi
[ -n "$phy" ] && [ -d "/sys/class/net/$phy" ] || { echo "ERREUR: carte physique introuvable"; exit 1; }
ip_cidr="$(ip -4 addr show dev "$dev" scope global 2>/dev/null | awk '/inet / {print $2; exit}')"
[ -n "$ip_cidr" ] || ip_cidr="$(ip -4 addr show dev "$phy" scope global 2>/dev/null | awk '/inet / {print $2; exit}')"
[ -n "$ip_cidr" ] || { echo "ERREUR: IP/CIDR introuvable"; exit 1; }
gw="$(ip -4 route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="via"){print $(i+1); exit}}' | head -n1)"; [ -n "$gw" ] || gw=192.168.1.254
mac="$(cat "/sys/class/net/$phy/address")"
dns="$(awk '/^nameserver / {print $2}' /etc/resolv.conf 2>/dev/null | xargs || true)"; [ -n "$dns" ] || dns="$gw 1.1.1.1"
cat > /etc/network/interfaces <<EOF
auto lo
iface lo inet loopback

auto $phy
iface $phy inet manual

auto br0
iface br0 inet static
    address $ip_cidr
    gateway $gw
    bridge_ports $phy
    bridge_stp off
    bridge_fd 0
    bridge_maxwait 0
    hwaddress ether $mac
    dns-nameservers $dns
EOF
cat > /root/.BR0/state.conf <<EOF
DEV=$dev
PHY=$phy
MAC=$mac
IP_CIDR=$ip_cidr
GW=$gw
DNS='$dns'
BACKUP_DIR=$backup
EOF
echo "OK: configuration br0 écrite. Redémarrage serveur/réseau nécessaire. Backup: $backup"
'''

DOCKER_BR0_INSTALL_CMD = r'''
set -Euo pipefail
command -v docker >/dev/null || { echo "Docker absent"; exit 1; }
mkdir -p /etc/modules-load.d /etc/sysctl.d
printf '%s\n' br_netfilter > /etc/modules-load.d/yoleo-br-netfilter.conf
cat > /etc/sysctl.d/99-yoleo-bridge-vm.conf <<'EOF_SYSCTL'
net.bridge.bridge-nf-call-iptables = 0
net.bridge.bridge-nf-call-ip6tables = 0
EOF_SYSCTL
modprobe br_netfilter 2>/dev/null || true
sysctl -w net.bridge.bridge-nf-call-iptables=0 2>/dev/null || true
sysctl -w net.bridge.bridge-nf-call-ip6tables=0 2>/dev/null || true
if docker network inspect br0 >/dev/null 2>&1; then docker network inspect br0; exit 0; fi
iface="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')"
[ -n "$iface" ] || { echo "Interface introuvable"; exit 1; }
cidr="$(ip -4 -o addr show dev "$iface" scope global 2>/dev/null | awk '{print $4; exit}')"
[ -n "$cidr" ] || { echo "CIDR introuvable sur $iface"; exit 1; }
subnet="$(python3 - <<PY
import ipaddress
print(ipaddress.ip_interface('$cidr').network)
PY
)"
gw="$(ip -4 route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="via"){print $(i+1); exit}}' | head -n1)"
[ -n "$gw" ] || { echo "Gateway introuvable"; exit 1; }
docker network create -d ipvlan --subnet="$subnet" --gateway="$gw" -o parent="$iface" -o ipvlan_mode=l2 br0
docker network inspect br0
echo "OK: réseau Docker br0 ipvlan créé."
'''

LIBVIRT_BR0_INSTALL_CMD = r'''
set -Euo pipefail
command -v virsh >/dev/null || apt-get install -y libvirt-daemon-system libvirt-clients bridge-utils
mkdir -p /etc/modules-load.d /etc/sysctl.d
printf '%s\n' br_netfilter > /etc/modules-load.d/yoleo-br-netfilter.conf
cat > /etc/sysctl.d/99-yoleo-bridge-vm.conf <<'EOF_SYSCTL'
net.bridge.bridge-nf-call-iptables = 0
net.bridge.bridge-nf-call-ip6tables = 0
EOF_SYSCTL
modprobe br_netfilter 2>/dev/null || true
sysctl -w net.bridge.bridge-nf-call-iptables=0 2>/dev/null || true
sysctl -w net.bridge.bridge-nf-call-ip6tables=0 2>/dev/null || true
[ -d /sys/class/net/br0 ] || { echo "Le bridge Linux br0 n'existe pas encore."; exit 1; }
systemctl enable --now libvirtd virtlogd 2>/dev/null || true
cat > /tmp/flask-system-libvirt-br0.xml <<'EOF'
<network>
  <name>BR0</name>
  <forward mode='bridge'/>
  <bridge name='br0'/>
</network>
EOF
virsh -c qemu:///system net-info BR0 >/dev/null 2>&1 || virsh -c qemu:///system net-define /tmp/flask-system-libvirt-br0.xml
virsh -c qemu:///system net-autostart BR0 || true
virsh -c qemu:///system net-start BR0 || true
virsh -c qemu:///system net-info BR0
echo "OK: réseau libvirt BR0 installé."
'''


def _admin_nvidia_driver_cmd(version: str) -> str:
    return rf'''
set -Euo pipefail
root="{shlex.quote(_admin_project_root()).strip(chr(39))}"
runfile=""
installer=""
for d in "$root/scripts/nvidia" "$(dirname "{__file__}")/../scripts/nvidia" /dockers/scripts/nvidia; do
  [ -z "$installer" ] && [ -x "$d/install.sh" ] && installer="$d/install.sh"
  [ -z "$runfile" ] && runfile="$(ls "$d"/NVIDIA-Linux-x86_64-{version}*.run 2>/dev/null | head -n1 || true)"
done
[ -n "$runfile" ] || {{ echo "Fichier NVIDIA {version}.run introuvable dans scripts/nvidia."; exit 1; }}
chmod +x "$runfile" "$installer" 2>/dev/null || true
if [ -n "$installer" ]; then bash "$installer" --file "$runfile"; else "$runfile" --dkms --silent; fi
nvidia-smi || true
'''


def _admin_nvidia_script_dirs_expr() -> str:
    root = shlex.quote(_admin_project_root())
    module_scripts = shlex.quote(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts", "nvidia")))
    return f'{root}/scripts/nvidia {module_scripts} /yoleo/scripts/nvidia /dockers/scripts/nvidia'


def _admin_nvidia_secureboot_reinstall_cmd() -> str:
    dirs = _admin_nvidia_script_dirs_expr()
    return f'''
set -Euo pipefail
script=""
for d in {dirs}; do
  [ -d "$d" ] || continue
  for f in "$d"/install_nvidia_secureboot_reinstall.sh "$d"/install_nvidia_secureboot_reinstall*.sh; do
    [ -f "$f" ] || continue
    script="$f"
    break 2
  done
done
[ -n "$script" ] || {{ echo "Script install_nvidia_secureboot_reinstall.sh introuvable dans scripts/nvidia."; exit 1; }}
chmod +x "$script" 2>/dev/null || true
echo "Script Secure Boot: $script"
bash "$script"
'''


def _admin_nvidia_secureboot_status_cmd() -> str:
    dirs = _admin_nvidia_script_dirs_expr()
    return f'''
set -Euo pipefail
script=""
for d in {dirs}; do
  [ -d "$d" ] || continue
  for f in "$d"/install_nvidia_secureboot_reinstall.sh "$d"/install_nvidia_secureboot_reinstall*.sh; do
    [ -f "$f" ] || continue
    script="$f"
    break 2
  done
done
[ -n "$script" ] || {{ echo "Script Secure Boot introuvable."; exit 1; }}
echo "Script Secure Boot: $script"
command -v nvidia-smi >/dev/null || {{ echo "nvidia-smi absent"; exit 1; }}
driver="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n1 | tr -d '[:space:]' || true)"
[ -n "$driver" ] || {{ echo "Driver NVIDIA actif introuvable"; exit 1; }}
echo "Driver NVIDIA actif: $driver"
signer="$(modinfo -F signer nvidia 2>/dev/null | head -n1 || true)"
sig_key="$(modinfo -F sig_key nvidia 2>/dev/null | head -n1 || true)"
if [ -n "$signer" ] || [ -n "$sig_key" ]; then
  [ -n "$signer" ] && echo "Module signé par: $signer"
  [ -n "$sig_key" ] && echo "Cle module: $sig_key"
  exit 0
fi
echo "Module NVIDIA actif, mais signature module non visible par modinfo."
exit 1
'''


def _admin_nvidia_pm1_install_cmd() -> str:
    dirs = _admin_nvidia_script_dirs_expr()
    return f'''
set -Euo pipefail
script=""
for d in {dirs}; do
  [ -d "$d" ] || continue
  for f in "$d"/nvidia_pm1.sh "$d"/1.nvidia_pm1.sh "$d"/*nvidia_pm1*.sh; do
    [ -f "$f" ] || continue
    script="$f"
    break 2
  done
done
[ -n "$script" ] || {{ echo "Script nvidia_pm1.sh introuvable dans scripts/nvidia."; exit 1; }}
chmod +x "$script" 2>/dev/null || true
echo "Script PM1: $script"
bash "$script" -install
'''


def _admin_nvidia_pm1_status_cmd() -> str:
    dirs = _admin_nvidia_script_dirs_expr()
    return f'''
set -Euo pipefail
source_script=""
for d in {dirs}; do
  [ -d "$d" ] || continue
  for f in "$d"/nvidia_pm1.sh "$d"/1.nvidia_pm1.sh "$d"/*nvidia_pm1*.sh; do
    [ -f "$f" ] || continue
    source_script="$f"
    break 2
  done
done
[ -n "$source_script" ] && echo "Source PM1: $source_script" || true
[ -x /usr/local/sbin/nvidia_pm1.sh ] || {{ echo "Script installé absent: /usr/local/sbin/nvidia_pm1.sh"; exit 1; }}
[ -f /etc/libvirt/hooks/qemu ] || {{ echo "Hook libvirt qemu absent"; exit 1; }}
grep -Fq "NVIDIA_PM1_HOOK_MANAGED_BY_NVIDIA_PM1_SH" /etc/libvirt/hooks/qemu || {{ echo "Bloc hook PM1 absent"; exit 1; }}
echo "PM1 installé: /usr/local/sbin/nvidia_pm1.sh + hook libvirt"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=index,name,persistence_mode,pstate --format=csv,noheader 2>/dev/null || true
fi
'''


def _admin_nvidia_pm1_remove_cmd() -> str:
    dirs = _admin_nvidia_script_dirs_expr()
    return f'''
set -Euo pipefail
if [ -x /usr/local/sbin/nvidia_pm1.sh ]; then
  /usr/local/sbin/nvidia_pm1.sh -remove
  exit 0
fi
script=""
for d in {dirs}; do
  [ -d "$d" ] || continue
  for f in "$d"/nvidia_pm1.sh "$d"/1.nvidia_pm1.sh "$d"/*nvidia_pm1*.sh; do
    [ -f "$f" ] || continue
    script="$f"
    break 2
  done
done
[ -n "$script" ] || {{ echo "Aucun script PM1 source ou installé à retirer."; exit 0; }}
chmod +x "$script" 2>/dev/null || true
bash "$script" -remove
'''


def _admin_features() -> List[Dict[str, Any]]:
    root = shlex.quote(_admin_project_root())
    return [
        {"id": "debian_base", "category": "Debian", "title": "Installer la base Debian NAS", "description": "Outils système, Python/Flask, stockage, rsync, mdadm, BTRFS, ZFS optionnel, SnapRAID, Samba/NFS/ProFTPD, Avahi, Docker + Compose, KVM/libvirt/noVNC.", "status_cmd": "dpkg -s openssh-server python3-flask samba nfs-kernel-server proftpd-core proftpd-mod-crypto avahi-daemon libvirt-daemon-system rsync mdadm btrfs-progs xfsprogs mergerfs snapraid >/dev/null 2>&1 && (command -v docker >/dev/null || dpkg -s docker.io >/dev/null 2>&1) && (docker compose version >/dev/null 2>&1 || docker-compose version >/dev/null 2>&1)", "install_cmd": BASE_INSTALL_CMD, "remove_cmd": "", "timeout": 3600},
        {"id": "webmin", "category": "Debian", "title": "Installer WebAdmin / Webmin", "description": "Webmin HTTPS port 10000.", "status_cmd": "dpkg -s webmin >/dev/null 2>&1 && systemctl is-active webmin --no-pager", "install_cmd": WEBMIN_INSTALL_CMD, "remove_cmd": "systemctl disable --now webmin 2>/dev/null || true; apt-get purge -y webmin; apt-get autoremove -y", "timeout": 1800},
        {"id": "cockpit", "category": "Debian", "title": "Installer Cockpit + KVM", "description": "Cockpit port 9090 + cockpit-machines + libvirt.", "status_cmd": "dpkg -s cockpit cockpit-machines >/dev/null 2>&1 && systemctl is-active cockpit.socket --no-pager", "install_cmd": COCKPIT_INSTALL_CMD, "remove_cmd": "systemctl disable --now cockpit.socket 2>/dev/null || true; apt-get purge -y cockpit-machines cockpit-packagekit cockpit-storaged cockpit-networkmanager cockpit-system cockpit-ws cockpit-bridge cockpit; apt-get autoremove -y", "timeout": 2400},
        {"id": "nvidia_580", "category": "GPU", "title": "Installer driver NVIDIA 580", "description": "Utilise le .run local dans scripts/nvidia s'il existe.", "status_cmd": _admin_nvidia_status_cmd("580"), "install_cmd": _admin_nvidia_driver_cmd("580"), "remove_cmd": "", "timeout": 3600, "danger": True},
        {"id": "nvidia_595", "category": "GPU", "title": "Installer driver NVIDIA 595", "description": "Utilise le .run local dans scripts/nvidia s'il existe.", "status_cmd": _admin_nvidia_status_cmd("595"), "install_cmd": _admin_nvidia_driver_cmd("595"), "remove_cmd": "", "timeout": 3600, "danger": True},
        {"id": "nvidia_secureboot_reinstall", "category": "GPU", "title": "Réinstaller NVIDIA Secure Boot", "description": "Réutilise l'ancienne clé MOK locale déjà enrôlée et signe le module NVIDIA.", "status_cmd": _admin_nvidia_secureboot_status_cmd(), "install_cmd": _admin_nvidia_secureboot_reinstall_cmd(), "remove_cmd": "", "timeout": 5400, "danger": True},
        {"id": "nvidia_pm1_hook", "category": "GPU", "title": "Installer script NVIDIA PM1", "description": "Installe nvidia_pm1.sh dans /usr/local/sbin et ajoute le hook libvirt PM1.", "status_cmd": _admin_nvidia_pm1_status_cmd(), "install_cmd": _admin_nvidia_pm1_install_cmd(), "remove_cmd": _admin_nvidia_pm1_remove_cmd(), "timeout": 900, "danger": True},
        {"id": "nvidia_docker", "category": "GPU", "title": "Installer outils NVIDIA Docker", "description": "nvidia-container-toolkit + nvidia-ctk runtime Docker.", "status_cmd": "dpkg -s nvidia-container-toolkit >/dev/null 2>&1 || command -v nvidia-ctk >/dev/null", "install_cmd": NVIDIA_TOOLS_INSTALL_CMD, "remove_cmd": "apt-get purge -y nvidia-container-toolkit nvidia-container-toolkit-base libnvidia-container-tools libnvidia-container1; rm -f /etc/apt/sources.list.d/nvidia-container-toolkit.list /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg; systemctl restart docker 2>/dev/null || true", "timeout": 1800},
        {"id": "intel_gpu", "category": "GPU", "title": "Installer pilotes/outils Intel GPU", "description": "Script Intel UHD 770 / i7-13700 : firmware Intel, VAAPI, intel-gpu-tools, /dev/dri.", "status_cmd": "dpkg -s intel-gpu-tools intel-media-va-driver-non-free firmware-intel-graphics >/dev/null 2>&1 && [ -e /dev/dri/renderD128 ] && echo 'Intel GPU OK: /dev/dri/renderD128'", "install_cmd": _admin_script_cmd("scripts/system/intel_gpu.sh"), "remove_cmd": "apt-get purge -y intel-gpu-tools libva-utils vainfo intel-media-va-driver-non-free firmware-intel-graphics; apt-get autoremove -y", "timeout": 2400},
        {"id": "root_ssh", "category": "Accès", "title": "Autoriser root SSH", "description": "Drop-in sshd_config.d : PermitRootLogin yes + PasswordAuthentication yes.", "status_cmd": "test -f /etc/ssh/sshd_config.d/99-root-aimable.conf && grep -Eiq '^PermitRootLogin[[:space:]]+yes' /etc/ssh/sshd_config.d/99-root-aimable.conf && systemctl is-active ssh --no-pager", "install_cmd": ROOT_SSH_INSTALL_CMD, "remove_cmd": ROOT_SSH_REMOVE_CMD, "timeout": 900, "danger": True},
        {"id": "screen_sleep", "category": "Console", "title": "Veille écran console", "description": "setterm tty1-tty6 via console-screen-sleep.service.", "status_cmd": "test -x /usr/local/sbin/console-screen-sleep.sh && test -f /etc/systemd/system/console-screen-sleep.service", "install_cmd": SCREEN_SLEEP_INSTALL_CMD, "remove_cmd": "systemctl disable --now console-screen-sleep.service 2>/dev/null || true; rm -f /etc/systemd/system/console-screen-sleep.service /usr/local/sbin/console-screen-sleep.sh; systemctl daemon-reload", "timeout": 600},
        {"id": "root_color", "category": "Console", "title": "Couleur session root", "description": "Prompt cyan, alias menu, couleurs fichiers dans /root/.bashrc.", "status_cmd": "grep -q \"alias menu='bash /dockers/scripts/menu.sh'\" /root/.bashrc && grep -q '1;36m' /root/.bashrc", "install_cmd": ROOT_COLOR_INSTALL_CMD, "remove_cmd": "cp -a /root/.bashrc /root/.bashrc.bak-flask-system-$(date +%Y%m%d-%H%M%S) 2>/dev/null || true; printf \"alias ls='ls --color=auto'\\nalias ll='ls -lah --color=auto'\\n\" > /root/.bashrc", "timeout": 300},
        {"id": "chmod_scripts", "category": "Maintenance", "title": "Rendre scripts exécutables", "description": "chmod +x sur scripts, bin et system autour du projet.", "status_cmd": f"for d in {root}/scripts {root}/bin {root}/system; do [ -d \"$d\" ] && find \"$d\" -type f \\( -name '*.sh' -o -name '*.py' \\) ! -perm -111 -print -quit; done | grep -q . && exit 1 || exit 0", "install_cmd": f"for d in {root}/scripts {root}/bin {root}/system; do [ -d \"$d\" ] && chmod -R +x \"$d\" && echo OK chmod +x \"$d\" || echo SKIP \"$d\"; done", "remove_cmd": "", "timeout": 600, "cache_status": True},
        {"id": "issue_ip", "category": "Console", "title": "Bannière IP avant login", "description": "Génère /etc/issue avec IP, gateway et accès utiles.", "status_cmd": "test -x /usr/local/sbin/update-issue.sh && test -f /etc/systemd/system/update-issue.service", "install_cmd": ISSUE_INSTALL_CMD, "remove_cmd": "systemctl disable --now update-issue.service 2>/dev/null || true; rm -f /etc/systemd/system/update-issue.service /usr/local/sbin/update-issue.sh; systemctl daemon-reload", "timeout": 600},
        {"id": "linux_br0", "category": "Réseau", "title": "Installer bridge Linux br0", "description": "Utilise scripts/system/lan_bro.sh : carte physique en port, br0 avec IP/MAC, sauvegarde et reboot.", "status_cmd": "ip link show br0 && ip -br -4 addr show br0 || test -f /root/.BR0/state.conf", "install_cmd": _admin_script_cmd("scripts/system/lan_bro.sh", "-install"), "remove_cmd": _admin_script_cmd("scripts/system/lan_bro.sh", "-remove"), "timeout": 900, "danger": True},
        {"id": "docker_br0", "category": "Réseau", "title": "Installer Docker br0 ipvlan", "description": "Utilise scripts/system/lan_docker.sh : cree le reseau Docker br0 en ipvlan L2.", "status_cmd": "docker network inspect br0 --format 'Nom={{.Name}} Driver={{.Driver}} Parent={{index .Options \"parent\"}} Mode={{index .Options \"ipvlan_mode\"}}'", "install_cmd": _admin_script_cmd("scripts/system/lan_docker.sh", "install"), "remove_cmd": _admin_script_cmd("scripts/system/lan_docker.sh", "remove"), "timeout": 600, "danger": True},
        {"id": "docker_lan_host", "category": "Réseau", "title": "Installer Docker LAN host", "description": "Ajoute la patte hote Docker macvlan/host pour communiquer hote <-> conteneurs LAN.", "status_cmd": "systemctl is-enabled yoleo-macvlan-host.service --quiet && systemctl is-active yoleo-macvlan-host.service --quiet && ip -br addr show mv-host", "install_cmd": _admin_docker_host_install_cmd(), "remove_cmd": _admin_docker_host_remove_cmd(), "timeout": 600, "danger": True},
        {"id": "libvirt_br0", "category": "Réseau", "title": "Installer VM en BR0", "description": "Crée le réseau libvirt BR0 branché sur le bridge Linux br0.", "status_cmd": "virsh -c qemu:///system net-info BR0", "install_cmd": LIBVIRT_BR0_INSTALL_CMD, "remove_cmd": "virsh -c qemu:///system net-destroy BR0 2>/dev/null || true; virsh -c qemu:///system net-undefine BR0", "timeout": 600, "danger": True},
        {"id": "tv_hauppauge_dualhd", "category": "TV", "title": "Installer carte TV Hauppauge dualHD", "description": "Script long valide : firmwares locaux + paquets DVB/V4L + udev + modules em28xx/si2168/si2157.", "status_cmd": "test -f /lib/firmware/dvb-demod-si2168-d60-01.fw && test -f /lib/firmware/dvb_driver_si2157_rom50.fw && test -f /etc/udev/rules.d/99-yoleo-hauppauge-dualhd.rules", "install_cmd": _admin_script_cmd("scripts/tv/yoleo_install_hauppauge_dualhd.sh"), "remove_cmd": "rm -f /etc/udev/rules.d/99-yoleo-hauppauge-dualhd.rules; udevadm control --reload-rules 2>/dev/null || true; echo 'Firmwares laisses en place dans /lib/firmware.'", "timeout": 1800},
    ]


def _admin_feature_by_id(feature_id: str) -> Optional[Dict[str, Any]]:
    for feature in _admin_features():
        if feature.get("id") == feature_id:
            return feature
    return None


# --------------------------------------------------
# JOBS SYSTEME : exécution longue + log lisible côté UI
# --------------------------------------------------
SYSTEM_JOB_MAX_OUTPUT = get_conf_int("SYSTEM_JOB_MAX_OUTPUT_CHARS", 120000)
_SYSTEM_JOBS: Dict[str, Dict[str, Any]] = {}
_SYSTEM_LATEST_JOB: Dict[str, str] = {}


def _system_job_dir() -> str:
    path = "/var/log/flask-system/jobs"
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except Exception:
        path = os.path.join("/tmp", "flask-system-jobs")
        os.makedirs(path, exist_ok=True)
        return path


def _system_job_kind(kind: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(kind or "job")).strip("_") or "job"


def _system_job_read_log(path: str, limit: int = SYSTEM_JOB_MAX_OUTPUT) -> str:
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size > limit:
                handle.seek(max(0, size - limit))
                data = handle.read()
                return "… sortie tronquée, fin du log affichée …\n" + data.decode("utf-8", "replace")
            handle.seek(0)
            return handle.read().decode("utf-8", "replace")
    except Exception as exc:
        return f"Erreur lecture log : {exc}"


def _system_job_start(kind: str, label: str, script: str, timeout: int = SYSTEM_ADMIN_TIMEOUT, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    ok, msg = _admin_need_root()
    if not ok:
        return {"ok": False, "message": msg, "output": msg}

    kind = _system_job_kind(kind)
    job_id = f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    log_path = os.path.join(_system_job_dir(), f"{job_id}.log")
    latest_path = os.path.join(_system_job_dir(), f"{kind}-latest.log")
    timeout = max(30, int(timeout or SYSTEM_ADMIN_TIMEOUT))
    started_at = now_label()

    wrapped = f"""
set +e
export DEBIAN_FRONTEND=noninteractive
echo "[{started_at}] JOB {job_id}"
echo "[{started_at}] {label}"
echo "------------------------------------------------------------"
if command -v timeout >/dev/null 2>&1; then
    timeout --foreground {timeout}s bash -lc {shlex.quote(script)}
else
    bash -lc {shlex.quote(script)}
fi
rc=$?
echo "------------------------------------------------------------"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] FIN rc=${{rc}}"
exit $rc
"""

    try:
        with open(log_path, "ab", buffering=0) as log_handle:
            proc = subprocess.Popen(
                ["bash", "-lc", wrapped],
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
                env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
            )
        try:
            if os.path.lexists(latest_path):
                os.remove(latest_path)
            os.symlink(log_path, latest_path)
        except Exception:
            try:
                shutil.copyfile(log_path, latest_path)
            except Exception:
                pass
        _SYSTEM_JOBS[job_id] = {
            "id": job_id,
            "kind": kind,
            "label": label,
            "log": log_path,
            "latest_log": latest_path,
            "process": proc,
            "pid": proc.pid,
            "started_at": started_at,
            "metadata": metadata or {},
            "cache_finalized": False,
        }
        _SYSTEM_LATEST_JOB[kind] = job_id
        return {"ok": True, "job_id": job_id, "kind": kind, "label": label, "log": log_path, "started_at": started_at}
    except Exception as exc:
        return {"ok": False, "message": str(exc), "output": str(exc)}


def _system_job_payload(job_id: str) -> Dict[str, Any]:
    job = _SYSTEM_JOBS.get(str(job_id or ""))
    if not job:
        return {"ok": False, "message": "Job introuvable ou Flask redémarré.", "running": False, "output": ""}
    proc = job.get("process")
    rc = proc.poll() if proc is not None else None
    running = rc is None
    output = _system_job_read_log(job.get("log", ""))
    status = "running" if running else ("success" if rc == 0 else "error")
    if not running and not job.get("cache_finalized"):
        meta = job.get("metadata") or {}
        if job.get("kind") == "install" and meta.get("feature_id"):
            action = str(meta.get("action") or "install").lower()
            _setup_cache_set(str(meta.get("feature_id")), bool(rc == 0 and action in {"install", "enable", "on"}))
        job["cache_finalized"] = True
    return {
        "ok": True,
        "job_id": job.get("id"),
        "kind": job.get("kind"),
        "label": job.get("label"),
        "running": running,
        "status": status,
        "rc": rc,
        "pid": job.get("pid"),
        "log": job.get("log"),
        "started_at": job.get("started_at"),
        "generated_at": now_label(),
        "output": output,
    }


@system_bp.route("/system/api/system/job/<job_id>")
def system_job_status_api(job_id: str):
    payload = _system_job_payload(job_id)
    return jsonify(payload), (200 if payload.get("ok") else 404)


@system_bp.route("/system/api/system/jobs/latest")
def system_job_latest_api():
    kind = _system_job_kind(request.args.get("kind", "install"))
    job_id = _SYSTEM_LATEST_JOB.get(kind)
    if job_id:
        return jsonify(_system_job_payload(job_id))
    latest_path = os.path.join(_system_job_dir(), f"{kind}-latest.log")
    return jsonify({
        "ok": os.path.exists(latest_path),
        "kind": kind,
        "running": False,
        "status": "log" if os.path.exists(latest_path) else "missing",
        "log": latest_path,
        "output": _system_job_read_log(latest_path),
        "generated_at": now_label(),
    })


@system_bp.route("/system/api/system/install/action_async", methods=["POST"])
def system_install_action_async_api():
    payload = request.get_json(silent=True) or {}
    feature_id = str(payload.get("id", "")).strip()
    action = str(payload.get("action", "install")).strip().lower()
    feature = _admin_feature_by_id(feature_id)
    if not feature:
        return jsonify({"ok": False, "message": "Bloc inconnu."}), 404
    key = "install_cmd" if action in {"install", "enable", "on"} else "remove_cmd"
    cmd = feature.get(key)
    if not cmd:
        return jsonify({"ok": False, "message": "Action non disponible pour ce bloc."}), 400
    title = str(feature.get("title") or feature_id)
    label = f"{action} :: {title}"
    result = _system_job_start(
        "install",
        label,
        cmd,
        timeout=int(feature.get("timeout", SYSTEM_ADMIN_TIMEOUT)),
        metadata={"feature_id": feature_id, "action": action},
    )
    return jsonify(result), (200 if result.get("ok") else 500)


@system_bp.route("/system/api/system/updates/refresh_async", methods=["POST"])
def system_updates_refresh_async_api():
    result = _system_job_start("updates", "APT update", "apt-get update", timeout=1800)
    return jsonify(result), (200 if result.get("ok") else 500)


@system_bp.route("/system/api/system/updates/action_async", methods=["POST"])
def system_updates_action_async_api():
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action", "selected")).strip().lower()
    packages = [str(x).strip() for x in (payload.get("packages") or []) if str(x).strip()]
    if action in {"all", "upgrade_all"}:
        cmd = "apt-get update && apt-get upgrade -y"
        label = "Mise à jour globale APT"
    else:
        safe = [p for p in packages if re.fullmatch(r"[A-Za-z0-9.+:\\-]+", p)]
        if not safe:
            return jsonify({"ok": False, "message": "Aucun paquet sélectionné.", "output": "Aucun paquet sélectionné."}), 400
        cmd = "apt-get update && apt-get install -y --only-upgrade " + " ".join(shlex.quote(p) for p in safe)
        label = "Mise à jour sélection : " + ", ".join(safe)
    result = _system_job_start("updates", label, cmd, timeout=3600)
    return jsonify(result), (200 if result.get("ok") else 500)


# --------------------------------------------------
# DEPANNAGE : remise à zéro des fichiers .conf
# --------------------------------------------------
CONF_RESET_DESCRIPTIONS = {
    "app.conf": "configuration centrale des modules Flask",
    "backup.conf": "configuration des backups",
    "build.conf": "configuration du système de build de Docker",
    "cache.conf": "configuration du cache",
    "disk.conf": "configuration des disques et de la veille",
    "docker.conf": "configuration des dockers",
    "dockers.conf": "configuration des dockers",
    "stacks.conf": "configuration Compose / stacks Docker",
    "compose.conf": "configuration Compose Docker",
    "env.conf": "configuration ENV Docker",
    "system.conf": "configuration du module Système",
    "mdns.conf": "configuration des noms mDNS .local",
    "lan.conf": "configuration réseau LAN",
    "terminal.conf": "configuration du terminal web",
    "user.conf": "configuration utilisateurs, SSH et P12",
    "users.conf": "configuration utilisateurs, SSH et P12",
    "vm.conf": "configuration des machines virtuelles",
    "vms.conf": "configuration des machines virtuelles",
    "share.conf": "configuration des partages Samba/NFS",
    "shares.conf": "configuration des partages Samba/NFS",
    "samba.conf": "configuration Samba",
    "nfs.conf": "configuration NFS",
    "minidlnad.conf": "configuration MiniDLNA",
    "minidlna.conf": "configuration MiniDLNA",
}



def _system_conf_set_flat_value(key: str, value: str) -> None:
    """Met à jour une clé plate dans system.conf sans casser le reste du fichier."""
    clean_key = str(key or "").strip()
    if not clean_key:
        return
    clean_value = str(value or "").strip()
    config_path = loaded_config or nas_conf_file("system.conf")
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    lines = []
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()

    final_lines = []
    written = False
    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped and not stripped.startswith(("#", ";")) and "=" in stripped:
            existing_key, _existing_value = stripped.split("=", 1)
            if existing_key.strip().lower() == clean_key.lower():
                final_lines.append(f"{clean_key}={clean_value}\n")
                written = True
                continue
        final_lines.append(raw_line)

    if not written:
        if final_lines and final_lines[-1].strip():
            final_lines.append("\n")
        final_lines.extend([
            "# ============================================================\n",
            "# Dépannage / rescue\n",
            "# Dernier dossier de configuration choisi dans l'interface.\n",
            "# ============================================================\n",
            f"{clean_key}={clean_value}\n",
        ])

    with open(config_path, "w", encoding="utf-8") as handle:
        handle.writelines(final_lines)
    CONF[clean_key] = clean_value


def _reset_conf_save_rescue_path(conf_dir: str) -> None:
    try:
        _system_conf_set_flat_value("path_rescue", conf_dir)
    except Exception as exc:
        print(f"⚠️ Impossible d'enregistrer path_rescue dans system.conf : {exc}")

def _reset_conf_description(name: str) -> str:
    clean = os.path.basename(str(name or ""))
    if clean in CONF_RESET_DESCRIPTIONS:
        return CONF_RESET_DESCRIPTIONS[clean]
    root = clean.rsplit(".", 1)[0].replace("_", " ").replace("-", " ").strip()
    return f"configuration {root}" if root else "configuration"


def _reset_conf_clean_relpath(value: str) -> str:
    clean = str(value or "").strip().replace("\\", "/")
    clean = clean.lstrip("/")
    parts = [part for part in clean.split("/") if part]
    return "/".join(parts)


def _reset_conf_requested_dir(value: str) -> str:
    """Dossier choisi par l'utilisateur pour le dépannage conf.

    Volontairement, on ne devine plus le dossier central : l'utilisateur choisit
    le dossier, puis l'UI fait simplement l'équivalent d'un ls direct dedans.
    """
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Veuillez d’abord choisir votre dossier de configuration.")
    expanded = os.path.expanduser(os.path.expandvars(raw))
    return os.path.realpath(os.path.abspath(expanded))


def _reset_conf_dir_is_safe(conf_dir: str) -> Tuple[bool, str]:
    path = os.path.realpath(os.path.abspath(os.path.expanduser(os.path.expandvars(str(conf_dir or "")))))
    # Garde-fou minimal contre les dossiers système évidents. /etc seul est
    # interdit, mais /etc/yoleo ou /etc/mon-app restent possibles.
    forbidden_exact = {
        "/", "/etc", "/var", "/usr", "/opt", "/home", "/mnt", "/srv",
        "/dev", "/proc", "/sys", "/run", "/tmp", "/boot", "/root",
        "/dockers", "/yoleo",
    }
    if not path or path in forbidden_exact:
        return False, path
    # Refuse les chemins trop larges du type /x.
    if len([part for part in path.split(os.sep) if part]) < 2:
        return False, path
    return True, path


def _reset_conf_allowed_name(name: str) -> bool:
    clean = _reset_conf_clean_relpath(name)
    if not clean or clean.startswith("/"):
        return False
    # Dépannage volontairement simple : équivalent d'un ls du dossier choisi,
    # donc seulement les entrées directes du dossier, pas de scan récursif.
    if "/" in clean:
        return False
    if clean in {"", ".", ".."}:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+", clean))


def _reset_conf_real_path(conf_dir: str, rel_path: str) -> str:
    clean = _reset_conf_clean_relpath(rel_path)
    base = os.path.realpath(conf_dir)
    path = os.path.realpath(os.path.join(base, clean))
    if path != base and path.startswith(base + os.sep):
        return path
    raise ValueError("Chemin conf refusé")


def _reset_conf_list(conf_dir: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    base = os.path.realpath(conf_dir)
    if not os.path.isdir(base):
        return items
    try:
        names = sorted(os.listdir(base), key=lambda value: value.lower())
    except Exception:
        return items

    for name in names:
        if not _reset_conf_allowed_name(name):
            continue
        try:
            real_path = _reset_conf_real_path(base, name)
        except Exception:
            continue
        try:
            st = os.stat(real_path)
            modified = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
            is_dir = os.path.isdir(real_path)
            is_file = os.path.isfile(real_path)
            entry_type = "dossier" if is_dir else "fichier" if is_file else "autre"
            size = None if is_dir else st.st_size
        except Exception:
            modified = ""
            entry_type = "inconnu"
            size = None
        items.append({
            "name": name,
            "description": _reset_conf_description(name),
            "rel_path": name,
            "path": real_path,
            "type": entry_type,
            "size": size,
            "modified": modified,
        })
    return items


@system_bp.route("/system/api/system/troubleshooting/confs")
def system_troubleshooting_confs_api():
    try:
        conf_dir = _reset_conf_requested_dir(request.args.get("conf_dir", ""))
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc), "conf_dir": "", "items": [], "generated_at": now_label()}), 400

    safe, checked_dir = _reset_conf_dir_is_safe(conf_dir)
    if not safe:
        return jsonify({
            "ok": False,
            "message": f"Dossier de configuration refusé par sécurité : {checked_dir}",
            "conf_dir": checked_dir,
            "items": [],
            "generated_at": now_label(),
        }), 400

    if not os.path.isdir(checked_dir):
        return jsonify({
            "ok": False,
            "message": f"Dossier introuvable : {checked_dir}",
            "conf_dir": checked_dir,
            "items": [],
            "generated_at": now_label(),
        }), 404

    _reset_conf_save_rescue_path(checked_dir)
    return jsonify({"ok": True, "conf_dir": checked_dir, "items": _reset_conf_list(checked_dir), "generated_at": now_label()})


@system_bp.route("/system/api/system/troubleshooting/confs/delete", methods=["POST"])
def system_troubleshooting_delete_confs_api():
    payload = request.get_json(silent=True) or {}
    delete_all = bool(payload.get("all"))
    requested = [str(x).strip() for x in (payload.get("files") or []) if str(x).strip()]

    try:
        conf_dir = _reset_conf_requested_dir(payload.get("conf_dir", ""))
    except ValueError as exc:
        return jsonify({
            "ok": False,
            "message": str(exc),
            "conf_dir": "",
            "deleted": [],
            "errors": [str(exc)],
            "restart": None,
            "generated_at": now_label(),
        }), 400

    safe, conf_dir = _reset_conf_dir_is_safe(conf_dir)
    if not safe:
        return jsonify({
            "ok": False,
            "message": f"Dossier conf refusé par sécurité : {conf_dir}",
            "conf_dir": conf_dir,
            "deleted": [],
            "errors": [f"Chemin refusé : {conf_dir}"],
            "restart": None,
            "generated_at": now_label(),
        }), 400

    if not os.path.isdir(conf_dir):
        return jsonify({
            "ok": False,
            "message": f"Dossier introuvable : {conf_dir}",
            "conf_dir": conf_dir,
            "deleted": [],
            "errors": [f"Dossier introuvable : {conf_dir}"],
            "restart": None,
            "generated_at": now_label(),
        }), 404

    include_menu_default = bool(
        payload.get("include_menu_default")
        or payload.get("reset_menu_default")
        or payload.get("delete_menu")
    )

    available = {item["name"]: item for item in _reset_conf_list(conf_dir)}
    deleted: List[str] = []
    errors: List[str] = []
    wiped_dir = False
    skipped: List[str] = []

    def delete_available_entry(name: str) -> None:
        path = available[name]["path"]
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            if name not in deleted:
                deleted.append(name)
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    if delete_all:
        # Remise par défaut : on vide les entrées directes du dossier choisi,
        # mais on garde conf/menu sauf si l'utilisateur coche explicitement
        # "rétablir le menu de démarrage par défaut".
        for name in list(available.keys()):
            if name == "menu" and not include_menu_default:
                skipped.append(name)
                continue
            delete_available_entry(name)
        wiped_dir = bool(deleted)
        try:
            os.makedirs(conf_dir, exist_ok=True)
        except Exception as exc:
            errors.append(f"{conf_dir}: {exc}")
    else:
        names: List[str] = []
        for name in requested:
            clean = _reset_conf_clean_relpath(name)
            if _reset_conf_allowed_name(clean) and clean in available and clean not in names:
                names.append(clean)
            # Suppression ciblée volontairement limitée à une seule ligne,
            # le dossier menu est ajouté séparément par la case dédiée.
            if len(names) >= 1:
                break

        if include_menu_default and "menu" in available and "menu" not in names:
            names.append("menu")

        if not names:
            return jsonify({"ok": False, "message": "Aucune entrée de configuration sélectionnée.", "deleted": [], "errors": []}), 400

        for name in names:
            if name == "menu" and not include_menu_default:
                skipped.append(name)
                errors.append("menu: coche d’abord “Rétablir le menu de démarrage par défaut”.")
                continue
            delete_available_entry(name)

    restart = None
    if wiped_dir or deleted:
        restart = _launch_restart_flask()

    ok = (wiped_dir or bool(deleted)) and not errors and bool(restart and restart.get("ok"))
    if delete_all:
        if deleted:
            message = "Configurations supprimées. Redémarrage Flask demandé."
        else:
            message = "Aucune configuration supprimée."
        if "menu" in skipped:
            message += " Le dossier menu a été conservé."
    else:
        message = f"{deleted[0]} supprimé. Redémarrage Flask demandé." if deleted else "Aucune entrée supprimée."
        if include_menu_default and "menu" in deleted and len(deleted) > 1:
            message = "Entrée sélectionnée et dossier menu supprimés. Redémarrage Flask demandé."

    return jsonify({
        "ok": ok,
        "message": message,
        "conf_dir": conf_dir,
        "deleted": deleted,
        "errors": errors,
        "skipped": skipped,
        "include_menu_default": include_menu_default,
        "wiped_dir": wiped_dir,
        "restart": restart,
        "generated_at": now_label(),
    }), (200 if (wiped_dir or deleted) else 500)



@system_bp.route("/system/api/system/install/status")
def system_install_status_api():
    rows = _admin_status_rows()
    return jsonify({"ok": True, "root": _admin_is_root(), "setup_conf": _setup_conf_path(), "rows": rows, "generated_at": now_label()})


@system_bp.route("/system/api/system/install/action", methods=["POST"])
def system_install_action_api():
    payload = request.get_json(silent=True) or {}
    feature_id = str(payload.get("id", "")).strip()
    action = str(payload.get("action", "install")).strip().lower()
    feature = _admin_feature_by_id(feature_id)
    if not feature:
        return jsonify({"ok": False, "message": "Bloc inconnu."}), 404
    ok, output = _admin_run_action(feature, action)
    row = _admin_feature_payload(feature)
    return jsonify({"ok": ok, "message": output, "output": output, "row": row, "generated_at": now_label()}), (200 if ok else 500)


def _admin_parse_upgradable(text: str) -> List[Dict[str, str]]:
    updates: List[Dict[str, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("Listing") or line.startswith("En train"):
            continue
        m = re.match(r"^([^/\s]+)/([^\s]+)\s+([^\s]+)\s+([^\s]+)\s+\[upgradable from:\s*([^\]]+)\]", line)
        if m:
            name, repo, new_version, arch, old_version = m.groups()
        else:
            parts = line.split()
            if not parts or "/" not in parts[0]:
                continue
            name, repo = parts[0].split("/", 1)
            new_version = parts[1] if len(parts) > 1 else ""
            arch = parts[2] if len(parts) > 2 else ""
            old_version = ""
        updates.append({"name": name, "repo": repo, "new_version": new_version, "arch": arch, "old_version": old_version})
    updates.sort(key=lambda x: x["name"].lower())
    return updates


@system_bp.route("/system/api/system/updates/list")
def system_updates_list_api():
    refresh = str(request.args.get("refresh", "0")).lower() in {"1", "true", "yes", "oui"}
    output = ""
    if refresh:
        _, output = _admin_sh("apt-get update", timeout=1800)
    rc, text = _admin_sh("apt list --upgradable", timeout=180)
    updates = _admin_parse_upgradable(text)
    return jsonify({"ok": rc == 0 or bool(updates), "updates": updates, "count": len(updates), "output": output, "generated_at": now_label()})


@system_bp.route("/system/api/system/updates/action", methods=["POST"])
def system_updates_action_api():
    ok, msg = _admin_need_root()
    if not ok:
        return jsonify({"ok": False, "message": msg, "output": msg}), 403
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action", "selected")).strip().lower()
    packages = [str(x).strip() for x in (payload.get("packages") or []) if str(x).strip()]
    if action in {"all", "upgrade_all"}:
        cmd = "apt-get update && apt-get upgrade -y"
    else:
        safe = [p for p in packages if re.fullmatch(r"[A-Za-z0-9.+:\-]+", p)]
        if not safe:
            return jsonify({"ok": False, "message": "Aucun paquet sélectionné.", "output": "Aucun paquet sélectionné."}), 400
        cmd = "apt-get update && apt-get install -y --only-upgrade " + " ".join(shlex.quote(p) for p in safe)
    rc, out = _admin_sh(cmd, timeout=3600)
    rc2, text = _admin_sh("apt list --upgradable", timeout=180)
    updates = _admin_parse_upgradable(text)
    return jsonify({"ok": rc == 0, "message": out, "output": out, "updates": updates, "count": len(updates), "generated_at": now_label()}), (200 if rc == 0 else 500)


# --------------------------------------------------
# ACTIONS HOTE : arrêt / reboot serveur / restart Flask
# --------------------------------------------------
def _system_action_log_path(action: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(action or "action"))
    os.makedirs("/var/log/flask-system/actions", exist_ok=True)
    return f"/var/log/flask-system/actions/flask-system-{safe}.log"


def _spawn_delayed_shell(command: str, action: str, delay_seconds: int = 2) -> Tuple[bool, str]:
    """
    Lance une commande hôte en arrière-plan après un petit délai.
    Important : Flask doit renvoyer la réponse JSON avant de se redémarrer,
    rebooter ou éteindre la machine.
    """
    log_path = _system_action_log_path(action)
    delay_seconds = max(0, int(delay_seconds or 0))
    started_at = now_label()
    # On écrit un petit en-tête dans le log pour savoir immédiatement si la route a bien déclenché quelque chose.
    shell = (
        f"echo '[{started_at}] action={shlex.quote(str(action))}'; "
        f"echo '[{started_at}] commande={shlex.quote(str(command))}'; "
        f"sleep {delay_seconds}; "
        f"{command}; "
        f"rc=$?; echo '[{started_at}] rc='${{rc}}; exit $rc"
    )
    try:
        log_handle = open(log_path, "ab", buffering=0)
        subprocess.Popen(
            ["bash", "-lc", shell],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            close_fds=True,
            start_new_session=True,
        )
        return True, log_path
    except FileNotFoundError:
        try:
            log_handle = open(log_path, "ab", buffering=0)
            subprocess.Popen(
                ["sh", "-c", shell],
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                close_fds=True,
                start_new_session=True,
            )
            return True, log_path
        except Exception as exc:
            return False, str(exc)
    except Exception as exc:
        return False, str(exc)


def _find_system_sh_script() -> str:
    """
    system.sh est prévu dans le même dossier que le Flask.
    On teste quand même les dossiers proches pour rester compatible si system.py
    est déplacé dans un sous-dossier modules/ plus tard.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    cwd = os.getcwd()
    candidates = []

    for base in [here, cwd, os.path.dirname(here), os.path.dirname(cwd)]:
        if base:
            candidates.append(os.path.join(base, "system.sh"))

    if loaded_config_dir:
        # Exemple : /dockers/conf/system.conf -> /dockers/system/system.sh possible.
        conf_parent = os.path.dirname(loaded_config_dir)
        candidates.append(os.path.join(conf_parent, "system", "system.sh"))
        candidates.append(os.path.join(conf_parent, "system.sh"))

    for candidate in _unique_existing_order(candidates):
        if os.path.isfile(candidate):
            return candidate
    return ""


def _launch_restart_flask() -> Dict[str, Any]:
    script_path = _find_system_sh_script()
    if not script_path:
        return {
            "ok": False,
            "message": "system.sh introuvable dans le dossier Flask ou les dossiers proches.",
            "tested": _unique_existing_order([
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "system.sh"),
                os.path.join(os.getcwd(), "system.sh"),
                os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "system.sh"),
            ]),
        }

    script_dir = os.path.dirname(script_path)
    command = f"cd {shlex.quote(script_dir)} && bash {shlex.quote(script_path)} -restart"
    ok, log_path = _spawn_delayed_shell(command, "restart-flask", delay_seconds=1)
    if not ok:
        return {"ok": False, "message": f"Impossible de lancer system.sh -restart : {log_path}"}

    return {
        "ok": True,
        "message": "Redémarrage Flask lancé : system.sh -restart",
        "command": "system.sh -restart",
        "script": script_path,
        "log": log_path,
    }


def _launch_host_power_action(action: str) -> Dict[str, Any]:
    if action == "reboot_server":
        command = "systemctl --no-wall reboot || reboot"
        label = "Redémarrage serveur lancé."
    elif action == "shutdown_server":
        command = "systemctl --no-wall poweroff || poweroff || shutdown -h now"
        label = "Arrêt serveur lancé."
    else:
        return {"ok": False, "message": "Action hôte inconnue."}

    ok, log_path = _spawn_delayed_shell(command, action, delay_seconds=2)
    if not ok:
        return {"ok": False, "message": f"Impossible de lancer l’action : {log_path}"}
    return {"ok": True, "message": label, "log": log_path}


@system_bp.route("/system/api/overview")
def overview_api():
    return jsonify(collect_overview())


@system_bp.route("/system/api/host")
def host_api():
    return jsonify({"ok": True, "host": collect_host_info()})


@system_bp.route("/system/api/processes")
def processes_api():
    return jsonify(collect_processes(
        limit=request.args.get("limit", "120"),
        query=request.args.get("q", ""),
        sort=request.args.get("sort", "cpu"),
    ))


@system_bp.route("/system/api/logs")
def system_logs_api():
    return jsonify(read_journal_logs(
        source=request.args.get("source", "system"),
        unit=request.args.get("unit", SYSTEM_LOG_DEFAULT_UNIT),
        lines=request.args.get("lines", str(SYSTEM_LOG_LINES)),
    ))


@system_bp.route("/system/api/sys_stats")
def sys_stats():
    cpu_temp = "N/A"
    try:
        t = psutil.sensors_temperatures()
        for k in ["coretemp", "k10temp", "cpu_thermal", "package_id_0"]:
            if k in t:
                cpu_temp = t[k][0].current
                break
    except Exception:
        pass

    dk = {"total": 0, "on": 0}
    if docker is not None:
        try:
            c = docker.from_env().containers.list(all=True)
            dk = {"total": len(c), "on": len([x for x in c if x.status == "running"])}
        except Exception:
            pass

    net_speed, ips, network_direct = get_direct_network_stats()

    gpus = []
    want_nvidia_local = str(request.args.get("nvidia_local", "1")).strip().lower() not in {"0", "false", "no", "non", "off"}
    want_nvidia_ssh = str(request.args.get("nvidia_ssh", "1")).strip().lower() not in {"0", "false", "no", "non", "off"}
    want_intel_gpu = str(request.args.get("intel_gpu", "1")).strip().lower() not in {"0", "false", "no", "non", "off"}

    if want_nvidia_local:
        try:
            gpus.extend(get_local_nvidia_stats())
        except Exception as e:
            print(f"[ERREUR GPU NVIDIA LOCAL] {e}")

    if want_nvidia_ssh:
        try:
            for gpu in get_remote_nvidia_stats():
                remote_gpu = dict(gpu)
                remote_gpu.setdefault("source", "ssh")
                remote_gpu.setdefault("label", "NVIDIA GPU (SSH)")
                gpus.append(remote_gpu)
        except Exception as e:
            print(f"[ERREUR GPU SSH] {e}")

    if want_intel_gpu and (os.path.exists("/dev/dri/renderD128") or os.path.exists("/dev/dri/card0")):
        try:
            intel_stats, _intel_text = get_intel_stats_and_text()
            gpus.append({
                "type": "intel",
                "name": intel_stats["name"],
                "load": intel_stats["load"],
                "mem": intel_stats["mem"],
                "temp": intel_stats["temp"],
                "power": intel_stats["power"],
                "fan": intel_stats["fan"],
            })
        except Exception as e:
            print(f"[ERREUR INTEL GPU] {e}")
            i_load = "0.0"
            try:
                with open("/sys/class/drm/card0/device/gpu_busy_percent", "r", encoding="utf-8", errors="replace") as f:
                    i_load = f.read().strip()
            except Exception:
                pass

            i_power = f"{_read_intel_power_watts():.1f}"
            gpus.append({
                "type": "intel",
                "name": "Intel iGPU",
                "load": i_load,
                "mem": "0.0",
                "temp": "-",
                "power": i_power,
                "fan": "-",
            })

    # Les blocs disques/volumes ont été retirés de l’onglet Info hôte.
    # On garde les clés vides pour compatibilité API, sans lancer lsblk/smartctl
    # ni psutil.disk_usage() sur les volumes de données.
    disks_all = []
    shares = []

    return jsonify({
        "cpu": {"load": psutil.cpu_percent(), "temp": cpu_temp},
        "ram": {"pct": psutil.virtual_memory().percent, "txt": f"{get_size_str(psutil.virtual_memory().used)}/{get_size_str(psutil.virtual_memory().total)}"},
        "docker": dk,
        "net": net_speed,
        "ips": ips,
        "gpus": gpus,
        "disks": disks_all,
        "shares": shares,
        "network_direct": network_direct,
    })




@system_bp.route("/system/api/host/action", methods=["POST"])
def system_host_action_api():
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action") or "").strip().lower()

    # Arrêt/reboot serveur : plus de mot à taper.
    # Le HTML envoie confirmed=true seulement si la case de confirmation est cochée.
    confirm_raw = str(payload.get("confirm") or "").strip().lower()
    confirmed = bool(payload.get("confirmed") is True or confirm_raw in {"1", "true", "yes", "oui", "ok", "checked"})

    if action in ("shutdown_server", "reboot_server") and not confirmed:
        return json_response(False, "Confirmation refusée : coche la case de confirmation puis valide.")

    if action == "restart_flask":
        result = _launch_restart_flask()
    elif action in ("shutdown_server", "reboot_server"):
        result = _launch_host_power_action(action)
    else:
        return json_response(False, "Action inconnue.")

    return jsonify(result), (200 if result.get("ok") else 400)


@system_bp.route("/system/conf/restart", methods=["POST"])
def system_conf_restart_route():
    # Compatibilité avec l’ancien bouton du gestionnaire de conf si présent.
    result = _launch_restart_flask()
    return jsonify({"status": "ok" if result.get("ok") else "error", **result}), (200 if result.get("ok") else 400)


@system_bp.route("/system/api/config")
def api_system_config():
    return jsonify({
        "system_conf_charge": loaded_config or "",
        "ssh_gpu_host": get_conf_str("SSH_GPU_HOST", ""),
        "ssh_gpu_user": get_conf_str("SSH_GPU_USER", ""),
        "ssh_gpu_key_path": get_conf_str("SSH_GPU_KEY_PATH", ""),
        "remote_nvidia_smi": get_conf_str("REMOTE_NVIDIA_SMI", ""),
        "refresh_seconds": REFRESH_SECONDS,
        "process_refresh_seconds": PROCESS_REFRESH_SECONDS,
        "system_log_lines": SYSTEM_LOG_LINES,
        "system_log_default_unit": SYSTEM_LOG_DEFAULT_UNIT,
    })


@system_bp.route("/system/api/nvidia_smi")
def api_nvidia_smi():
    try:
        return get_remote_nvidia_smi_raw()
    except Exception as e:
        return f"Erreur NVIDIA distante via SSH : {e}"


@system_bp.route("/system/api/intel_top")
def api_intel_top():
    try:
        _stats, text = get_intel_stats_and_text()
        return text
    except Exception as e:
        return f"Erreur Intel iGPU : {e}"


@system_bp.route("/system/api/gpu_ttyd")
def api_gpu_ttyd():
    kind = str(request.args.get("kind") or "").strip().lower().replace("_", "-")
    action_map = {
        "nvidia": ("NVIDIA - nvidia-smi", ["nvidia-smi-local"]),
        "nvidia-local": ("NVIDIA locale - nvidia-smi", ["nvidia-smi-local"]),
        "nvidia-ssh": ("NVIDIA SSH - nvidia-smi", ["nvidia-smi-ssh"]),
        "intel": ("Intel GPU - détails", ["intel-gpu-top"]),
        "intel-gpu": ("Intel GPU - détails", ["intel-gpu-top"]),
    }
    if kind not in action_map:
        return jsonify({"ok": False, "message": "GPU inconnu."}), 400

    title, action_args = action_map[kind]
    try:
        import terminal as yoleo_terminal
        term_conf = yoleo_terminal.get_config()
        ok, message = yoleo_terminal.ensure_terminal_url_args(term_conf)
        if not ok:
            return jsonify({"ok": False, "message": message}), 500
        term_conf = yoleo_terminal.get_config()
        return jsonify({
            "ok": True,
            "title": title,
            "message": message,
            "url": yoleo_terminal.ttyd_url_with_args(term_conf, action_args),
        })
    except Exception as exc:
        return jsonify({"ok": False, "message": f"Impossible d'ouvrir ttyd GPU : {exc}"}), 500


@system_bp.route("/system/api/services")
def services_api():
    return jsonify(collect_services())


@system_bp.route("/system/api/services/action", methods=["POST"])
def services_action_api():
    payload = request.get_json(silent=True) or {}
    service = str(payload.get("service") or "").strip()
    action = str(payload.get("action") or "").strip().lower()

    if not valid_service_name(service):
        return json_response(False, service_error(service))

    ok, cmd, success_message, timeout = action_command(action, service)
    if not ok:
        return json_response(False, success_message)

    rc, out = run_cmd(cmd, timeout=timeout)
    if rc == 0:
        details = show_one_service(service)
        return json_response(
            True,
            success_message,
            service=service,
            action=action,
            details=details,
            output=clean_output(out),
        )

    return json_response(
        False,
        f"Action impossible : {action} {service}",
        service=service,
        action=action,
        output=clean_output(out),
    )


@system_bp.route("/system/api/services/logs")
def services_logs_api():
    service = str(request.args.get("service") or "").strip()
    lines_raw = str(request.args.get("lines") or "120").strip()

    if not valid_service_name(service):
        return json_response(False, service_error(service))

    try:
        lines = max(20, min(500, int(lines_raw)))
    except Exception:
        lines = 120

    cmd = [
        journalctl_bin(),
        "-u",
        service,
        "-n",
        str(lines),
        "--no-pager",
        "--output=short-iso",
    ]
    rc, out = run_cmd(cmd, timeout=15)
    if rc == 0:
        return json_response(True, "Logs chargés.", service=service, output=clean_output(out, limit=20000))

    return json_response(False, "Impossible de lire les logs journalctl.", service=service, output=clean_output(out, limit=20000))


# =============================================================================
# LAN / Réseau Linux intégré dans le module Système
# =============================================================================
import datetime as _dt
import ipaddress
import json
import os
import re
import shlex
import signal
import subprocess
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple
