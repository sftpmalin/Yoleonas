#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Application terminale YoLeo.

Le fichier de configuration reste la source de verite : ../conf/menu.conf.
menu.py remplace uniquement la presentation Bash de menu.sh.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import os
import pwd
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.events import Click, Key
from textual.widgets import OptionList, ProgressBar, RichLog, Static
from textual.widgets.option_list import Option


SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
DEFAULT_CONF_DIR = BASE_DIR / "conf"
DEFAULT_MENU_CONF = DEFAULT_CONF_DIR / "menu.conf"
DEFAULT_PYTHON = str(SCRIPT_DIR / ".venv" / "bin" / "python")
OFFLINE_DEB_DIR = BASE_DIR / "offline" / "debs"
SETUP_CONF = DEFAULT_CONF_DIR / "setup.conf"


@dataclass
class MenuItem:
    item_id: int
    item_type: str
    title: str
    command: str
    parent: int


@dataclass
class MenuModel:
    items: list[MenuItem] = field(default_factory=list)

    def by_id(self, item_id: int) -> Optional[MenuItem]:
        if 0 <= item_id < len(self.items):
            return self.items[item_id]
        return None

    def children(self, menu_id: int) -> list[MenuItem]:
        return [item for item in self.items if item.parent == menu_id]

    def selectable_ids(self, menu_id: int, visible=None) -> list[int | str]:
        ids: list[int | str] = [
            item.item_id
            for item in self.children(menu_id)
            if item.item_type in {"entry", "submenu"}
            and (visible is None or visible(item))
        ]
        ids.append("__quit__" if menu_id == -1 else "__back__")
        return ids


def trim(value: str) -> str:
    return str(value).strip()


def clean_title(value: str) -> str:
    text = trim(value)
    text = re.sub(r"^-+\s*", "", text)
    text = re.sub(r"\s*-+$", "", text)
    match = re.fullmatch(r"\[(.*)\]", text)
    if match:
        text = match.group(1)
    return trim(text)


def parse_menu_conf(path: Path) -> MenuModel:
    model = MenuModel()
    active_submenu: dict[int, int] = {}

    def add(item_type: str, title: str, command: str, parent: int) -> int:
        item_id = len(model.items)
        model.items.append(MenuItem(item_id, item_type, title, command, parent))
        return item_id

    def reset_all_submenus() -> None:
        active_submenu.clear()

    def clear_submenus_from(level: int) -> None:
        for key in list(active_submenu):
            if key >= level:
                active_submenu.pop(key, None)

    if not path.is_file():
        return model

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = trim(raw.rstrip("\r"))
        if not line or line.startswith("#") or line.startswith(";"):
            continue

        level = 0
        while line.startswith(","):
            level += 1
            line = line[1:]

        body = trim(line)
        if not body:
            continue

        if body == "-":
            if level == 0:
                add("blank", "", "", -1)
                reset_all_submenus()
            else:
                parent = active_submenu.get(level)
                if parent is not None:
                    add("blank", "", "", parent)
            continue

        if "=" in body:
            title, command = body.split("=", 1)
            title = trim(title)
            command = trim(command)
            if not title or not command:
                continue

            if level == 0:
                add("entry", title, command, -1)
                reset_all_submenus()
            else:
                parent = active_submenu.get(level)
                if parent is not None:
                    add("entry", title, command, parent)
            continue

        title = clean_title(body)
        if not title:
            continue

        if level == 0:
            add("section", title, "", -1)
            reset_all_submenus()
            continue

        parent = -1 if level == 1 else active_submenu.get(level - 1)
        if parent is None:
            continue

        item_id = add("submenu", title, "", parent)
        active_submenu[level] = item_id
        clear_submenus_from(level + 1)

    return model


@dataclass
class CommandSpec:
    display: str
    argv: list[str]
    shell: bool = False
    cwd: str = ""


def internal_action_parts(cmdline: str) -> list[str]:
    text = trim(cmdline)
    if not text.startswith("internal:"):
        return []
    payload = trim(text[len("internal:"):])
    if not payload:
        return []
    try:
        return shlex.split(payload, posix=True)
    except ValueError:
        return [payload]


def is_midnight_commander_installed() -> bool:
    if shutil.which("mc"):
        return True
    try:
        return subprocess.run(
            ["dpkg", "-s", "mc"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        ).returncode == 0
    except Exception:
        return False


def resolve_command(cmdline: str, *, script_dir: Path, python_bin: str, cwd: str) -> CommandSpec:
    internal_parts = internal_action_parts(cmdline)
    if internal_parts:
        menu_py = script_dir / "menu.py"
        return CommandSpec(
            display=f"{python_bin} {shlex.quote(str(menu_py))} --internal-run "
            + " ".join(shlex.quote(part) for part in internal_parts),
            argv=[python_bin, str(menu_py), "--internal-run", *internal_parts],
            cwd=str(BASE_DIR),
        )

    try:
        parts = shlex.split(cmdline, posix=True)
    except ValueError:
        parts = []

    if not parts:
        return CommandSpec(display=cmdline, argv=["/bin/bash", "-lc", "false"], shell=False, cwd=cwd)

    first = parts[0]
    if first.endswith(".py"):
        full = Path(first) if Path(first).is_absolute() else script_dir / first
        return CommandSpec(
            display=f"{python_bin} {shlex.quote(str(full))} "
            + " ".join(shlex.quote(part) for part in parts[1:]),
            argv=[python_bin, str(full), *parts[1:]],
            cwd=cwd,
        )

    if first.endswith(".sh"):
        full = Path(first) if Path(first).is_absolute() else script_dir / first
        return CommandSpec(
            display="bash " + shlex.quote(str(full)) + " "
            + " ".join(shlex.quote(part) for part in parts[1:]),
            argv=["bash", str(full), *parts[1:]],
            cwd=cwd,
        )

    return CommandSpec(display=cmdline, argv=["/bin/bash", "-lc", cmdline], cwd=cwd)


def is_interactive_app_command(cmdline: str) -> bool:
    internal_parts = internal_action_parts(cmdline)
    if internal_parts and internal_parts[0] in {"file-manager"}:
        return True

    try:
        parts = shlex.split(cmdline, posix=True)
    except ValueError:
        return False
    if not parts:
        return False
    return Path(parts[0]).name in {"vm_cli.py", "lan_cli.py", "storage_cli.py", "menu_list.py"}


def run_capture(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def dpkg_installed(package: str) -> bool:
    if not command_exists("dpkg-query"):
        return False
    try:
        result = run_capture(["dpkg-query", "-W", "-f=${Status}", package])
    except Exception:
        return False
    return result.returncode == 0 and "install ok installed" in result.stdout


def service_active(service: str) -> bool:
    if not command_exists("systemctl"):
        return False
    try:
        return run_capture(["systemctl", "is-active", "--quiet", service]).returncode == 0
    except Exception:
        return False


def docker_network_exists(name: str) -> bool:
    if not command_exists("docker"):
        return False
    try:
        return run_capture(["docker", "network", "inspect", name]).returncode == 0
    except Exception:
        return False


def offline_package_names() -> set[str]:
    names: set[str] = set()
    packages = OFFLINE_DEB_DIR / "Packages"
    packages_gz = OFFLINE_DEB_DIR / "Packages.gz"

    try:
        if packages.is_file():
            lines = packages.read_text(encoding="utf-8", errors="replace").splitlines()
        elif packages_gz.is_file():
            with gzip.open(packages_gz, "rt", encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        else:
            return names
    except Exception:
        return names

    for line in lines:
        if line.startswith("Package: "):
            name = line.split(":", 1)[1].strip()
            if name:
                names.add(name)
    return names


def build_offline_apt_env(tmp_path: Path, extra_env: Optional[dict[str, str]] = None) -> dict[str, str]:
    repo_dir = OFFLINE_DEB_DIR.resolve()
    sources_list = tmp_path / "sources.list"
    empty_conf = tmp_path / "empty.conf"
    apt_conf = tmp_path / "apt.conf"

    sources_list.write_text(f"deb [trusted=yes] file:{repo_dir} ./\n", encoding="utf-8")
    empty_conf.write_text("", encoding="utf-8")
    apt_conf.write_text(
        "\n".join(
            [
                f'Dir::Etc::sourcelist "{sources_list}";',
                'Dir::Etc::sourceparts "-";',
                f'Dir::Etc::main "{empty_conf}";',
                'APT::Get::List-Cleanup "0";',
                'APT::Get::AllowUnauthenticated "true";',
                'Acquire::AllowInsecureRepositories "true";',
                'Acquire::Languages "none";',
                'Acquire::Retries "0";',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["APT_CONFIG"] = str(apt_conf)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env["APT_OFFLINE_ONLY"] = "1"
    env["PIP_OFFLINE"] = "1"
    if extra_env:
        env.update(extra_env)
    return env


def print_command(argv: list[str]) -> None:
    print(">>> " + " ".join(shlex.quote(part) for part in argv), flush=True)


def run_live(
    argv: list[str],
    *,
    env: Optional[dict[str, str]] = None,
    cwd: Optional[Path] = None,
) -> int:
    print_command(argv)
    try:
        return subprocess.call(argv, env=env, cwd=str(cwd) if cwd else None)
    except FileNotFoundError:
        print(f"ERREUR : commande introuvable : {argv[0]}", flush=True)
        return 127
    except KeyboardInterrupt:
        return 130


def run_live_offline_apt(
    argv: list[str],
    *,
    extra_env: Optional[dict[str, str]] = None,
    cwd: Optional[Path] = None,
) -> int:
    with tempfile.TemporaryDirectory(prefix="yoleo-apt.") as tmp:
        env = build_offline_apt_env(Path(tmp), extra_env)
        return run_live(argv, env=env, cwd=cwd)


def ensure_root(action_label: str) -> bool:
    if os.geteuid() == 0:
        return True
    print(f"ERREUR : {action_label} doit être lancé en root.", flush=True)
    return False


def ensure_file(path: Path) -> bool:
    if path.is_file():
        return True
    print(f"ERREUR : fichier introuvable : {path}", flush=True)
    return False


def offline_apt_install(packages: list[str], *, install_recommends: bool = False) -> int:
    if not ensure_root("l'installation offline APT"):
        return 1
    if not OFFLINE_DEB_DIR.is_dir():
        print(f"ERREUR : dossier offline absent : {OFFLINE_DEB_DIR}", flush=True)
        return 1

    wanted = [package for package in packages if not dpkg_installed(package)]
    if not wanted:
        print("OK : les paquets demandés sont déjà installés.", flush=True)
        return 0

    available = offline_package_names()
    missing = [package for package in wanted if package not in available]
    if missing:
        print("ERREUR : paquet(s) absent(s) du dépôt offline :", flush=True)
        for package in missing:
            print(f"  - {package}", flush=True)
        print(f"Dépôt utilisé : {OFFLINE_DEB_DIR}", flush=True)
        return 1

    with tempfile.TemporaryDirectory(prefix="yoleo-apt.") as tmp:
        env = build_offline_apt_env(Path(tmp))
        rc = run_live(["apt-get", "update"], env=env)
        if rc:
            return rc
        command = ["apt-get", "install", "-y"]
        if not install_recommends:
            command.append("--no-install-recommends")
        command.extend(wanted)
        return run_live(command, env=env)


def offline_apt_purge(packages: list[str]) -> int:
    if not ensure_root("la désinstallation APT"):
        return 1
    rc = run_live(["apt-get", "purge", "-y", *packages])
    if rc:
        return rc
    return run_live(["apt-get", "autoremove", "-y"])


def setup_cache_set(key: str, value: str) -> None:
    DEFAULT_CONF_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[str] = []
    if SETUP_CONF.is_file():
        rows = SETUP_CONF.read_text(encoding="utf-8", errors="replace").splitlines()
    prefix = f"{key}="
    rows = [row for row in rows if not row.startswith(prefix)]
    rows.append(f"{key}={value}")
    SETUP_CONF.write_text("\n".join(rows) + "\n", encoding="utf-8")


def first_normal_user() -> str:
    for key in ("SUDO_USER", "LOGNAME", "USER"):
        user = os.environ.get(key, "").strip()
        if user and user != "root":
            return user

    try:
        candidates = [
            entry.pw_name
            for entry in pwd.getpwall()
            if 1000 <= entry.pw_uid < 60000
            and entry.pw_name != "nobody"
            and not entry.pw_shell.endswith(("nologin", "false"))
        ]
    except Exception:
        candidates = []
    return candidates[0] if candidates else ""


def group_exists(group: str) -> bool:
    if not command_exists("getent"):
        return False
    return run_capture(["getent", "group", group]).returncode == 0


def add_normal_user_to_groups(groups: list[str]) -> None:
    user = first_normal_user()
    real_groups = [group for group in groups if group_exists(group)]
    if not user or not real_groups:
        return
    run_live(["usermod", "-aG", ",".join(real_groups), user])


def run_systemctl(args: list[str]) -> int:
    if not command_exists("systemctl"):
        print("INFO : systemctl introuvable, étape service ignorée.", flush=True)
        return 0
    return run_live(["systemctl", *args])


def run_file_manager() -> int:
    if not is_midnight_commander_installed():
        print("Midnight Commander n'est pas installé. Lance d'abord : Setup > Installer le gestionnaire de fichiers", flush=True)
        return 1
    os.execvp("mc", ["mc"])
    return 0


def run_setup_status() -> int:
    checks = [
        ("Gestionnaire de fichiers", is_midnight_commander_installed()),
        ("WebAdmin / Webmin", dpkg_installed("webmin") or service_active("webmin.service")),
        ("Cockpit + KVM", dpkg_installed("cockpit") and dpkg_installed("cockpit-machines")),
        ("NVIDIA Docker", dpkg_installed("nvidia-container-toolkit") or command_exists("nvidia-ctk")),
        ("Bridge Linux br0", (Path("/sys/class/net/br0") / "bridge").exists()),
        ("Réseau Docker br0", docker_network_exists("br0")),
        ("Accès hôte vers Docker LAN", service_active("yoleo-macvlan-host.service") or Path("/sys/class/net/mv-host").exists()),
        ("SSH root", Path("/etc/ssh/sshd_config.d/99-root-aimable.conf").is_file()),
    ]

    print("État Setup YoLeo")
    print("================")
    for label, ok in checks:
        print(f"{'OK' if ok else '--'}  {label}")
    print()
    print(f"Dépôt offline : {OFFLINE_DEB_DIR}")
    print(f"Paquets indexés : {len(offline_package_names())}")
    return 0


def run_install_file_manager() -> int:
    rc = offline_apt_install(["mc", "mc-data"])
    if rc == 0:
        setup_cache_set("file_manager", "installed")
        print("OK : Midnight Commander installé. L'entrée apparaîtra au-dessus de Setup.", flush=True)
    return rc


def run_install_webmin() -> int:
    rc = offline_apt_install(["webmin"])
    if rc:
        print("Webmin n'est pas encore disponible dans l'archive offline actuelle.", flush=True)
        print("Ajoute le paquet webmin dans /yoleo/offline/debs puis régénère l'index Packages.", flush=True)
        return rc
    run_systemctl(["enable", "--now", "webmin.service"])
    print("OK : WebAdmin / Webmin prêt sur le port 10000.", flush=True)
    return 0


def run_install_cockpit() -> int:
    packages = [
        "cockpit",
        "cockpit-machines",
        "cockpit-bridge",
        "cockpit-ws",
        "cockpit-system",
        "libvirt-daemon-system",
        "libvirt-clients",
        "libvirt-dbus",
        "qemu-system-x86",
        "qemu-system-gui",
        "qemu-utils",
        "virtinst",
        "novnc",
        "websockify",
        "bridge-utils",
        "ovmf",
        "swtpm",
        "swtpm-tools",
        "dnsmasq-base",
    ]
    rc = offline_apt_install(packages)
    if rc:
        return rc
    add_normal_user_to_groups(["libvirt", "kvm"])
    run_systemctl(["enable", "--now", "cockpit.socket"])
    run_systemctl(["enable", "--now", "libvirtd"])
    run_systemctl(["enable", "--now", "virtlogd"])
    print("OK : Cockpit + KVM installé. Accès Cockpit : https://IP_DU_SERVEUR:9090", flush=True)
    return 0


def run_install_nvidia_docker() -> int:
    packages = [
        "libnvidia-container1",
        "libnvidia-container-tools",
        "nvidia-container-toolkit-base",
        "nvidia-container-toolkit",
    ]
    rc = offline_apt_install(packages)
    if rc:
        return rc
    if command_exists("nvidia-ctk"):
        rc = run_live(["nvidia-ctk", "runtime", "configure", "--runtime=docker"])
        if rc:
            return rc
    else:
        print("ATTENTION : nvidia-ctk introuvable après installation.", flush=True)
    run_systemctl(["restart", "docker"])
    print("OK : outils NVIDIA Docker installés/configurés.", flush=True)
    return 0


def find_intel_gpu_script() -> Optional[Path]:
    candidates = [
        BASE_DIR / "pilote" / "intel" / "install_intel_gpu.sh",
        BASE_DIR / "pilote" / "intel" / "intel_gpu.sh",
        BASE_DIR / "pilote" / "intel_gpu" / "install_intel_gpu.sh",
        BASE_DIR / "pilote" / "intel_gpu.sh",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def run_install_intel_gpu() -> int:
    script = find_intel_gpu_script()
    if script is not None:
        print(f"Script Intel détecté : {script}", flush=True)
        return run_script_action(script, [], offline_apt=True)

    wanted = [
        "pciutils",
        "firmware-misc-nonfree",
        "intel-microcode",
        "intel-gpu-tools",
        "intel-media-va-driver",
        "intel-media-va-driver-non-free",
        "i965-va-driver",
        "i965-va-driver-shaders",
        "mesa-va-drivers",
        "mesa-vulkan-drivers",
        "libvulkan1",
        "libva2",
        "libva-drm2",
        "libva-x11-2",
        "vainfo",
    ]
    available = offline_package_names()
    packages = [package for package in wanted if package in available or dpkg_installed(package)]
    missing = [package for package in wanted if package not in available and not dpkg_installed(package)]
    if missing:
        print("INFO : paquet(s) Intel absent(s) de l'offline actuel, ignoré(s) :", flush=True)
        for package in missing:
            print(f"  - {package}", flush=True)
    rc = offline_apt_install(packages)
    if rc:
        return rc
    add_normal_user_to_groups(["render", "video"])
    run_live(["modprobe", "i915"])
    run_live(["lspci", "-nn"])
    run_live(["ls", "-l", "/dev/dri"])
    if Path("/dev/dri/renderD128").exists() and command_exists("vainfo"):
        run_live(["vainfo", "--display", "drm", "--device", "/dev/dri/renderD128"])
    print("OK : outils Intel GPU installés. Un reboot peut être nécessaire.", flush=True)
    return 0


def run_chmod_yoleo() -> int:
    if not ensure_root("la mise en exécutable des dossiers YoLeo"):
        return 1
    targets = [BASE_DIR / "scripts", BASE_DIR / "bin", BASE_DIR / "system"]
    changed = 0
    for directory in targets:
        if not directory.is_dir():
            print(f"SKIP : dossier introuvable : {directory}", flush=True)
            continue
        for path in directory.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            if directory.name != "bin" and path.suffix.lower() not in {".sh", ".py"}:
                continue
            try:
                mode = path.stat().st_mode
                path.chmod(mode | 0o111)
                changed += 1
            except Exception as exc:
                print(f"ERREUR : chmod impossible sur {path} : {exc}", flush=True)
                return 1
    print(f"OK : {changed} fichier(s) rendus exécutables dans scripts/bin/system.", flush=True)
    return 0


def run_script_action(
    path: Path,
    script_args: list[str],
    *,
    offline_apt: bool = False,
    extra_env: Optional[dict[str, str]] = None,
) -> int:
    if not ensure_file(path):
        return 1
    argv = ["bash", str(path), *script_args]
    if offline_apt:
        return run_live_offline_apt(argv, extra_env=extra_env, cwd=path.parent)
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return run_live(argv, env=env, cwd=path.parent)


def run_nvidia_secureboot(version: str) -> int:
    nvidia_dir = BASE_DIR / "pilote" / "nvidia"
    script = nvidia_dir / f"install_nvidia_secureboot_{version}.sh"
    driver = nvidia_dir / f"NVIDIA-Linux-x86_64-{version}.run"
    return run_script_action(
        script,
        [],
        offline_apt=True,
        extra_env={"NVIDIA_DIR": str(nvidia_dir), "DRIVER": str(driver)},
    )


def run_nvidia_classic(version: str) -> int:
    nvidia_dir = BASE_DIR / "pilote" / "nvidia"
    script = nvidia_dir / "install_nvidia.sh"
    driver = nvidia_dir / f"NVIDIA-Linux-x86_64-{version}.run"
    return run_script_action(script, ["--file", str(driver)], offline_apt=True)


def run_internal_action(args: list[str]) -> int:
    action = args[0] if args else ""
    scripts = BASE_DIR / "scripts"

    simple_actions: dict[str, tuple[Path, list[str], bool, dict[str, str]]] = {
        "setup-install-base": (SCRIPT_DIR / "setup.sh", ["-install"], True, {}),
        "setup-fix-docker-nvidia": (scripts / "bug_docker_nvidia.sh", [], False, {}),
        "setup-root-ssh-install": (scripts / "debian_root.sh", ["-install"], True, {}),
        "setup-root-ssh-remove": (scripts / "debian_root.sh", ["-remove"], True, {}),
        "setup-screen-sleep-install": (scripts / "ecran.sh", ["-install"], False, {}),
        "setup-root-color-install": (scripts / "couleur.sh", [], False, {}),
        "setup-lan-ip-install": (scripts / "lan_ip.sh", ["-install"], False, {}),
        "setup-linux-br0-install": (scripts / "lan_bro.sh", ["-install"], True, {}),
        "setup-lan-vm-install": (scripts / "lan_vw.sh", ["install"], False, {}),
        "setup-docker-br0-install": (scripts / "lan_docker.sh", ["install"], False, {}),
        "setup-docker-lan-host-install": (
            scripts / "lan_docker_host.sh",
            ["install-systemd"],
            False,
            {"SCRIPT_PATH": str(scripts / "lan_docker_host.sh")},
        ),
    }

    if action == "file-manager":
        return run_file_manager()
    if action == "setup-status":
        return run_setup_status()
    if action == "setup-install-file-manager":
        return run_install_file_manager()
    if action == "setup-install-webmin":
        return run_install_webmin()
    if action == "setup-remove-webmin":
        return offline_apt_purge(["webmin"])
    if action == "setup-install-cockpit":
        return run_install_cockpit()
    if action == "setup-remove-cockpit":
        return offline_apt_purge(["cockpit", "cockpit-machines", "cockpit-bridge", "cockpit-ws", "cockpit-system"])
    if action == "setup-install-nvidia-docker":
        return run_install_nvidia_docker()
    if action == "setup-remove-nvidia-docker":
        return offline_apt_purge(
            [
                "nvidia-container-toolkit",
                "nvidia-container-toolkit-base",
                "libnvidia-container-tools",
                "libnvidia-container1",
            ]
        )
    if action == "setup-install-intel-gpu":
        return run_install_intel_gpu()
    if action == "setup-chmod-yoleo":
        return run_chmod_yoleo()
    if action == "setup-install-nvidia-secureboot-595":
        return run_nvidia_secureboot("595.84")
    if action == "setup-install-nvidia-secureboot-580":
        return run_nvidia_secureboot("580.95.05")
    if action == "setup-install-nvidia-classic-595":
        return run_nvidia_classic("595.84")
    if action == "setup-install-nvidia-classic-580":
        return run_nvidia_classic("580.95.05")
    if action in simple_actions:
        path, script_args, offline_apt, extra_env = simple_actions[action]
        return run_script_action(path, script_args, offline_apt=offline_apt, extra_env=extra_env)

    print(f"Commande interne inconnue : {action or '(vide)'}", flush=True)
    return 2


from menu_progress import (  # noqa: E402
    OperationState,
    ProgressPlan,
    SPINNER,
    build_progress_plan,
    classify_operation,
    progress_bar_text,
    seconds_text,
    update_state_from_line,
    update_state_from_runtime,
)


class YoleoMenuApp(App[int]):
    CSS = """
    Screen {
        background: #071314;
        color: #d9fff3;
    }

    #frame {
        height: 100%;
        border: double #39f0b4;
        background: #071314;
    }

    #topbar {
        height: 3;
        padding: 0 2;
        background: #0e2a2b;
        color: #e8fff9;
        border-bottom: solid #39f0b4;
    }

    #app_title {
        text-style: bold;
        color: #ffffff;
    }

    #content {
        height: 1fr;
        padding: 1 2;
    }

    #footer {
        height: 2;
        padding: 0 2;
        background: #123638;
        color: #d9fff3;
    }

    #menu_view, #operation_view {
        height: 100%;
    }

    #breadcrumb {
        height: 2;
        color: #9ff5d5;
    }

    #menu_options {
        height: 1fr;
        border: solid #2fa77f;
        padding: 0 1;
        background: #091c1d;
    }

    #menu_status {
        height: 2;
        color: #bfeee0;
    }

    #operation_header {
        height: 5;
        border: solid #2fa77f;
        padding: 0 1;
        background: #091c1d;
    }

    #tabs {
        height: 1;
        color: #8fd9c2;
        text-style: bold;
    }

    .tab {
        width: auto;
        color: #39d9bd;
        text-style: bold;
    }

    .tab.active {
        background: #9df5ce;
        color: #071314;
        text-style: bold reverse;
    }

    #tab_separator {
        width: auto;
        color: #9df5ce;
    }

    #general_panel, #detail_panel {
        height: 1fr;
        border: solid #2fa77f;
        padding: 1 2;
        background: #091c1d;
    }

    #detail_log {
        height: 1fr;
        background: #050d0e;
    }

    #command_input {
        height: 1;
        color: #ffe7a6;
    }

    #progress {
        height: 1;
        margin-top: 1;
    }

    .hidden {
        display: none;
    }
    """

    BINDINGS = [
        ("escape", "back", "Retour"),
        ("left", "left", "Gauche"),
        ("right", "right", "Droite"),
        ("f1", "help", "Aide"),
        ("f2", "f2", "Général"),
        ("f3", "f3", "Détail"),
        ("f4", "f4", "Fin"),
    ]

    def __init__(
        self,
        *,
        menu_conf: Path,
        python_bin: str,
        menu_title: str,
        command_cwd: str,
        test_mode: bool = False,
    ) -> None:
        super().__init__()
        self.menu_conf = menu_conf
        self.python_bin = python_bin
        self.menu_title = menu_title
        self.command_cwd = command_cwd
        self.test_mode = test_mode
        self.model = MenuModel()
        self.current_menu_id = -1
        self.saved_selection: dict[int, int] = {}
        self.selectable_ids: list[int | str] = []
        self.option_to_target: dict[str, int | str] = {}
        self.in_operation = False
        self.operation_view = "general"
        self.state = OperationState()
        self.progress_plan = ProgressPlan()
        self.process: Optional[asyncio.subprocess.Process] = None
        self.numeric_buffer = ""
        self.numeric_timer = None
        self.spinner_index = 0
        self.follow_detail = True
        self.exit_code = 0

    def compose(self) -> ComposeResult:
        with Container(id="frame"):
            yield Static("", id="topbar")
            with Container(id="content"):
                with Vertical(id="menu_view"):
                    yield Static("", id="breadcrumb")
                    yield OptionList(id="menu_options")
                    yield Static("", id="menu_status")
                with Vertical(id="operation_view", classes="hidden"):
                    yield Static("", id="operation_header")
                    with Horizontal(id="tabs"):
                        yield Static(" GÉNÉRAL ", id="tab_general", classes="tab")
                        yield Static(" | ", id="tab_separator")
                        yield Static(" DÉTAIL ", id="tab_detail", classes="tab")
                    with Vertical(id="general_panel"):
                        yield Static("", id="general_title")
                        yield Static("", id="general_body")
                        yield ProgressBar(total=1, show_eta=False, id="progress")
                    with Vertical(id="detail_panel", classes="hidden"):
                        yield RichLog(
                            max_lines=None,
                            wrap=False,
                            highlight=False,
                            markup=False,
                            auto_scroll=True,
                            id="detail_log",
                        )
                        yield Static("", id="command_input")
            yield Static("", id="footer")

    def on_mount(self) -> None:
        self.load_model()
        self.render_menu()
        self.set_interval(0.2, self.tick_operation)
        self.query_one("#menu_options", OptionList).focus()

    def load_model(self) -> None:
        self.model = parse_menu_conf(self.menu_conf)

    def current_title(self) -> str:
        if self.current_menu_id == -1:
            return "Menu principal"
        item = self.model.by_id(self.current_menu_id)
        return item.title if item else "Menu"

    def breadcrumbs(self) -> str:
        names: list[str] = []
        current = self.current_menu_id
        while current != -1:
            item = self.model.by_id(current)
            if item is None:
                break
            names.append(item.title)
            current = item.parent
        names.reverse()
        return " / ".join(["Accueil", *names])

    def is_item_visible(self, item: MenuItem) -> bool:
        internal_parts = internal_action_parts(item.command)
        if internal_parts and internal_parts[0] == "file-manager":
            return is_midnight_commander_installed()
        return True

    def render_topbar(self) -> None:
        if self.in_operation:
            subtitle = self.state.category
        else:
            subtitle = self.breadcrumbs()
        self.query_one("#topbar", Static).update(
            Text.assemble(
                (self.menu_title, "bold white"),
                "\n",
                (subtitle, "cyan"),
            )
        )

    def render_footer(self) -> None:
        if self.in_operation:
            if self.process and self.process.returncode is None:
                text = (
                    "F2 Général  F3 Détail  ←/→ Vues  ↑↓ PgUp/PgDn Home/End Logs\n"
                    "Saisie+Entrée Envoyer  F4 Fin logs"
                )
            else:
                text = "Échap Retour  Q Quitter  F2 Général  F3 Détail  F4 Fin logs"
        else:
            text = (
                "↑↓ Nav  Entrée OK  ←/Échap Retour  0/Q Quitter  1..n Choix\n"
                "F1 Aide  F2 Accueil  F3 Recharger  F4 Quitter"
            )
        self.query_one("#footer", Static).update(text)

    def render_menu(self) -> None:
        self.in_operation = False
        self.render_topbar()
        self.render_footer()
        self.query_one("#menu_view").remove_class("hidden")
        self.query_one("#operation_view").add_class("hidden")

        options = self.query_one("#menu_options", OptionList)
        options.clear_options()
        self.option_to_target = {}
        self.selectable_ids = self.model.selectable_ids(self.current_menu_id, self.is_item_visible)
        selectable_numbers: dict[int | str, int] = {}
        for number, target in enumerate(self.selectable_ids, start=1):
            selectable_numbers[target] = number

        if not self.model.items:
            options.add_option(Option(Text(f"Menu vide : {self.menu_conf}", style="red"), disabled=True))

        for item in self.model.children(self.current_menu_id):
            if not self.is_item_visible(item):
                continue
            if item.item_type == "blank":
                options.add_option(Option(Text(""), disabled=True))
                continue
            if item.item_type == "section":
                options.add_option(Option(Text(item.title, style="bold bright_cyan"), disabled=True))
                continue

            number = selectable_numbers.get(item.item_id, 0)
            arrow = "  ▶" if item.item_type == "submenu" else ""
            prompt = Text.assemble(
                (f"{number:>2}. ", "dim"),
                (item.title, "white"),
                (arrow, "bright_green"),
            )
            option_id = f"item-{item.item_id}"
            self.option_to_target[option_id] = item.item_id
            options.add_option(Option(prompt, id=option_id))

        special_id = "special-quit" if self.current_menu_id == -1 else "special-back"
        special_label = "Quitter" if self.current_menu_id == -1 else "Retour"
        special_suffix = "" if self.current_menu_id == -1 else "  ◀"
        self.option_to_target[special_id] = "__quit__" if self.current_menu_id == -1 else "__back__"
        options.add_option(
            Option(
                Text.assemble((" 0. ", "dim"), (special_label + special_suffix, "yellow")),
                id=special_id,
            )
        )

        saved = self.saved_selection.get(self.current_menu_id, 0)
        if saved < 0:
            saved = 0
        if saved >= options.option_count:
            saved = max(0, options.option_count - 1)
        if saved < options.option_count:
            try:
                if options.get_option_at_index(saved).disabled:
                    saved = self.first_enabled_option_index(options)
            except Exception:
                saved = self.first_enabled_option_index(options)
        options.highlighted = saved

        self.query_one("#breadcrumb", Static).update(
            Text.assemble((self.current_title(), "bold white"), "\n", (str(self.menu_conf), "dim"))
        )
        self.query_one("#menu_status", Static).update("Configuration lue en direct. La commande sélectionnée sera exécutée par les moteurs existants.")
        options.focus()

    @staticmethod
    def first_enabled_option_index(options: OptionList) -> int:
        for index in range(options.option_count):
            try:
                if not options.get_option_at_index(index).disabled:
                    return index
            except Exception:
                continue
        return 0

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "menu_options" and not self.in_operation:
            self.saved_selection[self.current_menu_id] = event.option_index

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "menu_options" or self.in_operation:
            return
        target = self.option_to_target.get(str(event.option_id))
        self.activate_target(target)

    def activate_target(self, target: int | str | None) -> None:
        if target is None:
            return
        if target == "__quit__":
            self.exit(self.exit_code)
            return
        if target == "__back__":
            self.go_parent()
            return

        item = self.model.by_id(int(target))
        if item is None:
            return
        if item.item_type == "submenu":
            self.current_menu_id = item.item_id
            self.render_menu()
            return
        if item.item_type == "entry":
            self.start_operation(item)

    def go_parent(self) -> None:
        if self.current_menu_id == -1:
            self.exit(self.exit_code)
            return
        item = self.model.by_id(self.current_menu_id)
        self.current_menu_id = item.parent if item else -1
        self.render_menu()

    def start_operation(self, item: MenuItem) -> None:
        if is_interactive_app_command(item.command):
            self.run_worker(self.run_interactive_app(item), name=f"interactive-{item.item_id}", exit_on_error=False)
            return

        self.progress_plan = build_progress_plan(item.command)
        self.state = OperationState(
            title=item.title,
            command=item.command,
            category=classify_operation(item.title, item.command),
            total=self.progress_plan.total,
            status="lancement",
            started_at=time.time(),
        )
        self.in_operation = True
        self.operation_view = "general"
        self.follow_detail = True
        self.query_one("#menu_view").add_class("hidden")
        self.query_one("#operation_view").remove_class("hidden")
        log = self.query_one("#detail_log", RichLog)
        log.clear()
        log.auto_scroll = True
        self.render_operation()
        self.run_worker(self.run_operation(item), name="menu-command", exit_on_error=False)

    async def run_interactive_app(self, item: MenuItem) -> None:
        spec = resolve_command(
            item.command,
            script_dir=SCRIPT_DIR,
            python_bin=self.python_bin,
            cwd=self.command_cwd,
        )
        self.query_one("#menu_status", Static).update(f"Ouverture de {item.title}...")

        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        with self.suspend():
            process = await asyncio.create_subprocess_exec(
                *spec.argv,
                cwd=spec.cwd or None,
                env=env,
            )
            rc = await process.wait()
        self.exit_code = rc if rc else self.exit_code
        self.load_model()
        self.render_menu()
        if rc:
            self.query_one("#menu_status", Static).update(f"{item.title} terminé avec code retour {rc}.")
        else:
            self.query_one("#menu_status", Static).update(f"{item.title} fermé.")

    async def run_operation(self, item: MenuItem) -> None:
        spec = resolve_command(
            item.command,
            script_dir=SCRIPT_DIR,
            python_bin=self.python_bin,
            cwd=self.command_cwd,
        )
        self.write_log(f">>> {spec.display}\n")
        self.write_log("\n")

        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("COMPOSE_PROGRESS", "plain")
        env.setdefault("DOCKER_BUILDKIT", env.get("DOCKER_BUILDKIT", "1"))

        try:
            self.process = await asyncio.create_subprocess_exec(
                *spec.argv,
                cwd=spec.cwd or None,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            self.state.had_error = True
            self.state.status = "commande introuvable"
            self.state.return_code = 127
            self.write_log(f"❌ Commande introuvable : {exc}\n")
            self.operation_view = "detail"
            self.render_operation()
            return
        except Exception as exc:
            self.state.had_error = True
            self.state.status = "erreur de lancement"
            self.state.return_code = 1
            self.write_log(f"❌ Impossible de lancer la commande : {exc}\n")
            self.operation_view = "detail"
            self.render_operation()
            return

        assert self.process.stdout is not None
        while True:
            data = await self.process.stdout.readline()
            if not data:
                break
            text = data.decode("utf-8", errors="replace")
            self.write_log(text)
            update_state_from_line(self.state, text, self.progress_plan)
            update_state_from_runtime(self.state, self.progress_plan)
            if self.state.had_error:
                self.operation_view = "detail"
            self.render_operation()

        rc = await self.process.wait()
        self.state.return_code = rc
        self.state.ended_at = time.time()
        update_state_from_runtime(self.state, self.progress_plan)
        if rc == 0 and not self.state.had_error:
            if self.progress_plan.total:
                self.state.total = self.progress_plan.total
                self.state.current_index = self.progress_plan.total
            self.state.status = "terminé"
            self.write_log("\n✅ OK\n")
        else:
            self.state.had_error = True
            self.state.last_error_code = self.state.last_error_code or str(rc)
            self.state.status = "terminé avec erreur"
            self.operation_view = "detail"
            self.write_log(f"\n❌ Erreur : {rc}\n")
        self.render_operation()

    def write_log(self, text: str) -> None:
        log = self.query_one("#detail_log", RichLog)
        for line in text.splitlines(keepends=True):
            content = line.rstrip("\n")
            if "\x1b[" in content:
                log.write(Text.from_ansi(content), scroll_end=self.follow_detail)
            else:
                log.write(content, scroll_end=self.follow_detail)

    def render_operation(self) -> None:
        self.render_topbar()
        self.render_footer()

        running = self.process is not None and self.process.returncode is None
        elapsed_end = self.state.ended_at if self.state.ended_at is not None else time.time()
        elapsed = seconds_text(elapsed_end - self.state.started_at) if self.state.started_at else "00:00:00"

        command_text = self.state.command
        self.query_one("#operation_header", Static).update(
            Text.assemble(
                (self.state.title, "bold white"),
                "\n",
                (self.state.category, "cyan"),
                "  ",
                ("en cours" if running else "terminé", "yellow" if running else "green"),
                "  Durée: ",
                (elapsed, "white"),
                "\n",
                (command_text, "dim"),
            )
        )

        tab_general = self.query_one("#tab_general", Static)
        tab_detail = self.query_one("#tab_detail", Static)
        tab_general.update(" GÉNÉRAL ")
        tab_detail.update(" DÉTAIL ")
        tab_general.set_class(self.operation_view == "general", "active")
        tab_detail.set_class(self.operation_view == "detail", "active")

        self.query_one("#general_panel").set_class(self.operation_view != "general", "hidden")
        self.query_one("#detail_panel").set_class(self.operation_view != "detail", "hidden")

        if self.operation_view == "detail":
            self.query_one("#detail_log", RichLog).focus()

        spinner = SPINNER[self.spinner_index % len(SPINNER)] if running else "■"
        current_name = self.state.current_name or "en attente"
        current_index = self.state.current_index
        total = self.state.total
        if total and (current_index or getattr(self.state, "progress_unit", "") == "percent"):
            if getattr(self.state, "progress_unit", "") == "percent":
                progress_line = f"{current_index}%"
                progress_text = progress_bar_text(current_index, total, f"{current_index}%")
            else:
                progress_line = f"{current_index} sur {total}"
                progress_text = progress_bar_text(current_index, total)
            self.query_one("#progress", ProgressBar).update(total=total, progress=min(current_index, total))
        else:
            progress_line = "progression globale non encore détectée"
            progress_text = ""
            self.query_one("#progress", ProgressBar).update(total=1, progress=0)

        if self.progress_plan.tracks_stack_dockers:
            item_word = "Docker"
            name_label = "Docker"
        elif "Construction" in self.state.category:
            item_word = "Build"
            name_label = "Docker"
        elif "images Docker" in self.state.category:
            item_word = "Docker"
            name_label = "Docker"
        elif "stack" in self.state.category.lower():
            item_word = "Stack"
            name_label = "Nom"
        elif "rsync" in self.state.category.lower() or "sauvegarde" in self.state.category.lower():
            item_word = "Transfert"
            name_label = "Script"
        else:
            item_word = "Étape"
            name_label = "Nom"

        summary = ""
        if self.state.return_code is not None:
            if self.progress_plan.tracks_stack_dockers:
                failed = self.state.failed_count
                if self.state.return_code != 0 and failed == 0:
                    failed = 1
                summary = (
                    f"\n\nBilan\n"
                    f"{self.progress_plan.total} Docker/YML suivi(s)\n"
                    f"{self.state.ok_count} stack(s) OK\n"
                    f"{failed} stack(s) en erreur"
                )
            elif self.state.total or self.state.ok_count or self.state.failed_count or self.state.skipped_count:
                successful = self.state.ok_count
                failed = self.state.failed_count
                if self.state.return_code != 0 and failed == 0:
                    failed = 1
                requested = self.state.total or max(successful + failed + self.state.skipped_count, 1)
                summary = (
                    f"\n\nBilan\n"
                    f"{requested} demandé(s)\n"
                    f"{successful} réussi(s)\n"
                    f"{failed} en erreur\n"
                    f"{self.state.skipped_count} ignoré(s)/skip"
                )
            else:
                summary = f"\n\nCode retour : {self.state.return_code}"

        error_text = ""
        if self.state.had_error:
            target = self.state.last_error_target or current_name
            code = self.state.last_error_code or (str(self.state.return_code) if self.state.return_code is not None else "?")
            error_text = f"\n\nERREUR\nCible : {target}\nCode retour : {code}"

        detail_text = ""
        if getattr(self.state, "progress_detail", ""):
            detail_text = f"{self.state.progress_detail}\n"

        self.query_one("#general_title", Static).update(Text(self.state.category, style="bold white"))
        self.query_one("#general_body", Static).update(
            Text.assemble(
                f"\n{item_word} {progress_line}",
                ("  " + progress_text, "bold cyan") if progress_text else "",
                "\n",
                f"{name_label} : {current_name}\n",
                (detail_text, "cyan") if detail_text else "",
                f"État : {self.state.status} {spinner if running else ''}\n",
                f"Durée : {elapsed}\n",
                (summary, "green" if self.state.return_code == 0 else "yellow"),
                (error_text, "bold red"),
            )
        )
        self.query_one("#command_input", Static).update(
            f"> {self.state.stdin_buffer}" if running else "Commande terminée. Échap pour revenir au menu."
        )

    def tick_operation(self) -> None:
        if not self.in_operation:
            return
        update_state_from_runtime(self.state, self.progress_plan)
        self.spinner_index += 1
        self.render_operation()

    def action_back(self) -> None:
        if self.in_operation:
            if self.process is not None and self.process.returncode is None:
                self.query_one("#command_input", Static).update("Commande en cours: retour disponible après la fin.")
                return
            self.load_model()
            self.render_menu()
            return
        self.go_parent()

    def action_left(self) -> None:
        if self.in_operation:
            self.operation_view = "general"
            self.render_operation()
            return
        self.go_parent()

    def action_right(self) -> None:
        if self.in_operation:
            self.operation_view = "detail"
            self.render_operation()
            return
        options = self.query_one("#menu_options", OptionList)
        if options.highlighted is None:
            return
        try:
            option = options.get_option_at_index(options.highlighted)
        except Exception:
            return
        target = self.option_to_target.get(str(option.id))
        if isinstance(target, int):
            item = self.model.by_id(target)
            if item and item.item_type == "submenu":
                self.activate_target(target)

    def action_quit_app(self) -> None:
        if self.in_operation and self.process is not None and self.process.returncode is None:
            self.query_one("#command_input", Static).update("Commande en cours: quitte après la fin ou laisse le moteur détaché finir.")
            return
        self.exit(self.exit_code)

    def action_help(self) -> None:
        if self.in_operation:
            self.operation_view = "detail"
            self.render_operation()
            self.write_log("\nAide: F2 Général, F3 Détail, F4 Fin, Échap Retour quand terminé.\n")
        else:
            self.query_one("#menu_status", Static).update(
                "Aide: flèches pour naviguer, Entrée pour lancer, chiffres pour les raccourcis numériques, F3 pour relire menu.conf."
            )

    def action_f2(self) -> None:
        if self.in_operation:
            self.operation_view = "general"
            self.render_operation()
        else:
            self.current_menu_id = -1
            self.render_menu()

    def action_f3(self) -> None:
        if self.in_operation:
            self.operation_view = "detail"
            self.render_operation()
        else:
            self.load_model()
            self.render_menu()

    def action_f4(self) -> None:
        if self.in_operation:
            self.operation_view = "detail"
            self.follow_detail = True
            log = self.query_one("#detail_log", RichLog)
            log.auto_scroll = True
            log.action_scroll_end()
            self.render_operation()
            return
        self.exit(self.exit_code)

    def set_operation_view(self, view: str) -> None:
        if not self.in_operation or view not in {"general", "detail"}:
            return
        self.operation_view = view
        if view == "detail":
            self.query_one("#detail_log", RichLog).focus()
        self.render_operation()

    @on(Click, "#tab_general")
    def click_tab_general(self, event: Click) -> None:
        self.set_operation_view("general")
        event.stop()

    @on(Click, "#tab_detail")
    def click_tab_detail(self, event: Click) -> None:
        self.set_operation_view("detail")
        event.stop()

    @on(Click, "#general_panel")
    def click_general_panel(self, event: Click) -> None:
        self.set_operation_view("general")
        event.stop()

    @on(Click, "#detail_panel")
    @on(Click, "#detail_log")
    def click_detail_panel(self, event: Click) -> None:
        self.set_operation_view("detail")
        event.stop()

    async def send_stdin_line(self) -> None:
        if not self.process or self.process.returncode is not None or self.process.stdin is None:
            return
        line = self.state.stdin_buffer + "\n"
        self.state.stdin_buffer = ""
        self.process.stdin.write(line.encode("utf-8", errors="replace"))
        try:
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.render_operation()

    def on_key(self, event: Key) -> None:
        if self.in_operation:
            self.handle_operation_key(event)
            return
        self.handle_menu_key(event)

    def handle_operation_key(self, event: Key) -> None:
        log = self.query_one("#detail_log", RichLog)
        if event.key in {"up", "down", "pageup", "pagedown", "home", "end"}:
            self.operation_view = "detail"
            self.follow_detail = event.key == "end"
            log.auto_scroll = self.follow_detail
            if event.key == "up":
                log.action_scroll_up()
            elif event.key == "down":
                log.action_scroll_down()
            elif event.key == "pageup":
                log.action_page_up()
            elif event.key == "pagedown":
                log.action_page_down()
            elif event.key == "home":
                log.action_scroll_home()
            elif event.key == "end":
                log.action_scroll_end()
            self.render_operation()
            event.stop()
            return

        running = self.process is not None and self.process.returncode is None
        if not running:
            if event.key in {"q", "Q"}:
                self.exit(self.exit_code)
                event.stop()
            return

        if event.key == "enter":
            self.call_later(lambda: self.run_worker(self.send_stdin_line(), name="stdin-send", exit_on_error=False))
            event.stop()
            return
        if event.key == "backspace":
            self.state.stdin_buffer = self.state.stdin_buffer[:-1]
            self.render_operation()
            event.stop()
            return
        if event.character and len(event.character) == 1 and event.key not in {"left", "right"}:
            self.state.stdin_buffer += event.character
            self.render_operation()
            event.stop()

    def handle_menu_key(self, event: Key) -> None:
        if event.key in {"q", "Q"}:
            self.exit(self.exit_code)
            event.stop()
            return
        if event.character and event.character.isdigit():
            self.numeric_buffer += event.character
            self.select_numeric_buffer(force=False)
            event.stop()

    def commit_numeric_buffer(self) -> None:
        self.select_numeric_buffer(force=True)

    def numeric_targets(self) -> list[int | str]:
        return [target for target in self.selectable_ids if not (isinstance(target, str) and target.startswith("__"))]

    def select_numeric_buffer(self, *, force: bool) -> None:
        if not self.numeric_buffer:
            return
        if self.numeric_timer is not None:
            try:
                self.numeric_timer.stop()
            except Exception:
                pass
            self.numeric_timer = None
        if self.numeric_buffer == "0":
            self.numeric_buffer = ""
            self.activate_target("__quit__" if self.current_menu_id == -1 else "__back__")
            return
        try:
            wanted = int(self.numeric_buffer)
        except ValueError:
            self.numeric_buffer = ""
            return

        targets = self.numeric_targets()
        max_number = len(targets)
        has_longer_match = any(str(number).startswith(self.numeric_buffer) for number in range(1, max_number + 1) if str(number) != self.numeric_buffer)
        if not force and 1 <= wanted <= max_number and has_longer_match:
            self.numeric_timer = self.set_timer(0.45, self.commit_numeric_buffer)
            return

        if 1 <= wanted <= max_number:
            target = targets[wanted - 1]
            self.numeric_buffer = ""
            self.activate_target(target)
            return

        if not force and any(str(number).startswith(self.numeric_buffer) for number in range(1, max_number + 1)):
            self.numeric_timer = self.set_timer(0.45, self.commit_numeric_buffer)
            return

        self.query_one("#menu_status", Static).update(f"Raccourci numérique inconnu : {self.numeric_buffer}")
        self.numeric_buffer = ""


def dump_menu(path: Path) -> int:
    model = parse_menu_conf(path)
    print(f"MENU_CONF={path}")
    print(f"items={len(model.items)}")
    for item in model.items:
        parent = "root" if item.parent == -1 else str(item.parent)
        command = f" = {item.command}" if item.command else ""
        print(f"{item.item_id:03d} {item.item_type:<7} parent={parent:<4} {item.title}{command}")
    return 0


async def run_self_test(python_bin: str) -> int:
    with tempfile.TemporaryDirectory(prefix="yoleo-menu-test.") as tmp:
        tmp_path = Path(tmp)
        stacks_conf = tmp_path / "stacks.conf"
        stacks_conf.write_text(
            "\n".join(
                [
                    "STACK1=base",
                    "YML1=pi_hole.yml",
                    "STACK2=media",
                    "YML1=emby.yml",
                    "YML2=tvheadend.yml",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        plan = build_progress_plan(f"stacks.py --update --stacks-conf {shlex.quote(str(stacks_conf))}")
        assert plan.tracks_stack_dockers, "plan stacks Docker non créé"
        assert plan.total == 3, "total YAML non détecté"
        state = OperationState(total=plan.total)
        update_state_from_line(state, "[1/1] NAME: compose-interne", plan)
        assert state.current_index == 0 and state.total == 3, "faux compteur Compose non ignoré"
        update_state_from_line(state, "📂 Stack détectée : [media]", plan)
        assert state.current_index == 2 and state.current_name == "emby", "début de stack mal mappé"
        update_state_from_line(state, "  ✅ Stack [media] OK", plan)
        assert state.current_index == 3 and state.current_name == "tvheadend", "fin de stack mal mappée"
        update_state_from_line(state, "❌ Stacks erreur  : 0", plan)
        assert not state.had_error, "résumé zéro erreur détecté comme erreur"

        conf = tmp_path / "menu.conf"
        conf.write_text(
            "\n".join(
                [
                    "--- Test ---",
                    "Court = bash -lc 'echo court-ok'",
                    ",Sous-menu...",
                    ",Long = bash -lc 'for i in 1 2 3; do echo \"[$i/3] NOM : docker_$i\"; sleep 0.05; done'",
                    ",Erreur = bash -lc 'echo \"[1/1] NOM : casse\"; echo erreur >&2; exit 7'",
                    "--- Fin ---",
                    "Encore = bash -lc 'echo encore-ok'",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        app = YoleoMenuApp(
            menu_conf=conf,
            python_bin=python_bin,
            menu_title="YoLeo CLI Test",
            command_cwd=str(SCRIPT_DIR),
            test_mode=True,
        )
        async with app.run_test(headless=True, size=(90, 26)) as pilot:
            await pilot.pause()
            assert app.model.items, "configuration non lue"
            await pilot.press("enter")
            await pilot.pause()
            assert app.in_operation, "commande courte non lancée"
            while app.process is not None and app.process.returncode is None:
                await pilot.pause(0.05)
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert app.current_menu_id != -1, "sous-menu non ouvert"
            await pilot.press("1")
            await pilot.pause()
            assert app.in_operation, "commande longue non lancée"
            await pilot.press("right")
            await pilot.press("left")
            await pilot.press("right")
            await pilot.click("#tab_general")
            await pilot.pause()
            assert app.operation_view == "general", "clic Général inactif"
            await pilot.click("#tab_detail")
            await pilot.pause()
            assert app.operation_view == "detail", "clic Détail inactif"
            await pilot.click("#detail_log")
            await pilot.pause()
            assert app.operation_view == "detail", "clic log ne garde pas le détail"
            await pilot.press("home")
            await pilot.press("end")
            while app.process is not None and app.process.returncode is None:
                await pilot.pause(0.05)
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            while app.process is not None and app.process.returncode is None:
                await pilot.pause(0.05)
            assert app.state.return_code == 7, "erreur simulée non conservée"
            assert app.operation_view == "detail", "erreur ne bascule pas en détail"
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("0")
            await pilot.pause()
        small = YoleoMenuApp(
            menu_conf=conf,
            python_bin=python_bin,
            menu_title="YoLeo CLI Test Small",
            command_cwd=str(SCRIPT_DIR),
            test_mode=True,
        )
        async with small.run_test(headless=True, size=(48, 14)) as pilot:
            await pilot.pause()
            assert small.model.items, "petit terminal: configuration non lue"
        numeric_conf = tmp_path / "menu-numeric.conf"
        numeric_conf.write_text(
            "\n".join([f"Entrée {idx} = bash -lc 'echo numeric-{idx}'" for idx in range(1, 11)]) + "\n",
            encoding="utf-8",
        )
        numeric = YoleoMenuApp(
            menu_conf=numeric_conf,
            python_bin=python_bin,
            menu_title="YoLeo CLI Test Numeric",
            command_cwd=str(SCRIPT_DIR),
            test_mode=True,
        )
        async with numeric.run_test(headless=True, size=(90, 24)) as pilot:
            await pilot.pause()
            await pilot.press("1")
            await pilot.press("0")
            await pilot.pause()
            assert numeric.in_operation and numeric.state.title == "Entrée 10", "raccourci numérique 10 non lancé"
            while numeric.process is not None and numeric.process.returncode is None:
                await pilot.pause(0.05)
            await pilot.press("escape")
            await pilot.pause()
    print("self-test OK")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Application terminale YoLeo")
    parser.add_argument("--menu-conf", default=os.environ.get("MENU_CONF", str(DEFAULT_MENU_CONF)))
    parser.add_argument("--title", default=os.environ.get("MENU_TITLE", "YoLeo CLI"))
    parser.add_argument(
        "--python-bin",
        default=os.environ.get(
            "PYTHON_BIN",
            DEFAULT_PYTHON if Path(DEFAULT_PYTHON).is_file() else sys.executable,
        ),
    )
    parser.add_argument("--dump-menu", action="store_true", help="Affiche le menu parsé puis quitte.")
    parser.add_argument("--self-test", action="store_true", help="Lance les tests Textual headless.")
    parser.add_argument("--internal-run", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    menu_conf = Path(args.menu_conf).expanduser().resolve()
    python_bin = str(Path(args.python_bin).expanduser())

    if args.internal_run is not None:
        return run_internal_action(args.internal_run)
    if args.dump_menu:
        return dump_menu(menu_conf)
    if args.self_test:
        return asyncio.run(run_self_test(python_bin))

    app = YoleoMenuApp(
        menu_conf=menu_conf,
        python_bin=python_bin,
        menu_title=args.title,
        command_cwd=os.getcwd(),
    )
    result = app.run()
    return int(result or 0)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        try:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
        except Exception:
            pass
        raise SystemExit(130)
