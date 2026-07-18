def escape_exports_path(path: str) -> str:
    return path.replace(" ", "\\040")


def unescape_exports_path(path: str) -> str:
    return path.replace("\\040", " ")


def normalize_path(path: str) -> str:
    path = (path or "").strip()
    if not path.startswith("/"):
        raise ValueError(f"chemin non absolu : {path}")
    return os.path.normpath(path)


def validate_nfs_client(client: str) -> str:
    client = (client or nfs_default_client()).strip()
    if not client or re.search(r"\s", client):
        raise ValueError(f"client NFS invalide : {client}")
    return client


def ensure_safe_nfs_path(path: str, *, create: bool = False) -> None:
    real = os.path.realpath(path) if os.path.exists(path) else os.path.abspath(path)
    if path in FORBIDDEN_SHARE_PATHS or real in FORBIDDEN_SHARE_PATHS:
        raise ValueError(f"chemin NFS interdit : {path}")
    if create:
        Path(path).mkdir(parents=True, exist_ok=True)
    elif not Path(path).exists():
        raise ValueError(f"chemin introuvable : {path}")
    if not Path(path).is_dir():
        raise ValueError(f"ce n'est pas un dossier : {path}")


def normalize_nfs_options(value: str | None) -> str:
    """Nettoie les options NFS avancées saisies dans l'UI.

    On enlève rw/ro parce que le droit principal est piloté par la liste déroulante.
    Le reste est laissé libre pour les cas spéciaux : sync, async, subtree_check,
    no_root_squash, root_squash, fsid=..., insecure, anonuid=..., anongid=...
    """
    if value is None:
        return ""
    value = str(value).strip().replace(" ", "")
    if not value:
        return ""
    out: list[str] = []
    seen: set[str] = set()
    for part in value.split(","):
        part = part.strip()
        if not part or part in {"rw", "ro"}:
            continue
        if part in seen:
            continue
        seen.add(part)
        out.append(part)
    return ",".join(out)


def parse_exports_line(line: str) -> NfsEntry | None:
    clean = line.strip()
    if not clean or clean.startswith("#"):
        return None
    parts = clean.split(None, 1)
    if len(parts) != 2:
        return None
    path = unescape_exports_path(parts[0])
    rest = parts[1].strip()
    match = re.match(r"^([^\s(]+)\(([^)]*)\)$", rest)
    if not match:
        return None
    client = match.group(1)
    options = [x.strip() for x in match.group(2).split(",") if x.strip()]
    access = "rw" if "rw" in options else "ro" if "ro" in options else "rw"
    advanced_options = normalize_nfs_options(",".join(x for x in options if x not in {"rw", "ro"}))
    return NfsEntry(path=path, client=client, access=access, advanced_options=advanced_options)


def read_nfs_entries_from_file(path: Path) -> list[NfsEntry]:
    if not path.exists():
        return []
    entries: list[NfsEntry] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        entry = parse_exports_line(line)
        if entry:
            entries.append(entry)
    return entries


def active_export_to_nfs_entry(path: str, client: str, options: str) -> NfsEntry:
    opts = [x.strip() for x in str(options or "").split(",") if x.strip()]
    access = "rw" if "rw" in opts else "ro" if "ro" in opts else "rw"
    advanced_options = normalize_nfs_options(",".join(x for x in opts if x not in {"rw", "ro"}))
    return NfsEntry(path=path, client=client, access=access, advanced_options=advanced_options)


def render_nfs_config(entries: list[NfsEntry]) -> str:
    lines = [
        "# ============================================================",
        "# nfs_server.conf - exports NFS sauvegardés par Yoleo",
        "# Source portable du tableau Exports NFS permanents.",
        "# Le reload génère ensuite /etc/exports.d/nfs.exports.",
        "# ============================================================",
        "",
    ]
    for entry in entries:
        lines.append(entry.to_line())
    return "\n".join(lines).rstrip() + "\n"


def render_nfs_exports(entries: list[NfsEntry]) -> str:
    lines = [
        "# ============================================================",
        "# nfs.exports - généré par partage.py depuis nfs_server.conf",
        "# Ne pas mélanger avec /etc/exports principal.",
        "# ============================================================",
        "",
    ]
    for entry in entries:
        lines.append(entry.to_line())
    return "\n".join(lines).rstrip() + "\n"


def ensure_nfs_server_conf_seeded() -> None:
    conf_file = get_nfs_server_conf_file()
    if conf_file.exists():
        return

    entries = read_nfs_entries_from_file(get_nfs_exports_file())
    if not entries:
        entries = [
            active_export_to_nfs_entry(path, client, options)
            for path, client, options in active_exports()
        ]

    conf_file.parent.mkdir(parents=True, exist_ok=True)
    conf_file.write_text(render_nfs_config(entries), encoding="utf-8")
    try:
        conf_file.chmod(0o644)
    except OSError:
        pass


def read_nfs_entries(exports_file: Path | None = None) -> list[NfsEntry]:
    if exports_file is None:
        ensure_nfs_server_conf_seeded()
        exports_file = get_nfs_server_conf_file()
    return read_nfs_entries_from_file(exports_file)


def write_nfs_entries(entries: list[NfsEntry], exports_file: Path | None = None) -> tuple[bool, str]:
    exports_file = exports_file or get_nfs_server_conf_file()
    content = render_nfs_config(entries)
    return write_if_changed(exports_file, content, mode=0o644)


def write_nfs_linux_exports(entries: list[NfsEntry]) -> tuple[bool, str]:
    exports_file = get_nfs_exports_file()
    content = render_nfs_exports(entries)
    return write_if_changed(exports_file, content, mode=0o644)


def sync_active_exports_to_nfs_conf() -> tuple[bool, str]:
    current = read_nfs_entries()
    seen = {(entry.path, entry.client) for entry in current}
    linux_file_seen = {
        (entry.path, entry.client)
        for entry in read_nfs_entries_from_file(get_nfs_exports_file())
    }
    added: list[NfsEntry] = []
    for path, client, options in active_exports():
        key = (path, client)
        if key in seen:
            continue
        # Si l'export est encore dans notre fichier Linux cible mais plus dans
        # le conf portable, on considère que l'utilisateur vient de le retirer
        # du tableau haut et qu'il n'a pas encore rechargé exportfs.
        if key in linux_file_seen:
            continue
        seen.add(key)
        added.append(active_export_to_nfs_entry(path, client, options))
    if not added:
        return False, "Aucun export actif absent de nfs_server.conf."
    changed, msg = write_nfs_entries([*current, *added])
    return changed, f"{len(added)} export(s) actif(s) importé(s) dans nfs_server.conf.\n{msg}"


def sync_active_nfs_stream() -> Iterator[str]:
    try:
        changed, msg = sync_active_exports_to_nfs_conf()
        yield msg + "\n"
        if changed:
            yield "OK : le tableau permanent reprend maintenant ces exports actifs.\n"
    except Exception as exc:
        yield f"ERREUR import exports actifs : {exc}\n"


def nfs_entries_from_payload(payload: dict) -> list[NfsEntry]:
    entries: list[NfsEntry] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for raw in payload.get("entries") or []:
        path_raw = (raw.get("path") or "").strip()
        client_raw = (raw.get("client") or nfs_default_client()).strip()
        access_raw = (raw.get("access") or "rw").strip().lower()
        if "advanced_options" in raw:
            advanced_options_raw = raw.get("advanced_options") or ""
        elif "options" in raw:
            advanced_options_raw = raw.get("options") or ""
        else:
            advanced_options_raw = None
        if not path_raw and not client_raw:
            continue
        try:
            path = normalize_path(path_raw)
            client = validate_nfs_client(client_raw)
            access = access_raw if access_raw in {"rw", "ro"} else "rw"
            advanced_options = None if advanced_options_raw is None else normalize_nfs_options(advanced_options_raw)
            key = (path, client)
            if key in seen:
                continue
            seen.add(key)
            # À l'enregistrement on valide seulement que le chemin est sûr.
            # Le dossier peut être créé automatiquement au moment du reload, comme dans l'ancien nfs.py.
            real = os.path.realpath(path) if os.path.exists(path) else os.path.abspath(path)
            if path in FORBIDDEN_SHARE_PATHS or real in FORBIDDEN_SHARE_PATHS:
                raise ValueError(f"chemin NFS interdit : {path}")
            entries.append(NfsEntry(path=path, client=client, access=access, advanced_options=advanced_options))
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError("\n".join(errors))
    return entries


def active_exports() -> list[tuple[str, str, str]]:
    if not command_exists("exportfs"):
        return []
    res = run_capture(["exportfs", "-v"])
    if res.returncode != 0:
        return []
    result: list[tuple[str, str, str]] = []
    current_path = ""
    for raw in res.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("/"):
            parts = line.split(None, 1)
            current_path = unescape_exports_path(parts[0])
            if len(parts) == 2:
                m = re.match(r"^([^\s(]+)\((.*)\)$", parts[1])
                if m:
                    result.append((current_path, m.group(1), m.group(2)))
        elif current_path:
            m = re.match(r"^([^\s(]+)\((.*)\)$", line)
            if m:
                result.append((current_path, m.group(1), m.group(2)))
    return result


def ensure_nfs_service_stream() -> Iterator[str]:
    if not command_exists("systemctl"):
        yield "systemctl introuvable, impossible de piloter le service NFS.\n"
        return
    for service in NFS_SERVICES:
        if service_state(service)["exists"]:
            for args in (["enable", "--now", service], ["restart", service]):
                rc, out = systemctl_cmd(args)
                yield "$ systemctl " + " ".join(args) + "\n" + out
            return
    yield "Aucun service nfs-server/nfs-kernel-server détecté. Installe : apt install -y nfs-kernel-server rpcbind\n"


def reload_nfs_stream() -> Iterator[str]:
    root_error = require_root_text()
    if root_error:
        yield root_error + "\n"
        return
    if not command_exists("exportfs"):
        yield "ERREUR : exportfs introuvable. Installe : apt install -y nfs-kernel-server rpcbind\n"
        return
    yield from ensure_nfs_service_stream()
    entries = read_nfs_entries()
    for entry in entries:
        try:
            ensure_safe_nfs_path(entry.path, create=True)
            yield f"OK dossier NFS : {entry.path}\n"
        except Exception as exc:
            yield f"ERREUR dossier NFS {entry.path} : {exc}\n"
            return

    # Le fichier Linux est une cible générée depuis la conf portable Yoleo.
    # Même avec 0 entrée, on le réécrit pour que le reload retire les anciens exports.
    try:
        changed, msg = write_nfs_linux_exports(entries)
        if changed:
            yield f"OK : exports Linux générés depuis nfs_server.conf ({msg}).\n"
        else:
            yield "OK : exports Linux déjà synchronisés avec nfs_server.conf.\n"
    except Exception as exc:
        yield f"ERREUR : impossible de générer les exports Linux : {exc}\n"
        return

    res = run_capture(["exportfs", "-ra"])
    yield "$ exportfs -ra\n" + res.stdout
    if res.returncode == 0:
        yield "OK : exports NFS rechargés.\n"
    else:
        yield f"ERREUR : exportfs -ra a échoué code {res.returncode}\n"


def stop_nfs_stream() -> Iterator[str]:
    root_error = require_root_text()
    if root_error:
        yield root_error + "\n"
        return
    for service in NFS_SERVICES:
        if service_state(service)["exists"]:
            rc, out = systemctl_cmd(["stop", service])
            yield "$ systemctl stop " + service + "\n" + out


def save_raw_file(path: Path, content: str, *, mode: int = 0o644) -> tuple[bool, str]:
    backup = backup_file(path, "raw")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    try:
        path.chmod(mode)
    except OSError:
        pass
    return True, f"Fichier écrit : {path}" + (f"\nBackup : {backup}" if backup else "")


def to_samba_dict(cfg: SambaConfig) -> dict:
    return {
        "conf_path": str(cfg.conf_path),
        "global": {
            "workgroup": cfg.workgroup,
            "server_string": cfg.server_string,
            "netbios_name": cfg.netbios_name,
            "interface": cfg.interface,
            "smb_conf": str(cfg.smb_conf),
            "log_file": cfg.log_file,
            "max_log_size": cfg.max_log_size,
            "min_protocol": cfg.min_protocol,
            "enable_wsdd": cfg.enable_wsdd,
            "wsdd_name": cfg.wsdd_name,
            "create_missing_dirs": cfg.create_missing_dirs,
        },
        "users": [
            {
                "name": u.name,
                "password": u.password,
                "uid": u.uid,
                "gid": u.gid,
                "shell": u.shell,
                "home": u.home,
                "exists": user_exists(u.name),
            } for u in cfg.users
        ],
        "shares": [
            {
                "name": s.name,
                "path": str(s.path),
                "type": s.share_type,
                "guest_ok": s.guest_ok,
                "read_only": s.read_only,
                "browsable": s.browsable,
                "writable": s.writable,
                "recycle_bin": s.recycle_bin,
                "owner": s.owner,
                "access_mode": s.access_mode,
                "read_users": s.read_users,
                "write_users": s.write_users,
                "exists": s.path.exists(),
            } for s in cfg.shares
        ],
    }


def nfs_entries_view(entries: list[NfsEntry]) -> list[dict]:
    active = {(p, c) for p, c, _opts in active_exports()}
    return [
        {
            "path": e.path,
            "client": e.client,
            "access": e.access,
            "advanced_options": e.options_extra,
            "options": e.options,
            "active": (e.path, e.client) in active,
            "exists": Path(e.path).is_dir(),
        }
        for e in entries
    ]



def partage_settings_rows() -> list[dict]:
    conf = read_partage_config()
    # Les clés standards sont générées et conservées automatiquement par partage.py.
    # L'interface n'affiche ici que d'éventuels réglages personnalisés ajoutés à la main.
    out: list[dict] = []
    for key in sorted(k for k in conf.keys() if k not in HIDDEN_PARTAGE_SETTING_KEYS):
        out.append({"key": key, "label": key, "value": conf.get(key, ""), "placeholder": ""})
    return out


def render_partage_config(settings: dict[str, str]) -> str:
    current = read_partage_config()
    merged = DEFAULT_PARTAGE_CONFIG.copy()
    merged.update(current)
    for key, value in (settings or {}).items():
        key = str(key).strip()
        if not key:
            continue
        merged[key] = strip_conf_quotes(str(value or ""))

    order = [
        "SAMBA_CONF",
        "NFS_SERVER_CONF",
        "NFS_EXPORTS_FILE",
        "NFS_DEFAULT_CLIENT",
        "NFS_DEFAULT_OPTIONS",
        "LOG_LINES",
        "BROWSE_START",
        "SAV_SAMBA_BACKUP",
        "SAV_NFS_BACKUP",
        "SAV_PARTAGE_BACKUP",
        "restart_scripts",
    ]
    extra = sorted(k for k in merged.keys() if k not in order)

    lines = [
        "# partage.conf - configuration du module Flask partage.py",
        "# Les chemins relatifs sont résolus depuis le dossier où se trouve partage.py.",
        "# Exemple : si partage.py est dans /dockers/system,",
        "# SAMBA_CONF=../conf/samba.conf pointe vers /dockers/conf/samba.conf.",
        "",
        "# Fichier source Samba édité par l'interface.",
        f"SAMBA_CONF={merged.get('SAMBA_CONF', nas_conf_file('samba.conf'))}",
        "",
        "# Fichier NFS serveur portable edite par l'interface.",
        f"NFS_SERVER_CONF={merged.get('NFS_SERVER_CONF', '../conf/nfs_server.conf')}",
        "",
        "# Fichier Linux genere au reload exportfs.",
        f"NFS_EXPORTS_FILE={merged.get('NFS_EXPORTS_FILE', '/etc/exports.d/nfs.exports')}",
        "",
        "# Valeurs par défaut pour les nouveaux exports NFS.",
        f"NFS_DEFAULT_CLIENT={merged.get('NFS_DEFAULT_CLIENT', '192.168.1.0/24')}",
        f"NFS_DEFAULT_OPTIONS={merged.get('NFS_DEFAULT_OPTIONS', 'sync,no_subtree_check,no_root_squash')}",
        "",
        "# Nombre de lignes par défaut dans l'onglet logs.",
        f"LOG_LINES={merged.get('LOG_LINES', '300')}",
        "",
        "# Dossier de départ du bouton Parcourir.",
        f"BROWSE_START={merged.get('BROWSE_START', '/')}",
        "",
        "# Dossiers des sauvegardes automatiques.",
        "# Objectif : ne pas polluer le dossier conf avec les .bak.",
        f"SAV_SAMBA_BACKUP={merged.get('SAV_SAMBA_BACKUP', '../backups')}",
        f"SAV_NFS_BACKUP={merged.get('SAV_NFS_BACKUP', '../backups')}",
        f"SAV_PARTAGE_BACKUP={merged.get('SAV_PARTAGE_BACKUP', '../backups')}",
        "",
        "# Commande lancée automatiquement après Enregistrer partage.conf.",
        "# Chemin relatif = même dossier que app.py / partage.py / system.sh.",
        f"restart_scripts={merged.get('restart_scripts', merged.get('RESTART_SCRIPTS', 'system.sh -restart'))}",
    ]
    if extra:
        lines.extend(["", "# Réglages supplémentaires conservés."])
        for key in extra:
            lines.append(f"{key}={merged.get(key, '')}")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# NFS client : montage de partages NFS distants depuis le module Partage
# ---------------------------------------------------------------------------
# Import propre du petit Flask backup : on garde uniquement la logique de
# découverte/montage des exports réseau, adaptée au NAS host et au dossier
# de configuration Yoleo. Le fichier de configuration reste volontairement
# simple et portable : ../conf/nfs_client.conf.

