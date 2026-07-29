#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Progression des commandes lancees par menu.py.

Ce module regroupe les formats de logs/status connus pour eviter de faire
grossir menu.py a chaque nouveau script.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
DEFAULT_CONF_DIR = BASE_DIR / "conf"
BACKUP_STATUS_DIR = Path(os.environ.get("YOLEO_BACKUP_STATUS_DIR", "/var/lib/yoleo/backup/status"))
BACKUP_LOG_DIR = Path(os.environ.get("YOLEO_BACKUP_LOG_DIR", "/var/log/yoleo/backup"))


@dataclass
class OperationState:
    title: str = ""
    command: str = ""
    category: str = "Exécution"
    current_name: str = ""
    current_index: int = 0
    total: int = 0
    ok_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    return_code: Optional[int] = None
    status: str = "prêt"
    started_at: float = 0.0
    ended_at: Optional[float] = None
    had_error: bool = False
    last_error_target: str = ""
    last_error_code: str = ""
    stdin_buffer: str = ""
    progress_unit: str = ""
    progress_detail: str = ""
    progress_source: str = ""
    progress_updated_at: float = 0.0


@dataclass
class StackProgressRange:
    stack: str
    start: int
    end: int
    dockers: list[str] = field(default_factory=list)


@dataclass
class ProgressPlan:
    kind: str = ""
    total: int = 0
    stack_ranges: dict[str, StackProgressRange] = field(default_factory=dict)
    docker_indexes: dict[str, int] = field(default_factory=dict)
    docker_names: dict[str, str] = field(default_factory=dict)
    status_path: Path | None = None
    log_path: Path | None = None
    status_kind: str = ""

    @property
    def tracks_stack_dockers(self) -> bool:
        return self.kind == "stack_dockers" and self.total > 0

    @property
    def tracks_runtime_status(self) -> bool:
        return self.status_kind != "" and (self.status_path is not None or self.log_path is not None)


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def classify_operation(title: str, command: str) -> str:
    text = f"{title} {command}".lower()
    if is_backup_command(command):
        return "Sauvegarde / cache rsync"
    if "docker_cli.py" in text and "--save" in text:
        return "Construction des images Docker"
    if "docker_cli.py" in text and "--load" in text:
        return "Chargement des images Docker"
    if "stacks.py" in text and "--start" in text:
        return "Démarrage des stacks Docker"
    if "stacks.py" in text and "--update" in text:
        return "Configuration des stacks Docker"
    if "registry.py" in text:
        return "Nettoyage du registre Docker"
    if "update_pip.sh" in text:
        return "Mise à jour des dépendances Python"
    if "system.sh" in text:
        return "Maintenance du système YoLeo"
    if "build" in text:
        return "Construction Docker"
    return "Exécution de commande"


def clean_conf_value(value: str) -> str:
    value = value.strip().rstrip("\r")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value.strip()


def split_csv(value: str) -> list[str]:
    return [part.strip().strip("\"'") for part in value.split(",") if part.strip()]


def read_kv_conf(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip().rstrip("\r")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = clean_conf_value(value)
        try:
            parts = shlex.split(value, posix=True)
            value = parts[0] if parts else ""
        except ValueError:
            pass
        data[key] = value
    return data


def resolve_relative_path(value: str, base: Path) -> Path:
    raw = str(value or "").strip()
    path = Path(os.path.expanduser(os.path.expandvars(raw)))
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def command_parts(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return []


def command_has_flag(parts: list[str], *names: str) -> bool:
    for part in parts:
        for name in names:
            if part == name or part.startswith(name + "="):
                return True
    return False


def command_value(parts: list[str], name: str) -> str:
    for index, part in enumerate(parts):
        if part == name and index + 1 < len(parts):
            return parts[index + 1]
        if part.startswith(name + "="):
            return part.split("=", 1)[1]
    return ""


def script_path_from_command(command: str) -> Path | None:
    parts = command_parts(command)
    if not parts:
        return None
    for part in parts:
        candidate = Path(os.path.expanduser(os.path.expandvars(part)))
        if candidate.suffix == ".py":
            return candidate.resolve() if candidate.is_absolute() else (SCRIPT_DIR / candidate).resolve()
    return None


def is_backup_command(command: str) -> bool:
    path = script_path_from_command(command)
    if path and path.suffix == ".py":
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:4096]
            if "BACKUP_RSYNC" in head or "BACKUP_UI_CONFIG_START" in head:
                return True
        except Exception:
            pass
    return "rsync" in command.lower() or "backup/status" in command.lower()


def backup_status_path_for_command(command: str) -> Path | None:
    path = script_path_from_command(command)
    if path and path.suffix == ".py" and is_backup_command(command):
        return BACKUP_STATUS_DIR / f"{path.stem}.status.json"
    return None


def backup_log_path_for_command(command: str) -> Path | None:
    path = script_path_from_command(command)
    if path and path.suffix == ".py" and is_backup_command(command):
        return BACKUP_LOG_DIR / f"{path.stem}.log"
    return None


def parse_stack_progress_conf(path: Path) -> list[tuple[str, list[str]]]:
    stacks: list[tuple[str, list[str]]] = []
    current_stack = ""
    current_dockers: list[str] = []

    def flush_current() -> None:
        nonlocal current_stack, current_dockers
        if current_stack:
            stacks.append((current_stack, list(current_dockers)))
        current_stack = ""
        current_dockers = []

    if not path.exists():
        return stacks
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip().rstrip("\r")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key_upper = key.strip().upper()
        value = clean_conf_value(value)
        if key_upper.startswith("STACK"):
            flush_current()
            current_stack = value
            continue
        if key_upper.startswith("YML") or key_upper.startswith("YAML"):
            for name in split_csv(value):
                current_dockers.append(Path(name).stem)
    flush_current()
    return stacks


def effective_stacks_conf_from_command(parts: list[str]) -> tuple[Path, dict[str, str]]:
    stacks_conf_arg = command_value(parts, "--stacks-conf")
    if stacks_conf_arg:
        return resolve_relative_path(stacks_conf_arg, SCRIPT_DIR), {}

    dockers_conf_arg = command_value(parts, "--dockers-conf")
    if dockers_conf_arg:
        dockers_conf = resolve_relative_path(dockers_conf_arg, SCRIPT_DIR)
    else:
        dockers_conf = resolve_relative_path(
            os.environ.get("DOCKERS_CONF_FILE", str(DEFAULT_CONF_DIR / "dockers.conf")),
            SCRIPT_DIR,
        )
    conf = read_kv_conf(dockers_conf)
    configured = os.environ.get("STACKS_CONF_FILE") or conf.get("SYSTEM_STACKS_CONF_FILE") or conf.get("STACKS_FILE")
    if configured:
        return resolve_relative_path(configured, dockers_conf.parent), conf
    return DEFAULT_CONF_DIR / "stacks.conf", conf


def build_progress_plan(command: str) -> ProgressPlan:
    status_path = backup_status_path_for_command(command)
    log_path = backup_log_path_for_command(command)
    parts = command_parts(command)
    if not parts:
        return ProgressPlan(
            status_path=status_path,
            log_path=log_path,
            status_kind="backup_rsync" if status_path or log_path else "",
        )

    script_name = Path(parts[0]).name
    if script_name != "stacks.py":
        if status_path or log_path:
            return ProgressPlan(
                kind="runtime_status",
                status_path=status_path,
                log_path=log_path,
                status_kind="backup_rsync",
            )
        return ProgressPlan()

    if command_has_flag(parts, "--list", "--ollama", "--olama", "--remove-ollama", "--remove-olama"):
        return ProgressPlan()
    if not command_has_flag(parts, "--update", "--install", "--start", "--all"):
        return ProgressPlan()

    stacks_conf, dockers_conf = effective_stacks_conf_from_command(parts)
    stack_filter = command_value(parts, "--stack")
    if not stack_filter and dockers_conf:
        stack_filter = command_value(command_parts(dockers_conf.get("SYSTEM_STACKS_EXTRA_ARGS", "")), "--stack")

    wanted = {item.lower() for item in split_csv(stack_filter)}
    stack_ranges: dict[str, StackProgressRange] = {}
    docker_indexes: dict[str, int] = {}
    docker_names: dict[str, str] = {}
    index = 0
    for stack, dockers in parse_stack_progress_conf(stacks_conf):
        if wanted and stack.lower() not in wanted:
            continue
        start = index + 1
        for docker in dockers:
            index += 1
            key = docker.lower()
            docker_indexes[key] = index
            docker_names[key] = docker
        if dockers:
            stack_ranges[stack.lower()] = StackProgressRange(stack=stack, start=start, end=index, dockers=dockers)

    if index == 0:
        return ProgressPlan()
    return ProgressPlan(
        kind="stack_dockers",
        total=index,
        stack_ranges=stack_ranges,
        docker_indexes=docker_indexes,
        docker_names=docker_names,
    )


COUNT_RE = re.compile(r"\[(\d+)\s*/\s*(\d+)\]\s*(?:NOM|NAME)\s*:?\s*(.+?)\s*$", re.IGNORECASE)
TOTAL_RE = re.compile(r"\b(?:TOTAL|Total dossiers|Total)\s*:?\s+(\d+)\b", re.IGNORECASE)
STACK_LIST_RE = re.compile(r"Stacks\s*:?\s*(.+)$", re.IGNORECASE)
STACK_RE = re.compile(r"Stack détectée\s*:\s*\[(.+?)\]", re.IGNORECASE)
STACK_OK_RE = re.compile(r"Stack\s+\[(.+?)\]\s+OK", re.IGNORECASE)
STACK_ERR_RE = re.compile(r"(?:Erreur sur|Stack erreur).*?\[(.+?)\]", re.IGNORECASE)
RC_RE = re.compile(r"(?:exit code|code retour|retour|rc)\s*[:=]\s*(\d+)", re.IGNORECASE)
OK_SUMMARY_RE = re.compile(r"(?:OK ou skip|Importés|Stacks OK|réussis)\s*:?\s+(\d+)", re.IGNORECASE)
FAIL_SUMMARY_RE = re.compile(r"(?:Échecs/manquants|Échecs|Stacks erreur|en erreur)\s*:?\s+(\d+)", re.IGNORECASE)
SKIP_SUMMARY_RE = re.compile(r"(?:Sans Dockerfile|ignoré\(s\)/skip)\s*:?\s+(\d+)", re.IGNORECASE)
SAVE_ITEM_OK_RE = re.compile(r"OK/SKIP\s*:\s*([A-Za-z0-9_.-]+)", re.IGNORECASE)
SAVE_ITEM_FAIL_RE = re.compile(r"ÉCHEC\s*:\s*([A-Za-z0-9_.-]+)", re.IGNORECASE)
RSYNC_PROGRESS_RE = re.compile(
    r"^\s*(?P<amount>[0-9][0-9., ]*\s*(?:[KMGTPE]?i?B?|bytes?|octets?)?)\s+"
    r"(?P<percent>100|[0-9]{1,2})%\s+"
    r"(?P<speed>[0-9][0-9.,]*\s*[KMGTPE]?i?B?/s)?\s*"
    r"(?P<eta>\d+:\d{2}:\d{2}|\d+:\d{2})?",
    re.IGNORECASE,
)


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)


def parse_size_to_bytes(value: str) -> int | None:
    raw = str(value or "").strip().replace("\xa0", " ")
    if not raw:
        return None
    match = re.match(r"^([0-9][0-9., ]*)\s*([KMGTPE]?i?B?|bytes?|octets?)?$", raw, flags=re.IGNORECASE)
    if not match:
        return None
    number = match.group(1).strip().replace(" ", "")
    unit = (match.group(2) or "").strip().upper()
    if not unit:
        try:
            return int(number.replace(",", "").replace(".", ""))
        except Exception:
            return None
    if "," in number and "." not in number:
        number = number.replace(",", ".")
    else:
        number = number.replace(",", "")
    try:
        amount = float(number)
    except Exception:
        return None
    if unit.startswith("BYTE") or unit.startswith("OCTET") or unit == "B":
        factor = 1
    else:
        base = 1024 if "I" in unit else 1000
        factor = base ** {"K": 1, "M": 2, "G": 3, "T": 4, "P": 5, "E": 6}.get(unit[:1], 0)
    return int(amount * factor)


def human_size(bytes_value: int | float | None) -> str:
    if bytes_value is None:
        return "-"
    value = float(bytes_value)
    units = ["o", "Ko", "Mo", "Go", "To", "Po"]
    for unit in units:
        if value < 1000 or unit == units[-1]:
            if unit == "o":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1000.0
    return f"{bytes_value} o"


def set_percent_progress(
    state: OperationState,
    percent: int,
    *,
    name: str = "",
    status: str = "",
    detail: str = "",
    source: str = "",
) -> None:
    percent = max(0, min(100, int(percent)))
    state.total = 100
    state.current_index = percent
    state.progress_unit = "percent"
    if name:
        state.current_name = name
    if status:
        state.status = status
    if detail:
        state.progress_detail = detail
    if source:
        state.progress_source = source
    state.progress_updated_at = time.time()


def update_state_from_rsync_line(state: OperationState, line: str, *, status: str = "rsync en cours") -> bool:
    match = RSYNC_PROGRESS_RE.search(line)
    if not match:
        return False
    amount_text = " ".join((match.group("amount") or "").split())
    copied_bytes = parse_size_to_bytes(amount_text)
    percent = int(match.group("percent"))
    copied_label = human_size(copied_bytes) if copied_bytes is not None else amount_text
    speed = " ".join((match.group("speed") or "").split())
    eta = match.group("eta") or ""
    detail_parts = [f"{copied_label} transférés"]
    if speed:
        detail_parts.append(speed)
    if eta:
        detail_parts.append(f"ETA {eta}")
    set_percent_progress(
        state,
        percent,
        name=state.current_name or state.title,
        status=status,
        detail=" · ".join(detail_parts),
        source="rsync",
    )
    return True


def update_stack_docker_progress(state: OperationState, line: str, plan: ProgressPlan) -> bool:
    stack_ok = STACK_OK_RE.search(line)
    if stack_ok:
        stack_name = stack_ok.group(1).strip()
        stack_range = plan.stack_ranges.get(stack_name.lower())
        if stack_range:
            state.total = plan.total
            state.current_index = max(state.current_index, stack_range.end)
            state.current_name = stack_range.dockers[-1] if stack_range.dockers else stack_range.stack
            state.status = f"stack {stack_range.stack} OK"
            return True

    stack_match = STACK_RE.search(line)
    if stack_match:
        stack_name = stack_match.group(1).strip()
        stack_range = plan.stack_ranges.get(stack_name.lower())
        if stack_range:
            state.total = plan.total
            state.current_index = max(state.current_index, stack_range.start)
            state.current_name = stack_range.dockers[0] if stack_range.dockers else stack_range.stack
            state.status = f"stack {stack_range.stack} en cours"
            return True
        state.current_name = stack_name
        state.status = "stack en cours"
        return True

    if "YAML :" in line or " -f " in line or " docker compose " in line or " docker-compose " in line:
        return False
    lower_line = line.lower()
    for key in sorted(plan.docker_indexes, key=len, reverse=True):
        if re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(key)}(?![A-Za-z0-9_.-])", lower_line):
            state.total = plan.total
            state.current_index = max(state.current_index, plan.docker_indexes[key])
            state.current_name = plan.docker_names.get(key, key)
            state.status = "mise à jour Docker"
            return True
    return False


def update_state_from_line(state: OperationState, raw_line: str, progress_plan: Optional[ProgressPlan] = None) -> None:
    line = strip_ansi(raw_line).strip()
    if not line:
        return
    progress_plan = progress_plan or ProgressPlan()

    if update_state_from_rsync_line(state, line):
        return

    if progress_plan.tracks_stack_dockers:
        state.total = progress_plan.total
        update_stack_docker_progress(state, line, progress_plan)

    total_match = TOTAL_RE.search(line)
    if total_match and state.total == 0:
        state.total = int(total_match.group(1))

    ok_summary = OK_SUMMARY_RE.search(line)
    if ok_summary:
        state.ok_count = int(ok_summary.group(1))
    fail_summary = FAIL_SUMMARY_RE.search(line)
    if fail_summary:
        state.failed_count = int(fail_summary.group(1))
    skip_summary = SKIP_SUMMARY_RE.search(line)
    if skip_summary:
        state.skipped_count = int(skip_summary.group(1))

    stack_list = STACK_LIST_RE.search(line)
    if stack_list and "Docker" not in line and not progress_plan.tracks_stack_dockers:
        names = [part.strip() for part in stack_list.group(1).split(",") if part.strip()]
        if names and state.total == 0:
            state.total = len(names)

    count_match = COUNT_RE.search(line)
    if count_match and not progress_plan.tracks_stack_dockers:
        state.current_index = int(count_match.group(1))
        state.total = max(state.total, int(count_match.group(2)))
        state.current_name = count_match.group(3).strip()
        state.status = "traitement en cours"
        return

    stack_match = STACK_RE.search(line)
    if stack_match and not progress_plan.tracks_stack_dockers:
        state.current_name = stack_match.group(1).strip()
        if state.total:
            state.current_index = min(state.current_index + 1, state.total)
        else:
            state.current_index += 1
        state.status = "configuration en cours"
        return

    if any(token in line for token in ("BUILDX OCI TAR", "buildx build", "docker buildx build")):
        state.status = "build en cours"
    elif "Vérification SHA256" in line:
        state.status = "vérification Docker"
    elif "IMPORT" in line or "Lecture du TAR OCI" in line:
        state.status = "chargement Docker"
    elif "pull" in line.lower():
        state.status = "mise à jour Docker"

    if STACK_OK_RE.search(line) or SAVE_ITEM_OK_RE.search(line):
        state.ok_count += 1
    if SAVE_ITEM_FAIL_RE.search(line):
        state.failed_count += 1
    zero_failure_summary = fail_summary and int(fail_summary.group(1)) == 0
    if not zero_failure_summary and ("❌" in line or "ÉCHEC" in line or "Erreur" in line or "ERROR:" in line):
        state.had_error = True
        state.status = "erreur détectée"
        state.last_error_target = state.current_name or state.title
        rc_match = RC_RE.search(line)
        if rc_match:
            state.last_error_code = rc_match.group(1)
    stack_error = STACK_ERR_RE.search(line)
    if stack_error:
        state.had_error = True
        state.failed_count += 1
        state.last_error_target = stack_error.group(1).strip()


def read_status_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


def status_is_fresh(path: Path, state: OperationState) -> bool:
    try:
        mtime = path.stat().st_mtime
    except Exception:
        return False
    if state.started_at and mtime < state.started_at - 2:
        return False
    return True


def update_state_from_backup_status(state: OperationState, path: Path) -> bool:
    if not status_is_fresh(path, state):
        return False
    data = read_status_json(path)
    if not data:
        return False

    title = str(data.get("title") or state.title or path.stem.replace(".status", ""))
    phase = str(data.get("phase") or "").strip()
    message = str(data.get("message") or "").strip()
    result = str(data.get("result") or "").strip()
    running = bool(data.get("running"))
    progress = data.get("progress") if isinstance(data.get("progress"), dict) else {}

    percent = progress.get("percent")
    try:
        percent_int = int(percent)
    except Exception:
        percent_int = None

    if percent_int is not None:
        detail = (
            progress.get("copied_total_label")
            or data.get("progress_text")
            or progress.get("copied_label")
            or progress.get("percent_label")
            or ""
        )
        speed = progress.get("speed") or ""
        eta = progress.get("eta") or ""
        extra = [str(detail)] if detail else []
        if speed:
            extra.append(str(speed))
        if eta:
            extra.append(f"ETA {eta}")
        set_percent_progress(
            state,
            percent_int,
            name=title,
            status=message or result or ("rsync en cours" if running else "rsync terminé"),
            detail=" · ".join(part for part in extra if part),
            source=str(path),
        )
    else:
        total_bytes = data.get("progress_total_bytes")
        total_label = data.get("progress_total_label")
        if total_bytes or total_label:
            state.current_name = title
            state.status = message or "pré-analyse rsync"
            state.progress_detail = f"Volume prévu : {total_label or human_size(total_bytes)}"
            state.progress_source = str(path)
        elif message or result:
            state.current_name = title
            state.status = message or result

    if not running and result:
        result_low = result.lower()
        if "erreur" in result_low or "arrêt" in result_low:
            state.had_error = True
            state.last_error_target = title
            if state.return_code is not None:
                state.last_error_code = state.last_error_code or str(state.return_code)
    return True


def update_state_from_backup_log(state: OperationState, path: Path) -> bool:
    if not status_is_fresh(path, state):
        return False
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 262_144))
            text = handle.read().decode("utf-8", errors="replace")
    except Exception:
        return False

    fallback_status = state.status if state.status not in {"", "prêt"} else "rsync lu depuis le log"
    for line in reversed(text.splitlines()):
        if update_state_from_rsync_line(state, line, status=fallback_status):
            state.progress_source = str(path)
            return True
    return False


def update_state_from_runtime(state: OperationState, progress_plan: Optional[ProgressPlan] = None) -> bool:
    plan = progress_plan or ProgressPlan()
    if plan.status_kind == "backup_rsync":
        changed = False
        if plan.status_path:
            changed = update_state_from_backup_status(state, plan.status_path)
        if (not state.progress_unit or state.progress_unit != "percent") and plan.log_path:
            changed = update_state_from_backup_log(state, plan.log_path) or changed
        return changed
    return False


def seconds_text(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def progress_bar_text(current: int, total: int, label: str = "") -> str:
    if total <= 0:
        return ""
    width = max(24, min(total, 40))
    current = max(0, min(current, total))
    filled = round((current / total) * width)
    if current > 0:
        filled = max(1, filled)
    filled = min(width, filled)
    suffix = label or f"{current}/{total}"
    return f"{'█' * filled}{'░' * (width - filled)}  {suffix}"
