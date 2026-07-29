#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
samba.py - Gestion Samba host façon Docker/Unraid.

Version : 2026-05-17-wsdd2-distro-install

Commandes :
  samba.py -list
  samba.py -install
  samba.py -apply
  samba.py -start
  samba.py -stop
  samba.py -remove

Chemin conf par défaut : ../conf/samba.conf si le script est dans scripts/.
Exemple :
  /mnt/user/dockers/scripts/samba.py
  /mnt/user/dockers/conf/samba.conf
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import os
import pwd
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

VERSION = "2026-05-17-wsdd2-distro-install"
MANAGED_MARKER = "# Managed by samba.py"
WSDD_SERVICE = "samba-wsdd-host.service"  # ancien service custom, supprimé par -install/-apply
DISTRO_WSDD_SERVICES = ("wsdd2.service", "wsdd.service")
APPLY_SERVICE = "samba-host-apply.service"
STATE_DIR = Path("/var/lib/samba-host")
USER_HASH_FILE = STATE_DIR / "users.sha256"

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONF = SCRIPT_DIR.parent / "conf" / "samba.conf"
FALLBACK_CONF = SCRIPT_DIR / "samba.conf"


@dataclass
class User:
    name: str
    password: str
    uid: int
    gid: int
    shell: str = "/usr/sbin/nologin"
    home: str = "/nonexistent"


@dataclass
class Share:
    name: str
    path: Path
    share_type: str = "normal"
    guest_ok: str = "no"
    read_only: str = "no"
    browsable: str = "yes"
    writable: str = "yes"


@dataclass
class SambaConfig:
    conf_path: Path
    workgroup: str
    server_string: str
    netbios_name: str
    interface: str
    smb_conf: Path
    log_file: str
    max_log_size: str
    min_protocol: str
    enable_wsdd: bool
    wsdd_name: str
    create_missing_dirs: bool
    users: list[User]
    shares: list[Share]


def bool_value(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "yes", "y", "true", "on", "oui"}


def run(cmd: list[str], *, input_text: str | None = None, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess[str]:
    if not quiet:
        print("$ " + " ".join(cmd))
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        check=check,
        stdout=subprocess.PIPE if quiet else None,
        stderr=subprocess.PIPE if quiet else None,
    )


def command_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def require_root() -> None:
    if os.geteuid() != 0:
        print("ERREUR : cette action doit être lancée en root.", file=sys.stderr)
        sys.exit(1)


def resolve_conf_path(raw: str | None) -> Path:
    if raw:
        return Path(raw).expanduser().resolve()
    if DEFAULT_CONF.exists():
        return DEFAULT_CONF.resolve()
    if FALLBACK_CONF.exists():
        return FALLBACK_CONF.resolve()
    return DEFAULT_CONF.resolve()


def load_config(conf_path: Path) -> SambaConfig:
    if not conf_path.exists():
        print(f"ERREUR : fichier conf introuvable : {conf_path}", file=sys.stderr)
        print("Astuce : place samba.conf dans ../conf/samba.conf ou passe --conf /chemin/samba.conf", file=sys.stderr)
        sys.exit(1)

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(conf_path, encoding="utf-8")

    if "global" not in parser:
        print("ERREUR : section [global] absente du fichier conf.", file=sys.stderr)
        sys.exit(1)

    g = parser["global"]
    users: list[User] = []
    shares: list[Share] = []

    for section in parser.sections():
        if section.startswith("user:"):
            name = section.split(":", 1)[1].strip()
            data = parser[section]
            if not name or not data.get("password") or not data.get("uid") or not data.get("gid"):
                print(f"AVERTISSEMENT : user incomplet ignoré : [{section}]")
                continue
            users.append(User(
                name=name,
                password=data.get("password", ""),
                uid=int(data.get("uid", "0")),
                gid=int(data.get("gid", "0")),
                shell=data.get("shell", "/usr/sbin/nologin"),
                home=data.get("home", "/nonexistent"),
            ))
        elif section.startswith("share:"):
            name = section.split(":", 1)[1].strip()
            data = parser[section]
            if not name or not data.get("path"):
                print(f"AVERTISSEMENT : partage incomplet ignoré : [{section}]")
                continue
            share_type = data.get("type", "normal").strip().lower()
            if share_type in {"root", "admin", "777", "force_root", "force-root"}:
                share_type = "root"
            else:
                share_type = "normal"
            shares.append(Share(
                name=name,
                path=Path(data.get("path", "")).expanduser(),
                share_type=share_type,
                guest_ok=data.get("guest_ok", "no"),
                read_only=data.get("read_only", "no"),
                browsable=data.get("browsable", "yes"),
                writable=data.get("writable", "yes"),
            ))

    return SambaConfig(
        conf_path=conf_path,
        workgroup=g.get("workgroup", "WORKGROUP"),
        server_string=g.get("server_string", "Host Samba Multi"),
        netbios_name=g.get("netbios_name", "Samba"),
        interface=g.get("interface", "br0"),
        smb_conf=Path(g.get("smb_conf", "/etc/samba/smb.conf")),
        log_file=g.get("log_file", "/var/log/samba/log.%m"),
        max_log_size=g.get("max_log_size", "50"),
        min_protocol=g.get("min_protocol", "SMB2"),
        enable_wsdd=bool_value(g.get("enable_wsdd"), True),
        wsdd_name=g.get("wsdd_name", g.get("netbios_name", "Samba")),
        create_missing_dirs=bool_value(g.get("create_missing_dirs"), True),
        users=users,
        shares=shares,
    )


def existing_group_by_gid(gid: int) -> str | None:
    try:
        result = run(["getent", "group", str(gid)], quiet=True, check=False)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split(":", 1)[0]
    except Exception:
        return None
    return None


def user_exists(name: str) -> bool:
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def apt_install(packages: list[str], *, fatal: bool = True) -> bool:
    if not packages:
        return True
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    cmd = ["apt-get", "install", "-y", *packages]
    print("$ " + " ".join(cmd))
    result = subprocess.run(cmd, env=env)
    if result.returncode == 0:
        return True
    if fatal:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return False


def detect_wsdd_binary() -> tuple[str | None, str | None]:
    """Retourne (implémentation, chemin). implémentation = wsdd2 ou wsdd."""
    for name in ("wsdd2", "wsdd"):
        path = shutil.which(name)
        if path:
            return name, path
    return None, None


def install_packages() -> None:
    # Base Samba obligatoire.
    base_commands = ["smbd", "nmbd", "smbpasswd", "testparm"]
    if not all(command_exists(cmd) for cmd in base_commands):
        if not command_exists("apt-get"):
            print("ERREUR : apt-get introuvable. Installe manuellement samba, samba-vfs-modules, acl, attr.", file=sys.stderr)
            sys.exit(1)
        env = os.environ.copy()
        env["DEBIAN_FRONTEND"] = "noninteractive"
        print("Installation base Samba : samba, samba-vfs-modules, acl, attr")
        subprocess.run(["apt-get", "update"], check=True, env=env)
        apt_install(["samba", "samba-vfs-modules", "acl", "attr"], fatal=True)
    else:
        print("Base Samba déjà présente.")

    # Découverte Windows : trixie utilise wsdd2. Ne jamais bloquer Samba si la découverte échoue.
    impl, _ = detect_wsdd_binary()
    if impl:
        print(f"Découverte Windows déjà présente : {impl}")
        return

    if not command_exists("apt-get"):
        print("AVERTISSEMENT : apt-get introuvable, wsdd/wsdd2 non installé.")
        return

    print("Installation découverte Windows : tentative wsdd2 d'abord.")
    if apt_install(["wsdd2"], fatal=False):
        return

    print("wsdd2 indisponible, tentative fallback wsdd.")
    if apt_install(["wsdd"], fatal=False):
        return

    print("AVERTISSEMENT : ni wsdd2 ni wsdd installables. Samba fonctionnera, mais la visibilité automatique Windows peut manquer.")


def users_hash(cfg: SambaConfig) -> str:
    h = hashlib.sha256()
    for user in sorted(cfg.users, key=lambda u: u.name):
        h.update(f"{user.name}:{user.password}:{user.uid}:{user.gid}:{user.shell}:{user.home}\n".encode("utf-8"))
    return h.hexdigest()


def ensure_users(cfg: SambaConfig, *, force_password_update: bool = False) -> None:
    print("--- Création / mise à jour des utilisateurs Samba ---")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    new_hash = users_hash(cfg)
    old_hash = USER_HASH_FILE.read_text(encoding="utf-8").strip() if USER_HASH_FILE.exists() else ""
    password_update_needed = force_password_update or new_hash != old_hash

    for user in cfg.users:
        group_name = existing_group_by_gid(user.gid)
        if not group_name:
            group_name = user.name
            if command_exists("groupadd"):
                run(["groupadd", "-g", str(user.gid), group_name])
            else:
                print(f"ERREUR : groupadd introuvable pour créer GID {user.gid}", file=sys.stderr)
                sys.exit(1)

        if user_exists(user.name):
            pw = pwd.getpwnam(user.name)
            print(f"Utilisateur Linux existant : {user.name} (UID:{pw.pw_uid} GID:{pw.pw_gid})")
            if pw.pw_uid != user.uid:
                print(f"AVERTISSEMENT : UID existant différent du conf ({pw.pw_uid} != {user.uid}), je ne modifie pas l'user Linux.")
            if pw.pw_gid != user.gid:
                print(f"AVERTISSEMENT : GID existant différent du conf ({pw.pw_gid} != {user.gid}), je ne modifie pas l'user Linux.")
        else:
            shell = user.shell if Path(user.shell).exists() else "/bin/false"
            run(["useradd", "-M", "-u", str(user.uid), "-g", group_name, "-d", user.home, "-s", shell, user.name])

        if password_update_needed:
            print(f"Mot de passe Samba mis à jour : {user.name}")
            run(["smbpasswd", "-a", "-s", user.name], input_text=f"{user.password}\n{user.password}\n")
            run(["smbpasswd", "-e", user.name], check=False)
        else:
            print(f"Mot de passe Samba inchangé : {user.name}")

    USER_HASH_FILE.write_text(new_hash + "\n", encoding="utf-8")
    USER_HASH_FILE.chmod(0o600)


def ensure_share_dirs(cfg: SambaConfig) -> None:
    if not cfg.create_missing_dirs:
        return
    print("--- Création des dossiers de partage manquants ---")
    for share in cfg.shares:
        if share.path.exists():
            print(f"OK : {share.path}")
        else:
            print(f"Création : {share.path}")
            share.path.mkdir(parents=True, exist_ok=True)


def render_smb_conf(cfg: SambaConfig) -> str:
    valid_users = " ".join(user.name for user in cfg.users).strip()
    lines: list[str] = [
        MANAGED_MARKER,
        f"# Source: {cfg.conf_path}",
        "",
        "[global]",
        f"    workgroup = {cfg.workgroup}",
        f"    server string = {cfg.server_string}",
        f"    netbios name = {cfg.netbios_name}",
        "    security = user",
        "    map to guest = Bad User",
        "    guest account = nobody",
        f"    log file = {cfg.log_file}",
        f"    max log size = {cfg.max_log_size}",
        f"    min protocol = {cfg.min_protocol}",
        "    change notify = yes",
        "    kernel change notify = yes",
        "    notify:allow_extended_notifications = yes",
        "    oplocks = no",
        "    level2 oplocks = no",
        "    strict sync = yes",
        "    sync always = yes",
        "    load printers = no",
        "    printing = bsd",
        "    printcap name = /dev/null",
        "    disable spoolss = yes",
        "",
    ]

    for share in cfg.shares:
        lines.extend([
            f"[{share.name}]",
            f"    path = {share.path}",
            f"    guest ok = {share.guest_ok}",
            f"    read only = {share.read_only}",
            f"    browsable = {share.browsable}",
            f"    writable = {share.writable}",
        ])
        if valid_users:
            lines.append(f"    valid users = {valid_users}")
        if share.share_type == "root":
            lines.extend([
                "    force user = root",
                "    force group = root",
                "    create mask = 0777",
                "    directory mask = 0777",
            ])
        else:
            lines.extend([
                "    inherit permissions = yes",
                "    vfs objects = fruit streams_xattr",
            ])
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def backup_file(path: Path, reason: str = "backup") -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.name}.{reason}.{stamp}")
    shutil.copy2(path, backup)
    print(f"Backup : {backup}")
    return backup


def write_if_changed(path: Path, content: str, *, mode: int = 0o644, backup_unmanaged_marker: str | None = None) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        current = path.read_text(encoding="utf-8", errors="ignore")
        if current == content:
            print(f"Inchangé : {path}")
            return False
        if backup_unmanaged_marker and backup_unmanaged_marker not in current:
            print(f"AVERTISSEMENT : fichier existant non géré par samba.py, backup avant remplacement : {path}")
        backup_file(path, "bak")
    else:
        print(f"Création : {path}")

    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    print(f"Écrit : {path}")
    return True


def write_smb_conf(cfg: SambaConfig) -> bool:
    print(f"--- Application Samba : {cfg.smb_conf} ---")
    changed = write_if_changed(cfg.smb_conf, render_smb_conf(cfg), mode=0o644, backup_unmanaged_marker=MANAGED_MARKER)
    if changed and command_exists("testparm"):
        run(["testparm", "-s", str(cfg.smb_conf)])
    elif command_exists("testparm"):
        run(["testparm", "-s", str(cfg.smb_conf)], check=False)
    return changed


def render_apply_service(cfg: SambaConfig) -> str:
    python_bin = sys.executable or "/usr/bin/python3"
    script = Path(__file__).resolve()
    return textwrap.dedent(f"""
    [Unit]
    Description=Applique la configuration Samba host générée par samba.py
    After=local-fs.target network-pre.target
    Before=smbd.service nmbd.service

    [Service]
    Type=oneshot
    ExecStart={python_bin} {script} -apply --conf {cfg.conf_path}
    RemainAfterExit=yes

    [Install]
    WantedBy=multi-user.target
    """).lstrip()


def render_wsdd_service(cfg: SambaConfig) -> str | None:
    if not cfg.enable_wsdd:
        return None

    impl, path = detect_wsdd_binary()
    if not impl or not path:
        print("AVERTISSEMENT : wsdd2/wsdd absent, service WSDD non généré.")
        return None

    if impl == "wsdd2":
        exec_start = f"{path} -N {cfg.netbios_name} -G {cfg.workgroup} -i {cfg.interface}"
    else:
        exec_start = f"{path} -n {cfg.wsdd_name} -i {cfg.interface}"

    return textwrap.dedent(f"""
    [Unit]
    Description=Visibilité réseau Windows pour Samba host ({impl})
    After=network-online.target smbd.service nmbd.service
    Wants=network-online.target

    [Service]
    Type=simple
    ExecStart={exec_start}
    Restart=on-failure
    RestartSec=3

    [Install]
    WantedBy=multi-user.target
    """).lstrip()


def write_apply_service(cfg: SambaConfig) -> bool:
    service_path = Path("/etc/systemd/system") / APPLY_SERVICE
    print(f"--- Service apply : {service_path} ---")
    return write_if_changed(service_path, render_apply_service(cfg), mode=0o644)


def write_wsdd_service(cfg: SambaConfig) -> bool:
    """
    Depuis 2026-05-17, on ne génère plus de service custom pour WSDD.
    On utilise le service Debian installé par le paquet : wsdd2.service ou wsdd.service.
    Avantage : après un simple -install, la commande `systemctl status wsdd2`
    montre bien le service actif, comme attendu.
    """
    changed = False
    legacy_path = Path("/etc/systemd/system") / WSDD_SERVICE

    if legacy_path.exists():
        print(f"--- Suppression ancien service WSDD custom : {legacy_path} ---")
        systemctl(["disable", "--now", WSDD_SERVICE], check=False)
        backup_file(legacy_path, "legacy")
        legacy_path.unlink()
        print(f"Supprimé : {legacy_path}")
        changed = True

    if cfg.enable_wsdd:
        service = detect_wsdd_systemd_service()
        if service:
            print(f"Découverte Windows gérée par le service système : {service}")
        else:
            impl, path = detect_wsdd_binary()
            if impl:
                print(f"AVERTISSEMENT : binaire {impl} trouvé ({path}), mais aucun service systemd wsdd2/wsdd détecté.")
            else:
                print("AVERTISSEMENT : wsdd2/wsdd absent, visibilité automatique Windows probablement absente.")
    else:
        print("Découverte Windows désactivée dans samba.conf : enable_wsdd = no")
        for svc in DISTRO_WSDD_SERVICES:
            if service_exists(svc):
                systemctl(["disable", "--now", svc], check=False)

    return changed


def systemctl(args: Iterable[str], *, check: bool = False) -> None:
    if not command_exists("systemctl"):
        print("systemctl introuvable, action ignorée : " + " ".join(args))
        return
    run(["systemctl", *args], check=check)


def service_exists(service: str) -> bool:
    if not command_exists("systemctl"):
        return False
    result = run(["systemctl", "list-unit-files", service, "--no-legend"], quiet=True, check=False)
    return result.returncode == 0 and bool(result.stdout.strip())


def detect_wsdd_systemd_service() -> str | None:
    """Retourne le service systemd WSDD disponible, en priorité wsdd2 sur Debian récent."""
    for svc in DISTRO_WSDD_SERVICES:
        if service_exists(svc):
            return svc
    return None


def apply_config(cfg: SambaConfig, *, update_passwords: bool = False) -> bool:
    ensure_share_dirs(cfg)
    ensure_users(cfg, force_password_update=update_passwords)
    changed = write_smb_conf(cfg)
    changed = write_apply_service(cfg) or changed
    changed = write_wsdd_service(cfg) or changed
    if changed:
        systemctl(["daemon-reload"], check=False)
    else:
        print("Aucun fichier système à mettre à jour.")
    return changed


def start_services(cfg: SambaConfig, *, restart: bool = True) -> None:
    print("--- Démarrage / redémarrage services Samba ---")
    systemctl(["daemon-reload"], check=False)
    systemctl(["enable", APPLY_SERVICE], check=False)

    for svc in ("smbd.service", "nmbd.service"):
        systemctl(["enable", svc], check=False)
        systemctl(["restart" if restart else "start", svc], check=False)

    # Ancienne version : service custom samba-wsdd-host.service.
    # Nouvelle version : service paquet Debian wsdd2.service/wsdd.service, activé par -install.
    if service_exists(WSDD_SERVICE):
        systemctl(["disable", "--now", WSDD_SERVICE], check=False)

    if cfg.enable_wsdd:
        service = detect_wsdd_systemd_service()
        if service:
            systemctl(["enable", service], check=False)
            systemctl(["restart" if restart else "start", service], check=False)
        else:
            print("AVERTISSEMENT : aucun service wsdd2/wsdd disponible à démarrer.")
    else:
        for svc in DISTRO_WSDD_SERVICES:
            if service_exists(svc):
                systemctl(["disable", "--now", svc], check=False)


def stop_services() -> None:
    print("--- Arrêt services Samba ---")
    for svc in (WSDD_SERVICE, *DISTRO_WSDD_SERVICES, "smbd.service", "nmbd.service"):
        systemctl(["stop", svc], check=False)


def disable_services() -> None:
    print("--- Désactivation services Samba ---")
    for svc in (APPLY_SERVICE, WSDD_SERVICE, "smbd.service", "nmbd.service", *DISTRO_WSDD_SERVICES):
        systemctl(["disable", svc], check=False)


def action_list(cfg: SambaConfig) -> None:
    print(f"Version samba.py : {VERSION}")
    print(f"Conf lue : {cfg.conf_path}")
    print(f"smb.conf cible : {cfg.smb_conf}")
    print(f"Interface WSDD : {cfg.interface}")
    impl, path = detect_wsdd_binary()
    service = detect_wsdd_systemd_service()
    print(f"Découverte Windows : {impl or 'absente'} {path or ''}".rstrip())
    print(f"Service découverte : {service or 'absent'}")
    print(f"Utilisateurs : {', '.join(u.name for u in cfg.users) if cfg.users else '(aucun)'}")
    print("\nPartages :")
    for share in cfg.shares:
        status = "OK" if share.path.exists() else "MANQUANT"
        print(f"  - {share.name:12} {share.share_type:6} {status:8} {share.path}")

    if command_exists("systemctl"):
        print("\nServices :")
        for svc in ("smbd.service", "nmbd.service", APPLY_SERVICE, *DISTRO_WSDD_SERVICES, WSDD_SERVICE):
            result = run(["systemctl", "is-active", svc], quiet=True, check=False)
            state = result.stdout.strip() or "unknown"
            print(f"  - {svc:24} {state}")


def action_install(cfg: SambaConfig) -> None:
    require_root()
    install_packages()
    cfg.conf_path.chmod(0o600)
    apply_config(cfg, update_passwords=True)
    start_services(cfg, restart=True)
    print("OK : Samba host installé et démarré.")


def action_apply(cfg: SambaConfig) -> None:
    require_root()
    cfg.conf_path.chmod(0o600)
    apply_config(cfg, update_passwords=False)
    print("OK : configuration Samba appliquée.")


def action_start(cfg: SambaConfig) -> None:
    require_root()
    apply_config(cfg, update_passwords=False)
    start_services(cfg, restart=True)
    print("OK : Samba host appliqué à chaud et démarré.")


def action_stop(_: SambaConfig) -> None:
    require_root()
    stop_services()
    print("OK : Samba arrêté.")


def action_remove(cfg: SambaConfig) -> None:
    require_root()
    stop_services()
    disable_services()

    for service_name in (WSDD_SERVICE, APPLY_SERVICE):
        service_path = Path("/etc/systemd/system") / service_name
        if service_path.exists():
            backup_file(service_path, "removed")
            service_path.unlink()
            print(f"Supprimé : {service_path}")

    systemctl(["daemon-reload"], check=False)

    if cfg.smb_conf.exists():
        current = cfg.smb_conf.read_text(encoding="utf-8", errors="ignore")
        if MANAGED_MARKER in current:
            backup_file(cfg.smb_conf, "removed")
            cfg.smb_conf.unlink()
            print(f"Supprimé : {cfg.smb_conf}")
        else:
            print(f"Je ne supprime pas {cfg.smb_conf} : fichier non marqué samba.py.")

    print("OK : Samba host retiré. Les paquets, les users Linux et les données ne sont pas supprimés.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gestion Samba host depuis samba.conf")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-list", action="store_true", help="Lister la configuration et l'état")
    group.add_argument("-install", action="store_true", help="Installer les paquets, écrire smb.conf, activer et démarrer")
    group.add_argument("-apply", action="store_true", help="Appliquer la conf sans réinstaller les paquets")
    group.add_argument("-start", action="store_true", help="Appliquer la conf à chaud et démarrer/redémarrer")
    group.add_argument("-stop", action="store_true", help="Arrêter smbd/nmbd/wsdd")
    group.add_argument("-remove", action="store_true", help="Désactiver services et retirer les fichiers générés")
    parser.add_argument("--conf", help="Chemin du fichier samba.conf")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(resolve_conf_path(args.conf))

    if args.list:
        action_list(cfg)
    elif args.install:
        action_install(cfg)
    elif args.apply:
        action_apply(cfg)
    elif args.start:
        action_start(cfg)
    elif args.stop:
        action_stop(cfg)
    elif args.remove:
        action_remove(cfg)


if __name__ == "__main__":
    main()
