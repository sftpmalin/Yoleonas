#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nettoyage CLI du registre Docker Yoleo.

Même logique que le bouton Flask « Nettoyer le registre » :
  1) lit builds.conf + registry.conf,
  2) récupère DATA_DIR dans registry.conf,
  3) arrête le registre host/service,
  4) vide uniquement le contenu du DATA_DIR,
  5) invalide l'état local TAR -> Registre,
  6) redémarre le registre.

Par défaut ce script ne recharge pas les TAR, comme l'UI Flask.
Ajoute --load si tu veux retrouver l'ancien comportement après nettoyage.
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


DEFAULT_SERVICE_NAME = "registry-labo-host.service"
DEFAULT_RUNTIME_BIN = "/tmp/registry-host-labo"
DEFAULT_PID_FILE = "/run/registry_labo_host.pid"
DEFAULT_LOG_DIR = "/var/log/registry"
DEFAULT_LOG_FILE = "/var/log/registry/registry.log"
DEFAULT_REGISTRY_CONF_NAME = "registry.conf"
DEFAULT_BUILDS_CONF_NAME = "builds.conf"
DEFAULT_REGISTRY_YML_NAME = "registry.yml"


# ---------------------------------------------------------------------------
# Petits outils génériques
# ---------------------------------------------------------------------------

def die(message: str, code: int = 1) -> None:
    print(f"❌ {message}", flush=True)
    raise SystemExit(code)


def info(message: str = "") -> None:
    print(message, flush=True)


def run_cmd(cmd: List[str], timeout: int = 60, check: bool = False) -> Tuple[int, str]:
    info("$ " + " ".join(shlex.quote(str(x)) for x in cmd))
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except Exception as exc:
        if check:
            raise RuntimeError(str(exc)) from exc
        return 127, str(exc)

    out = proc.stdout or ""
    if out:
        print(out, end="" if out.endswith("\n") else "\n", flush=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"Commande échouée ({proc.returncode}) : {' '.join(cmd)}")
    return proc.returncode, out


def require_root() -> None:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        die("Ce nettoyage doit être lancé en root, comme la fonction Flask du registre.")


def strip_quotes(value: str) -> str:
    value = str(value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def read_kv_file(path: Path) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not path.is_file():
        return data
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        data[key] = strip_quotes(value.strip())
    return data


def write_text_atomic(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        shutil.move(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def human_size(size: int) -> str:
    units = ["o", "Ko", "Mo", "Go", "To", "Po"]
    value = float(max(0, int(size)))
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "o":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} Po"


def path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file() and not path.is_symlink():
        try:
            return path.stat().st_size
        except OSError:
            return 0

    total = 0
    for root, dirs, files in os.walk(path):
        # Ne jamais suivre les liens symboliques.
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(root, name))]
        for name in files:
            item = os.path.join(root, name)
            try:
                if not os.path.islink(item):
                    total += os.path.getsize(item)
            except OSError:
                pass
    return total


# ---------------------------------------------------------------------------
# Résolution de chemins compatible avec le module Flask builds.py
# ---------------------------------------------------------------------------

def existing_or_first(candidates: Iterable[Path]) -> Path:
    items = [candidate.expanduser() for candidate in candidates if str(candidate).strip()]
    for item in items:
        if item.exists():
            return item.resolve()
    return items[0].resolve() if items else Path.cwd().resolve()


def guess_conf_dir(script_path: Path, cli_conf_dir: str = "") -> Path:
    if cli_conf_dir:
        return Path(cli_conf_dir).expanduser().resolve()

    env_conf = os.environ.get("NAS_CONF_DIR", "").strip()
    env_root = os.environ.get("NAS_ROOT_DIR", "").strip() or os.environ.get("YOLEO_ROOT", "").strip()

    candidates: List[Path] = []
    if env_conf:
        candidates.append(Path(env_conf))
    if env_root:
        candidates.append(Path(env_root) / "conf")

    script_dir = script_path.resolve().parent
    # Cas classiques : /yoleo/system/registry.py, /yoleo/scripts/registry.py,
    # /mnt/user/dockers/scripts/registry.py, ou lancement depuis le dossier system.
    candidates.extend([
        script_dir.parent / "conf",
        script_dir / "conf",
        Path.cwd() / "conf",
        Path.cwd().parent / "conf",
        Path("/yoleo/conf"),
        Path("/mnt/user/dockers/conf"),
    ])
    return existing_or_first(candidates)


def resolve_path(value: str, base: Optional[Path] = None) -> Path:
    raw = strip_quotes(value).strip()
    if not raw:
        return Path("")
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p.resolve()
    candidates: List[Path] = []
    if base:
        candidates.append((base / p).resolve())
    candidates.append((Path.cwd() / p).resolve())
    for candidate in candidates:
        if candidate.exists() or candidate.parent.exists():
            return candidate
    return candidates[0]


def load_conf(conf_dir: Path, builds_conf_arg: str = "") -> Tuple[Dict[str, str], Path]:
    builds_conf = Path(builds_conf_arg).expanduser().resolve() if builds_conf_arg else Path(
        os.environ.get("BUILDS_CONFIG_PATH", "") or conf_dir / DEFAULT_BUILDS_CONF_NAME
    ).expanduser().resolve()

    conf = read_kv_file(builds_conf)

    # Valeurs par défaut identiques à l'esprit de builds.py.
    conf.setdefault("HOST_CONF_DIR", str(conf_dir))
    conf.setdefault("REGCTL", str(conf_dir.parent / "bin" / "regctl"))
    conf.setdefault("STATE_DIR", str(conf_dir / ".save_state"))
    conf.setdefault("REGISTRY_HOST_CONF_FILE", str(conf_dir / DEFAULT_REGISTRY_CONF_NAME))
    conf.setdefault("REGISTRY_HOST_YAML_FILE", str(conf_dir / DEFAULT_REGISTRY_YML_NAME))
    conf.setdefault("REGISTRY_HOST_LOG_DIR", DEFAULT_LOG_DIR)
    conf.setdefault("REGISTRY_HOST_LOG_FILE", DEFAULT_LOG_FILE)
    conf.setdefault("REGISTRY_HOST_RUNTIME_BIN", DEFAULT_RUNTIME_BIN)
    conf.setdefault("REGISTRY_HOST_PID_FILE", DEFAULT_PID_FILE)
    conf.setdefault("REGISTRY_HOST_SERVICE_NAME", DEFAULT_SERVICE_NAME)
    conf.setdefault("REGISTRY_HOST_SERVICE_FILE", f"/etc/systemd/system/{conf['REGISTRY_HOST_SERVICE_NAME']}")
    conf.setdefault("REGISTRY_HOST_MNT_READY_DIR", "")
    conf.setdefault("REGISTRY_HOST_MNT_ROOT", "")
    return conf, builds_conf


class RegistrySettings:
    def __init__(self, conf: Dict[str, str], conf_dir: Path) -> None:
        self.conf = conf
        self.conf_dir = conf_dir

        host_conf_raw = conf.get("REGISTRY_HOST_CONF_FILE", "").strip()
        if not host_conf_raw:
            host_conf_raw = str(conf_dir / DEFAULT_REGISTRY_CONF_NAME)
        self.human_conf = resolve_path(host_conf_raw, conf_dir)
        self.registry_conf_dir = self.human_conf.parent

        base_dir_raw = conf.get("REGISTRY_HOST_BASE_DIR", "").strip()
        if base_dir_raw:
            self.base_dir = resolve_path(base_dir_raw, self.registry_conf_dir)
        else:
            self.base_dir = self.registry_conf_dir.parent.resolve()

        regctl_raw = conf.get("REGCTL", "").strip()
        regctl_path = resolve_path(regctl_raw, self.registry_conf_dir) if regctl_raw else Path("")
        regctl_bin_dir = regctl_path.parent if str(regctl_path) else Path("")

        explicit_bin_dir = conf.get("REGISTRY_HOST_BIN_DIR", "").strip()
        if explicit_bin_dir:
            self.bin_dir = resolve_path(explicit_bin_dir, self.registry_conf_dir)
        elif str(regctl_bin_dir):
            self.bin_dir = regctl_bin_dir
        else:
            self.bin_dir = self.base_dir / "bin"

        self.yaml_conf = resolve_path(conf.get("REGISTRY_HOST_YAML_FILE", str(conf_dir / DEFAULT_REGISTRY_YML_NAME)), self.registry_conf_dir)
        self.log_dir = resolve_path(conf.get("REGISTRY_HOST_LOG_DIR", DEFAULT_LOG_DIR), self.registry_conf_dir)
        self.log_file = resolve_path(conf.get("REGISTRY_HOST_LOG_FILE", DEFAULT_LOG_FILE), self.registry_conf_dir)
        self.pid_file = Path(strip_quotes(conf.get("REGISTRY_HOST_PID_FILE", DEFAULT_PID_FILE)) or DEFAULT_PID_FILE)
        self.runtime_bin = Path(strip_quotes(conf.get("REGISTRY_HOST_RUNTIME_BIN", DEFAULT_RUNTIME_BIN)) or DEFAULT_RUNTIME_BIN)
        self.service_name = strip_quotes(conf.get("REGISTRY_HOST_SERVICE_NAME", DEFAULT_SERVICE_NAME)) or DEFAULT_SERVICE_NAME
        self.service_file = Path(strip_quotes(conf.get("REGISTRY_HOST_SERVICE_FILE", f"/etc/systemd/system/{self.service_name}")) or f"/etc/systemd/system/{self.service_name}")

        mnt_root_raw = strip_quotes(conf.get("REGISTRY_HOST_MNT_ROOT", "")).strip()
        self.mnt_root = Path(mnt_root_raw) if mnt_root_raw else df_mount(self.base_dir)
        mnt_ready_raw = strip_quotes(conf.get("REGISTRY_HOST_MNT_READY_DIR", "")).strip()
        self.mnt_ready_dir = resolve_path(mnt_ready_raw, self.registry_conf_dir) if mnt_ready_raw else self.base_dir

        self.bin_amd64 = self.bin_dir / "registry_amd64"
        self.bin_arm64 = self.bin_dir / "registry_arm64"


def load_registry_values(settings: RegistrySettings) -> Dict[str, str]:
    if not settings.human_conf.is_file():
        raise RuntimeError(f"registry.conf introuvable : {settings.human_conf}")

    raw = read_kv_file(settings.human_conf)
    required = ("PORT", "BIND_ADDR", "DATA_DIR", "LOG_LEVEL", "DELETE_ENABLED", "HTTP_SECRET")
    values: Dict[str, str] = {}
    for key in required:
        value = strip_quotes(raw.get(key, "")).strip()
        if not value:
            raise RuntimeError(f"{key} manquant ou vide dans {settings.human_conf}")
        values[key] = value

    if not values["PORT"].isdigit():
        raise RuntimeError(f"PORT invalide dans {settings.human_conf} : {values['PORT']}")
    if values["DELETE_ENABLED"] not in {"true", "false"}:
        raise RuntimeError(f"DELETE_ENABLED doit être true ou false dans {settings.human_conf}")
    if values["LOG_LEVEL"] not in {"debug", "info", "warn", "warning", "error", "fatal", "panic"}:
        raise RuntimeError(f"LOG_LEVEL invalide dans {settings.human_conf} : {values['LOG_LEVEL']}")

    data_dir = Path(values["DATA_DIR"]).expanduser()
    if data_dir.is_absolute():
        values["DATA_DIR"] = str(data_dir.resolve())
    else:
        values["DATA_DIR"] = str((settings.registry_conf_dir / data_dir).resolve())
    return values


def render_registry_yaml(values: Dict[str, str]) -> str:
    return "\n".join([
        "version: 0.1",
        "",
        "log:",
        f"  level: {values['LOG_LEVEL']}",
        "",
        "storage:",
        "  filesystem:",
        f"    rootdirectory: {values['DATA_DIR']}",
        "  delete:",
        f"    enabled: {values['DELETE_ENABLED']}",
        "",
        "http:",
        f"  addr: {values['BIND_ADDR']}:{values['PORT']}",
        f"  secret: {values['HTTP_SECRET']}",
        "  headers:",
        "    X-Content-Type-Options:",
        "      - nosniff",
        "",
    ])


def ensure_registry_yaml(settings: RegistrySettings, values: Dict[str, str]) -> None:
    data_dir = Path(values["DATA_DIR"])
    settings.registry_conf_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    content = render_registry_yaml(values)
    old = settings.yaml_conf.read_text(encoding="utf-8", errors="replace") if settings.yaml_conf.exists() else ""
    if old == content:
        info(f"OK: registry.yml déjà correct : {settings.yaml_conf}")
        return
    if settings.yaml_conf.exists():
        backup = settings.yaml_conf.with_name(settings.yaml_conf.name + ".backup_" + time.strftime("%Y%m%d_%H%M%S"))
        shutil.copy2(settings.yaml_conf, backup)
        info(f"registry.yml différent, sauvegarde : {backup}")
    else:
        info(f"registry.yml absent, création : {settings.yaml_conf}")
    write_text_atomic(settings.yaml_conf, content)
    info(f"OK: registry.yml généré : {settings.yaml_conf}")


# ---------------------------------------------------------------------------
# Gestion service/process : même logique que le Flask
# ---------------------------------------------------------------------------

def df_mount(path: Path) -> Path:
    try:
        proc = subprocess.run(["df", "-P", str(path)], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 6 and parts[5]:
                return Path(parts[5])
    except Exception:
        pass
    return path.parent if path.name else Path("/")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def pids_by_cmd(settings: RegistrySettings) -> List[int]:
    target = [str(settings.runtime_bin), "serve", str(settings.yaml_conf)]
    proc_dir = Path("/proc")
    pids: List[int] = []
    if not proc_dir.is_dir():
        return pids
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
            parts = [part.decode("utf-8", "ignore") for part in raw.split(b"\0") if part]
        except Exception:
            continue
        if len(parts) >= 3 and parts[:3] == target:
            pids.append(int(entry.name))
    return sorted(set(pids))


def read_pid_file(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8", errors="replace").strip().split()[0])
    except Exception:
        return 0


def adopt_pid(settings: RegistrySettings) -> Optional[int]:
    old = read_pid_file(settings.pid_file)
    if old and pid_alive(old):
        return old
    pids = pids_by_cmd(settings)
    if pids:
        settings.pid_file.parent.mkdir(parents=True, exist_ok=True)
        settings.pid_file.write_text(str(pids[0]) + "\n", encoding="utf-8")
        info(f"PID adopté depuis process existant : {pids[0]}")
        return pids[0]
    return None


def systemctl(settings: RegistrySettings, args: List[str], timeout: int = 60) -> Tuple[int, str]:
    return run_cmd(["systemctl", *args, settings.service_name], timeout=timeout, check=False)


def service_installed(settings: RegistrySettings) -> bool:
    if settings.service_file.exists():
        return True
    rc, _ = subprocess.getstatusoutput(f"systemctl status --no-pager {shlex.quote(settings.service_name)} >/dev/null 2>&1")
    return rc == 0


def service_active(settings: RegistrySettings) -> bool:
    rc, _ = subprocess.getstatusoutput(f"systemctl is-active --quiet {shlex.quote(settings.service_name)} >/dev/null 2>&1")
    return rc == 0


def detect_registry_bin(settings: RegistrySettings) -> Path:
    arch = platform.machine().strip().lower()
    if arch in {"x86_64", "amd64"}:
        return settings.bin_amd64
    if arch in {"aarch64", "arm64"}:
        return settings.bin_arm64
    raise RuntimeError(f"architecture inconnue : {arch or '?'}")


def write_service_file(settings: RegistrySettings, values: Dict[str, str]) -> None:
    source_bin = detect_registry_bin(settings)
    if not source_bin.is_file():
        raise RuntimeError(f"binaire registry introuvable : {source_bin}")

    qbin = shlex.quote(str(source_bin))
    qrun = shlex.quote(str(settings.runtime_bin))
    qyaml = shlex.quote(str(settings.yaml_conf))
    qlog = shlex.quote(str(settings.log_file))
    qpid = shlex.quote(str(settings.pid_file))
    qlogdir = shlex.quote(str(settings.log_dir))
    qdatadir = shlex.quote(values["DATA_DIR"])

    service_text = f"""[Unit]
Description=Registry Docker LABO host
After=local-fs.target network-online.target
Wants=network-online.target
RequiresMountsFor={settings.mnt_root}

[Service]
Type=simple
PIDFile={settings.pid_file}
Environment=OTEL_TRACES_EXPORTER=none
ExecStartPre=/bin/sh -lc 'mkdir -p {qlogdir} {qdatadir}'
ExecStartPre=/bin/sh -lc 'cp {qbin} {qrun} && chmod 755 {qrun}'
ExecStart=/bin/sh -lc 'echo $$ > {qpid}; exec {qrun} serve {qyaml} >> {qlog} 2>&1'
ExecStopPost=/bin/rm -f {settings.pid_file}
Restart=no
TimeoutStartSec=180
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
"""
    write_text_atomic(settings.service_file, service_text, mode=0o644)
    info(f"OK: service écrit : {settings.service_file}")


def storage_ready(settings: RegistrySettings) -> bool:
    return settings.mnt_root.is_mount() and settings.mnt_ready_dir.is_dir()


def stop_registry(settings: RegistrySettings) -> None:
    info(">>> Arrêt du registre")
    if service_installed(settings):
        info(f"Arrêt via systemd : {settings.service_name}")
        rc, out = systemctl(settings, ["stop"], timeout=80)
        if rc != 0:
            info(f"Info: systemctl stop a retourné {rc}, nettoyage manuel quand même.")

    pids = []
    old = read_pid_file(settings.pid_file)
    if old:
        pids.append(old)
    pids.extend(pids_by_cmd(settings))
    pids = sorted(set(pid for pid in pids if pid > 0))

    killed = False
    for pid in pids:
        if not pid_alive(pid):
            continue
        info(f"Arrêt du registry labo PID {pid}...")
        try:
            os.kill(pid, signal.SIGTERM)
            killed = True
        except OSError:
            pass

    for _ in range(5):
        if not any(pid_alive(pid) for pid in pids):
            break
        time.sleep(1)

    for pid in pids:
        if pid_alive(pid):
            info(f"Forçage arrêt PID {pid}...")
            try:
                os.kill(pid, signal.SIGKILL)
                killed = True
            except OSError:
                pass

    try:
        settings.pid_file.unlink()
    except OSError:
        pass

    info("OK: Registry labo arrêté" if killed else "Registry labo déjà arrêté")


def start_registry(settings: RegistrySettings, values: Dict[str, str]) -> None:
    info(">>> Redémarrage du registre")
    ensure_registry_yaml(settings, values)

    pid = adopt_pid(settings)
    if pid:
        info(f"Registry labo déjà démarré PID {pid}")
        return

    settings.log_dir.mkdir(parents=True, exist_ok=True)
    settings.bin_dir.mkdir(parents=True, exist_ok=True)
    Path(values["DATA_DIR"]).mkdir(parents=True, exist_ok=True)

    if not storage_ready(settings):
        raise RuntimeError(f"stockage pas prêt : {settings.mnt_root} + {settings.mnt_ready_dir}")

    if service_installed(settings):
        info(f"Démarrage via systemd : {settings.service_name}")
        write_service_file(settings, values)
        rc_reload, _ = run_cmd(["systemctl", "daemon-reload"], timeout=60, check=False)
        if rc_reload != 0:
            raise RuntimeError(f"systemctl daemon-reload a échoué (code {rc_reload})")
        rc, _ = systemctl(settings, ["start"], timeout=190)
        time.sleep(1)
        pid = adopt_pid(settings)
        if rc == 0 and (pid or service_active(settings)):
            info(f"OK: service démarré : {settings.service_name}")
            return
        raise RuntimeError(f"systemctl start a échoué (code {rc})")

    source_bin = detect_registry_bin(settings)
    if not source_bin.is_file():
        raise RuntimeError(f"binaire introuvable : {source_bin}")

    # Vérification simple du port, comme dans l'UI, pour éviter un faux démarrage.
    rc_ss, out_ss = run_cmd(["ss", "-lntp"], timeout=8, check=False)
    if rc_ss == 0:
        port = values["PORT"]
        busy = [line for line in out_ss.splitlines() if f":{port} " in line or line.rstrip().endswith(f":{port}")]
        if busy:
            raise RuntimeError("le port est déjà utilisé :\n" + "\n".join(busy))

    shutil.copy2(source_bin, settings.runtime_bin)
    settings.runtime_bin.chmod(0o755)
    if not os.access(settings.runtime_bin, os.X_OK):
        raise RuntimeError(f"binaire runtime non exécutable : {settings.runtime_bin}")

    info("Démarrage du registry labo host...")
    info(f"Binaire source  : {source_bin}")
    info(f"Binaire runtime : {settings.runtime_bin}")
    info(f"Conf humain     : {settings.human_conf}")
    info(f"Config YAML     : {settings.yaml_conf}")
    info(f"Data            : {values['DATA_DIR']}")
    info(f"Log             : {settings.log_file}")
    info(f"PID             : {settings.pid_file}")
    info(f"Port            : {values['PORT']}")

    env = os.environ.copy()
    env["OTEL_TRACES_EXPORTER"] = "none"
    with open(settings.log_file, "ab", buffering=0) as log_handle:
        proc = subprocess.Popen([str(settings.runtime_bin), "serve", str(settings.yaml_conf)], stdout=log_handle, stderr=subprocess.STDOUT, env=env)
    settings.pid_file.parent.mkdir(parents=True, exist_ok=True)
    settings.pid_file.write_text(str(proc.pid) + "\n", encoding="utf-8")
    time.sleep(1)
    pid = adopt_pid(settings)
    if pid:
        info(f"OK: Registry labo démarré PID {pid}")
        info(f"URL: http://{values['BIND_ADDR']}:{values['PORT']}/v2/")
        return

    try:
        settings.pid_file.unlink()
    except OSError:
        pass
    tail = ""
    try:
        if settings.log_file.exists():
            tail = "".join(settings.log_file.read_text(encoding="utf-8", errors="replace").splitlines(True)[-40:]).strip()
    except Exception:
        pass
    raise RuntimeError("Registry labo n'a pas démarré" + (f"\n{tail}" if tail else ""))


# ---------------------------------------------------------------------------
# Nettoyage DATA_DIR + invalidation cache TAR -> Registre
# ---------------------------------------------------------------------------

def registry_is_safe_data_dir(path: Path) -> bool:
    if not str(path):
        return False
    real = path.resolve(strict=False)
    forbidden = {
        Path("/"), Path("/bin"), Path("/boot"), Path("/data"), Path("/dev"),
        Path("/dockers"), Path("/etc"), Path("/home"), Path("/lib"), Path("/lib64"),
        Path("/mnt"), Path("/opt"), Path("/proc"), Path("/root"), Path("/run"),
        Path("/sbin"), Path("/srv"), Path("/sys"), Path("/tmp"), Path("/usr"), Path("/var"),
        Path("/yoleo"), Path("/mnt/user"), Path("/mnt/user/dockers"),
    }
    parts_count = len([part for part in real.parts if part and part != os.sep])
    return real not in forbidden and parts_count >= 2


def empty_directory_without_removing_it(path: Path, dry_run: bool = False) -> int:
    real = path.resolve(strict=False)
    if not registry_is_safe_data_dir(real):
        raise RuntimeError(f"DATA_DIR refusé par sécurité : {real}")
    if real.is_symlink():
        raise RuntimeError(f"DATA_DIR refusé car c'est un lien symbolique : {real}")
    if real.exists() and not real.is_dir():
        raise RuntimeError(f"DATA_DIR n'est pas un dossier : {real}")

    if not real.exists():
        info(f"Création du dossier : {real}")
        if not dry_run:
            real.mkdir(parents=True, exist_ok=True)
        return 0

    removed = 0
    for entry in list(real.iterdir()):
        info(f"DELETE: {entry}")
        removed += 1
        if dry_run:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()

    if not dry_run:
        real.mkdir(parents=True, exist_ok=True)
    return removed


def clear_registry_import_state(conf: Dict[str, str], conf_dir: Path, names: Optional[Iterable[str]] = None, dry_run: bool = False) -> int:
    state_raw = conf.get("STATE_DIR", "").strip() or str(conf_dir / ".save_state")
    state_dir = resolve_path(state_raw, conf_dir)
    if not state_dir.is_dir():
        return 0

    paths: List[Path] = []
    if names is None:
        paths.extend(state_dir.glob("*.registry.tar.sha256"))
        paths.extend(state_dir.glob("*.registry.target"))
    else:
        for name in names:
            safe = str(name).strip()
            if safe:
                paths.append(state_dir / f"{safe}.registry.tar.sha256")
                paths.append(state_dir / f"{safe}.registry.target")

    removed = 0
    for path in sorted(set(paths)):
        if not path.exists() or path.is_dir():
            continue
        info(f"INVALIDATE: {path}")
        removed += 1
        if not dry_run:
            try:
                path.unlink()
            except OSError as exc:
                info(f"ERREUR: impossible de supprimer {path}: {exc}")
    return removed


def find_docker_py(script_path: Path, cli_value: str = "") -> Path:
    if cli_value:
        return Path(cli_value).expanduser().resolve()
    script_dir = script_path.resolve().parent
    candidates = [
        script_dir / "docker.py",
        script_dir.parent / "scripts" / "docker.py",
        Path("/mnt/user/dockers/scripts/docker.py"),
        Path("/yoleo/scripts/docker.py"),
    ]
    return existing_or_first(candidates)


def clean_registry(conf: Dict[str, str], conf_dir: Path, settings: RegistrySettings, values: Dict[str, str], dry_run: bool = False) -> None:
    data_dir = Path(values["DATA_DIR"])
    if not registry_is_safe_data_dir(data_dir):
        raise RuntimeError(f"DATA_DIR refusé par sécurité : {data_dir}")

    size_before = path_size(data_dir)

    info("=" * 76)
    info("NETTOYAGE REGISTRE - LOGIQUE FLASK")
    info("=" * 76)
    info(f"Conf builds : {conf_dir / DEFAULT_BUILDS_CONF_NAME}")
    info(f"Conf registre : {settings.human_conf}")
    info(f"Service       : {settings.service_name}")
    info(f"DATA_DIR      : {data_dir}")
    info(f"Taille avant  : {human_size(size_before)}")
    info("")

    if dry_run:
        info("MODE DRY-RUN : aucune suppression, aucun arrêt/démarrage réel.")
    else:
        stop_registry(settings)

    info("")
    info(">>> Vidage du stockage registre")
    removed = empty_directory_without_removing_it(data_dir, dry_run=dry_run)

    invalidated = clear_registry_import_state(conf, conf_dir, dry_run=dry_run)
    size_after = path_size(data_dir) if not dry_run else size_before

    info(f"Elements supprimés : {removed}")
    info(f"Taille après       : {human_size(size_after)}")
    info(f"Etat local TAR -> Registre invalidé : {invalidated} fichier(s) supprimé(s).")

    if not dry_run:
        start_registry(settings, values)

    info("")
    info("✅ Nettoyage du registre terminé.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Nettoie le registre Docker Yoleo avec la même logique que l'UI Flask.")
    parser.add_argument("--conf-dir", default="", help="Dossier conf Yoleo. Défaut: auto, NAS_CONF_DIR, /yoleo/conf, /mnt/user/dockers/conf.")
    parser.add_argument("--builds-conf", default="", help="Chemin builds.conf. Défaut: <conf-dir>/builds.conf ou BUILDS_CONFIG_PATH.")
    parser.add_argument("--dry-run", action="store_true", help="Affiche ce qui serait fait sans arrêter ni supprimer.")
    parser.add_argument("--load", action="store_true", help="Après nettoyage, recharge les TAR via docker.py --load. Non utilisé par défaut, comme l'UI.")
    parser.add_argument("--docker-py", default="", help="Chemin docker.py pour --load.")
    args = parser.parse_args()

    script_path = Path(__file__).resolve()
    conf_dir = guess_conf_dir(script_path, args.conf_dir)
    conf, builds_conf = load_conf(conf_dir, args.builds_conf)
    # HOST_CONF_DIR peut venir de builds.conf ; on le respecte pour les chemins relatifs.
    real_conf_dir = resolve_path(conf.get("HOST_CONF_DIR", str(conf_dir)), conf_dir) if conf.get("HOST_CONF_DIR") else conf_dir
    if real_conf_dir != conf_dir:
        conf_dir = real_conf_dir
        conf, builds_conf = load_conf(conf_dir, args.builds_conf or str(builds_conf))

    require_root()
    settings = RegistrySettings(conf, conf_dir)
    values = load_registry_values(settings)

    clean_registry(conf, conf_dir, settings, values, dry_run=args.dry_run)

    if args.load and not args.dry_run:
        docker_py = find_docker_py(script_path, args.docker_py)
        if not docker_py.is_file():
            raise RuntimeError(f"docker.py introuvable pour --load : {docker_py}")
        info("")
        info(">>> Réimport des TAR demandé (--load)")
        run_cmd([sys.executable, str(docker_py), "--load"], timeout=0 if False else 24 * 3600, check=True)
        info("✅ TAR réimportés.")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrompu.", flush=True)
        raise SystemExit(130)
    except Exception as exc:
        die(str(exc), 1)
