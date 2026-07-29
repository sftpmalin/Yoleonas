#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Interface de secours LAN YoLeo, autonome sans scripts shell externes."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.events import Click, Key
from textual.widgets import OptionList, RichLog, Static
from textual.widgets.option_list import Option


SCRIPT_PATH = Path(__file__).resolve()
SCRIPT_DIR = SCRIPT_PATH.parent
VENV_PYTHON = SCRIPT_DIR / ".venv" / "bin" / "python"
SERVICE_PYTHON = str(VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable).resolve())

LIBVIRT_URI = os.environ.get("LIBVIRT_URI", "qemu:///system")
BR0_STATE_DIR = Path("/root/.BR0")
BR0_BACKUP_ROOT = BR0_STATE_DIR / "backups"
BR0_STATE_FILE = BR0_STATE_DIR / "state.conf"
INTERFACES_FILE = Path("/etc/network/interfaces")
RESOLV_CONF = Path("/etc/resolv.conf")

DOCKER_NETWORK = os.environ.get("DOCKER_NETWORK", "br0")
DOCKER_PARENT = os.environ.get("PARENT_IF", "br0")
SHIM_IF = os.environ.get("SHIM_IF", "mv-host")
SHIM_IP_CIDR = os.environ.get("SHIM_IP_CIDR", "192.168.1.253/32")
MACVLAN_MODE = os.environ.get("MACVLAN_MODE", "bridge")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "yoleo-macvlan-host.service")
DOCKER_LAN_STATE_FILE = Path(os.environ.get("DOCKER_LAN_STATE_FILE", str(SCRIPT_DIR.parent / "conf" / "docker_lan.json")))


@dataclass
class ActionResult:
    ok: bool
    output: str = ""
    rc: int = 0


@dataclass
class LanAction:
    id: str
    group: str
    label: str
    description: str
    runner: Callable[[], ActionResult]
    danger: bool = False


@dataclass
class InterfaceRow:
    name: str
    state: str = ""
    kind: str = ""
    mac: str = ""
    ipv4: str = ""
    master: str = ""
    gateway: str = ""
    default_route: bool = False
    bridge_ports: list[str] = field(default_factory=list)


@dataclass
class LanSnapshot:
    interfaces: list[InterfaceRow] = field(default_factory=list)
    mode: str = "inconnu"
    default_iface: str = ""
    gateway: str = ""
    docker_info: str = ""
    docker_ok: bool = False
    libvirt_info: str = ""
    libvirt_ok: bool = False
    shim_info: str = ""
    shim_ok: bool = False
    errors: list[str] = field(default_factory=list)
    generated_at: str = ""


class LanError(RuntimeError):
    pass


def run_cmd(argv: list[str], timeout: int = 25, env: Optional[dict[str, str]] = None) -> tuple[int, str]:
    full_env = os.environ.copy()
    full_env.update({"LC_ALL": "C", "LANG": "C", "LANGUAGE": "C"})
    if env:
        full_env.update(env)
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=full_env,
        )
        return proc.returncode, proc.stdout or ""
    except subprocess.TimeoutExpired:
        return 124, f"Timeout apres {timeout}s : {shlex.join(argv)}"
    except FileNotFoundError:
        return 127, f"Commande introuvable : {argv[0]}"
    except Exception as exc:
        return 1, f"Exception Python : {exc}"


def checked(argv: list[str], timeout: int = 25, ignore_error: bool = False) -> str:
    rc, out = run_cmd(argv, timeout=timeout)
    text = f"$ {shlex.join(argv)}\n{out}".rstrip()
    if rc != 0 and not ignore_error:
        raise LanError(text + f"\nCode retour {rc}")
    return text


def action_wrap(fn: Callable[[], str]) -> ActionResult:
    try:
        return ActionResult(True, fn(), 0)
    except LanError as exc:
        return ActionResult(False, str(exc), 1)
    except Exception as exc:
        return ActionResult(False, f"Exception Python : {exc}", 1)


def need_root() -> None:
    if os.geteuid() != 0:
        raise LanError("Lance cette action en root.")


def need_cmd(cmd: str, package: str = "") -> None:
    if shutil.which(cmd):
        return
    msg = f"Commande introuvable : {cmd}"
    if package:
        msg += f"\nPaquet utile : apt install -y {package}"
    raise LanError(msg)


def read_json_cmd(argv: list[str], timeout: int = 10) -> tuple[object, str]:
    rc, out = run_cmd(argv, timeout=timeout)
    if rc != 0:
        return [], out.strip()
    try:
        return json.loads(out or "[]"), ""
    except json.JSONDecodeError as exc:
        return [], f"JSON invalide pour {shlex.join(argv)} : {exc}"


def read_sysfs(iface: str, name: str) -> str:
    try:
        return (Path("/sys/class/net") / iface / name).read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""


def sysfs_master(iface: str) -> str:
    path = Path("/sys/class/net") / iface / "master"
    try:
        if path.is_symlink():
            return Path(os.readlink(path)).name
    except Exception:
        return ""
    return ""


def is_bridge(iface: str) -> bool:
    return bool(iface) and (Path("/sys/class/net") / iface / "bridge").exists()


def bridge_ports(iface: str) -> list[str]:
    brif = Path("/sys/class/net") / iface / "brif"
    if not brif.exists():
        return []
    try:
        return sorted(item.name for item in brif.iterdir())
    except Exception:
        return []


def link_kind(link: dict) -> str:
    info = link.get("linkinfo") or {}
    kind = str(info.get("info_kind") or "")
    if kind:
        return kind
    name = str(link.get("ifname") or "")
    if is_bridge(name):
        return "bridge"
    if name == "lo":
        return "loopback"
    if "/devices/virtual/" in os.path.realpath(f"/sys/class/net/{name}"):
        return "virtual"
    return "ethernet"


def first_ipv4(link: dict) -> str:
    for item in link.get("addr_info") or []:
        if item.get("family") == "inet" and item.get("local") and item.get("prefixlen") is not None:
            return f"{item['local']}/{item['prefixlen']}"
    return ""


def default_route() -> tuple[str, str, str]:
    data, error = read_json_cmd(["ip", "-j", "route", "show", "default"], timeout=8)
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                return str(item.get("dev") or ""), str(item.get("gateway") or ""), error
    return "", "", error


def route_get_iface() -> str:
    rc, out = run_cmd(["ip", "-4", "route", "get", "1.1.1.1"], timeout=8)
    if rc == 0:
        parts = out.split()
        for index, part in enumerate(parts):
            if part == "dev" and index + 1 < len(parts):
                return parts[index + 1]
    iface, _gateway, _error = default_route()
    return iface


def iface_ipv4(iface: str) -> str:
    rc, out = run_cmd(["ip", "-4", "-o", "addr", "show", "dev", iface, "scope", "global"], timeout=8)
    if rc != 0:
        return ""
    parts = out.split()
    return parts[3] if len(parts) >= 4 else ""


def dns_servers(gateway: str = "") -> list[str]:
    out: list[str] = []
    try:
        for raw in RESOLV_CONF.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = raw.split()
            if len(parts) >= 2 and parts[0] == "nameserver":
                out.append(parts[1])
    except Exception:
        pass
    if out:
        return out
    return [gateway, "1.1.1.1"] if gateway else ["1.1.1.1", "8.8.8.8"]


def physical_from_route_dev(dev: str) -> str:
    if not dev:
        return ""
    if is_bridge(dev):
        ports = bridge_ports(dev)
        return ports[0] if ports else ""
    return dev


def detect_br0_source() -> dict[str, str]:
    dev = route_get_iface()
    gateway_dev, gateway, _error = default_route()
    if not gateway and gateway_dev == dev:
        _d, gateway, _e = default_route()
    if not dev:
        raise LanError("Aucune interface avec route par défaut détectée.")
    phy = physical_from_route_dev(dev)
    if not phy or not Path("/sys/class/net", phy).exists():
        raise LanError(f"Impossible de retrouver la carte physique depuis {dev}.")
    mac = read_sysfs(phy, "address")
    ip_cidr = iface_ipv4(dev) or (iface_ipv4(phy) if phy != dev else "")
    if not gateway:
        gateway = "192.168.1.254"
    return {
        "dev": dev,
        "phy": phy,
        "mac": mac,
        "ip_cidr": ip_cidr,
        "gateway": gateway,
        "dns": " ".join(dns_servers(gateway)),
    }


def collect_snapshot() -> LanSnapshot:
    snap = LanSnapshot(generated_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    default_iface, gateway, route_error = default_route()
    if route_error:
        snap.errors.append(route_error)
    snap.default_iface = default_iface
    snap.gateway = gateway

    data, link_error = read_json_cmd(["ip", "-j", "-d", "addr", "show"], timeout=10)
    if link_error:
        snap.errors.append(link_error)
    if isinstance(data, list):
        for link in data:
            if not isinstance(link, dict):
                continue
            name = str(link.get("ifname") or "")
            if not name or name == "lo":
                continue
            row = InterfaceRow(
                name=name,
                state=str(link.get("operstate") or read_sysfs(name, "operstate") or "unknown").lower(),
                kind=link_kind(link),
                mac=read_sysfs(name, "address") or str(link.get("address") or ""),
                ipv4=first_ipv4(link),
                master=sysfs_master(name),
                gateway=gateway if name == default_iface else "",
                default_route=name == default_iface,
                bridge_ports=bridge_ports(name),
            )
            snap.interfaces.append(row)

    snap.interfaces.sort(key=lambda row: (0 if row.default_route else 1, 0 if row.kind == "bridge" else 1, row.name))
    if default_iface:
        snap.mode = "bridge" if is_bridge(default_iface) else "direct"
    snap.docker_ok, snap.docker_info = docker_br0_status()
    snap.libvirt_ok, snap.libvirt_info = vm_br0_status()
    snap.shim_ok, snap.shim_info = docker_host_status()
    return snap


def fix_bridge_netfilter() -> str:
    need_root()
    Path("/etc/modules-load.d").mkdir(parents=True, exist_ok=True)
    Path("/etc/sysctl.d").mkdir(parents=True, exist_ok=True)
    Path("/etc/modules-load.d/yoleo-br-netfilter.conf").write_text("br_netfilter\n", encoding="utf-8")
    Path("/etc/sysctl.d/99-yoleo-bridge-vm.conf").write_text(
        "# YoLeo: garder br0 en switch L2 pour Docker et VM.\n"
        "net.bridge.bridge-nf-call-iptables = 0\n"
        "net.bridge.bridge-nf-call-ip6tables = 0\n",
        encoding="utf-8",
    )
    outputs = [
        checked(["modprobe", "br_netfilter"], timeout=15, ignore_error=True),
        checked(["sysctl", "-w", "net.bridge.bridge-nf-call-iptables=0"], timeout=15, ignore_error=True),
        checked(["sysctl", "-w", "net.bridge.bridge-nf-call-ip6tables=0"], timeout=15, ignore_error=True),
    ]
    return "\n".join(x for x in outputs if x.strip())


def backup_network_config() -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = BR0_BACKUP_ROOT / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    if INTERFACES_FILE.exists():
        shutil.copy2(INTERFACES_FILE, backup_dir / "interfaces")
    interfaces_d = Path("/etc/network/interfaces.d")
    if interfaces_d.exists():
        shutil.copytree(interfaces_d, backup_dir / "interfaces.d", dirs_exist_ok=True)
    if RESOLV_CONF.exists():
        try:
            shutil.copy2(RESOLV_CONF, backup_dir / "resolv.conf")
        except Exception:
            pass
    return backup_dir


def write_br0_state(info: dict[str, str], backup_dir: Path) -> None:
    BR0_STATE_DIR.mkdir(parents=True, exist_ok=True)
    BR0_STATE_FILE.write_text(
        "\n".join(
            [
                f'PHY="{info["phy"]}"',
                f'DEV_BEFORE="{info["dev"]}"',
                f'MAC="{info["mac"]}"',
                f'IP_CIDR="{info["ip_cidr"]}"',
                f'GW="{info["gateway"]}"',
                f'DNS="{info["dns"]}"',
                f'BACKUP_DIR="{backup_dir}"',
                f'INSTALLED_AT="{time.strftime("%Y-%m-%dT%H:%M:%S%z")}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    BR0_STATE_FILE.chmod(0o600)


def read_br0_state() -> dict[str, str]:
    data: dict[str, str] = {}
    if not BR0_STATE_FILE.exists():
        return data
    for raw in BR0_STATE_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        data[key.strip()] = value.strip().strip("'\"")
    return data


def disable_interfaces_d() -> None:
    interfaces_d = Path("/etc/network/interfaces.d")
    interfaces_d.mkdir(parents=True, exist_ok=True)
    for item in interfaces_d.iterdir():
        if not item.is_file() or item.name.endswith(".disabled-by-BR0"):
            continue
        item.rename(item.with_name(item.name + ".disabled-by-BR0"))


def write_br0_config(info: dict[str, str], backup_dir: Path) -> None:
    if not info.get("ip_cidr"):
        raise LanError("IP actuelle non détectée. Impossible de créer une config statique propre.")
    INTERFACES_FILE.write_text(
        f"""# Généré par lan_cli.py BR0 -install
# Sauvegarde précédente: {backup_dir}

auto lo
iface lo inet loopback

# Carte physique: simple port du bridge, sans IP.
auto {info["phy"]}
iface {info["phy"]} inet manual

# br0: interface principale de Linux.
# La MAC est forcée sur la MAC physique pour éviter un changement DHCP.
auto br0
iface br0 inet static
    address {info["ip_cidr"]}
    gateway {info["gateway"]}
    dns-nameservers {info["dns"]}
    bridge_ports {info["phy"]}
    bridge_stp off
    bridge_fd 0
    bridge_maxwait 0
    hwaddress ether {info["mac"]}
""",
        encoding="utf-8",
    )


def br0_status_action() -> str:
    lines = ["=== Détection réseau ==="]
    try:
        info = detect_br0_source()
        lines.extend(
            [
                f"Interface route défaut : {info['dev']}",
                f"Interface physique     : {info['phy']}",
                f"MAC physique           : {info['mac']}",
                f"IP actuelle            : {info['ip_cidr'] or 'non détectée'}",
                f"Gateway                : {info['gateway']}",
                f"DNS                    : {info['dns']}",
            ]
        )
    except Exception as exc:
        lines.append(f"Détection partielle : {exc}")
    for title, cmd in (
        ("Interfaces", ["ip", "-br", "link", "show"]),
        ("IP", ["ip", "-br", "-4", "addr", "show"]),
        ("Routes", ["ip", "-4", "route", "show"]),
        ("Bridge", ["bridge", "link", "show"]),
    ):
        rc, out = run_cmd(cmd, timeout=10)
        lines.extend(["", f"=== {title} ===", out.strip() if out.strip() else f"rc={rc}"])
    lines.extend(["", "=== Etat BR0 ==="])
    if BR0_STATE_FILE.exists():
        lines.append(f"State file: {BR0_STATE_FILE}")
        lines.append(BR0_STATE_FILE.read_text(encoding="utf-8", errors="replace").strip())
    else:
        lines.append("Aucun état BR0 enregistré.")
    return "\n".join(lines)


def br0_install_action() -> str:
    need_root()
    need_cmd("ip", "iproute2")
    need_cmd("apt", "apt")
    info = detect_br0_source()
    outputs = [
        "Détection:",
        f"  Interface actuelle : {info['dev']}",
        f"  Carte physique     : {info['phy']}",
        f"  MAC physique       : {info['mac']}",
        f"  IP                 : {info['ip_cidr'] or 'non détectée'}",
        f"  Gateway            : {info['gateway']}",
        f"  DNS                : {info['dns']}",
        checked(["apt", "update"], timeout=900),
        checked(["apt", "install", "-y", "ifupdown", "bridge-utils", "iproute2"], timeout=900),
    ]
    backup_dir = backup_network_config()
    outputs.append(f"Sauvegarde créée: {backup_dir}")
    disable_interfaces_d()
    outputs.append(checked(["modprobe", "bridge"], timeout=15, ignore_error=True))
    outputs.append(fix_bridge_netfilter())
    write_br0_config(info, backup_dir)
    write_br0_state(info, backup_dir)
    outputs.append("Configuration br0 écrite dans /etc/network/interfaces")
    outputs.append(f"br0 prendra la MAC: {info['mac']}")
    outputs.append("Redémarrage demandé.")
    outputs.append(checked(["reboot"], timeout=10, ignore_error=True))
    return "\n".join(x for x in outputs if x.strip())


def restore_backup_if_possible() -> str:
    state = read_br0_state()
    backup_dir = Path(state.get("BACKUP_DIR", ""))
    if backup_dir and (backup_dir / "interfaces").exists():
        shutil.copy2(backup_dir / "interfaces", INTERFACES_FILE)
        interfaces_d = Path("/etc/network/interfaces.d")
        if (backup_dir / "interfaces.d").exists():
            if interfaces_d.exists():
                shutil.rmtree(interfaces_d)
            shutil.copytree(backup_dir / "interfaces.d", interfaces_d)
        else:
            interfaces_d.mkdir(parents=True, exist_ok=True)
        if (backup_dir / "resolv.conf").exists():
            try:
                shutil.copy2(backup_dir / "resolv.conf", RESOLV_CONF)
            except Exception:
                pass
        return f"Configuration d'origine restaurée depuis {backup_dir}."
    return ""


def write_classic_config_from_state_or_detect() -> str:
    state = read_br0_state()
    if state:
        phy = state.get("PHY", "")
        mac = state.get("MAC", "")
        ip_cidr = state.get("IP_CIDR", "")
        gateway = state.get("GW", "")
        dns = state.get("DNS", "")
    else:
        info = detect_br0_source()
        phy = info["phy"]
        mac = info["mac"]
        ip_cidr = info["ip_cidr"]
        gateway = info["gateway"]
        dns = info["dns"]
    if not phy or not ip_cidr:
        raise LanError("Impossible de déterminer la carte physique/IP à restaurer.")
    INTERFACES_FILE.write_text(
        f"""# Généré par lan_cli.py BR0 -remove
# Retour réseau classique: IP directement sur la carte physique.

auto lo
iface lo inet loopback

auto {phy}
iface {phy} inet static
    address {ip_cidr}
    gateway {gateway}
    dns-nameservers {dns}
    hwaddress ether {mac}
""",
        encoding="utf-8",
    )
    Path("/etc/network/interfaces.d").mkdir(parents=True, exist_ok=True)
    return "Configuration classique régénérée dans /etc/network/interfaces."


def br0_remove_action() -> str:
    need_root()
    outputs = []
    restored = restore_backup_if_possible()
    outputs.append(restored or write_classic_config_from_state_or_detect())
    if BR0_STATE_FILE.exists():
        moved = BR0_STATE_FILE.with_name(BR0_STATE_FILE.name + ".removed-" + time.strftime("%Y%m%d-%H%M%S"))
        BR0_STATE_FILE.rename(moved)
        outputs.append(f"State BR0 archivé: {moved}")
    outputs.append("Mode réseau classique restauré. Redémarrage demandé.")
    outputs.append(checked(["reboot"], timeout=10, ignore_error=True))
    return "\n".join(outputs)


def docker_br0_status() -> tuple[bool, str]:
    rc, out = run_cmd(
        [
            "docker",
            "network",
            "inspect",
            DOCKER_NETWORK,
            "--format",
            "Nom={{.Name}}\nDriver={{.Driver}}\nParent={{index .Options \"parent\"}}\nMode={{index .Options \"ipvlan_mode\"}}{{index .Options \"macvlan_mode\"}}\nSubnet={{(index .IPAM.Config 0).Subnet}}\nGateway={{(index .IPAM.Config 0).Gateway}}\nContainers={{len .Containers}}",
        ],
        timeout=12,
    )
    return rc == 0, out.strip() or f"Réseau Docker absent : {DOCKER_NETWORK}"


def docker_network_exists(name: str = DOCKER_NETWORK) -> bool:
    rc, _out = run_cmd(["docker", "network", "inspect", name], timeout=12)
    return rc == 0


def docker_network_containers(name: str = DOCKER_NETWORK) -> list[tuple[str, str]]:
    rc, out = run_cmd(["docker", "network", "inspect", "-f", "{{range .Containers}}{{println .Name .IPv4Address}}{{end}}", name], timeout=12)
    rows: list[tuple[str, str]] = []
    if rc != 0:
        return rows
    for raw in out.splitlines():
        parts = raw.split()
        if len(parts) >= 2:
            rows.append((parts[0], parts[1].split("/", 1)[0]))
    return rows


def docker_detect_gateway(iface: str) -> str:
    rc, out = run_cmd(["ip", "-4", "route", "show", "default", "dev", iface], timeout=8)
    if rc != 0 or not out.strip():
        rc, out = run_cmd(["ip", "-4", "route", "show", "default"], timeout=8)
    parts = out.split()
    for index, part in enumerate(parts):
        if part == "via" and index + 1 < len(parts):
            return parts[index + 1]
    raise LanError("Gateway introuvable.")


def docker_lan_config_entry(name: str = DOCKER_NETWORK) -> dict[str, str]:
    try:
        data = json.loads(DOCKER_LAN_STATE_FILE.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    networks = data.get("networks") if isinstance(data, dict) else {}
    item = networks.get(name) if isinstance(networks, dict) else {}
    if not isinstance(item, dict):
        return {}
    return {str(key): str(value) for key, value in item.items() if value is not None}


def docker_install_action(parent: str = DOCKER_PARENT) -> str:
    need_root()
    need_cmd("ip", "iproute2")
    need_cmd("docker", "docker.io")
    entry = docker_lan_config_entry(DOCKER_NETWORK)
    if entry:
        parent = entry.get("parent") or parent
    if parent == "auto":
        parent = route_get_iface()
    if not parent or run_cmd(["ip", "link", "show", parent], timeout=8)[0] != 0:
        raise LanError(f"Interface introuvable : {parent}")

    cidr = iface_ipv4(parent)
    subnet = entry.get("subnet", "").strip()
    gateway = entry.get("gateway", "").strip()
    if not subnet:
        if not cidr:
            raise LanError(f"Aucune IPv4 globale trouvée sur {parent}.")
        subnet = str(ipaddress.ip_interface(cidr).network)
    if not gateway:
        gateway = docker_detect_gateway(parent)

    driver = (entry.get("driver") or "ipvlan").strip().lower()
    if driver not in {"macvlan", "ipvlan"}:
        driver = "ipvlan"
    mode = (entry.get("mode") or ("bridge" if driver == "macvlan" else "l2")).strip().lower()
    if driver == "macvlan":
        option_name = "macvlan_mode"
        if mode not in {"bridge", "private", "vepa", "passthru"}:
            mode = "bridge"
    else:
        option_name = "ipvlan_mode"
        if mode not in {"l2", "l3"}:
            mode = "l2"

    create_cmd = [
        "docker",
        "network",
        "create",
        "-d",
        driver,
        f"--subnet={subnet}",
        f"--gateway={gateway}",
    ]
    ip_range = entry.get("ip_range", "").strip()
    if ip_range:
        create_cmd.append(f"--ip-range={ip_range}")
    create_cmd += ["-o", f"parent={parent}", "-o", f"{option_name}={mode}", DOCKER_NETWORK]

    outputs = [
        fix_bridge_netfilter(),
        "Création réseau Docker br0",
        f"Nom réseau      : {DOCKER_NETWORK}",
        f"Interface       : {parent}",
        f"IP interface    : {cidr or '-'}",
        f"Subnet Docker   : {subnet}",
        f"Gateway Docker  : {gateway}",
        f"Driver          : {driver}",
        f"Mode            : {mode}",
        f"Source conf     : {DOCKER_LAN_STATE_FILE if entry else 'détection runtime'}",
    ]
    if docker_network_exists(DOCKER_NETWORK):
        ok, status = docker_br0_status()
        outputs.append("INFO : le réseau existe déjà.")
        outputs.append(status)
        return "\n".join(outputs)
    outputs.append(checked(create_cmd, timeout=120))
    outputs.append(docker_br0_status()[1])
    return "\n".join(x for x in outputs if x.strip())


def docker_remove_action() -> str:
    need_root()
    need_cmd("docker", "docker.io")
    if not docker_network_exists(DOCKER_NETWORK):
        rc, out = run_cmd(["docker", "network", "ls"], timeout=12)
        return f"INFO : réseau Docker absent : {DOCKER_NETWORK}\n{out if rc == 0 else ''}".strip()
    containers = docker_network_containers(DOCKER_NETWORK)
    if containers:
        rows = "\n".join(f"{name}\t{ip}" for name, ip in containers)
        raise LanError(f"Le réseau {DOCKER_NETWORK} est utilisé par des conteneurs.\n{rows}")
    return checked(["docker", "network", "rm", DOCKER_NETWORK], timeout=60)


def docker_recreate_action() -> str:
    outputs = [docker_remove_action(), docker_install_action(DOCKER_PARENT)]
    return "\n\n".join(outputs)


def docker_host_status() -> tuple[bool, str]:
    rc_ip, ip_out = run_cmd(["ip", "-br", "addr", "show", SHIM_IF], timeout=8)
    rc_svc, svc_out = run_cmd(["systemctl", "is-active", SERVICE_NAME], timeout=8)
    rc_routes, routes = run_cmd(["ip", "-4", "route", "show", "dev", SHIM_IF], timeout=8)
    parts = [(ip_out or f"{SHIM_IF} absent").strip(), f"service: {(svc_out or '').strip() or 'inconnu'}"]
    if rc_routes == 0 and routes.strip():
        parts.append("routes:\n" + routes.strip())
    rows = docker_network_containers(DOCKER_NETWORK)
    if rows:
        parts.append("conteneurs:\n" + "\n".join(f"{name}\t{ip}" for name, ip in rows))
    return rc_ip == 0 and rc_svc == 0 and "active" in svc_out, "\n".join(parts)


def ensure_docker_host_base() -> None:
    need_root()
    need_cmd("ip", "iproute2")
    need_cmd("docker", "docker.io")
    if run_cmd(["ip", "link", "show", DOCKER_PARENT], timeout=8)[0] != 0:
        raise LanError(f"Interface parente introuvable : {DOCKER_PARENT}")
    if not docker_network_exists(DOCKER_NETWORK):
        raise LanError(f"Réseau Docker introuvable : {DOCKER_NETWORK}")


def ensure_docker_host_shim() -> list[str]:
    outputs: list[str] = []
    if run_cmd(["ip", "link", "show", SHIM_IF], timeout=8)[0] != 0:
        outputs.append(checked(["ip", "link", "add", SHIM_IF, "link", DOCKER_PARENT, "type", "macvlan", "mode", MACVLAN_MODE], timeout=20))
    outputs.append(checked(["ip", "-4", "addr", "flush", "dev", SHIM_IF], timeout=20, ignore_error=True))
    outputs.append(checked(["ip", "addr", "add", SHIM_IP_CIDR, "dev", SHIM_IF], timeout=20))
    outputs.append(checked(["ip", "link", "set", SHIM_IF, "up"], timeout=20))
    return outputs


def clear_docker_host_routes() -> str:
    rc, out = run_cmd(["ip", "-4", "route", "show", "dev", SHIM_IF], timeout=8)
    outputs: list[str] = []
    if rc == 0:
        for raw in out.splitlines():
            route = raw.split()[0] if raw.split() else ""
            if route and route != "default":
                outputs.append(checked(["ip", "route", "del", route, "dev", SHIM_IF], timeout=15, ignore_error=True))
    return "\n".join(x for x in outputs if x.strip())


def docker_host_up_action() -> str:
    ensure_docker_host_base()
    outputs = ensure_docker_host_shim()
    clear = clear_docker_host_routes()
    if clear:
        outputs.append(clear)
    rows = docker_network_containers(DOCKER_NETWORK)
    if not rows:
        outputs.append(f"OK: {SHIM_IF} est prêt en {SHIM_IP_CIDR}, mais aucun conteneur n'est attaché à {DOCKER_NETWORK}.")
        return "\n".join(outputs)
    for _name, ip in rows:
        if ip:
            outputs.append(checked(["ip", "route", "replace", f"{ip}/32", "dev", SHIM_IF], timeout=15))
    outputs.append(f"OK: hôte <-> Docker actif via {SHIM_IF} ({SHIM_IP_CIDR}).")
    outputs.append(docker_host_status()[1])
    return "\n".join(x for x in outputs if x.strip())


def docker_host_down_action() -> str:
    need_root()
    need_cmd("ip", "iproute2")
    if run_cmd(["ip", "link", "show", SHIM_IF], timeout=8)[0] == 0:
        return checked(["ip", "link", "del", SHIM_IF], timeout=30)
    return f"OK: {SHIM_IF} n'existe pas."


def docker_host_install_systemd_action() -> str:
    need_root()
    need_cmd("systemctl", "systemd")
    service_path = Path("/etc/systemd/system") / SERVICE_NAME
    service_path.write_text(
        f"""[Unit]
Description=Yoleo macvlan host access shim
After=network-online.target docker.service
Wants=network-online.target docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=DOCKER_NETWORK={DOCKER_NETWORK}
Environment=PARENT_IF={DOCKER_PARENT}
Environment=SHIM_IF={SHIM_IF}
Environment=SHIM_IP_CIDR={SHIM_IP_CIDR}
Environment=MACVLAN_MODE={MACVLAN_MODE}
Environment=SERVICE_NAME={SERVICE_NAME}
ExecStart={SERVICE_PYTHON} {SCRIPT_PATH} --action docker-host-up
ExecStop={SERVICE_PYTHON} {SCRIPT_PATH} --action docker-host-down

[Install]
WantedBy=multi-user.target
""",
        encoding="utf-8",
    )
    outputs = [
        f"Service écrit : {service_path}",
        checked(["systemctl", "daemon-reload"], timeout=60),
        checked(["systemctl", "enable", SERVICE_NAME], timeout=60),
        checked(["systemctl", "start", SERVICE_NAME], timeout=120),
        checked(["systemctl", "status", SERVICE_NAME, "--no-pager", "-l"], timeout=60, ignore_error=True),
    ]
    return "\n".join(x for x in outputs if x.strip())


def docker_host_test_action() -> str:
    ensure_docker_host_base()
    need_cmd("ping", "iputils-ping")
    rows = docker_network_containers(DOCKER_NETWORK)
    if not rows:
        raise LanError(f"Aucun conteneur à tester sur {DOCKER_NETWORK}.")
    outputs = ["Test hôte -> conteneurs:"]
    for name, ip in rows:
        outputs.append(f"== {name} {ip} ==")
        outputs.append(checked(["ping", "-c", "1", "-W", "1", ip], timeout=5, ignore_error=True))
    return "\n".join(outputs)


def detect_vm_bridge() -> str:
    if run_cmd(["ip", "link", "show", "br0"], timeout=8)[0] == 0 and is_bridge("br0") and iface_ipv4("br0"):
        return "br0"
    iface = route_get_iface()
    if iface and is_bridge(iface):
        return iface
    rc, out = run_cmd(["ip", "-br", "-4", "addr"], timeout=8)
    if rc == 0:
        for raw in out.splitlines():
            parts = raw.split()
            if len(parts) < 3:
                continue
            name = parts[0].split("@", 1)[0]
            if name in {"docker0", "virbr0", "lxcbr0"}:
                continue
            if parts[1] == "UP" and is_bridge(name):
                return name
    raise LanError("Aucun vrai bridge LAN détecté. Etat attendu : br0 UP avec IPv4, carte physique master br0.")


def vm_net_exists(name: str = "BR0") -> bool:
    return run_cmd(["virsh", "-c", LIBVIRT_URI, "net-info", name], timeout=15)[0] == 0


def vm_net_active(name: str = "BR0") -> bool:
    rc, out = run_cmd(["virsh", "-c", LIBVIRT_URI, "net-info", name], timeout=15)
    return rc == 0 and any("Active:" in line and "yes" in line.lower() for line in out.splitlines())


def vm_net_uses_bridge(name: str, bridge_name: str) -> bool:
    rc, out = run_cmd(["virsh", "-c", LIBVIRT_URI, "net-dumpxml", name], timeout=15)
    return rc == 0 and (f"<bridge name='{bridge_name}'" in out or f'<bridge name="{bridge_name}"' in out)


def vm_write_xml(net_name: str, bridge_name: str) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix=f"{net_name}-", suffix=".xml")
    os.close(fd)
    path = Path(raw_path)
    path.write_text(
        f"""<network>
  <name>{net_name}</name>
  <forward mode='bridge'/>
  <bridge name='{bridge_name}'/>
</network>
""",
        encoding="utf-8",
    )
    return path


def vm_br0_status() -> tuple[bool, str]:
    rc_info, info = run_cmd(["virsh", "-c", LIBVIRT_URI, "net-info", "BR0"], timeout=15)
    rc_xml, xml = run_cmd(["virsh", "-c", LIBVIRT_URI, "net-dumpxml", "BR0"], timeout=15)
    text = (info or "").strip()
    if rc_xml == 0 and xml.strip():
        for raw in xml.splitlines():
            line = raw.strip()
            if line.startswith("<bridge "):
                text += "\n" + line
                break
    return rc_info == 0, text or "Réseau libvirt BR0 absent."


def vm_br0_install_action(net_name: str = "BR0", bridge_name: str = "auto") -> str:
    need_root()
    need_cmd("ip", "iproute2")
    need_cmd("bridge", "iproute2")
    need_cmd("virsh", "libvirt-clients")
    outputs = [fix_bridge_netfilter()]
    if bridge_name == "auto":
        bridge_name = detect_vm_bridge()
    if run_cmd(["ip", "link", "show", bridge_name], timeout=8)[0] != 0:
        raise LanError(f"Bridge introuvable : {bridge_name}")
    if not is_bridge(bridge_name):
        raise LanError(f"L'interface détectée n'est pas un bridge Linux : {bridge_name}")
    outputs.extend(
        [
            "Création réseau libvirt VM LAN via vrai bridge",
            f"Nom réseau libvirt : {net_name}",
            f"Bridge Linux       : {bridge_name}",
            f"IPv4 bridge        : {iface_ipv4(bridge_name) or 'aucune'}",
            "Ports du bridge    : " + (", ".join(bridge_ports(bridge_name)) or "-"),
        ]
    )
    if vm_net_exists(net_name):
        if not vm_net_uses_bridge(net_name, bridge_name):
            outputs.append("Réseau existant mais mauvais bridge : remplacement.")
            outputs.append(checked(["virsh", "-c", LIBVIRT_URI, "net-destroy", net_name], timeout=60, ignore_error=True))
            outputs.append(checked(["virsh", "-c", LIBVIRT_URI, "net-undefine", net_name], timeout=60, ignore_error=True))
            xml = vm_write_xml(net_name, bridge_name)
            outputs.append(checked(["virsh", "-c", LIBVIRT_URI, "net-define", str(xml)], timeout=60))
        else:
            outputs.append(f"INFO : le réseau {net_name} existe déjà et utilise {bridge_name}.")
    else:
        xml = vm_write_xml(net_name, bridge_name)
        outputs.append(checked(["virsh", "-c", LIBVIRT_URI, "net-define", str(xml)], timeout=60))
    if not vm_net_active(net_name):
        outputs.append(checked(["virsh", "-c", LIBVIRT_URI, "net-start", net_name], timeout=60))
    outputs.append(checked(["virsh", "-c", LIBVIRT_URI, "net-autostart", net_name], timeout=60))
    outputs.append(vm_br0_status()[1])
    return "\n".join(x for x in outputs if x.strip())


def vm_br0_remove_action(net_name: str = "BR0") -> str:
    need_root()
    need_cmd("virsh", "libvirt-clients")
    outputs = [f"Suppression réseau libvirt : {net_name}"]
    if vm_net_exists(net_name):
        outputs.append(checked(["virsh", "-c", LIBVIRT_URI, "net-destroy", net_name], timeout=60, ignore_error=True))
        outputs.append(checked(["virsh", "-c", LIBVIRT_URI, "net-undefine", net_name], timeout=60, ignore_error=True))
        outputs.append(f"OK : réseau supprimé : {net_name}")
    else:
        outputs.append(f"INFO : réseau introuvable : {net_name}")
    outputs.append(checked(["virsh", "-c", LIBVIRT_URI, "net-list", "--all"], timeout=60, ignore_error=True))
    return "\n".join(x for x in outputs if x.strip())


def vm_br0_recreate_action() -> str:
    return vm_br0_remove_action("BR0") + "\n\n" + vm_br0_install_action("BR0", "br0")


def status_action() -> ActionResult:
    return ActionResult(True, print_status_text(), 0)


def print_status_text() -> str:
    snap = collect_snapshot()
    lines = [
        f"Mode: {snap.mode}  defaut={snap.default_iface or '-'}  gateway={snap.gateway or '-'}",
        f"Docker br0: {'OK' if snap.docker_ok else 'ERREUR'}",
        snap.docker_info,
        f"VM BR0: {'OK' if snap.libvirt_ok else 'ERREUR'}",
        snap.libvirt_info,
        f".253: {'OK' if snap.shim_ok else 'ERREUR'}",
        snap.shim_info,
    ]
    for row in snap.interfaces:
        lines.append(f"{row.name}\t{row.kind}\t{row.state}\t{row.ipv4 or '-'}\tmaster={row.master or '-'}\tports={','.join(row.bridge_ports) or '-'}")
    if snap.errors:
        lines.append("\n".join(snap.errors))
    return "\n".join(lines)


def lan_actions() -> list[LanAction]:
    def docker_status_result() -> ActionResult:
        ok, output = docker_br0_status()
        return ActionResult(ok, output)

    def docker_host_status_result() -> ActionResult:
        ok, output = docker_host_status()
        return ActionResult(ok, output)

    def vm_status_result() -> ActionResult:
        ok, output = vm_br0_status()
        return ActionResult(ok, output)

    return [
        LanAction("status", "Général", "Statut général", "Relire état hôte, Docker, VM et .253.", status_action),
        LanAction("br0-status", "BR0", "BR0 -statut", "Détection réseau, routes, bridge et état sauvegardé.", lambda: action_wrap(br0_status_action)),
        LanAction("br0-install", "BR0", "BR0 -install", "Carte physique en port, br0 avec IP/MAC du serveur, puis reboot.", lambda: action_wrap(br0_install_action), True),
        LanAction("br0-remove", "BR0", "BR0 -remove", "Restaurer réseau classique depuis sauvegarde ou état, puis reboot.", lambda: action_wrap(br0_remove_action), True),
        LanAction("docker-status", "Docker br0", "Docker br0 -status", "Afficher le réseau Docker LAN.", docker_status_result),
        LanAction("docker-install", "Docker br0", "Docker br0 -install", "Créer Docker br0 depuis docker_lan.json, sinon détection runtime.", lambda: action_wrap(lambda: docker_install_action("br0")), True),
        LanAction("docker-remove", "Docker br0", "Docker br0 -remove", "Supprimer Docker br0 si aucun conteneur ne l'utilise.", lambda: action_wrap(docker_remove_action), True),
        LanAction("docker-recreate", "Docker br0", "Docker br0 -recreate", "Supprimer puis recréer Docker br0.", lambda: action_wrap(docker_recreate_action), True),
        LanAction("docker-host-status", ".253", ".253 -status", "Etat mv-host, service et routes vers conteneurs.", docker_host_status_result),
        LanAction("docker-host-up", ".253", ".253 -up/update", "Créer mv-host 192.168.1.253 et mettre à jour les routes conteneurs.", lambda: action_wrap(docker_host_up_action)),
        LanAction("docker-host-down", ".253", ".253 -down", "Supprimer l'interface mv-host.", lambda: action_wrap(docker_host_down_action), True),
        LanAction("docker-host-install-systemd", ".253", ".253 -install-systemd", "Installer le service autonome yoleo-macvlan-host.service.", lambda: action_wrap(docker_host_install_systemd_action), True),
        LanAction("docker-host-test", ".253", ".253 -test", "Tester ping hôte vers conteneurs du réseau Docker br0.", lambda: action_wrap(docker_host_test_action)),
        LanAction("vm-status", "VM BR0", "VM BR0 -status", "Afficher le réseau libvirt BR0.", vm_status_result),
        LanAction("vm-install", "VM BR0", "VM BR0 -install", "Créer réseau libvirt BR0 branché au bridge Linux br0.", lambda: action_wrap(lambda: vm_br0_install_action("BR0", "br0")), True),
        LanAction("vm-remove", "VM BR0", "VM BR0 -remove", "Supprimer réseau libvirt BR0.", lambda: action_wrap(lambda: vm_br0_remove_action("BR0")), True),
        LanAction("vm-recreate", "VM BR0", "VM BR0 -recreate", "Supprimer puis recréer réseau libvirt BR0.", lambda: action_wrap(vm_br0_recreate_action), True),
    ]


class LanCliApp(App[int]):
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

    #content {
        height: 1fr;
        padding: 1 2;
    }

    #actions_panel {
        width: 38%;
        min-width: 42;
        height: 100%;
        border: solid #2fa77f;
        padding: 0 1;
        background: #091c1d;
    }

    #status_panel {
        width: 1fr;
        height: 100%;
        border: solid #2fa77f;
        padding: 0 1;
        background: #091c1d;
    }

    #action_list {
        height: 1fr;
        background: #091c1d;
    }

    #status_text {
        height: 10;
    }

    #log {
        height: 1fr;
        border-top: solid #2fa77f;
        background: #050d0e;
    }

    #message {
        height: 2;
        padding: 0 2;
        background: #123638;
        color: #bfeee0;
    }
    """

    BINDINGS = [
        ("escape", "quit", "Retour"),
        ("q", "quit", "Quitter"),
        ("r", "refresh", "Rafraichir"),
        ("enter", "run_selected", "Envoyer"),
    ]

    def __init__(self, *, refresh_seconds: float = 5.0) -> None:
        super().__init__()
        self.refresh_seconds = max(2.0, refresh_seconds)
        self.actions = lan_actions()
        self.action_map: dict[str, LanAction] = {}
        self.snapshot = LanSnapshot()
        self.busy = False
        self.pending_confirm = ""
        self.message = "Pret."

    def compose(self) -> ComposeResult:
        with Container(id="frame"):
            yield Static("", id="topbar")
            with Horizontal(id="content"):
                with Vertical(id="actions_panel"):
                    yield Static("LAN secours", id="actions_title")
                    yield OptionList(id="action_list")
                with Vertical(id="status_panel"):
                    yield Static("", id="status_text")
                    yield RichLog(id="log", markup=True, wrap=True, highlight=False)
            yield Static("", id="message")

    def on_mount(self) -> None:
        self.query_one("#action_list", OptionList).focus()
        self.render_actions()
        self.set_interval(self.refresh_seconds, self.schedule_refresh)
        self.schedule_refresh()

    def render_actions(self) -> None:
        options = self.query_one("#action_list", OptionList)
        options.clear_options()
        self.action_map = {}
        last_group = ""
        for action in self.actions:
            if action.group != last_group:
                options.add_option(Option(Text(f"-- {action.group} --", style="bold green"), disabled=True))
                last_group = action.group
            style = "bold yellow" if action.danger else "bold cyan"
            text = Text.assemble((action.label, style), "\n", (action.description, "dim"))
            self.action_map[action.id] = action
            options.add_option(Option(text, id=action.id))

    def schedule_refresh(self) -> None:
        if self.busy:
            return
        self.run_worker(self.refresh_data(), name="lan-refresh", exit_on_error=False, exclusive=True)

    async def refresh_data(self) -> None:
        self.busy = True
        try:
            self.snapshot = await asyncio.to_thread(collect_snapshot)
            self.render_status()
        finally:
            self.busy = False

    def render_status(self) -> None:
        snap = self.snapshot
        mode_style = "bold green" if snap.mode == "bridge" else ("bold yellow" if snap.mode == "direct" else "bold red")
        lines = Text.assemble(
            ("Statut machine : ", "bold cyan"),
            (snap.mode.upper(), mode_style),
            f"  defaut={snap.default_iface or '-'}  gateway={snap.gateway or '-'}\n",
            ("Docker br0 : ", "bold cyan"),
            ("OK", "bold green") if snap.docker_ok else ("ABSENT/ERREUR", "bold red"),
            "   ",
            ("VM BR0 : ", "bold cyan"),
            ("OK", "bold green") if snap.libvirt_ok else ("ABSENT/ERREUR", "bold red"),
            "   ",
            (".253 : ", "bold cyan"),
            ("OK", "bold green") if snap.shim_ok else ("ABSENT/ERREUR", "bold red"),
            "\n",
        )
        if snap.errors:
            lines.append("Erreurs: " + " | ".join(snap.errors[-2:]) + "\n", style="bold red")
        table = Table(show_header=True, show_edge=False, box=None, pad_edge=False, collapse_padding=True, header_style="bold cyan", expand=True)
        for column in ("IF", "Type", "Etat", "IPv4", "Master", "Ports"):
            table.add_column(column)
        for row in snap.interfaces[:8]:
            table.add_row(
                row.name + (" *" if row.default_route else ""),
                row.kind or "-",
                row.state or "-",
                row.ipv4 or "-",
                row.master or "-",
                ",".join(row.bridge_ports) or "-",
            )
        self.query_one("#topbar", Static).update(Text.assemble(("YoLeo LAN CLI", "bold white"), "\n", (f"{snap.generated_at}  Autonome: br0/Docker/VM/.253", "cyan")))
        self.query_one("#status_text", Static).update(Group(lines, table))
        self.query_one("#message", Static).update(self.message)

    def selected_action(self) -> Optional[LanAction]:
        options = self.query_one("#action_list", OptionList)
        if not options.option_count:
            return None
        option_id = options.get_option_at_index(options.highlighted).id
        return self.action_map.get(str(option_id))

    def action_quit(self) -> None:
        self.exit(0)

    def action_refresh(self) -> None:
        self.pending_confirm = ""
        self.message = "Rafraichissement..."
        self.schedule_refresh()

    def action_run_selected(self) -> None:
        action = self.selected_action()
        if action:
            self.request_action(action)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "action_list":
            return
        action = self.action_map.get(str(event.option_id))
        if action:
            self.request_action(action)

    def request_action(self, action: LanAction) -> None:
        if self.busy:
            self.message = "Action deja en cours."
            self.render_status()
            return
        if action.id == "status":
            self.action_refresh()
            return
        if action.danger and self.pending_confirm != action.id:
            self.pending_confirm = action.id
            self.message = f"Confirme {action.label} : appuie encore sur Entree."
            self.render_status()
            return
        self.pending_confirm = ""
        self.run_worker(self.perform_action(action), name=f"lan-{action.id}", exit_on_error=False)

    async def perform_action(self, action: LanAction) -> None:
        self.busy = True
        log = self.query_one("#log", RichLog)
        log.write(f"[bold cyan]# {action.label}[/]")
        try:
            result = await asyncio.to_thread(action.runner)
            if result.output.strip():
                log.write(result.output.rstrip())
            if not result.ok:
                log.write(f"[bold red]Code retour {result.rc}[/]")
            self.message = "OK." if result.ok else "Erreur pendant l'action."
        finally:
            self.busy = False
        await self.refresh_data()

    @on(Click, "#message")
    def click_message(self, event: Click) -> None:
        self.action_refresh()
        event.stop()

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            self.exit(0)
            event.stop()
            return
        if event.character and event.character.isdigit():
            enabled_actions = [a for a in self.actions if a.id != "status"]
            index = int(event.character) - 1
            if 0 <= index < len(enabled_actions):
                self.highlight_action(enabled_actions[index].id)
                event.stop()

    def highlight_action(self, action_id: str) -> None:
        options = self.query_one("#action_list", OptionList)
        for index in range(options.option_count):
            if str(options.get_option_at_index(index).id) == action_id:
                options.highlighted = index
                return


def print_status() -> int:
    text = print_status_text()
    print(text)
    return 1 if "Cannot open netlink socket" in text else 0


def action_aliases() -> dict[str, str]:
    aliases = {item.id: item.id for item in lan_actions()}
    aliases.update(
        {
            "br0-statut": "br0-status",
            "-statut": "br0-status",
            "-statue": "br0-status",
            "statut": "br0-status",
            "status": "br0-status",
            "-status": "br0-status",
            "-install": "br0-install",
            "-remove": "br0-remove",
            "docker-up": "docker-host-up",
            "docker-update": "docker-host-up",
            "253-up": "docker-host-up",
            "253-update": "docker-host-up",
            "253-down": "docker-host-down",
            "253-status": "docker-host-status",
            "vm-status": "vm-status",
        }
    )
    return aliases


def run_action_once(action_id: str) -> int:
    alias = action_aliases().get(action_id, action_id)
    action = next((item for item in lan_actions() if item.id == alias), None)
    if not action:
        print(f"Action inconnue: {action_id}", file=sys.stderr)
        return 2
    result = action.runner()
    if result.output:
        print(result.output.rstrip())
    return 0 if result.ok else (result.rc or 1)


def main(argv: list[str]) -> int:
    aliases = action_aliases()
    if argv and argv[0] in aliases:
        return run_action_once(argv[0])
    parser = argparse.ArgumentParser(description="CLI de secours LAN YoLeo")
    parser.add_argument("--status", action="store_true", help="Affiche le statut puis quitte.")
    parser.add_argument("--action", choices=sorted(aliases), help="Lance une action puis quitte.")
    parser.add_argument("--refresh", type=float, default=5.0)
    args = parser.parse_args(argv)
    if args.status:
        return print_status()
    if args.action:
        return run_action_once(args.action)
    return int(LanCliApp(refresh_seconds=args.refresh).run() or 0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
