#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Menu generique pour lister et lancer des scripts .py/.sh d'un dossier."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shlex
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.events import Click, Key
from textual.widgets import OptionList, ProgressBar, RichLog, Static
from textual.widgets.option_list import Option


SCRIPT_DIR = Path(__file__).resolve().parent
VENV_PYTHON = SCRIPT_DIR / ".venv" / "bin" / "python"
PYTHON_BIN = str(VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable).resolve())
ALLOWED_EXTENSIONS = {".py", ".sh"}
METADATA_RE = re.compile(
    r"^\s*#\s*(title|titre|name|nom|label|display|description|detail|détail)\s*=\s*(.*?)\s*$",
    re.IGNORECASE,
)

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

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


@dataclass
class ScriptEntry:
    title: str
    name: str
    path: Path
    description: str = ""

    @property
    def command(self) -> str:
        return str(self.path)


def single_line(value: str, limit: int = 240) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    return text[:limit].strip()


def metadata_key(raw_key: str) -> str:
    key = raw_key.lower().strip()
    if key in {"title", "titre", "name", "nom", "label", "display"}:
        return "title"
    return "description"


def parse_script_metadata(path: Path) -> tuple[str, str]:
    title = ""
    description = ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:96_000]
    except Exception:
        return path.stem, ""

    header_open = True
    for index, raw in enumerate(text.splitlines()[:120]):
        clean = raw.strip()
        match = METADATA_RE.match(raw) if header_open else None
        if match:
            key = metadata_key(match.group(1))
            value = single_line(match.group(2), 1000)
            if key == "title" and value:
                title = value
            elif key == "description" and value:
                description = value
            continue

        if index == 0 and clean.startswith("#!"):
            continue
        if header_open and (not clean or clean.startswith("#")):
            continue
        header_open = False

    return title or path.stem, description


def normalize_directory_arg(value: str) -> str:
    raw = str(value or "").strip()
    if raw.startswith("-/") or raw.startswith("-~") or raw.startswith("-."):
        raw = raw[1:]
    return raw


def resolve_directory(value: str) -> Path:
    raw = normalize_directory_arg(value)
    if not raw:
        raw = "."
    return Path(os.path.expanduser(os.path.expandvars(raw))).resolve()


def scan_scripts(directory: Path) -> tuple[list[ScriptEntry], str]:
    if not directory.exists():
        return [], f"Dossier introuvable : {directory}"
    if not directory.is_dir():
        return [], f"Ce chemin n'est pas un dossier : {directory}"

    rows: list[ScriptEntry] = []
    try:
        children = sorted(directory.iterdir(), key=lambda item: item.name.lower())
    except Exception as exc:
        return [], f"Lecture impossible : {exc}"

    for path in children:
        if path.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        if path.is_symlink() or not path.is_file():
            continue
        title, description = parse_script_metadata(path)
        rows.append(ScriptEntry(title=title, name=path.name, path=path, description=description))

    if not rows:
        return [], f"Aucun script .py/.sh trouve dans : {directory}"
    return rows, ""


def command_for_entry(entry: ScriptEntry) -> list[str]:
    if entry.path.suffix.lower() == ".py":
        return [PYTHON_BIN, str(entry.path)]
    return ["bash", str(entry.path)]


def row_text(index: int, entry: ScriptEntry) -> Text:
    title = single_line(entry.title, 38)
    name = single_line(entry.name, 42)
    style = "cyan" if entry.path.suffix.lower() == ".py" else "green"
    text = Text()
    text.append(f"{index + 1:>2}. ", style="dim")
    text.append(f"{title:<38.38}", style="bold white")
    text.append("  ")
    text.append(name, style=style)
    return text


class MenuListApp(App[int]):
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

    #footer {
        height: 2;
        padding: 0 2;
        background: #123638;
        color: #d9fff3;
    }

    #list_view, #operation_view {
        height: 100%;
    }

    #list_area {
        height: 1fr;
    }

    #script_panel {
        width: 56%;
        border: solid #2fa77f;
        padding: 0 1;
        background: #091c1d;
    }

    #details_panel {
        width: 1fr;
        border: solid #2fa77f;
        padding: 0 1;
        background: #091c1d;
    }

    #table_header {
        height: 1;
        color: #9df5ce;
        text-style: bold;
    }

    #script_options {
        height: 1fr;
        background: #050d0e;
    }

    #message {
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
        ("q", "quit", "Quitter"),
        ("r", "refresh", "Rafraichir"),
        ("f2", "general", "Général"),
        ("f3", "detail", "Détail"),
    ]

    def __init__(self, directory: Path) -> None:
        super().__init__()
        self.directory = directory
        self.entries: list[ScriptEntry] = []
        self.option_map: dict[str, int] = {}
        self.selected_index = 0
        self.message = ""
        self.in_operation = False
        self.operation_view = "general"
        self.state = OperationState()
        self.progress_plan = ProgressPlan()
        self.process: asyncio.subprocess.Process | None = None
        self.spinner_index = 0
        self.tick_timer = None

    def compose(self) -> ComposeResult:
        with Container(id="frame"):
            yield Static("", id="topbar")
            with Container(id="content"):
                with Container(id="list_view"):
                    with Horizontal(id="list_area"):
                        with Vertical(id="script_panel"):
                            yield Static("", id="table_header")
                            yield OptionList(id="script_options")
                        yield Static("", id="details_panel")
                    yield Static("", id="message")
                with Container(id="operation_view", classes="hidden"):
                    yield Static("", id="operation_header")
                    with Horizontal(id="tabs"):
                        yield Static(" GÉNÉRAL ", id="tab_general", classes="tab active")
                        yield Static(" | ", id="tab_separator")
                        yield Static(" DÉTAIL ", id="tab_detail", classes="tab")
                    with Container(id="general_panel"):
                        yield Static("", id="general_title")
                        yield Static("", id="general_body")
                        yield ProgressBar(total=1, show_eta=False, id="progress")
                    with Container(id="detail_panel", classes="hidden"):
                        yield RichLog(id="detail_log", wrap=True, highlight=True, markup=False)
            yield Static("", id="footer")

    def on_mount(self) -> None:
        self.tick_timer = self.set_interval(0.5, self.tick_operation)
        self.refresh_entries()

    def render_topbar(self) -> None:
        running = self.process is not None and self.process.returncode is None
        subtitle = "Execution de script" if self.in_operation else f"{len(self.entries)} script(s)  Dossier: {self.directory}"
        if running:
            subtitle += "  en cours"
        self.query_one("#topbar", Static).update(Text.assemble(("YoLeo Menu List", "bold white"), "\n", (subtitle, "cyan")))

    def render_footer(self) -> None:
        if self.in_operation:
            text = "Échap Retour  Q Quitter  F2 Général  F3 Détail"
        else:
            text = "Entrée Lancer  R Rafraîchir  Échap/Q Retour"
        self.query_one("#footer", Static).update(text)

    def refresh_entries(self) -> None:
        self.entries, self.message = scan_scripts(self.directory)
        self.option_map.clear()
        options = self.query_one("#script_options", OptionList)
        options.clear_options()
        for index, entry in enumerate(self.entries):
            option_id = f"script-{index}"
            self.option_map[option_id] = index
            options.add_option(Option(row_text(index, entry), id=option_id))
        if self.entries:
            self.selected_index = min(self.selected_index, len(self.entries) - 1)
            options.highlighted = self.selected_index
            options.focus()
        else:
            self.selected_index = 0
        self.render_list()

    def selected_entry(self) -> ScriptEntry | None:
        if 0 <= self.selected_index < len(self.entries):
            return self.entries[self.selected_index]
        return None

    def render_list(self) -> None:
        self.in_operation = False
        self.query_one("#list_view").remove_class("hidden")
        self.query_one("#operation_view").add_class("hidden")
        self.render_topbar()
        self.render_footer()
        self.query_one("#table_header", Static).update(Text.assemble(("Nom affiche", "bold"), " " * 31, ("Script", "bold cyan")))
        entry = self.selected_entry()
        if entry:
            details = Text.assemble(
                ("Selection\n", "bold white"),
                ("Nom affiche : ", "cyan"),
                entry.title,
                "\n",
                ("Script      : ", "cyan"),
                entry.name,
                "\n",
                ("Chemin      : ", "cyan"),
                str(entry.path),
                "\n",
                ("Type        : ", "cyan"),
                entry.path.suffix.lower(),
                "\n",
            )
            if entry.description:
                details.append("\n")
                details.append("Description\n", style="bold white")
                details.append(entry.description)
        else:
            details = Text("Aucun script selectionne.", style="yellow")
        self.query_one("#details_panel", Static).update(details)
        self.query_one("#message", Static).update(self.message)

    def show_operation(self, entry: ScriptEntry) -> None:
        self.in_operation = True
        self.operation_view = "general"
        self.query_one("#list_view").add_class("hidden")
        self.query_one("#operation_view").remove_class("hidden")
        self.state = OperationState(
            title=entry.title,
            command=entry.command,
            category=classify_operation(entry.title, entry.command),
            started_at=time.time(),
            status="lancement",
        )
        self.progress_plan = build_progress_plan(entry.command)
        self.query_one("#detail_log", RichLog).clear()
        self.render_operation()

    async def run_entry(self, entry: ScriptEntry) -> None:
        self.show_operation(entry)
        argv = command_for_entry(entry)
        self.write_log(f">>> {shlex.join(argv)}\n\n")
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("COMPOSE_PROGRESS", "plain")
        try:
            self.process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(entry.path.parent),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            self.state.had_error = True
            self.state.status = "commande introuvable"
            self.state.return_code = 127
            self.write_log(f"Commande introuvable : {exc}\n")
            self.render_operation()
            return
        except Exception as exc:
            self.state.had_error = True
            self.state.status = "erreur de lancement"
            self.state.return_code = 1
            self.write_log(f"Impossible de lancer le script : {exc}\n")
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
            if self.state.progress_unit == "percent":
                self.state.total = 100
                self.state.current_index = 100
            elif self.progress_plan.total:
                self.state.total = self.progress_plan.total
                self.state.current_index = self.progress_plan.total
            self.state.status = "termine"
            self.write_log("\nOK\n")
        else:
            self.state.had_error = True
            self.state.last_error_code = self.state.last_error_code or str(rc)
            self.state.status = "termine avec erreur"
            self.operation_view = "detail"
            self.write_log(f"\nErreur : {rc}\n")
        self.render_operation()

    def write_log(self, text: str) -> None:
        log = self.query_one("#detail_log", RichLog)
        for line in text.splitlines(keepends=True):
            content = line.rstrip("\n")
            if "\x1b[" in content:
                log.write(Text.from_ansi(content), scroll_end=True)
            else:
                log.write(content, scroll_end=True)

    def render_operation(self) -> None:
        self.render_topbar()
        self.render_footer()
        running = self.process is not None and self.process.returncode is None
        elapsed_end = self.state.ended_at if self.state.ended_at is not None else time.time()
        elapsed = seconds_text(elapsed_end - self.state.started_at) if self.state.started_at else "00:00:00"
        self.query_one("#operation_header", Static).update(
            Text.assemble(
                (self.state.title, "bold white"),
                "\n",
                (self.state.category, "cyan"),
                "  ",
                ("en cours" if running else "termine", "yellow" if running else "green"),
                "  Duree: ",
                (elapsed, "white"),
                "\n",
                (self.state.command, "dim"),
            )
        )
        tab_general = self.query_one("#tab_general", Static)
        tab_detail = self.query_one("#tab_detail", Static)
        tab_general.set_class(self.operation_view == "general", "active")
        tab_detail.set_class(self.operation_view == "detail", "active")
        self.query_one("#general_panel").set_class(self.operation_view != "general", "hidden")
        self.query_one("#detail_panel").set_class(self.operation_view != "detail", "hidden")

        spinner = SPINNER[self.spinner_index % len(SPINNER)] if running else "■"
        current_name = self.state.current_name or self.state.title or "en attente"
        current_index = self.state.current_index
        total = self.state.total
        if total and (current_index or self.state.progress_unit == "percent"):
            if self.state.progress_unit == "percent":
                progress_line = f"{current_index}%"
                progress_text = progress_bar_text(current_index, total, f"{current_index}%")
            else:
                progress_line = f"{current_index} sur {total}"
                progress_text = progress_bar_text(current_index, total)
            self.query_one("#progress", ProgressBar).update(total=total, progress=min(current_index, total))
        else:
            progress_line = "progression globale non encore detectee"
            progress_text = ""
            self.query_one("#progress", ProgressBar).update(total=1, progress=0)

        if self.progress_plan.tracks_stack_dockers:
            item_word = "Docker"
            name_label = "Docker"
        elif "rsync" in self.state.category.lower() or "sauvegarde" in self.state.category.lower():
            item_word = "Transfert"
            name_label = "Script"
        else:
            item_word = "Etape"
            name_label = "Nom"

        summary = ""
        if self.state.return_code is not None:
            if self.state.total or self.state.ok_count or self.state.failed_count or self.state.skipped_count:
                failed = self.state.failed_count
                if self.state.return_code != 0 and failed == 0:
                    failed = 1
                summary = (
                    f"\n\nBilan\n"
                    f"{self.state.total or max(self.state.ok_count + failed + self.state.skipped_count, 1)} demande(s)\n"
                    f"{self.state.ok_count} reussi(s)\n"
                    f"{failed} en erreur\n"
                    f"{self.state.skipped_count} ignore(s)/skip"
                )
            else:
                summary = f"\n\nCode retour : {self.state.return_code}"

        error_text = ""
        if self.state.had_error:
            code = self.state.last_error_code or (str(self.state.return_code) if self.state.return_code is not None else "?")
            error_text = f"\n\nERREUR\nCible : {self.state.last_error_target or current_name}\nCode retour : {code}"

        detail_text = f"{self.state.progress_detail}\n" if self.state.progress_detail else ""
        self.query_one("#general_title", Static).update(Text(self.state.category, style="bold white"))
        self.query_one("#general_body", Static).update(
            Text.assemble(
                f"\n{item_word} {progress_line}",
                ("  " + progress_text, "bold cyan") if progress_text else "",
                "\n",
                f"{name_label} : {current_name}\n",
                (detail_text, "cyan") if detail_text else "",
                f"Etat : {self.state.status} {spinner if running else ''}\n",
                f"Duree : {elapsed}\n",
                (summary, "green" if self.state.return_code == 0 else "yellow"),
                (error_text, "bold red"),
            )
        )

    def tick_operation(self) -> None:
        if not self.in_operation:
            return
        update_state_from_runtime(self.state, self.progress_plan)
        self.spinner_index += 1
        self.render_operation()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id != "script_options":
            return
        index = self.option_map.get(str(event.option_id))
        if index is not None:
            self.selected_index = index
            self.render_list()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "script_options":
            return
        index = self.option_map.get(str(event.option_id))
        if index is None or not (0 <= index < len(self.entries)):
            return
        self.selected_index = index
        self.run_worker(self.run_entry(self.entries[index]), name="menu-list-run", exit_on_error=False)

    @on(Click, "#tab_general")
    def click_tab_general(self, event: Click) -> None:
        if self.in_operation:
            self.operation_view = "general"
            self.render_operation()

    @on(Click, "#tab_detail")
    def click_tab_detail(self, event: Click) -> None:
        if self.in_operation:
            self.operation_view = "detail"
            self.render_operation()

    def action_back(self) -> None:
        if self.in_operation:
            if self.process is not None and self.process.returncode is None:
                self.query_one("#footer", Static).update("Commande en cours : retour disponible apres la fin.")
                return
            self.refresh_entries()
            return
        self.exit(0)

    def action_quit(self) -> None:
        self.exit(0)

    def action_refresh(self) -> None:
        if not self.in_operation:
            self.refresh_entries()

    def action_general(self) -> None:
        if self.in_operation:
            self.operation_view = "general"
            self.render_operation()

    def action_detail(self) -> None:
        if self.in_operation:
            self.operation_view = "detail"
            self.render_operation()

    def on_key(self, event: Key) -> None:
        if self.in_operation:
            return
        if event.character and event.character.isdigit():
            wanted = int(event.character)
            if 1 <= wanted <= len(self.entries):
                self.selected_index = wanted - 1
                self.query_one("#script_options", OptionList).highlighted = self.selected_index
                self.run_worker(self.run_entry(self.entries[self.selected_index]), name="menu-list-run", exit_on_error=False)
                event.stop()


def run_self_test() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "backup.py").write_text("#!/usr/bin/env python3\n# TITLE=Docker Backup\nprint('ok')\n", encoding="utf-8")
        (root / "script.sh").write_text("#!/bin/bash\n# title=Update Offline Yoleo\necho ok\n", encoding="utf-8")
        (root / "note.txt").write_text("ignore\n", encoding="utf-8")
        rows, error = scan_scripts(root)
        assert not error
        assert [row.name for row in rows] == ["backup.py", "script.sh"]
        assert rows[0].title == "Docker Backup"
        assert rows[1].title == "Update Offline Yoleo"
    print("self-test OK")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    normalized: list[str] = []
    for item in argv:
        if item.startswith("-/") or item.startswith("-~") or item.startswith("-."):
            normalized.append(item[1:])
        else:
            normalized.append(item)
    parser = argparse.ArgumentParser(description="Liste et lance les scripts .py/.sh d'un dossier.")
    parser.add_argument("directory", nargs="?", default=".", help="Dossier a lister. Accepte aussi la forme -/chemin.")
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args(normalized)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.self_test:
        return run_self_test()
    directory = resolve_directory(args.directory)
    app = MenuListApp(directory)
    return int(app.run() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
