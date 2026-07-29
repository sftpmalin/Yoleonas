#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Interface de secours stockage YoLeo."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.events import Click, Key
from textual.widgets import Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import disk  # noqa: E402


@dataclass
class TargetRow:
    id: str
    label: str
    target: str
    kind: str
    disk_name: str
    size: str = ""
    fstype: str = ""
    mount: str = ""
    state: str = ""
    protected: bool = False
    raw: dict[str, Any] | None = None

    @property
    def mounted(self) -> bool:
        return bool(self.mount)


@dataclass
class PendingFormat:
    target: str
    fstype: str
    label: str


def collect_storage() -> tuple[dict[str, Any], list[TargetRow], str]:
    try:
        data = disk.collect_disks()
    except Exception as exc:
        return {}, [], f"Erreur inventaire disques : {exc}"
    rows: list[TargetRow] = []
    for disk_index, item in enumerate(data.get("disks", []) or []):
        device = str(item.get("device") or "")
        disk_name = str(item.get("name") or os.path.basename(device) or f"disk{disk_index}")
        protected = bool(item.get("is_system_disk"))
        row_id = f"disk-{disk_index}"
        rows.append(
            TargetRow(
                id=row_id,
                label=str(item.get("logical_name") or disk_name),
                target=device,
                kind="disk",
                disk_name=disk_name,
                size=str(item.get("size") or ""),
                fstype=str(item.get("fstype") or item.get("blkid_fstype") or ""),
                mount=str(item.get("mountpoint") or ""),
                state=str(item.get("power_label") or item.get("status_label") or ""),
                protected=protected,
                raw=item,
            )
        )
        for part_index, part in enumerate(item.get("parts", []) or []):
            target = str(part.get("path") or "")
            if not target:
                continue
            rows.append(
                TargetRow(
                    id=f"part-{disk_index}-{part_index}",
                    label="  " + str(part.get("name") or os.path.basename(target)),
                    target=target,
                    kind="part",
                    disk_name=disk_name,
                    size=str(part.get("size") or ""),
                    fstype=str(part.get("fstype") or part.get("blkid_fstype") or ""),
                    mount=str(part.get("mount") or ""),
                    state="partition",
                    protected=protected,
                    raw=part,
                )
            )
    return data, rows, ""


def short_output(out: str, limit: int = 4000) -> str:
    out = out or ""
    if len(out) <= limit:
        return out
    return out[-limit:]


def mount_target(target: str, mountpoint: str) -> tuple[bool, str]:
    conf = disk.get_config()
    ok, target, error = disk.require_safe_target(conf, target, allow_disk=True, allow_part=True)
    if not ok:
        return False, error
    path_ok, mountpoint, path_error = disk.safe_real_mount_path(conf, mountpoint)
    if not path_ok:
        return False, path_error
    if not os.path.exists(mountpoint):
        os.makedirs(mountpoint, exist_ok=True)
    options = str(conf.get("MOUNT_DEFAULT_OPTIONS") or "defaults,nofail,noatime").strip()
    cmd = ["mount"]
    if options:
        cmd += ["-o", options]
    cmd += [target, mountpoint]
    rc, out = disk.run_cmd(cmd, timeout=25)
    disk.clear_disk_runtime_caches()
    return rc == 0, short_output(out or ("Montage terminé." if rc == 0 else "Montage impossible."))


def unmount_target(target: str) -> tuple[bool, str]:
    conf = disk.get_config()
    ok, target, error = disk.require_safe_target(conf, target, allow_disk=True, allow_part=True)
    if not ok:
        return False, error
    mounts = disk.mount_paths_for_target(conf, target)
    if not mounts:
        return True, "Déjà démonté."
    outputs: list[str] = []
    for mount in mounts:
        if mount == "/" or mount.startswith("/boot"):
            return False, "Montage système/boot refusé."
        rc, out = disk.run_cmd(["umount", mount], timeout=20)
        outputs.append(f"$ umount {mount}\n{out}".strip())
        if rc != 0:
            return False, short_output("\n\n".join(outputs))
    disk.clear_disk_runtime_caches()
    return True, short_output("\n\n".join(outputs) or "Démontage terminé.")


def format_target(target: str, fstype: str, label: str) -> tuple[bool, str]:
    conf = disk.get_config()
    ok, target, error = disk.require_safe_target(conf, target, allow_disk=True, allow_part=True)
    if not ok:
        return False, error
    if disk.mount_paths_for_target(conf, target):
        return False, "Le périphérique est monté : démonte-le avant de formater."
    ok_cmd, cmd, cmd_error = disk.make_mkfs_command(target, fstype, label, True)
    if not ok_cmd:
        return False, cmd_error
    rc, out = disk.run_cmd(cmd, timeout=1800)
    disk.refresh_udev_best_effort(target)
    if rc == 0:
        meta = disk.parse_blkid_export(target)
        if meta:
            disk.save_persistent_block_meta(conf, target, meta)
    disk.clear_disk_runtime_caches()
    return rc == 0, short_output(out or ("Formatage terminé." if rc == 0 else "Formatage impossible."))


def check_target(target: str) -> tuple[bool, str]:
    conf = disk.get_config()
    ok, target, error = disk.require_safe_target(conf, target, allow_disk=True, allow_part=True)
    if not ok:
        return False, error
    if disk.mount_paths_for_target(conf, target):
        return False, "Vérification refusée : démonte d'abord ce volume."
    ok_tmux, session, tmux_msg, _created = disk.start_disk_check_tmux(conf, target, "fsck")
    if not ok_tmux:
        return False, tmux_msg
    return True, f"{tmux_msg}\nSession tmux : {session}\nCommande utile : tmux attach -t {shlex.quote(session)}"


def default_mountpoint(row: TargetRow) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]+", "-", os.path.basename(row.target) or row.disk_name).strip("-")
    return f"/mnt/{base or 'disk'}"


class StorageCliApp(App[int]):
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

    #disk_panel {
        width: 44%;
        min-width: 46;
        height: 100%;
        border: solid #2fa77f;
        padding: 0 1;
        background: #091c1d;
    }

    #detail_panel {
        width: 1fr;
        height: 100%;
        border: solid #2fa77f;
        padding: 0 1;
        background: #091c1d;
    }

    #disk_list {
        height: 1fr;
        background: #091c1d;
    }

    #details {
        height: 9;
    }

    #log {
        height: 1fr;
        border-top: solid #2fa77f;
        background: #050d0e;
    }

    #prompt {
        height: 3;
        padding: 0 2;
        background: #123638;
    }

    #prompt_input {
        height: 1;
    }

    #actions {
        height: 2;
        padding: 0 2;
        background: #123638;
    }

    .action {
        width: auto;
        min-width: 12;
        height: 1;
        margin-right: 2;
        content-align: center middle;
        text-style: bold;
    }

    .enabled {
        background: #9df5ce;
        color: #071314;
    }

    .disabled {
        background: #173234;
        color: #5f7f78;
    }

    .hidden {
        display: none;
    }
    """

    BINDINGS = [
        ("escape", "quit", "Retour"),
        ("q", "quit", "Quitter"),
        ("r", "refresh", "Rafraichir"),
        ("m", "mount", "Monter"),
        ("u", "unmount", "Demonter"),
        ("f", "format", "Formater"),
        ("c", "check", "Verifier"),
    ]

    def __init__(self, *, refresh_seconds: float = 10.0) -> None:
        super().__init__()
        self.refresh_seconds = max(5.0, refresh_seconds)
        self.data: dict[str, Any] = {}
        self.rows: list[TargetRow] = []
        self.row_map: dict[str, TargetRow] = {}
        self.selected_id = ""
        self.busy = False
        self.message = "Pret."
        self.prompt_mode = ""
        self.pending_confirm = ""
        self.pending_format: Optional[PendingFormat] = None

    def compose(self) -> ComposeResult:
        with Container(id="frame"):
            yield Static("", id="topbar")
            with Horizontal(id="content"):
                with Vertical(id="disk_panel"):
                    yield Static("Disques", id="disk_title")
                    yield OptionList(id="disk_list")
                with Vertical(id="detail_panel"):
                    yield Static("", id="details")
                    yield RichLog(id="log", markup=True, wrap=True, highlight=False)
            with Vertical(id="prompt", classes="hidden"):
                yield Static("", id="prompt_label")
                yield Input("", id="prompt_input")
            with Horizontal(id="actions"):
                yield Static("Monter", id="action_mount", classes="action disabled")
                yield Static("Demonter", id="action_unmount", classes="action disabled")
                yield Static("Formater", id="action_format", classes="action disabled")
                yield Static("Verifier", id="action_check", classes="action disabled")
                yield Static("Rafraichir", id="action_refresh", classes="action enabled")

    def on_mount(self) -> None:
        self.query_one("#disk_list", OptionList).focus()
        self.set_interval(self.refresh_seconds, self.schedule_refresh)
        self.schedule_refresh()

    def schedule_refresh(self) -> None:
        if self.busy:
            return
        self.run_worker(self.refresh_data(), name="storage-refresh", exit_on_error=False, exclusive=True)

    async def refresh_data(self) -> None:
        self.busy = True
        try:
            data, rows, error = await asyncio.to_thread(collect_storage)
            self.data = data
            self.rows = rows
            self.row_map = {row.id: row for row in rows}
            if error:
                self.message = error
            if self.selected_id not in self.row_map:
                self.selected_id = rows[0].id if rows else ""
            self.render_all()
        finally:
            self.busy = False

    def selected_row(self) -> Optional[TargetRow]:
        return self.row_map.get(self.selected_id)

    def render_all(self) -> None:
        self.render_topbar()
        self.render_disk_list()
        self.render_details()
        self.render_actions()

    def render_topbar(self) -> None:
        summary = self.data.get("summary") or {}
        self.query_one("#topbar", Static).update(
            Text.assemble(
                ("YoLeo Stockage CLI", "bold white"),
                "\n",
                (
                    f"{summary.get('count', 0)} disque(s)  {summary.get('mounted', 0)} monte(s)  "
                    f"{summary.get('standby', 0)} veille  Total {summary.get('total_size', '-')}  "
                    f"Cache: {self.data.get('disk_log_file', '-')}",
                    "cyan",
                ),
            )
        )

    def render_disk_list(self) -> None:
        options = self.query_one("#disk_list", OptionList)
        options.clear_options()
        if not self.rows:
            options.add_option(Option(Text(self.message or "Aucun disque detecte.", style="bold red"), disabled=True))
            return
        selected_index = 0
        for index, row in enumerate(self.rows):
            style = "dim" if row.protected else ("bold cyan" if row.kind == "disk" else "white")
            mount = row.mount or "-"
            state = row.state or "-"
            text = Text.assemble(
                (row.label, style),
                ("  ", "dim"),
                (row.target, "dim"),
                "\n",
                (f"{row.size or '-'}  {row.fstype or '-'}  {state}  {mount}", "dim"),
            )
            options.add_option(Option(text, id=row.id))
            if row.id == self.selected_id:
                selected_index = index
        options.highlighted = selected_index

    def render_details(self) -> None:
        row = self.selected_row()
        if not row:
            self.query_one("#details", Static).update(self.message)
            return
        raw = row.raw or {}
        status_style = "dim" if row.protected else ("bold green" if row.mounted else "bold yellow")
        text = Text.assemble(
            ("Cible : ", "bold cyan"),
            (row.target, "bold white"),
            "  ",
            ("PROTEGE" if row.protected else ("MONTE" if row.mounted else "DEMONTE"), status_style),
            "\n",
            f"Type : {row.kind}  Disque : {row.disk_name}  Taille : {row.size or '-'}  FS : {row.fstype or '-'}\n",
            f"Montage : {row.mount or '-'}\n",
            f"Etat : {row.state or '-'}\n",
            f"Mode inventaire : {self.data.get('mode', '-')}\n",
            self.message,
        )
        if raw.get("health_label"):
            text.append(f"\nSMART : {raw.get('health_label')}", style="bold red" if raw.get("health_state") == "bad" else "green")
        self.query_one("#details", Static).update(text)

    def render_actions(self) -> None:
        row = self.selected_row()
        enabled = {
            "mount": bool(row and not row.mounted and not self.busy),
            "unmount": bool(row and row.mounted and not self.busy),
            "format": bool(row and not row.mounted and not row.protected and not self.busy),
            "check": bool(row and not row.mounted and not row.protected and not self.busy),
            "refresh": not self.busy,
        }
        for name, label in (
            ("mount", "Monter"),
            ("unmount", "Demonter"),
            ("format", "Formater"),
            ("check", "Verifier"),
            ("refresh", "Rafraichir"),
        ):
            widget = self.query_one(f"#action_{name}", Static)
            widget.update(label)
            widget.set_class(enabled[name], "enabled")
            widget.set_class(not enabled[name], "disabled")

    def set_message(self, message: str) -> None:
        self.message = message
        self.render_details()
        self.render_actions()

    def show_prompt(self, mode: str, label: str, value: str = "") -> None:
        self.prompt_mode = mode
        prompt = self.query_one("#prompt")
        prompt.remove_class("hidden")
        self.query_one("#prompt_label", Static).update(label)
        field = self.query_one("#prompt_input", Input)
        field.value = value
        field.focus()

    def hide_prompt(self) -> None:
        self.prompt_mode = ""
        self.query_one("#prompt").add_class("hidden")
        self.query_one("#disk_list", OptionList).focus()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id != "disk_list":
            return
        self.selected_id = str(event.option_id)
        self.pending_confirm = ""
        self.pending_format = None
        self.render_details()
        self.render_actions()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "disk_list":
            return
        self.selected_id = str(event.option_id)
        self.action_mount()

    @on(Input.Submitted, "#prompt_input")
    def on_prompt_submitted(self, event: Input.Submitted) -> None:
        row = self.selected_row()
        value = (event.value or "").strip()
        mode = self.prompt_mode
        self.hide_prompt()
        if not row:
            return
        if mode == "mount":
            mountpoint = value or default_mountpoint(row)
            self.run_worker(self.perform_storage_action("mount", row.target, mountpoint=mountpoint), name="storage-mount", exit_on_error=False)
        elif mode == "format":
            parts = value.split()
            fstype = (parts[0] if parts else "xfs").lower()
            label = " ".join(parts[1:]).strip()
            self.pending_format = PendingFormat(row.target, fstype, label)
            self.pending_confirm = "format"
            self.set_message(f"Confirme formatage {row.target} en {fstype} : appuie encore sur Formater.")
        event.stop()

    def action_quit(self) -> None:
        self.exit(0)

    def action_refresh(self) -> None:
        self.pending_confirm = ""
        self.pending_format = None
        self.message = "Rafraichissement..."
        self.schedule_refresh()

    def action_mount(self) -> None:
        row = self.selected_row()
        if not row or row.mounted:
            self.set_message("Montage indisponible pour cette ligne.")
            return
        self.show_prompt("mount", f"Point de montage pour {row.target}", default_mountpoint(row))

    def action_unmount(self) -> None:
        row = self.selected_row()
        if not row or not row.mounted:
            self.set_message("Démontage indisponible pour cette ligne.")
            return
        token = f"unmount:{row.target}"
        if self.pending_confirm != token:
            self.pending_confirm = token
            self.set_message(f"Confirme démontage {row.target} : appuie encore sur Demonter.")
            return
        self.pending_confirm = ""
        self.run_worker(self.perform_storage_action("unmount", row.target), name="storage-unmount", exit_on_error=False)

    def action_format(self) -> None:
        row = self.selected_row()
        if not row or row.mounted or row.protected:
            self.set_message("Formatage indisponible pour cette ligne.")
            return
        if self.pending_confirm == "format" and self.pending_format and self.pending_format.target == row.target:
            pending = self.pending_format
            self.pending_confirm = ""
            self.pending_format = None
            self.run_worker(
                self.perform_storage_action("format", pending.target, fstype=pending.fstype, label=pending.label),
                name="storage-format",
                exit_on_error=False,
            )
            return
        self.show_prompt("format", f"Format FS + label optionnel pour {row.target} (ex: xfs DATA)", "xfs")

    def action_check(self) -> None:
        row = self.selected_row()
        if not row or row.mounted or row.protected:
            self.set_message("Vérification indisponible pour cette ligne.")
            return
        token = f"check:{row.target}"
        if self.pending_confirm != token:
            self.pending_confirm = token
            self.set_message(f"Confirme vérification fsck {row.target} : appuie encore sur Verifier.")
            return
        self.pending_confirm = ""
        self.run_worker(self.perform_storage_action("check", row.target), name="storage-check", exit_on_error=False)

    async def perform_storage_action(self, action: str, target: str, **kwargs: str) -> None:
        self.busy = True
        self.render_actions()
        log = self.query_one("#log", RichLog)
        log.write(f"[bold cyan]# {action} {target}[/]")
        ok = False
        message = ""
        try:
            if action == "mount":
                ok, message = await asyncio.to_thread(mount_target, target, kwargs.get("mountpoint", ""))
            elif action == "unmount":
                ok, message = await asyncio.to_thread(unmount_target, target)
            elif action == "format":
                ok, message = await asyncio.to_thread(format_target, target, kwargs.get("fstype", "xfs"), kwargs.get("label", ""))
            elif action == "check":
                ok, message = await asyncio.to_thread(check_target, target)
            log.write(message.rstrip() or ("OK" if ok else "Erreur"))
            self.message = "OK." if ok else "Erreur."
        finally:
            self.busy = False
        await self.refresh_data()

    @on(Click, "#action_mount")
    def click_mount(self, event: Click) -> None:
        self.action_mount()
        event.stop()

    @on(Click, "#action_unmount")
    def click_unmount(self, event: Click) -> None:
        self.action_unmount()
        event.stop()

    @on(Click, "#action_format")
    def click_format(self, event: Click) -> None:
        self.action_format()
        event.stop()

    @on(Click, "#action_check")
    def click_check(self, event: Click) -> None:
        self.action_check()
        event.stop()

    @on(Click, "#action_refresh")
    def click_refresh(self, event: Click) -> None:
        self.action_refresh()
        event.stop()

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            self.exit(0)
            event.stop()
            return
        if event.character and event.character.isdigit():
            index = int(event.character) - 1
            if 0 <= index < len(self.rows):
                self.query_one("#disk_list", OptionList).highlighted = index
                event.stop()


def print_inventory() -> int:
    data, rows, error = collect_storage()
    if error:
        print(error, file=sys.stderr)
        return 1
    summary = data.get("summary") or {}
    print(f"{summary.get('count', 0)} disque(s), {summary.get('mounted', 0)} monté(s), total {summary.get('total_size', '-')}")
    for row in rows:
        print(f"{row.target}\t{row.kind}\t{row.size or '-'}\t{row.fstype or '-'}\t{row.mount or '-'}\t{row.state or '-'}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="CLI de secours stockage YoLeo")
    parser.add_argument("--list", action="store_true", help="Liste les disques puis quitte.")
    parser.add_argument("--refresh", type=float, default=10.0)
    args = parser.parse_args(argv)
    if args.list:
        return print_inventory()
    return int(StorageCliApp(refresh_seconds=args.refresh).run() or 0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
