#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ============================================================
# nfs.py - Gestion simple des partages NFS hôte Debian/Linux
# ============================================================
#
# BUT
#   Simplifier /etc/exports et exportfs avec des commandes humaines.
#   Le script ne monte pas un partage distant : il publie un dossier local
#   vers le réseau depuis le serveur NFS.
#
# FICHIER GÉRÉ
#   /etc/exports.d/yoyo.exports
#
# LEXIQUE DES COMMANDES
#
# Liste des partages configurés + actifs :
#   python3 nfs.py -list
#   python3 nfs.py -liste
#   python3 nfs.py -l
#
# Ajouter un partage permanent + l'activer maintenant :
#   sudo python3 nfs.py -install /mnt/user/Media:rw 192.168.1.0/24
#   sudo python3 nfs.py -install /mnt/user/Backup:ro 192.168.1.158
#
# Activer un partage à chaud, sans l'ajouter au démarrage :
#   sudo python3 nfs.py -mount /mnt/user/Media:rw 192.168.1.0/24
#
# Supprimer un partage permanent depuis /etc/exports.d/yoyo.exports :
#   sudo python3 nfs.py -remove /mnt/user/Media
#
# Désactiver un export à chaud, sans modifier le fichier permanent :
#   sudo python3 nfs.py -unmount /mnt/user/Media
#   sudo python3 nfs.py -umount /mnt/user/Media
#
# Recharger les exports depuis les fichiers système :
#   sudo python3 nfs.py -reload
#
# NOTES
#   /chemin:rw       = lecture/écriture
#   /chemin:ro       = lecture seule
#   192.168.1.0/24   = tout le LAN 192.168.1.x
#   192.168.1.158    = une seule machine
#
#   Si le client n'est pas précisé, le script utilise par défaut :
#   192.168.1.0/24
#
# OPTIONS NFS UTILISÉES
#   rw/ro,sync,no_subtree_check,no_root_squash
#
# SÉCURITÉ
#   - Le script ne fait aucun apt install.
#   - Il refuse les chemins dangereux : /, /etc, /boot, /usr, /var, etc.
#   - Il crée une sauvegarde avant de modifier yoyo.exports.
#   - Il ne touche pas directement à /etc/exports.
#
# ============================================================

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

EXPORTS_FILE = Path("/etc/exports.d/yoyo.exports")
DEFAULT_CLIENT = "192.168.1.0/24"
DEFAULT_BASE_OPTIONS = "sync,no_subtree_check,no_root_squash"

FORBIDDEN_SHARE_PATHS = {
    "/", "/bin", "/boot", "/boot/efi", "/dev", "/etc", "/lib", "/lib64",
    "/proc", "/run", "/sbin", "/sys", "/tmp", "/usr", "/var", "/home",
    # On garde /mnt interdit : c'est la racine technique des montages.
    # /mnt/user est autorisé plus bas et reçoit automatiquement fsid=100.
    "/mnt",
}

# Certains points de montage comme mergerfs/FUSE peuvent nécessiter un fsid explicite
# pour être exportés par NFS. Valeurs stables et uniques dans ce fichier.
AUTO_FSID_PATHS = {
    "/mnt/user": "100",
    "/mnt/user0": "101",
    "/mnt/cache": "102",
}


@dataclass(frozen=True)
class ExportEntry:
    path: str
    client: str
    access: str  # rw ou ro

    @property
    def options(self) -> str:
        opts = [self.access]
        opts.extend(x for x in DEFAULT_BASE_OPTIONS.split(",") if x)

        fsid = AUTO_FSID_PATHS.get(os.path.normpath(self.path))
        if fsid:
            opts.append(f"fsid={fsid}")

        return ",".join(opts)

    def to_line(self) -> str:
        return f"{escape_exports_path(self.path)} {self.client}({self.options})"


def run(cmd: list[str], *, check: bool = False, quiet: bool = False) -> subprocess.CompletedProcess[str]:
    if quiet:
        return subprocess.run(
            cmd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )
    return subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", check=check)


def need_root() -> None:
    if os.geteuid() != 0:
        print("ERREUR : lance cette commande en root.")
        print("Exemple : sudo python3 nfs.py -install /mnt/user/Media:rw 192.168.1.0/24")
        sys.exit(1)


def need_cmd(name: str, package_hint: str | None = None) -> None:
    if shutil.which(name) is None:
        print(f"ERREUR : commande introuvable : {name}")
        if package_hint:
            print(f"Installe le paquet si besoin : apt install -y {package_hint}")
        print("Le script n'installe rien automatiquement.")
        sys.exit(1)


def escape_exports_path(path: str) -> str:
    # /etc/exports utilise \040 pour les espaces.
    return path.replace(" ", "\\040")


def unescape_exports_path(path: str) -> str:
    return path.replace("\\040", " ")


def normalize_path(path: str) -> str:
    if not path.startswith("/"):
        raise ValueError(f"chemin non absolu : {path}")
    return os.path.normpath(path)


def parse_share_spec(spec: str, *, require_access: bool = True) -> tuple[str, str | None]:
    spec = spec.strip()
    if not spec:
        raise ValueError("spécification vide")

    if ":" in spec:
        path, access = spec.rsplit(":", 1)
        path = normalize_path(path.strip())
        access = access.strip().lower()
        if access not in {"rw", "ro"}:
            raise ValueError(f"droit invalide : {access}. Utilise rw ou ro.")
        return path, access

    path = normalize_path(spec)
    if require_access:
        raise ValueError("droit manquant. Exemple : /mnt/user/Media:rw ou /mnt/user/Media:ro")
    return path, None


def validate_client(client: str) -> str:
    client = client.strip()
    if not client:
        raise ValueError("client vide")
    if re.search(r"\s", client):
        raise ValueError(f"client invalide avec espace : {client}")
    # On accepte IP, CIDR, hostname, * si l'utilisateur le veut vraiment.
    return client


def ensure_safe_share_path(path: str, *, create: bool) -> None:
    real = os.path.realpath(path) if os.path.exists(path) else os.path.abspath(path)

    if path in FORBIDDEN_SHARE_PATHS or real in FORBIDDEN_SHARE_PATHS:
        print(f"ERREUR : chemin de partage interdit : {path}")
        print("Choisis un vrai dossier de partage, exemple : /mnt/user/Media ou /mnt/user0/Backup")
        sys.exit(1)

    if not path.startswith("/"):
        print(f"ERREUR : chemin non absolu : {path}")
        sys.exit(1)

    if create:
        Path(path).mkdir(parents=True, exist_ok=True)
    elif not Path(path).exists():
        print(f"ERREUR : chemin introuvable : {path}")
        sys.exit(1)

    if not Path(path).is_dir():
        print(f"ERREUR : ce n'est pas un dossier : {path}")
        sys.exit(1)


def backup_exports_file() -> Path | None:
    if not EXPORTS_FILE.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = EXPORTS_FILE.with_name(EXPORTS_FILE.name + f".backup_{stamp}")
    shutil.copy2(EXPORTS_FILE, backup)
    return backup


def parse_exports_line(line: str) -> ExportEntry | None:
    clean = line.strip()
    if not clean or clean.startswith("#"):
        return None

    parts = clean.split(None, 1)
    if len(parts) != 2:
        return None

    path = unescape_exports_path(parts[0])
    rest = parts[1].strip()

    # On gère seulement les lignes simples générées par ce script :
    # /path client(options)
    match = re.match(r"^([^\s(]+)\(([^)]*)\)$", rest)
    if not match:
        return None

    client = match.group(1)
    options = match.group(2).split(",")
    access = "rw" if "rw" in options else "ro" if "ro" in options else "rw"
    return ExportEntry(path=path, client=client, access=access)


def read_entries() -> list[ExportEntry]:
    if not EXPORTS_FILE.exists():
        return []

    entries: list[ExportEntry] = []
    with EXPORTS_FILE.open("r", encoding="utf-8", errors="replace") as fp:
        for line in fp:
            entry = parse_exports_line(line)
            if entry:
                entries.append(entry)
    return entries


def write_entries(entries: list[ExportEntry]) -> None:
    EXPORTS_FILE.parent.mkdir(parents=True, exist_ok=True)

    with EXPORTS_FILE.open("w", encoding="utf-8") as fp:
        fp.write("# ============================================================\n")
        fp.write("# yoyo.exports - généré par nfs.py\n")
        fp.write("# Ne pas mélanger avec /etc/exports principal.\n")
        fp.write("# ============================================================\n\n")
        for entry in entries:
            fp.write(entry.to_line() + "\n")


def ensure_nfs_service() -> None:
    # NFS sur Debian utilise généralement nfs-server.service / nfs-kernel-server.
    if shutil.which("systemctl"):
        run(["systemctl", "enable", "--now", "nfs-server"], quiet=True)
        if run(["systemctl", "is-active", "--quiet", "nfs-server"], quiet=True).returncode == 0:
            return
        run(["systemctl", "enable", "--now", "nfs-kernel-server"], quiet=True)
        return

    if shutil.which("service"):
        run(["service", "nfs-kernel-server", "start"], quiet=True)


def reload_exports() -> None:
    need_cmd("exportfs", "nfs-kernel-server")
    ensure_nfs_service()
    r = run(["exportfs", "-ra"], quiet=True)
    if r.returncode != 0:
        print("ERREUR : exportfs -ra a échoué.")
        if r.stdout:
            print(r.stdout)
        if r.stderr:
            print(r.stderr)
        sys.exit(r.returncode)


def export_now(entry: ExportEntry) -> None:
    need_cmd("exportfs", "nfs-kernel-server")
    ensure_nfs_service()
    ensure_safe_share_path(entry.path, create=True)

    r = run(["exportfs", "-o", entry.options, f"{entry.client}:{entry.path}"], quiet=True)
    if r.returncode != 0:
        print("ERREUR : export à chaud impossible.")
        if r.stdout:
            print(r.stdout)
        if r.stderr:
            print(r.stderr)
        sys.exit(r.returncode)


def unexport(client: str, path: str) -> bool:
    need_cmd("exportfs", "nfs-kernel-server")
    r = run(["exportfs", "-u", f"{client}:{path}"], quiet=True)
    return r.returncode == 0


def active_exports() -> list[tuple[str, str, str]]:
    if shutil.which("exportfs") is None:
        return []

    r = run(["exportfs", "-v"], quiet=True)
    if r.returncode != 0:
        return []

    result: list[tuple[str, str, str]] = []
    for raw in r.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue

        # Exemples possibles :
        # /mnt/user/Media 192.168.1.0/24(sync,wdelay,...)
        # /mnt/user/Media
        #     192.168.1.0/24(sync,...)
        if line.startswith("/"):
            parts = line.split(None, 1)
            if len(parts) == 2:
                path = unescape_exports_path(parts[0])
                rest = parts[1]
                m = re.match(r"^([^\s(]+)\((.*)\)$", rest)
                if m:
                    result.append((path, m.group(1), m.group(2)))
    return result


def list_nfs() -> None:
    print()
    print("NFS - partages gérés par nfs.py")
    print("================================")
    print(f"Fichier : {EXPORTS_FILE}")
    print()

    entries = read_entries()
    if entries:
        print("Configurés dans yoyo.exports :")
        for entry in entries:
            print(f"  {entry.path}:{entry.access}  {entry.client}  ({entry.options})")
    else:
        print("Aucun partage configuré dans yoyo.exports.")

    print()
    print("Exports actifs vus par exportfs -v :")
    if shutil.which("exportfs") is None:
        print("  exportfs introuvable. Paquet probable manquant : nfs-kernel-server")
    else:
        r = run(["exportfs", "-v"], quiet=True)
        if r.stdout.strip():
            print(r.stdout.rstrip())
        else:
            print("  aucun export actif visible")

    print()
    if shutil.which("systemctl"):
        state = run(["systemctl", "is-active", "nfs-server"], quiet=True).returncode
        if state == 0:
            print("Service : nfs-server actif")
        else:
            state2 = run(["systemctl", "is-active", "nfs-kernel-server"], quiet=True).returncode
            print("Service : actif" if state2 == 0 else "Service : inactif ou non installé")
    print()
    print_examples()


def install_share(spec: str, client_arg: str | None) -> None:
    need_root()
    need_cmd("exportfs", "nfs-kernel-server")

    try:
        path, access = parse_share_spec(spec, require_access=True)
        assert access is not None
        client = validate_client(client_arg or DEFAULT_CLIENT)
    except ValueError as exc:
        print(f"ERREUR : {exc}")
        usage()
        sys.exit(1)

    ensure_safe_share_path(path, create=True)
    new_entry = ExportEntry(path=path, client=client, access=access)

    entries = read_entries()
    changed = False
    replaced = False
    updated: list[ExportEntry] = []

    for entry in entries:
        if entry.path == path and entry.client == client:
            replaced = True
            if entry != new_entry:
                updated.append(new_entry)
                changed = True
            else:
                updated.append(entry)
        else:
            updated.append(entry)

    if not replaced:
        updated.append(new_entry)
        changed = True

    if changed:
        backup = backup_exports_file()
        write_entries(updated)
        if backup:
            print(f"Sauvegarde créée : {backup}")
        print(f"OK : partage écrit dans {EXPORTS_FILE}")
    else:
        print("OK : partage déjà présent, aucune réécriture.")

    reload_exports()
    print(f"OK : NFS permanent actif : {path}:{access} -> {client}")


def mount_share(spec: str, client_arg: str | None) -> None:
    need_root()
    try:
        path, access = parse_share_spec(spec, require_access=True)
        assert access is not None
        client = validate_client(client_arg or DEFAULT_CLIENT)
    except ValueError as exc:
        print(f"ERREUR : {exc}")
        usage()
        sys.exit(1)

    entry = ExportEntry(path=path, client=client, access=access)
    export_now(entry)
    print(f"OK : export NFS à chaud actif : {path}:{access} -> {client}")
    print("Note : ce partage ne reviendra pas au redémarrage sauf avec -install.")


def remove_share(path_arg: str) -> None:
    need_root()
    try:
        path, _ = parse_share_spec(path_arg, require_access=False)
    except ValueError as exc:
        print(f"ERREUR : {exc}")
        usage()
        sys.exit(1)

    entries = read_entries()
    kept = [e for e in entries if e.path != path]
    removed = [e for e in entries if e.path == path]

    if not removed:
        print(f"Aucun partage permanent trouvé pour : {path}")
        return

    backup = backup_exports_file()
    write_entries(kept)
    if backup:
        print(f"Sauvegarde créée : {backup}")

    for entry in removed:
        unexport(entry.client, entry.path)

    reload_exports()
    print(f"OK : partage permanent supprimé : {path}")


def unmount_share(path_arg: str, client_arg: str | None) -> None:
    need_root()
    try:
        path, _ = parse_share_spec(path_arg, require_access=False)
        client = validate_client(client_arg) if client_arg else None
    except ValueError as exc:
        print(f"ERREUR : {exc}")
        usage()
        sys.exit(1)

    targets: list[tuple[str, str]] = []

    if client:
        targets.append((client, path))
    else:
        # D'abord les entrées du fichier géré.
        for entry in read_entries():
            if entry.path == path:
                targets.append((entry.client, entry.path))
        # Puis ce qu'on peut lire dans exportfs -v.
        for active_path, active_client, _opts in active_exports():
            if active_path == path:
                targets.append((active_client, active_path))

    # Déduplication.
    seen: set[tuple[str, str]] = set()
    targets = [x for x in targets if not (x in seen or seen.add(x))]

    if not targets:
        print(f"Aucun export actif connu pour : {path}")
        return

    ok_any = False
    for target_client, target_path in targets:
        if unexport(target_client, target_path):
            print(f"OK : export désactivé à chaud : {target_client}:{target_path}")
            ok_any = True
        else:
            print(f"INFO : impossible ou déjà inactif : {target_client}:{target_path}")

    if ok_any:
        print("Note : si le partage est encore dans yoyo.exports, il reviendra avec -reload ou au démarrage.")


def reload_cmd() -> None:
    need_root()
    reload_exports()
    print("OK : exports NFS rechargés avec exportfs -ra")



def print_examples() -> None:
    print("EXEMPLES COPIER-COLLER")
    print("======================")
    print("# Installer les paquets NFS si besoin :")
    print("  apt update && apt install -y nfs-kernel-server rpcbind")
    print()
    print("# Voir les partages NFS gérés + actifs :")
    print("  python3 nfs.py -list")
    print()
    print("# Créer un partage NFS permanent lecture/écriture pour tout le LAN :")
    print("  sudo python3 nfs.py -install /mnt/user/Media:rw 192.168.1.0/24")
    print()
    print("# Créer un partage NFS permanent lecture seule pour tout le LAN :")
    print("  sudo python3 nfs.py -install /mnt/user/Backup:ro 192.168.1.0/24")
    print()
    print("# Créer un partage NFS seulement pour une machine précise :")
    print("  sudo python3 nfs.py -install /mnt/user/Media:rw 192.168.1.158")
    print()
    print("# Activer un export à chaud sans l'inscrire au démarrage :")
    print("  sudo python3 nfs.py -mount /mnt/user/Temp:rw 192.168.1.0/24")
    print()
    print("# Supprimer un partage permanent :")
    print("  sudo python3 nfs.py -remove /mnt/user/Media")
    print()
    print("# Désactiver un export à chaud :")
    print("  sudo python3 nfs.py -umount /mnt/user/Media")
    print()
    print("# Recharger les exports :")
    print("  sudo python3 nfs.py -reload")
    print()
    print("# Vérifier ce que le serveur publie :")
    print("  exportfs -v")
    print("  showmount -e localhost")
    print()
    print("RAPPEL")
    print("======")
    print("/chemin:rw        = lecture/écriture")
    print("/chemin:ro        = lecture seule")
    print("192.168.1.0/24    = tout le LAN 192.168.1.x")
    print("192.168.1.158     = une seule machine")
    print("Sans client précisé, le défaut du script est 192.168.1.0/24")
    print()

def usage() -> None:
    print()
    print("Usage :")
    print("  python3 nfs.py")
    print("  python3 nfs.py -list")
    print("  sudo python3 nfs.py -install /chemin:rw [client]")
    print("  sudo python3 nfs.py -install /chemin:ro [client]")
    print("  sudo python3 nfs.py -mount   /chemin:rw [client]")
    print("  sudo python3 nfs.py -remove  /chemin")
    print("  sudo python3 nfs.py -umount  /chemin")
    print("  sudo python3 nfs.py -reload")
    print()
    print_examples()


def main(argv: list[str]) -> int:
    if not argv:
        list_nfs()
        return 0

    action = argv[0].lower()

    if action in {"-h", "--help", "help"}:
        usage()
        return 0

    if action in {"-list", "--list", "-liste", "--liste", "-l", "list", "liste"}:
        list_nfs()
        return 0

    if action in {"-install", "--install", "install"}:
        if len(argv) not in {2, 3}:
            usage()
            return 1
        install_share(argv[1], argv[2] if len(argv) == 3 else None)
        return 0

    if action in {"-mount", "--mount", "mount"}:
        if len(argv) not in {2, 3}:
            usage()
            return 1
        mount_share(argv[1], argv[2] if len(argv) == 3 else None)
        return 0

    if action in {"-remove", "--remove", "-delete", "--delete", "remove", "delete"}:
        if len(argv) != 2:
            usage()
            return 1
        remove_share(argv[1])
        return 0

    if action in {"-unmount", "--unmount", "-umount", "--umount", "unmount", "umount"}:
        if len(argv) not in {2, 3}:
            usage()
            return 1
        unmount_share(argv[1], argv[2] if len(argv) == 3 else None)
        return 0

    if action in {"-reload", "--reload", "-apply", "--apply", "reload", "apply"}:
        reload_cmd()
        return 0

    print(f"ERREUR : action inconnue : {action}")
    usage()
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
