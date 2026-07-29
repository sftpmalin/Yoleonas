#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
monitor.py - Petit moniteur terminal NAS Debian / OMV / Unraid-like

Commandes :
  python3 monitor.py
  python3 monitor.py -watch 2
  python3 monitor.py -no-smart
  python3 monitor.py -help

Paquets utiles optionnels :
  apt install -y smartmontools hdparm lm-sensors pciutils
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


def run(cmd: list[str], timeout: int = 4) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return p.returncode, p.stdout or ""
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def human_size(num: int | float | None) -> str:
    if num is None:
        return "—"
    try:
        value = float(num)
    except Exception:
        return "—"
    units = ["o", "Ko", "Mo", "Go", "To", "Po"]
    for unit in units:
        if abs(value) < 1024 or unit == units[-1]:
            if unit == "o":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} Po"


def read_cpu_total_idle() -> tuple[int, int]:
    with open("/proc/stat", "r", encoding="utf-8") as f:
        parts = f.readline().split()
    values = [int(x) for x in parts[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return total, idle


def cpu_percent(delay: float = 0.25) -> float:
    t1, i1 = read_cpu_total_idle()
    time.sleep(delay)
    t2, i2 = read_cpu_total_idle()
    total = max(t2 - t1, 1)
    idle = max(i2 - i1, 0)
    return max(0.0, min(100.0, 100.0 * (1.0 - idle / total)))


def mem_info() -> dict[str, int]:
    data: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                key, rest = line.split(":", 1)
                value = int(rest.strip().split()[0]) * 1024
                data[key] = value
    except Exception:
        pass
    return data


def loadavg() -> str:
    try:
        return " ".join(Path("/proc/loadavg").read_text().split()[:3])
    except Exception:
        return "—"


def uptime_human() -> str:
    try:
        seconds = int(float(Path("/proc/uptime").read_text().split()[0]))
    except Exception:
        return "—"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    if days:
        return f"{days}j {hours}h {minutes}m"
    return f"{hours}h {minutes}m"


def bar(pct: float, width: int = 18) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100.0))
    return "[" + "#" * filled + "." * (width - filled) + f"] {pct:5.1f}%"


def terminal_width(default: int = 140) -> int:
    try:
        return shutil.get_terminal_size((default, 30)).columns
    except Exception:
        return default


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        print("(aucune donnée)")
        return

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    sep = "  "
    line = sep.join(headers[i].ljust(widths[i]) for i in range(len(headers)))
    print(line)
    print(sep.join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        print(sep.join(str(row[i]).ljust(widths[i]) for i in range(len(headers))))


def lsblk_json() -> list[dict]:
    if not have("lsblk"):
        return []
    cmd = [
        "lsblk", "-J", "-b",
        "-o", "NAME,PATH,TYPE,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,MODEL,SERIAL,TRAN,ROTA,STATE"
    ]
    rc, out = run(cmd)
    if rc != 0 or not out.strip():
        return []
    try:
        return json.loads(out).get("blockdevices", [])
    except Exception:
        return []


def flatten_devices(devs: list[dict], parent: dict | None = None) -> list[tuple[dict, dict | None]]:
    out: list[tuple[dict, dict | None]] = []
    for d in devs:
        out.append((d, parent))
        children = d.get("children") or []
        out.extend(flatten_devices(children, d))
    return out


def mount_usage(path: str) -> tuple[str, str, str]:
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = max(total - free, 0)
        pct = (used / total * 100.0) if total else 0.0
        return human_size(used), human_size(total), f"{pct:.0f}%"
    except Exception:
        return "—", "—", "—"


def first_mount(mountpoints) -> str:
    if isinstance(mountpoints, list):
        for m in mountpoints:
            if m:
                return m
    if isinstance(mountpoints, str) and mountpoints:
        return mountpoints
    return ""


def smart_temp(devpath: str) -> tuple[str, str]:
    if not have("smartctl"):
        return "—", "smartctl absent"
    rc, out = run(["smartctl", "-A", devpath], timeout=6)
    if rc not in (0, 2, 4, 64) and not out:
        return "—", "non lu"
    temp = "—"
    for line in out.splitlines():
        low = line.lower()
        if "temperature_celsius" in low or "airflow_temperature_cel" in low:
            parts = line.split()
            if parts:
                temp = parts[-1] + "°C"
                break
        if "temperature:" in low and "celsius" in low:
            nums = [x for x in line.replace("+", " ").split() if x.isdigit()]
            if nums:
                temp = nums[0] + "°C"
                break
    return temp, "ok" if temp != "—" else "—"


def disk_power_state(devpath: str) -> str:
    if have("smartctl"):
        rc, out = run(["smartctl", "-n", "standby", "-i", devpath], timeout=6)
        text = out.lower()
        if "standby mode" in text or "device is in standby" in text:
            return "veille"
        if rc == 0:
            return "actif"
    if have("hdparm"):
        rc, out = run(["hdparm", "-C", devpath], timeout=4)
        low = out.lower()
        if "standby" in low or "sleeping" in low:
            return "veille"
        if "active" in low or "idle" in low:
            return "actif"
    return "—"


def disks_rows(no_smart: bool = False) -> list[list[str]]:
    rows: list[list[str]] = []
    devices = lsblk_json()
    for d, parent in flatten_devices(devices):
        typ = d.get("type") or ""
        if typ not in {"disk", "part"}:
            continue

        path = d.get("path") or ("/dev/" + (d.get("name") or ""))
        mount = first_mount(d.get("mountpoints"))
        model = (d.get("model") or (parent or {}).get("model") or "").strip()
        label = d.get("label") or ""
        fstype = d.get("fstype") or ""
        size = human_size(d.get("size"))
        tran = d.get("tran") or (parent or {}).get("tran") or ""
        rota = d.get("rota")
        media = "HDD" if str(rota) == "1" else "SSD/NVMe" if str(rota) == "0" else "—"

        used, total, pct = mount_usage(mount) if mount else ("—", "—", "—")

        state = "—"
        temp = "—"
        if typ == "disk" and not no_smart:
            state = disk_power_state(path)
            temp, _ = smart_temp(path)

        rows.append([
            path,
            typ,
            size,
            media,
            fstype or "—",
            label or "—",
            mount or "—",
            used,
            pct,
            state,
            temp,
            model[:28] or "—",
        ])
    return rows


def docker_summary() -> list[list[str]]:
    if not have("docker"):
        return [["docker", "absent", "—"]]
    rc, out = run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"], timeout=6)
    if rc != 0:
        return [["docker", "erreur/daemon arrêté", out.strip().splitlines()[0] if out.strip() else "—"]]
    rows = []
    lines = [x for x in out.splitlines() if x.strip()]
    for line in lines[:12]:
        parts = line.split("\t")
        while len(parts) < 3:
            parts.append("—")
        rows.append([parts[0], parts[1], parts[2]])
    if not rows:
        rows.append(["aucun container actif", "—", "—"])
    return rows


def nvidia_rows() -> list[list[str]]:
    if not have("nvidia-smi"):
        return []
    rc, out = run([
        "nvidia-smi",
        "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw",
        "--format=csv,noheader,nounits"
    ], timeout=6)
    if rc != 0 or not out.strip():
        return []
    rows = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        while len(parts) < 6:
            parts.append("—")
        rows.append([
            parts[0],
            parts[1] + "°C" if parts[1] != "—" else "—",
            parts[2] + "%" if parts[2] != "—" else "—",
            f"{parts[3]} / {parts[4]} MiB",
            parts[5] + " W" if parts[5] != "—" else "—",
        ])
    return rows


def intel_gpu_hint() -> list[list[str]]:
    # Sans intel_gpu_top, on vérifie seulement la présence du rendu iGPU.
    paths = sorted(Path("/dev/dri").glob("*")) if Path("/dev/dri").exists() else []
    if not paths:
        return []
    return [["/dev/dri", ", ".join(p.name for p in paths), "présent"]]


def print_monitor(no_smart: bool = False, show_docker: bool = True) -> None:
    mem = mem_info()
    total = mem.get("MemTotal", 0)
    available = mem.get("MemAvailable", 0)
    used = max(total - available, 0)
    mem_pct = (used / total * 100.0) if total else 0.0
    cpu = cpu_percent()

    print("=" * min(terminal_width(), 120))
    print(f"MONITOR NAS TERMINAL  |  {datetime.now().strftime('%F %T')}  |  host={os.uname().nodename}")
    print("=" * min(terminal_width(), 120))
    print(f"Uptime : {uptime_human()}   Load : {loadavg()}")
    print(f"CPU    : {bar(cpu)}")
    print(f"RAM    : {bar(mem_pct)}  {human_size(used)} / {human_size(total)}")
    print()

    print("DISQUES / MONTAGES")
    print("------------------")
    print_table(
        ["DEV", "TYPE", "TAILLE", "MEDIA", "FS", "LABEL", "MONTAGE", "UTILISÉ", "%", "ÉTAT", "TEMP", "MODÈLE"],
        disks_rows(no_smart=no_smart),
    )
    print()

    print("MERGERFS")
    print("--------")
    if have("findmnt"):
        rc, out = run(["findmnt", "-rn", "-t", "fuse.mergerfs", "-o", "TARGET,SOURCE,OPTIONS"], timeout=4)
        if out.strip():
            for line in out.splitlines():
                print(line)
        else:
            print("aucun montage mergerfs actif")
    else:
        print("findmnt absent")
    print()

    gpu = nvidia_rows()
    intel = intel_gpu_hint()
    if gpu or intel:
        print("GPU")
        print("---")
        if gpu:
            print_table(["NVIDIA", "TEMP", "UTIL", "VRAM", "W"], gpu)
        if intel:
            print_table(["INTEL/iGPU", "DEVICES", "ÉTAT"], intel)
        print()

    if show_docker:
        print("DOCKER")
        print("------")
        print_table(["NOM", "STATUS", "IMAGE"], docker_summary())
        print()

    print("COMMANDES UTILES")
    print("----------------")
    print("  python3 monitor.py -watch 2")
    print("  lsblk -o NAME,SIZE,FSTYPE,UUID,MOUNTPOINTS")
    print("  findmnt -t fuse.mergerfs")
    print("  exportfs -v")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-watch", "--watch", type=float, default=0.0)
    parser.add_argument("-no-smart", "--no-smart", action="store_true")
    parser.add_argument("-no-docker", "--no-docker", action="store_true")
    parser.add_argument("-h", "--help", action="store_true")
    args, unknown = parser.parse_known_args()

    if args.help or any(x == "help" for x in unknown) or unknown:
        print("Usage :")
        print("  python3 monitor.py")
        print("  python3 monitor.py -watch 2")
        print("  python3 monitor.py -no-smart")
        print("  python3 monitor.py -no-docker")
        print()
        print("Paquets utiles :")
        print("  apt install -y smartmontools hdparm lm-sensors pciutils")
        return 0

    if args.watch and args.watch > 0:
        while True:
            print("\033[2J\033[H", end="")
            print_monitor(no_smart=args.no_smart, show_docker=not args.no_docker)
            time.sleep(args.watch)
    else:
        print_monitor(no_smart=args.no_smart, show_docker=not args.no_docker)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
