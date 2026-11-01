"""Per-function PCI reservations prepared for the next boot, never applied live."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from flask import jsonify, request


ADDRESS_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[01][0-9a-f]\.[0-7]$")
ID_RE = re.compile(r"^[0-9a-f]{4}$")
ID_FIELDS = ("vendor", "device", "subsystem_vendor", "subsystem_device")
CONFIG_HEADER = "# Exclusions PCI Yoleo : adresse vendor device subsystem_vendor subsystem_device\n# Appliquees au prochain demarrage uniquement.\n"


def parse_config(text: str) -> dict[str, dict[str, str]]:
    entries = {}
    for number, line in enumerate(text.splitlines(), 1):
        values = line.split("#", 1)[0].split()
        if not values:
            continue
        if len(values) != 5 or not ADDRESS_RE.fullmatch(values[0]) or not all(ID_RE.fullmatch(v) for v in values[1:]):
            raise ValueError(f"Configuration PCI invalide à la ligne {number}.")
        if values[0] in entries:
            raise ValueError(f"Adresse PCI en double à la ligne {number}.")
        entries[values[0]] = dict(zip(ID_FIELDS, values[1:]))
    return entries


def format_config(entries: dict) -> str:
    return CONFIG_HEADER + "".join(
        " ".join([address, *(entries[address][key] for key in ID_FIELDS)]) + "\n"
        for address in sorted(entries)
    )


def config_digest(entries: dict) -> str:
    return hashlib.sha256(format_config(entries).encode()).hexdigest()


def atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class PciReservations:
    def __init__(self, root: Path | str = "/", conf_dir: Path | None = None):
        self.root = Path(root)
        default_conf = Path(__file__).resolve().parent.parent / "conf"
        self.conf_dir = Path(conf_dir or os.environ.get("NAS_CONF_DIR", default_conf)).resolve()
        self.config = self.conf_dir / "pci_exclusions.conf"
        self.runtime = self.path("/run/yoleo-pci")
        self.state_file = self.runtime / "build-state.json"
        self.sysfs = self.path("/sys/bus/pci/devices")

    def path(self, absolute: str) -> Path:
        return self.root / absolute.lstrip("/")

    @staticmethod
    def read_text(path: Path) -> str:
        try:
            return path.read_text().strip()
        except FileNotFoundError:
            return ""

    def entries(self, path: Path | None = None) -> dict:
        return parse_config(self.read_text(path or self.config))

    @contextmanager
    def lock(self, name: str = "settings.lock"):
        self.runtime.mkdir(parents=True, exist_ok=True)
        with (self.runtime / name).open("a") as handle:
            os.fchmod(handle.fileno(), 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def read_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except FileNotFoundError:
            return {}

    def write_state(self, state: dict) -> None:
        atomic_write(self.state_file, json.dumps(state, ensure_ascii=False) + "\n", 0o600)

    def device(self, address: str) -> dict:
        if not isinstance(address, str) or not ADDRESS_RE.fullmatch(address):
            raise ValueError("Adresse PCI invalide.")
        path = self.sysfs / address
        if not path.is_dir():
            raise ValueError("Cette carte PCI n'est plus présente.")
        ids = {key: self.read_text(path / key).removeprefix("0x").lower() for key in ID_FIELDS}
        if not all(ID_RE.fullmatch(value) for value in ids.values()):
            raise ValueError("Identification de la carte PCI indisponible.")
        driver = (path / "driver").resolve().name if (path / "driver").is_symlink() else ""
        group = (path / "iommu_group").resolve().name if (path / "iommu_group").is_symlink() else ""
        return {"address": address, **ids, "driver": driver, "iommu_group": group,
                "class": self.read_text(path / "class"), "present": True}

    @staticmethod
    def pci_ancestors(path: Path) -> set[str]:
        resolved = path.resolve()
        return {part for part in resolved.parts if ADDRESS_RE.fullmatch(part)}

    def protected_devices(self) -> dict[str, str]:
        protected = {}
        # The host cannot boot if its root controller is reserved for a VM.
        root_device = os.stat(self.root).st_dev
        block = self.path(f"/sys/dev/block/{os.major(root_device)}:{os.minor(root_device)}")
        if block.exists():
            for address in self.pci_ancestors(block):
                protected[address] = "Contrôleur du disque système : nécessaire au démarrage du NAS."
        # Follow the default-route bridge to its physical management interface.
        routes = self.read_text(self.path("/proc/net/route")).splitlines()
        interfaces = [line.split()[0] for line in routes[1:] if len(line.split()) > 1 and line.split()[1] == "00000000"]
        visited = set()
        while interfaces:
            name = interfaces.pop()
            if name in visited:
                continue
            visited.add(name)
            interface = self.path("/sys/class/net") / name
            for address in self.pci_ancestors(interface / "device"):
                protected[address] = "Carte réseau utilisée pour accéder au NAS."
            for directory, pattern in [(interface / "brif", "*"), (interface, "lower_*")]:
                if directory.is_dir():
                    interfaces.extend(p.name.removeprefix("lower_") for p in directory.glob(pattern))
        return protected

    def devices(self) -> dict:
        protected = self.protected_devices()
        result = {}
        for path in sorted(self.sysfs.glob("*")):
            if not ADDRESS_RE.fullmatch(path.name):
                continue
            try:
                item = self.device(path.name)
            except ValueError:
                continue
            reason = protected.get(path.name, "")
            if item["class"].lower().startswith("0x06"):
                reason = "Pont ou composant du jeu de composants système."
            item["protected_reason"] = reason
            result[path.name] = item
        return result

    def boot_support_installed(self) -> bool:
        assets = Path(__file__).resolve().with_name("pci_boot")
        links = (("/etc/initramfs-tools/hooks/yoleo-pci", "initramfs-hook"),
                 ("/etc/initramfs-tools/scripts/init-top/udev", "init-top-udev"))
        return all(self.path(path).is_symlink() and self.path(path).resolve() == assets / source
                   and self.path(path).is_file() for path, source in links) and self.path("/usr/sbin/update-initramfs").is_file()

    def status(self) -> dict:
        selected = self.entries()
        applied = self.entries(self.runtime / "applied.conf")
        state = self.read_state()
        devices = self.devices()
        for address, signature in selected.items():
            if address not in devices:
                devices[address] = {"address": address, **signature, "present": False, "driver": "", "iommu_group": "", "protected_reason": ""}
        for address, item in devices.items():
            item["excluded"] = address in selected
            item["identity_matches"] = address not in selected or all(item.get(key) == selected[address][key] for key in ID_FIELDS)
        prepared = state.get("prepared_hash") == config_digest(selected)
        busy = state.get("phase") in {"queued", "running"}
        reboot = prepared and not busy and state.get("phase") != "error" and selected != applied
        message = ""
        if state.get("phase") == "error":
            message = "La préparation du démarrage a échoué : " + state.get("error", "erreur inconnue")
        elif busy:
            progress = f" ({state.get('done', 0)}/{state['total']})" if state.get("total") else ""
            message = "Choix enregistré. Préparation du prochain démarrage en cours" + progress + "…"
        elif reboot:
            message = "Configuration PCI enregistrée. Veuillez redémarrer le NAS pour appliquer."
        elif prepared:
            message = "Configuration PCI enregistrée." if not selected else "Les exclusions PCI sont appliquées."
        return {"ok": True, "supported": self.boot_support_installed(), "devices": devices,
                "selected_count": len(selected), "reboot_required": reboot,
                "revision": state.get("revision", 0),
                "phase": state.get("phase", "idle"), "message": message,
                "done": state.get("done", 0), "total": state.get("total", 0)}

    def launch_builder(self, unit: str) -> None:
        self.path("/var/log/flask-system").mkdir(parents=True, exist_ok=True)
        command = ["/usr/bin/systemd-run", "--quiet", "--collect", "--unit=" + unit,
                   "--property=Type=exec", "--property=KillMode=control-group",
                   "--property=StandardOutput=append:/var/log/flask-system/pci-exclusions.log",
                   "--property=StandardError=append:/var/log/flask-system/pci-exclusions.log",
                   "--setenv=NAS_CONF_DIR=" + str(self.conf_dir), "--", sys.executable,
                   str(Path(__file__).resolve()), "--rebuild"]
        result = subprocess.run(command, text=True, capture_output=True, timeout=15)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Impossible de lancer la préparation du démarrage.")

    def set_excluded(self, address: str, excluded: bool) -> dict:
        if not isinstance(address, str) or not ADDRESS_RE.fullmatch(address) or not isinstance(excluded, bool):
            raise ValueError("Adresse PCI et case à cocher invalides.")
        if not self.boot_support_installed():
            raise RuntimeError("Le support des exclusions PCI n'est pas installé sur le NAS.")
        with self.lock():
            selected = self.entries()
            old_text = self.config.read_text() if self.config.exists() else CONFIG_HEADER
            previous = self.read_state()
            if excluded:
                inventory = self.devices()
                if address not in inventory:
                    raise ValueError("Cette carte PCI n'est plus présente.")
                item = inventory[address]
                if item.get("protected_reason"):
                    raise ValueError(item["protected_reason"])
                selected[address] = {key: item[key] for key in ID_FIELDS}
            else:
                selected.pop(address, None)
            content = format_config(selected)
            if self.entries() == selected and previous.get("phase") != "error":
                return self.status()
            unit = "yoleo-pci-" + uuid.uuid4().hex
            state = {**previous, "phase": "queued", "error": "", "requested_at": time.time(),
                     "unit": unit, "done": 0, "total": 0, "revision": previous.get("revision", 0) + 1}
            atomic_write(self.config, content)
            self.write_state(state)
            try:
                self.launch_builder(unit)
            except Exception:
                atomic_write(self.config, old_text)
                self.write_state(previous)
                raise
        return self.status()

    def kernel_versions(self) -> list[str]:
        versions = [p.name.removeprefix("initrd.img-") for p in self.path("/boot").glob("initrd.img-*")
                    if p.is_file() and self.path("/lib/modules").joinpath(p.name.removeprefix("initrd.img-")).is_dir()]
        if not versions:
            raise RuntimeError("Aucun initramfs de noyau installé n'a été trouvé.")
        return sorted(versions)

    def rebuild_images(self, snapshot: Path, versions: list[str]) -> None:
        for index, version in enumerate(versions):
            print(f"Yoleo PCI: préparation du noyau {version}", flush=True)
            result = subprocess.run(["/usr/sbin/update-initramfs", "-u", "-k", version],
                                    env={**os.environ, "YOLEO_PCI_CONFIG": str(snapshot)}, timeout=900)
            if result.returncode:
                raise RuntimeError(f"Échec de la préparation du noyau {version} (code {result.returncode}).")
            with self.lock():
                state = self.read_state()
                self.write_state({**state, "done": index + 1, "total": len(versions)})

    def rebuild(self) -> None:
        # A worker has its own systemd unit, so restarting Flask cannot kill it.
        # A worker is queued for each change; this lock serializes all boot writes.
        with self.lock("build.lock"):
            time.sleep(0.8)
            while True:
                with self.lock():
                    selected = self.entries()
                    digest = config_digest(selected)
                    state = self.read_state()
                    if state.get("prepared_hash") == digest:
                        self.write_state({**state, "phase": "ready", "error": ""})
                        return
                    # Once a build begins, an interrupted update may leave mixed
                    # images. Never trust an older prepared hash after a failure.
                    self.write_state({**state, "phase": "running", "prepared_hash": "", "error": "", "done": 0, "total": 0})
                try:
                    versions = self.kernel_versions()
                    with self.lock():
                        self.write_state({**self.read_state(), "total": len(versions)})
                    with tempfile.TemporaryDirectory(prefix="build-", dir=self.runtime) as directory:
                        snapshot = Path(directory) / "pci-exclusions.conf"
                        atomic_write(snapshot, format_config(selected))
                        self.rebuild_images(snapshot, versions)
                except Exception as exc:
                    with self.lock():
                        self.write_state({**self.read_state(), "phase": "error", "error": str(exc), "finished_at": time.time()})
                    raise
                with self.lock():
                    changed = self.entries() != selected
                    self.write_state({**self.read_state(), "prepared_hash": digest,
                                      "phase": "queued" if changed else "ready", "error": "", "finished_at": time.time()})
                if not changed:
                    return

    def install(self) -> None:
        assets = Path(__file__).resolve().with_name("pci_boot")
        links = [("initramfs-hook", "/etc/initramfs-tools/hooks/yoleo-pci"),
                 ("init-top-udev", "/etc/initramfs-tools/scripts/init-top/udev")]
        # Check the entire installation before writing anything. A restored ISO
        # can safely rerun this without erasing saved choices or custom hooks.
        for source in ("initramfs-hook", "init-top-udev", "reserve-pci"):
            if not os.access(assets / source, os.X_OK):
                raise RuntimeError(f"Support PCI absent ou non exécutable : {assets / source}")
        for required in ("/usr/sbin/update-initramfs", "/usr/share/initramfs-tools/scripts/init-top/udev"):
            if not self.path(required).is_file():
                raise RuntimeError(f"initramfs-tools requis pour les exclusions PCI : {required}")
        for source, destination in links:
            target = self.path(destination)
            expected = assets / source
            if target.exists() or target.is_symlink():
                if not target.is_symlink() or target.resolve() != expected:
                    raise RuntimeError(f"Configuration existante conservée : {destination}")
        self.entries()  # Validate existing selections without replacing them.
        for source, destination in links:
            target = self.path(destination)
            if not target.is_symlink():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(assets / source)
        if not self.config.exists():
            atomic_write(self.config, CONFIG_HEADER)


def register_pci_routes(blueprint, manager_factory=PciReservations) -> None:
    def get_reservations():
        try:
            return jsonify(manager_factory().status())
        except (OSError, ValueError) as exc:
            return jsonify(ok=False, message=str(exc)), 500

    def save_reservation():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(ok=False, message="Requête JSON attendue."), 400
        try:
            return jsonify(manager_factory().set_excluded(payload.get("address"), payload.get("excluded")))
        except ValueError as exc:
            return jsonify(ok=False, message=str(exc)), 400
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return jsonify(ok=False, message=str(exc)), 500

    blueprint.add_url_rule("/system/api/pci-exclusions", "pci_exclusions_status", get_reservations, methods=["GET"])
    blueprint.add_url_rule("/system/api/pci-exclusions", "pci_exclusions_save", save_reservation, methods=["POST"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--install", action="store_true")
    options = parser.parse_args()
    manager = PciReservations()
    if options.install:
        manager.install()
    elif options.rebuild:
        manager.rebuild()
    else:
        print(json.dumps(manager.status(), ensure_ascii=False, indent=2))
