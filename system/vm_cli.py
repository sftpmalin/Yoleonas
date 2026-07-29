#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gestionnaire VM minimaliste YoLeo.

Interface terminale libvirt/virsh :
  - lister les VM
  - demarrer
  - arret propre
  - stop force
  - statut live host + VM
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.events import Click, Key
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option


SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
DEFAULT_CONF_DIR = BASE_DIR / "conf"
DEFAULT_VM_CONF = DEFAULT_CONF_DIR / "vm.conf"


DEFAULT_CONFIG = {
    "VIRSH_BIN": "virsh",
    "LIBVIRT_URI": "qemu:///system",
    "ACTION_TIMEOUT": "90",
    "REFRESH_SECONDS": "2",
}

SAFE_NAME_RE = re.compile(r"^[^\x00/]+$")


@dataclass
class VMInfo:
    name: str
    state: str = "unknown"
    state_class: str = "unknown"
    uuid: str = ""
    vcpus: str = ""
    memory_bytes: int = 0
    current_memory_bytes: int = 0
    cpu_mode: str = ""
    cpu_model: str = ""
    virt_type: str = ""
    arch: str = ""
    machine: str = ""
    disks: int = 0
    nics: int = 0
    error: str = ""

    @property
    def running(self) -> bool:
        return self.state_class in {"running", "paused"}


@dataclass
class HostStats:
    cpu_percent: Optional[float] = None
    ram_total: int = 0
    ram_used: int = 0
    ram_available: int = 0


@dataclass
class VMStats:
    name: str
    sampled_at: float = 0.0
    cpu_time_ns: Optional[int] = None
    vm_cpu_host_percent: Optional[float] = None
    vm_cpu_vcpu_percent: Optional[float] = None
    memory_total: int = 0
    memory_available: Optional[int] = None
    memory_source: str = ""
    raw: dict[str, str] = field(default_factory=dict)


def strip_quotes(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def read_config_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return data
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            data[key] = strip_quotes(value)
    return data


def load_config(path: Path) -> dict[str, str]:
    conf = dict(DEFAULT_CONFIG)
    conf.update(read_config_file(path))
    conf["_CONFIG_FILE"] = str(path)
    return conf


def conf_int(conf: dict[str, str], key: str, default: int) -> int:
    try:
        return int(str(conf.get(key, default)).strip())
    except Exception:
        return default


def clean_vm_name(name: str) -> str:
    name = (name or "").strip()
    if not name or not SAFE_NAME_RE.match(name):
        raise ValueError("Nom de VM invalide.")
    return name


def run_cmd(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        env = os.environ.copy()
        env.update({"LC_ALL": "C", "LANG": "C", "LANGUAGE": "C"})
        completed = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )
        return completed.returncode, completed.stdout or ""
    except subprocess.TimeoutExpired:
        return 124, f"Timeout apres {timeout}s : {' '.join(shlex.quote(x) for x in cmd)}"
    except FileNotFoundError:
        return 127, f"Commande introuvable : {cmd[0]}"
    except Exception as exc:
        return 1, f"Exception Python : {exc}"


def virsh_cmd(conf: dict[str, str], *args: str) -> list[str]:
    cmd = [conf.get("VIRSH_BIN", "virsh").strip() or "virsh"]
    uri = conf.get("LIBVIRT_URI", "").strip()
    if uri:
        cmd.extend(["-c", uri])
    cmd.extend(args)
    return cmd


def virsh(conf: dict[str, str], *args: str, timeout: Optional[int] = None) -> tuple[int, str]:
    if timeout is None:
        timeout = conf_int(conf, "ACTION_TIMEOUT", 90)
    return run_cmd(virsh_cmd(conf, *args), timeout=timeout)


def parse_key_values(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in (text or "").splitlines():
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def parse_equals(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip("'\"")
    return out


def parse_space_stats(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in (text or "").splitlines():
        parts = raw.strip().split()
        if len(parts) >= 2:
            out[parts[0]] = parts[1]
    return out


def human_bytes(value: Optional[int]) -> str:
    if value is None or value < 0:
        return "-"
    units = ["o", "Ko", "Mo", "Go", "To"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "o":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{value} o"


def memory_to_bytes(text: Optional[str], unit: Optional[str] = "KiB") -> int:
    if not text:
        return 0
    try:
        value = int(str(text).strip())
    except ValueError:
        return 0
    unit = (unit or "KiB").strip().lower()
    factor = 1
    if unit in {"kb", "k", "kib"}:
        factor = 1024
    elif unit in {"mb", "m", "mib"}:
        factor = 1024 ** 2
    elif unit in {"gb", "g", "gib"}:
        factor = 1024 ** 3
    return value * factor


def first_text(root: ET.Element, path: str, default: str = "") -> str:
    node = root.find(path)
    return (node.text or "").strip() if node is not None and node.text else default


def node_attr(node: Optional[ET.Element], name: str, default: str = "") -> str:
    if node is None:
        return default
    return (node.get(name) or default).strip()


def state_class(state: str) -> str:
    low = (state or "").strip().lower()
    if any(word in low for word in ("running", "active", "paused", "pmsuspended", "suspended")):
        return "paused" if any(word in low for word in ("paused", "suspended")) else "running"
    if any(word in low for word in ("shut off", "shutoff", "off", "crashed", "stopped", "inactive", "dead")):
        return "stopped"
    return "unknown"


def state_label(vm: VMInfo) -> tuple[str, str]:
    if vm.state_class == "running":
        return "EN ROUTE", "bold green"
    if vm.state_class == "paused":
        return "PAUSE", "bold yellow"
    if vm.state_class == "stopped":
        return "ETEINTE", "dim"
    return "INCONNU", "yellow"


def list_vm_names(conf: dict[str, str]) -> tuple[list[str], Optional[str]]:
    rc, out = virsh(conf, "list", "--all", "--name", timeout=15)
    if rc != 0:
        return [], out.strip() or "virsh list a echoue."
    names = [line.strip() for line in out.splitlines() if line.strip()]
    return sorted(names, key=str.lower), None


def get_vm_state(conf: dict[str, str], name: str) -> str:
    rc, out = virsh(conf, "domstate", name, "--reason", timeout=15)
    if rc == 0 and out.strip():
        return out.strip().replace("\n", " ")
    rc, out = virsh(conf, "dominfo", name, timeout=15)
    if rc == 0:
        return parse_key_values(out).get("State", "unknown")
    return "unknown"


def parse_domain_xml(xml_text: str) -> dict[str, object]:
    root = ET.fromstring(xml_text)
    os_type = root.find("./os/type")
    memory_node = root.find("memory")
    current_memory_node = root.find("currentMemory")
    vcpu_node = root.find("vcpu")
    cpu_node = root.find("cpu")
    return {
        "uuid": first_text(root, "uuid"),
        "vcpus": (vcpu_node.text or "").strip() if vcpu_node is not None and vcpu_node.text else "",
        "memory_bytes": memory_to_bytes(first_text(root, "memory"), node_attr(memory_node, "unit", "KiB")),
        "current_memory_bytes": memory_to_bytes(first_text(root, "currentMemory"), node_attr(current_memory_node, "unit", "KiB")),
        "cpu_mode": node_attr(cpu_node, "mode", ""),
        "cpu_model": first_text(root, "./cpu/model"),
        "virt_type": node_attr(root, "type", ""),
        "arch": node_attr(os_type, "arch", ""),
        "machine": node_attr(os_type, "machine", ""),
        "disks": len(root.findall("./devices/disk")),
        "nics": len(root.findall("./devices/interface")),
    }


def collect_one_vm(conf: dict[str, str], name: str) -> VMInfo:
    vm = VMInfo(name=name)
    rc_info, info_text = virsh(conf, "dominfo", name, timeout=15)
    info = parse_key_values(info_text) if rc_info == 0 else {}
    vm.state = get_vm_state(conf, name)
    vm.state_class = state_class(vm.state)
    if info:
        vm.uuid = info.get("UUID", "")
        vm.vcpus = info.get("CPU(s)", "")
        vm.memory_bytes = memory_to_bytes(info.get("Max memory", "").split()[0] if info.get("Max memory") else "", "KiB")
        vm.current_memory_bytes = memory_to_bytes(info.get("Used memory", "").split()[0] if info.get("Used memory") else "", "KiB")

    rc_xml, xml_text = virsh(conf, "dumpxml", name, timeout=25)
    if rc_xml != 0:
        vm.error = xml_text.strip() or "Impossible de lire le XML."
        return vm
    try:
        parsed = parse_domain_xml(xml_text)
    except Exception as exc:
        vm.error = f"Erreur lecture XML : {exc}"
        return vm

    for key, value in parsed.items():
        if value not in {"", 0, None}:
            setattr(vm, key, value)
    if not vm.current_memory_bytes:
        vm.current_memory_bytes = vm.memory_bytes
    return vm


def collect_inventory(conf: dict[str, str]) -> tuple[list[VMInfo], Optional[str]]:
    names, error = list_vm_names(conf)
    if error:
        return [], error
    return [collect_one_vm(conf, name) for name in names], None


def read_proc_cpu_totals() -> Optional[tuple[int, int]]:
    try:
        parts = Path("/proc/stat").read_text(encoding="utf-8", errors="ignore").splitlines()[0].split()
        if not parts or parts[0] != "cpu":
            return None
        values = [int(x) for x in parts[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle
    except Exception:
        return None


def host_cpu_percent(sample_delay: float = 0.08) -> Optional[float]:
    first = read_proc_cpu_totals()
    if not first:
        return None
    time.sleep(max(0.02, min(sample_delay, 0.25)))
    second = read_proc_cpu_totals()
    if not second:
        return None
    total_delta = second[0] - first[0]
    idle_delta = second[1] - first[1]
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * (1.0 - idle_delta / total_delta))), 1)


def host_memory_values() -> tuple[int, int, int]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore").splitlines():
            if ":" not in line:
                continue
            key, raw = line.split(":", 1)
            parts = raw.strip().split()
            if parts:
                values[key] = int(parts[0]) * 1024
    except Exception:
        return 0, 0, 0
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    used = max(0, total - available)
    return total, used, available


def collect_host_stats() -> HostStats:
    total, used, available = host_memory_values()
    return HostStats(
        cpu_percent=host_cpu_percent(),
        ram_total=total,
        ram_used=used,
        ram_available=available,
    )


def vm_vcpu_count(vm: Optional[VMInfo], stats: dict[str, str]) -> int:
    for value in (stats.get("vcpu.current"), vm.vcpus if vm else ""):
        try:
            count = int(str(value).strip())
            if count > 0:
                return count
        except Exception:
            pass
    return 1


def collect_vm_stats(conf: dict[str, str], vm: VMInfo) -> VMStats:
    result = VMStats(name=vm.name, sampled_at=time.monotonic(), memory_total=vm.current_memory_bytes or vm.memory_bytes)
    if not vm.running:
        return result

    rc, out = virsh(conf, "domstats", "--state", "--cpu-total", "--balloon", "--vcpu", vm.name, timeout=20)
    if rc == 0:
        result.raw.update(parse_equals(out))
        cpu_time = result.raw.get("cpu.time")
        if cpu_time:
            try:
                result.cpu_time_ns = int(cpu_time)
            except Exception:
                result.cpu_time_ns = None
        current = result.raw.get("balloon.current") or result.raw.get("balloon.maximum")
        available = result.raw.get("balloon.available") or result.raw.get("balloon.unused")
        if current:
            result.memory_total = int(current) * 1024
            result.memory_source = "domstats"
        if available:
            result.memory_available = int(available) * 1024
            result.memory_source = "domstats"

    rc_mem, mem_out = virsh(conf, "dommemstat", vm.name, timeout=15)
    if rc_mem == 0:
        mem = parse_space_stats(mem_out)
        actual = mem.get("actual") or mem.get("available")
        remaining = mem.get("unused") or mem.get("usable") or mem.get("available")
        if actual:
            result.memory_total = int(actual) * 1024
            result.memory_source = "dommemstat"
        if remaining:
            result.memory_available = int(remaining) * 1024
            result.memory_source = "dommemstat"

    return result


def run_vm_action(conf: dict[str, str], name: str, action: str) -> tuple[bool, str]:
    name = clean_vm_name(name)
    mapping = {
        "start": ["start", name],
        "shutdown": ["shutdown", name],
        "stop": ["destroy", name],
    }
    if action not in mapping:
        return False, f"Action inconnue : {action}"
    rc, out = virsh(conf, *mapping[action], timeout=conf_int(conf, "ACTION_TIMEOUT", 90))
    message = (out or "").strip()
    if rc == 0:
        labels = {
            "start": "Demarrage demande.",
            "shutdown": "Arret propre demande.",
            "stop": "Stop force demande.",
        }
        return True, message or labels[action]
    return False, message or f"virsh {mapping[action][0]} a echoue."


class VMMinimalApp(App[int]):
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

    #vm_list_panel {
        width: 43%;
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

    #vm_options {
        height: 1fr;
        background: #091c1d;
    }

    #vm_hint, #message {
        height: 2;
        color: #bfeee0;
    }

    #details {
        height: 5;
        color: #d9fff3;
    }

    #host_table, #guest_table {
        height: auto;
        margin-top: 0;
    }

    #actions {
        height: 2;
        padding: 0 2;
        background: #123638;
    }

    .action {
        width: auto;
        min-width: 13;
        height: 1;
        margin-right: 2;
        content-align: center middle;
        text-style: bold;
    }

    .action.enabled {
        background: #9df5ce;
        color: #071314;
    }

    .action.disabled {
        background: #173234;
        color: #5f7f78;
    }
    """

    BINDINGS = [
        ("escape", "quit", "Retour"),
        ("q", "quit", "Quitter"),
        ("r", "refresh", "Rafraichir"),
        ("s", "start", "Demarrer"),
        ("a", "shutdown", "Arret"),
        ("x", "stop", "Stop"),
        ("enter", "status", "Statut"),
    ]

    def __init__(self, *, conf_path: Path, refresh_seconds: Optional[float] = None) -> None:
        super().__init__()
        self.conf_path = conf_path
        self.conf = load_config(conf_path)
        self.refresh_seconds = refresh_seconds or max(1.0, float(conf_int(self.conf, "REFRESH_SECONDS", 2) or 2))
        self.vms: list[VMInfo] = []
        self.error = ""
        self.selected_name = ""
        self.host_stats = HostStats()
        self.vm_stats = VMStats(name="")
        self.vm_cpu_samples: dict[str, tuple[float, int]] = {}
        self.option_map: dict[str, str] = {}
        self.busy = False
        self.message = "Pret."

    def compose(self) -> ComposeResult:
        with Container(id="frame"):
            yield Static("", id="topbar")
            with Horizontal(id="content"):
                with Vertical(id="vm_list_panel"):
                    yield Static("Machines virtuelles", id="vm_hint")
                    yield OptionList(id="vm_options")
                    yield Static("", id="message")
                with Vertical(id="status_panel"):
                    yield Static("", id="details")
                    yield Static("", id="host_table")
                    yield Static("", id="guest_table")
            with Horizontal(id="actions"):
                yield Static("Demarrer", id="action_start", classes="action disabled")
                yield Static("Arret", id="action_shutdown", classes="action disabled")
                yield Static("Stop", id="action_stop", classes="action disabled")
                yield Static("Statut", id="action_status", classes="action disabled")

    def on_mount(self) -> None:
        self.query_one("#vm_options", OptionList).focus()
        self.message = "VM CLI en pause. Appuie sur r pour lire libvirt une seule fois."
        self.render_all()

    def schedule_refresh(self) -> None:
        if self.busy:
            return
        self.run_worker(self.refresh_data(), name="vm-refresh", exit_on_error=False, exclusive=True)

    async def refresh_data(self) -> None:
        self.busy = True
        try:
            vms, error = await asyncio.to_thread(collect_inventory, self.conf)
            self.vms = vms
            self.error = error or ""
            if self.selected_name and not any(vm.name == self.selected_name for vm in self.vms):
                self.selected_name = ""
            if not self.selected_name and self.vms:
                self.selected_name = self.vms[0].name
            self.host_stats = await asyncio.to_thread(collect_host_stats)
            selected = self.selected_vm()
            if selected and selected.running:
                raw_stats = await asyncio.to_thread(collect_vm_stats, self.conf, selected)
                self.vm_stats = self.with_cpu_delta(selected, raw_stats)
            else:
                self.vm_stats = VMStats(name=selected.name if selected else "")
            self.render_all()
        finally:
            self.busy = False

    def with_cpu_delta(self, vm: VMInfo, stats: VMStats) -> VMStats:
        if stats.cpu_time_ns is None:
            return stats
        previous = self.vm_cpu_samples.get(vm.name)
        self.vm_cpu_samples[vm.name] = (stats.sampled_at, stats.cpu_time_ns)
        if not previous:
            return stats
        prev_at, prev_ns = previous
        elapsed = max(0.001, stats.sampled_at - prev_at)
        delta_ns = max(0, stats.cpu_time_ns - prev_ns)
        host_percent = (delta_ns / 1_000_000_000.0) / elapsed * 100.0
        vcpus = vm_vcpu_count(vm, stats.raw)
        stats.vm_cpu_host_percent = round(host_percent, 1)
        stats.vm_cpu_vcpu_percent = round(host_percent / max(1, vcpus), 1)
        return stats

    def selected_vm(self) -> Optional[VMInfo]:
        for vm in self.vms:
            if vm.name == self.selected_name:
                return vm
        return None

    def action_allowed(self, action: str) -> bool:
        vm = self.selected_vm()
        if vm is None or self.busy:
            return False
        if action == "start":
            return not vm.running
        if action in {"shutdown", "stop", "status"}:
            return vm.running
        return False

    def render_all(self) -> None:
        self.render_topbar()
        self.render_vm_options()
        self.render_actions()
        self.render_status()
        self.query_one("#message", Static).update(self.message if not self.error else self.error)

    def render_topbar(self) -> None:
        running = sum(1 for vm in self.vms if vm.state_class == "running")
        stopped = sum(1 for vm in self.vms if vm.state_class == "stopped")
        self.query_one("#topbar", Static).update(
            Text.assemble(
                ("YoLeo VM CLI", "bold white"),
                "\n",
                (f"{len(self.vms)} VM  {running} en route  {stopped} eteinte(s)  Conf: {self.conf_path}", "cyan"),
            )
        )

    def render_vm_options(self) -> None:
        options = self.query_one("#vm_options", OptionList)
        highlighted_name = self.selected_name
        options.clear_options()
        self.option_map = {}
        if self.error:
            options.add_option(Option(Text(self.error, style="bold red"), disabled=True))
            return
        if not self.vms:
            options.add_option(Option(Text("Aucune VM libvirt detectee.", style="yellow"), disabled=True))
            return
        highlighted_index = 0
        for index, vm in enumerate(self.vms):
            label, style = state_label(vm)
            mem = human_bytes(vm.current_memory_bytes or vm.memory_bytes)
            text = Text.assemble(
                (f"{index + 1:>2}. ", "dim"),
                (vm.name, "bold white"),
                "  ",
                (label, style),
                ("  ", "dim"),
                (f"{vm.vcpus or '?'} vCPU", "dim"),
                ("  ", "dim"),
                (mem, "dim"),
            )
            option_id = f"vm-{index}"
            self.option_map[option_id] = vm.name
            options.add_option(Option(text, id=option_id))
            if vm.name == highlighted_name:
                highlighted_index = index
        options.highlighted = highlighted_index

    def render_actions(self) -> None:
        for action, label in (
            ("start", "Demarrer"),
            ("shutdown", "Arret"),
            ("stop", "Stop"),
            ("status", "Statut"),
        ):
            widget = self.query_one(f"#action_{action}", Static)
            enabled = self.action_allowed(action)
            widget.update(label)
            widget.set_class(enabled, "enabled")
            widget.set_class(not enabled, "disabled")

    def render_status(self) -> None:
        vm = self.selected_vm()
        if vm is None:
            self.query_one("#details", Static).update("Selectionne une VM.")
            self.query_one("#host_table", Static).update("")
            self.query_one("#guest_table", Static).update("")
            return

        label, style = state_label(vm)
        details = Text.assemble(
            ("VM : ", "bold cyan"),
            (vm.name, "bold white"),
            "  ",
            (label, style),
            "\n",
            f"Etat brut : {vm.state}\n",
            f"UUID : {vm.uuid or '-'}\n",
            f"vCPU : {vm.vcpus or '-'}  RAM : {human_bytes(vm.current_memory_bytes or vm.memory_bytes)}  Type : {vm.virt_type or '-'}  Arch : {vm.arch or '-'}\n",
            f"Disques : {vm.disks}  Reseaux : {vm.nics}  CPU : {vm.cpu_mode or vm.cpu_model or '-'}",
        )
        if vm.error:
            details.append(f"\nErreur : {vm.error}", style="bold red")
        self.query_one("#details", Static).update(details)
        self.query_one("#host_table", Static).update(self.host_table())
        self.query_one("#guest_table", Static).update(self.guest_table(vm))

    def host_table(self) -> Table:
        table = Table(
            title="Hote",
            title_justify="left",
            show_header=True,
            show_edge=False,
            box=None,
            pad_edge=False,
            collapse_padding=True,
            header_style="bold cyan",
            expand=True,
        )
        table.add_column("CPU")
        table.add_column("RAM totale")
        table.add_column("RAM utilisee")
        table.add_column("RAM restante")
        cpu = "-" if self.host_stats.cpu_percent is None else f"{self.host_stats.cpu_percent:.1f}%"
        table.add_row(
            cpu,
            human_bytes(self.host_stats.ram_total),
            human_bytes(self.host_stats.ram_used),
            human_bytes(self.host_stats.ram_available),
        )
        return table

    def guest_table(self, vm: VMInfo) -> Table:
        table = Table(
            title="VM",
            title_justify="left",
            show_header=True,
            show_edge=False,
            box=None,
            pad_edge=False,
            collapse_padding=True,
            header_style="bold cyan",
            expand=True,
        )
        table.add_column("CPU VM")
        table.add_column("CPU / vCPU")
        table.add_column("RAM VM totale")
        table.add_column("RAM VM restante")
        table.add_column("Source")
        if not vm.running:
            table.add_row("grise", "grise", human_bytes(vm.current_memory_bytes or vm.memory_bytes), "-", "VM eteinte")
            return table
        cpu_host = "-" if self.vm_stats.vm_cpu_host_percent is None else f"{self.vm_stats.vm_cpu_host_percent:.1f}% hote"
        cpu_vcpu = "-" if self.vm_stats.vm_cpu_vcpu_percent is None else f"{self.vm_stats.vm_cpu_vcpu_percent:.1f}%"
        table.add_row(
            cpu_host,
            cpu_vcpu,
            human_bytes(self.vm_stats.memory_total or vm.current_memory_bytes or vm.memory_bytes),
            human_bytes(self.vm_stats.memory_available) if self.vm_stats.memory_available is not None else "-",
            self.vm_stats.memory_source or "libvirt",
        )
        return table

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id != "vm_options":
            return
        name = self.option_map.get(str(event.option_id))
        if name:
            self.selected_name = name
            selected = self.selected_vm()
            if selected and not selected.running:
                self.vm_stats = VMStats(name=selected.name)
            self.render_actions()
            self.render_status()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "vm_options":
            return
        name = self.option_map.get(str(event.option_id))
        if name:
            self.selected_name = name
            vm = self.selected_vm()
            if vm and vm.running:
                self.action_status()
            elif vm:
                self.request_action("start")

    @on(Click, "#action_start")
    def click_start(self, event: Click) -> None:
        self.request_action("start")
        event.stop()

    @on(Click, "#action_shutdown")
    def click_shutdown(self, event: Click) -> None:
        self.request_action("shutdown")
        event.stop()

    @on(Click, "#action_stop")
    def click_stop(self, event: Click) -> None:
        self.request_action("stop")
        event.stop()

    @on(Click, "#action_status")
    def click_status(self, event: Click) -> None:
        self.action_status()
        event.stop()

    def request_action(self, action: str) -> None:
        if not self.action_allowed(action):
            self.message = "Action indisponible pour l'etat actuel."
            self.render_all()
            return
        vm = self.selected_vm()
        if vm is None:
            return
        self.run_worker(self.perform_action(action, vm.name), name=f"vm-{action}", exit_on_error=False)

    async def perform_action(self, action: str, name: str) -> None:
        self.busy = True
        self.message = f"Action en cours : {action} {name}"
        self.render_all()
        try:
            ok, message = await asyncio.to_thread(run_vm_action, self.conf, name, action)
            self.message = ("OK : " if ok else "Erreur : ") + message
        finally:
            self.busy = False
        await self.refresh_data()

    def action_quit(self) -> None:
        self.exit(0)

    def action_refresh(self) -> None:
        self.message = "Rafraichissement..."
        self.schedule_refresh()

    def action_start(self) -> None:
        self.request_action("start")

    def action_shutdown(self) -> None:
        self.request_action("shutdown")

    def action_stop(self) -> None:
        self.request_action("stop")

    def action_status(self) -> None:
        vm = self.selected_vm()
        if vm and not vm.running:
            self.request_action("start")
            return
        if not self.action_allowed("status"):
            self.message = "Statut grise : la VM n'est pas en route."
            self.render_all()
            return
        self.message = "Statut live actif."
        self.schedule_refresh()

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            self.exit(0)
            event.stop()
            return
        if event.character and event.character.isdigit() and not self.busy:
            wanted = int(event.character)
            if 1 <= wanted <= len(self.vms):
                self.selected_name = self.vms[wanted - 1].name
                self.render_all()
                event.stop()


def print_inventory(conf: dict[str, str]) -> int:
    vms, error = collect_inventory(conf)
    if error:
        print(error)
        return 1
    for vm in vms:
        label, _style = state_label(vm)
        print(f"{vm.name}\t{label}\t{vm.vcpus or '-'} vCPU\t{human_bytes(vm.current_memory_bytes or vm.memory_bytes)}")
    return 0


def print_status(conf: dict[str, str], name: str) -> int:
    vm = collect_one_vm(conf, clean_vm_name(name))
    label, _style = state_label(vm)
    print(f"VM      : {vm.name}")
    print(f"Etat    : {label} ({vm.state})")
    print(f"UUID    : {vm.uuid or '-'}")
    print(f"vCPU    : {vm.vcpus or '-'}")
    print(f"RAM     : {human_bytes(vm.current_memory_bytes or vm.memory_bytes)}")
    if not vm.running:
        print("Statut  : indisponible, VM eteinte")
        return 0
    host = collect_host_stats()
    stats = collect_vm_stats(conf, vm)
    print(f"CPU hote: {'-' if host.cpu_percent is None else f'{host.cpu_percent:.1f}%'}")
    print(f"RAM hote: {human_bytes(host.ram_available)} restante / {human_bytes(host.ram_total)} total")
    print(f"RAM VM  : {human_bytes(stats.memory_available) if stats.memory_available is not None else '-'} restante / {human_bytes(stats.memory_total)} total")
    return 0


async def run_self_test() -> int:
    xml = """
    <domain type="kvm">
      <name>test</name>
      <uuid>1234</uuid>
      <memory unit="MiB">2048</memory>
      <currentMemory unit="MiB">1024</currentMemory>
      <vcpu placement="static">2</vcpu>
      <os><type arch="x86_64" machine="pc">hvm</type></os>
      <cpu mode="host-passthrough"/>
      <devices>
        <disk type="file" device="disk"/>
        <interface type="bridge"/>
      </devices>
    </domain>
    """
    parsed = parse_domain_xml(xml)
    assert parsed["vcpus"] == "2"
    assert parsed["memory_bytes"] == 2048 * 1024 * 1024
    assert parsed["current_memory_bytes"] == 1024 * 1024 * 1024
    assert parsed["disks"] == 1
    assert parsed["nics"] == 1
    assert state_class("running") == "running"
    assert state_class("shut off") == "stopped"
    assert parse_equals("cpu.time=123\nvcpu.current=2")["cpu.time"] == "123"
    print("self-test OK")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gestionnaire VM minimaliste YoLeo")
    parser.add_argument("--conf", default=os.environ.get("VM_CONFIG_PATH", str(DEFAULT_VM_CONF)))
    parser.add_argument("--refresh", type=float, default=None, help="Secondes entre rafraichissements TUI.")
    parser.add_argument("--list", action="store_true", help="Liste les VM puis quitte.")
    parser.add_argument("--start", metavar="VM", help="Demarre une VM puis quitte.")
    parser.add_argument("--shutdown", metavar="VM", help="Arret propre d'une VM puis quitte.")
    parser.add_argument("--stop", metavar="VM", help="Stop force / virsh destroy puis quitte.")
    parser.add_argument("--status", metavar="VM", help="Affiche le statut d'une VM puis quitte.")
    parser.add_argument("--self-test", action="store_true", help="Tests internes sans libvirt.")
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        return asyncio.run(run_self_test())

    conf_path = Path(args.conf).expanduser().resolve()
    conf = load_config(conf_path)
    if args.list:
        return print_inventory(conf)
    for action, name in (("start", args.start), ("shutdown", args.shutdown), ("stop", args.stop)):
        if name:
            ok, message = run_vm_action(conf, name, action)
            print(("OK: " if ok else "ERREUR: ") + message)
            return 0 if ok else 1
    if args.status:
        return print_status(conf, args.status)

    app = VMMinimalApp(conf_path=conf_path, refresh_seconds=args.refresh)
    result = app.run()
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
