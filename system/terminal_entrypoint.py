#!/usr/bin/env python3
import argparse
import os
import re
import shlex
import subprocess
import sys
import time


SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:/-]+$")


def safe_value(value: str) -> str:
    value = str(value or "").strip().lstrip("/")
    if not value or not SAFE_NAME_RE.match(value):
        raise ValueError("Valeur invalide.")
    return value


def clear_screen() -> None:
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def exec_shell(shell: str, start_dir: str) -> None:
    shell = shell or "/bin/bash"
    start_dir = start_dir if os.path.isdir(start_dir or "") else "/"
    os.chdir(start_dir)
    shell_name = os.path.basename(shell)
    if shell_name in {"bash", "zsh", "sh", "dash"}:
        os.execvp(shell, [shell, "-l"])
    os.execvp(shell, [shell])


def run_and_keep_shell(command, shell: str, start_dir: str, title: str) -> None:
    clear_screen()
    print(f"=== {title} ===")
    print()
    sys.stdout.flush()
    code = subprocess.call(command)
    print()
    print("--- Fin de commande ---")
    print(f"Code retour: {code}")
    print("Le terminal reste ouvert pour depannage.")
    sys.stdout.flush()
    exec_shell(shell, start_dir)


def run_looping_shell(command: str, shell: str, start_dir: str, title: str) -> None:
    run_and_keep_shell(["/bin/sh", "-lc", command], shell, start_dir, title)


def system_conf_candidates():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    conf_dir = os.environ.get("NAS_CONF_DIR", "").strip()
    candidates = []
    if conf_dir:
        candidates.append(os.path.join(conf_dir, "system.conf"))
    candidates.extend([
        os.path.join(base_dir, "..", "conf", "system.conf"),
        os.path.join(base_dir, "conf", "system.conf"),
        "/yoleo/conf/system.conf",
        "/dockers/conf/system.conf",
    ])
    seen = set()
    for path in candidates:
        path = os.path.abspath(os.path.expanduser(os.path.expandvars(path)))
        if path in seen:
            continue
        seen.add(path)
        yield path


def read_simple_conf(path: str) -> dict:
    data = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or line.startswith(";") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                data[key.strip().upper()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return data


def load_system_conf_for_terminal():
    for path in system_conf_candidates():
        data = read_simple_conf(path)
        if data:
            return path, data
    return "", {}


def resolve_conf_path(value: str, conf_path: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    value = os.path.expanduser(os.path.expandvars(value))
    if os.path.isabs(value):
        return value
    base = os.path.dirname(conf_path) if conf_path else os.getcwd()
    return os.path.abspath(os.path.join(base, value))


def nvidia_smi_local(shell: str, start_dir: str) -> None:
    command = """
if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo 'nvidia-smi introuvable sur cet hote.'
  exit 127
fi
while true; do
  clear
  date '+%Y-%m-%d %H:%M:%S'
  echo
  nvidia-smi
  sleep 2
done
"""
    run_looping_shell(command, shell, start_dir, "NVIDIA locale - nvidia-smi")


def nvidia_smi_ssh(shell: str, start_dir: str) -> None:
    conf_path, conf = load_system_conf_for_terminal()
    host = conf.get("SSH_GPU_HOST", "")
    user = conf.get("SSH_GPU_USER", "")
    port = conf.get("SSH_GPU_PORT", "22") or "22"
    key_path = resolve_conf_path(conf.get("SSH_GPU_KEY_PATH", ""), conf_path)
    remote_smi = conf.get("REMOTE_NVIDIA_SMI", "/usr/bin/nvidia-smi") or "/usr/bin/nvidia-smi"

    if not host or not user:
        clear_screen()
        print("GPU SSH non configure dans system.conf.")
        print("Renseigne SSH_GPU_HOST et SSH_GPU_USER.")
        print()
        print("Ouverture du shell standard.")
        sys.stdout.flush()
        exec_shell(shell, start_dir)

    ssh_cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=accept-new",
        "-p", str(port),
    ]
    if key_path:
        ssh_cmd.extend(["-i", key_path])
    ssh_cmd.extend([f"{user}@{host}", remote_smi])

    try:
        while True:
            clear_screen()
            print("=== NVIDIA SSH - nvidia-smi ===")
            print(f"Cible : {user}@{host}:{port}")
            print()
            sys.stdout.flush()
            subprocess.call(ssh_cmd)
            print()
            print("--- rafraichissement dans 2s, Ctrl+C pour le shell ---")
            sys.stdout.flush()
            time.sleep(2)
    except KeyboardInterrupt:
        print()
        print("Le terminal reste ouvert pour depannage.")
        sys.stdout.flush()
        exec_shell(shell, start_dir)


def intel_gpu_top_terminal(shell: str, start_dir: str) -> None:
    command = """
if command -v intel_gpu_top >/dev/null 2>&1; then
  intel_gpu_top
  exit $?
fi
while true; do
  clear
  date '+%Y-%m-%d %H:%M:%S'
  echo
  echo 'intel_gpu_top introuvable, affichage simple /sys.'
  if [ -r /sys/class/drm/card0/device/gpu_busy_percent ]; then
    printf 'GPU busy : '; cat /sys/class/drm/card0/device/gpu_busy_percent; echo ' %'
  fi
  for f in /sys/class/drm/card*/device/hwmon/hwmon*/power1_average; do
    [ -r "$f" ] || continue
    awk '{printf "Power    : %.2f W\\n", $1/1000000}' "$f"
  done
  sleep 2
done
"""
    run_looping_shell(command, shell, start_dir, "Intel GPU - details")


def docker_logs(container: str, shell: str, start_dir: str) -> None:
    container = safe_value(container)
    run_and_keep_shell(["docker", "logs", "--tail=300", "-f", container], shell, start_dir, f"docker logs --tail=300 -f {container}")


def docker_exec(container: str, shell: str, start_dir: str) -> None:
    container = safe_value(container)
    clear_screen()
    print(f"=== docker exec -it {container} ===")
    print()
    sys.stdout.flush()
    code = subprocess.call(["docker", "exec", "-it", container, "/bin/bash"])
    if code != 0:
        code = subprocess.call(["docker", "exec", "-it", container, "/bin/sh"])
    print()
    print("--- Session docker exec terminee ---")
    print(f"Code retour: {code}")
    print("Le terminal reste ouvert pour depannage.")
    sys.stdout.flush()
    exec_shell(shell, start_dir)


def compose_logs(log_path: str, shell: str, start_dir: str) -> None:
    log_path = log_path or "/var/log/dockers.log"
    subprocess.call(["touch", log_path])
    run_and_keep_shell(["tail", "-n", "300", "-f", log_path], shell, start_dir, "Logs Docker Compose")


def safe_log_path(value: str) -> str:
    path = os.path.abspath(os.path.expanduser(str(value or "").strip()))
    allowed_roots = [
        os.path.abspath(root)
        for root in os.environ.get("YOLEO_TTYD_ALLOWED_LOG_ROOTS", "/var/log/builds:/var/log/registry:/tmp").split(":")
        if root.strip()
    ]
    for root in allowed_roots:
        try:
            common = os.path.commonpath([root, path])
        except ValueError:
            continue
        if common == root:
            return path
    raise ValueError("Chemin de log non autorise.")


def tail_log_file(log_path: str, shell: str, start_dir: str, title: str = "Log") -> None:
    path = safe_log_path(log_path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    subprocess.call(["touch", path])
    run_and_keep_shell(["tail", "-n", "300", "-f", path], shell, start_dir, title)


def menu_demo(shell: str, start_dir: str) -> None:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    script = os.path.abspath(os.path.join(base_dir, "..", "scripts", "menu.sh"))
    if not os.path.isfile(script):
        raise ValueError(f"Script menu introuvable : {script}")
    clear_screen()
    print("=== Demo Menu CLI : ../scripts/menu.sh ===")
    print()
    sys.stdout.flush()
    code = subprocess.call(["bash", script], cwd=base_dir)
    print()
    print("--- Fin de la demo Menu CLI ---")
    print(f"Code retour: {code}")
    print("Le terminal reste ouvert pour depannage.")
    sys.stdout.flush()
    exec_shell(shell, start_dir)


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--start-dir", default="/")
    parser.add_argument("--shell", default="/bin/bash")
    parser.add_argument("--compose-log", default="/var/log/dockers.log")
    known, args = parser.parse_known_args()

    if not args:
        exec_shell(known.shell, known.start_dir)

    action = args[0]
    try:
        if action == "docker-logs" and len(args) >= 2:
            docker_logs(args[1], known.shell, known.start_dir)
        elif action == "docker-exec" and len(args) >= 2:
            docker_exec(args[1], known.shell, known.start_dir)
        elif action == "compose-logs":
            compose_logs(known.compose_log, known.shell, known.start_dir)
        elif action == "tail-log" and len(args) >= 2:
            tail_log_file(args[1], known.shell, known.start_dir, "Log Yoleo")
        elif action == "menu-demo":
            menu_demo(known.shell, known.start_dir)
        elif action == "nvidia-smi-local":
            nvidia_smi_local(known.shell, known.start_dir)
        elif action == "nvidia-smi-ssh":
            nvidia_smi_ssh(known.shell, known.start_dir)
        elif action == "intel-gpu-top":
            intel_gpu_top_terminal(known.shell, known.start_dir)
        else:
            raise ValueError(f"Action terminal inconnue : {action}")
    except Exception as exc:
        clear_screen()
        print(f"Action terminal refusee : {exc}")
        print()
        print("Ouverture du shell standard.")
        sys.stdout.flush()
        exec_shell(known.shell, known.start_dir)


if __name__ == "__main__":
    main()
