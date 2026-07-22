#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime indépendant du gestionnaire de scripts Yoleo.

Deux modes :
- --service : processus possédé par systemd (ou réadopté par PID 1) qui crée et
  surveille la session tmux ; il n'est pas un enfant durable de Gunicorn.
- --pane : processus exécuté dans le pane tmux qui lance le script dans un PTY,
  recopie la sortie dans le terminal et dans le fichier de log, puis conserve
  la session ouverte en cas d'erreur.

Ce module n'utilise volontairement pas subprocess pour porter l'exécution.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import pty
import re
import select
import shlex
import shutil
import signal
import sys
import termios
import time
import tty
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

TMUX_SOCKET_NAME = "yoleo-scripts"
SAFE_SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json_write(path: str, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + f".tmp.{os.getpid()}")
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, target)


def read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def merge_status(path: str, **updates) -> dict:
    data = read_json(path)
    data.update(updates)
    data.setdefault("updated_at", utc_now())
    data["updated_at"] = utc_now()
    atomic_json_write(path, data)
    return data


def _wait_status(pid: int) -> int:
    while True:
        try:
            _pid, status = os.waitpid(pid, 0)
            return status
        except InterruptedError:
            continue


def run_argv_capture(argv: Iterable[str], timeout: float = 12.0) -> tuple[int, str]:
    """Exécute une commande de contrôle courte via fork/exec, sans subprocess."""
    args = [str(item) for item in argv]
    if not args:
        return 127, "Commande vide."

    read_fd, write_fd = os.pipe()
    os.set_inheritable(write_fd, True)
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_fd)
            os.dup2(write_fd, 1)
            os.dup2(write_fd, 2)
            if write_fd > 2:
                os.close(write_fd)
            os.execvpe(args[0], args, os.environ.copy())
        except BaseException as exc:
            try:
                os.write(2, (f"exec impossible: {exc}\n").encode("utf-8", "replace"))
            except Exception:
                pass
            os._exit(127)

    os.close(write_fd)
    os.set_blocking(read_fd, False)
    output = bytearray()
    deadline = time.monotonic() + max(0.5, float(timeout))
    child_status = None
    eof = False

    while child_status is None or not eof:
        if time.monotonic() >= deadline and child_status is None:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            child_status = _wait_status(pid)

        try:
            ready, _, _ = select.select([read_fd], [], [], 0.08)
        except InterruptedError:
            ready = []
        if ready:
            try:
                chunk = os.read(read_fd, 65536)
                if chunk:
                    output.extend(chunk)
                else:
                    eof = True
            except BlockingIOError:
                pass
            except OSError:
                eof = True

        if child_status is None:
            try:
                waited_pid, status = os.waitpid(pid, os.WNOHANG)
                if waited_pid == pid:
                    child_status = status
            except ChildProcessError:
                child_status = 0

        if child_status is not None and not ready:
            try:
                chunk = os.read(read_fd, 65536)
                if chunk:
                    output.extend(chunk)
                else:
                    eof = True
            except BlockingIOError:
                pass
            except OSError:
                eof = True

    try:
        os.close(read_fd)
    except OSError:
        pass

    if child_status is None:
        child_status = _wait_status(pid)
    if os.WIFEXITED(child_status):
        code = os.WEXITSTATUS(child_status)
    elif os.WIFSIGNALED(child_status):
        code = 128 + os.WTERMSIG(child_status)
    else:
        code = 1
    return code, output.decode("utf-8", "replace").strip()


def tmux_binary() -> str:
    return shutil.which("tmux") or "/usr/bin/tmux"


def valid_session(session: str) -> str:
    value = str(session or "").strip()
    if not SAFE_SESSION_RE.fullmatch(value):
        raise ValueError("Nom de session tmux invalide.")
    return value


def tmux_argv(*args: str) -> list[str]:
    return [tmux_binary(), "-L", TMUX_SOCKET_NAME, *[str(arg) for arg in args]]


def tmux_has_session(session: str) -> bool:
    session = valid_session(session)
    code, _ = run_argv_capture(tmux_argv("has-session", "-t", session), timeout=3.0)
    return code == 0


def tmux_kill_session(session: str) -> tuple[bool, str]:
    session = valid_session(session)
    if not tmux_has_session(session):
        return True, "Session tmux déjà arrêtée."
    code, output = run_argv_capture(tmux_argv("kill-session", "-t", session), timeout=6.0)
    if code == 0:
        return True, "Session tmux arrêtée."
    return False, output or f"tmux a retourné le code {code}."


def pane_command(script_path: str) -> list[str]:
    suffix = Path(script_path).suffix.lower()
    if suffix == ".sh":
        return [shutil.which("bash") or "/bin/bash", script_path]
    if suffix == ".py":
        return [shutil.which("python3") or "/usr/bin/python3", "-u", script_path]
    raise ValueError("Seuls les scripts .sh et .py sont acceptés.")


def append_log_line(log_path: str, text: str) -> None:
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab", buffering=0) as handle:
        handle.write(text.encode("utf-8", "replace"))


def run_pane(args: argparse.Namespace) -> int:
    script_path = os.path.abspath(args.script)
    status_path = os.path.abspath(args.status)
    log_path = os.path.abspath(args.log)
    session = valid_session(args.session)
    title = str(args.title or Path(script_path).stem)

    if not os.path.isfile(script_path):
        merge_status(
            status_path,
            state="error",
            session=session,
            script=script_path,
            title=title,
            message="Script introuvable.",
            exit_code=127,
            finished_at=utc_now(),
        )
        print(f"Script introuvable : {script_path}", flush=True)
        return 127

    command = pane_command(script_path)
    started_at = utc_now()
    merge_status(
        status_path,
        state="running",
        session=session,
        script=script_path,
        title=title,
        command=command,
        pid=os.getpid(),
        started_at=started_at,
        finished_at="",
        exit_code=None,
        message="Exécution dans tmux.",
    )

    header = (
        "\n\n"
        + "=" * 78
        + f"\n[{started_at}] {title}\nScript : {script_path}\nSession tmux : {session}\nCommande : {shlex.join(command)}\n"
        + "=" * 78
        + "\n"
    )
    append_log_line(log_path, header)
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.write(header)
    sys.stdout.flush()

    child_pid = 0
    stop_requested = {"value": False, "signal": 0}

    def request_stop(signum, _frame):
        stop_requested["value"] = True
        stop_requested["signal"] = int(signum)
        if child_pid > 0:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except OSError:
                pass

    for signum in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(signum, request_stop)

    old_tty = None
    stdin_fd = None
    try:
        if sys.stdin.isatty():
            stdin_fd = sys.stdin.fileno()
            old_tty = termios.tcgetattr(stdin_fd)
            tty.setraw(stdin_fd)
    except Exception:
        old_tty = None
        stdin_fd = None

    try:
        child_pid, master_fd = pty.fork()
        if child_pid == 0:
            try:
                os.chdir(os.path.dirname(script_path) or "/")
                env = os.environ.copy()
                env.setdefault("PYTHONUNBUFFERED", "1")
                env.setdefault("TERM", "xterm-256color")
                os.execvpe(command[0], command, env)
            except BaseException as exc:
                os.write(2, (f"Impossible de lancer le script : {exc}\n").encode("utf-8", "replace"))
                os._exit(127)

        with open(log_path, "ab", buffering=0) as log_handle:
            watched = [master_fd]
            if stdin_fd is not None:
                watched.append(stdin_fd)

            while True:
                if stop_requested["value"]:
                    try:
                        os.kill(child_pid, signal.SIGTERM)
                    except OSError:
                        pass

                try:
                    readable, _, _ = select.select(watched, [], [], 0.15)
                except InterruptedError:
                    readable = []

                if master_fd in readable:
                    try:
                        data = os.read(master_fd, 65536)
                    except OSError as exc:
                        if exc.errno == errno.EIO:
                            data = b""
                        else:
                            raise
                    if not data:
                        break
                    try:
                        os.write(sys.stdout.fileno(), data)
                    except OSError:
                        pass
                    log_handle.write(data)

                if stdin_fd is not None and stdin_fd in readable:
                    try:
                        data = os.read(stdin_fd, 4096)
                    except OSError:
                        data = b""
                    if data:
                        try:
                            os.write(master_fd, data)
                        except OSError:
                            pass

                try:
                    waited_pid, child_status = os.waitpid(child_pid, os.WNOHANG)
                except ChildProcessError:
                    waited_pid, child_status = child_pid, 0
                if waited_pid == child_pid:
                    # Vide les derniers octets encore présents dans le PTY.
                    while True:
                        try:
                            trailing = os.read(master_fd, 65536)
                        except OSError:
                            break
                        if not trailing:
                            break
                        try:
                            os.write(sys.stdout.fileno(), trailing)
                        except OSError:
                            pass
                        log_handle.write(trailing)
                    break
            else:
                child_status = _wait_status(child_pid)

        try:
            os.close(master_fd)
        except OSError:
            pass

        # Si la boucle s'est terminée sur EOF avant le waitpid final.
        try:
            waited_pid, final_status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                child_status = final_status
            elif waited_pid == 0:
                child_status = _wait_status(child_pid)
        except ChildProcessError:
            pass

    finally:
        if old_tty is not None and stdin_fd is not None:
            try:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_tty)
            except Exception:
                pass

    if os.WIFEXITED(child_status):
        exit_code = os.WEXITSTATUS(child_status)
    elif os.WIFSIGNALED(child_status):
        exit_code = 128 + os.WTERMSIG(child_status)
    else:
        exit_code = 1

    finished_at = utc_now()
    stopped = stop_requested["value"] or exit_code in {129, 130, 143}
    if stopped:
        state = "stopped"
        message = "Script arrêté."
    elif exit_code == 0:
        state = "success"
        message = "Script terminé sans erreur."
    else:
        state = "error"
        message = f"Le script s'est terminé avec le code {exit_code}."

    footer = (
        "\n"
        + "-" * 78
        + f"\n[{finished_at}] {message}\nCode retour : {exit_code}\n"
        + "-" * 78
        + "\n"
    )
    append_log_line(log_path, footer)
    sys.stdout.write(footer)
    sys.stdout.flush()

    merge_status(
        status_path,
        state=state,
        session=session,
        script=script_path,
        title=title,
        exit_code=exit_code,
        finished_at=finished_at,
        message=message,
    )

    if state == "success":
        time.sleep(1.0)
        return 0
    if state == "stopped":
        time.sleep(0.4)
        return exit_code

    # En erreur, le pane reste vivant. La popup garde donc exactement la sortie
    # et offre un shell de diagnostic. Le bouton Stop ou la fermeture de la
    # popup terminée détruit ensuite la session tmux.
    sys.stdout.write("\nLa session reste ouverte pour diagnostic. Le bouton Stop la fermera.\n\n")
    sys.stdout.flush()
    shell = shutil.which("bash") or "/bin/bash"
    env = os.environ.copy()
    env["PS1"] = "[script en erreur] \\w # "
    os.chdir(os.path.dirname(script_path) or "/")
    os.execvpe(shell, [shell, "--noprofile", "--norc", "-i"], env)
    return exit_code


def run_service(args: argparse.Namespace) -> int:
    script_path = os.path.abspath(args.script)
    status_path = os.path.abspath(args.status)
    log_path = os.path.abspath(args.log)
    session = valid_session(args.session)
    title = str(args.title or Path(script_path).stem)

    if tmux_has_session(session):
        merge_status(status_path, state="running", message="Session tmux déjà présente.")
        return 0

    runtime = os.path.abspath(__file__)
    pane_argv = [
        sys.executable or "/usr/bin/python3",
        runtime,
        "--pane",
        "--session", session,
        "--script", script_path,
        "--status", status_path,
        "--log", log_path,
        "--title", title,
    ]
    shell_command = shlex.join(pane_argv)
    merge_status(
        status_path,
        state="starting",
        session=session,
        script=script_path,
        title=title,
        service_pid=os.getpid(),
        message="Création de la session tmux.",
    )

    code, output = run_argv_capture(
        tmux_argv("new-session", "-d", "-s", session, "-c", os.path.dirname(script_path) or "/", shell_command),
        timeout=8.0,
    )
    if code != 0:
        merge_status(
            status_path,
            state="error",
            exit_code=code,
            finished_at=utc_now(),
            message=output or f"Impossible de créer la session tmux (code {code}).",
        )
        return code or 1

    # Le service reste vivant comme propriétaire système de l'exécution. Il est
    # dans une unité systemd distincte de Gunicorn, ou réadopté par PID 1 en
    # fallback double-fork.
    while tmux_has_session(session):
        time.sleep(1.0)

    data = read_json(status_path)
    if data.get("state") in {"starting", "running", ""}:
        merge_status(
            status_path,
            state="stopped",
            finished_at=utc_now(),
            message="La session tmux s'est arrêtée sans état final.",
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Runtime du gestionnaire de scripts Yoleo")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--service", action="store_true")
    mode.add_argument("--pane", action="store_true")
    parser.add_argument("--session", required=True)
    parser.add_argument("--script", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--title", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.service:
        return run_service(args)
    return run_pane(args)


if __name__ == "__main__":
    raise SystemExit(main())
