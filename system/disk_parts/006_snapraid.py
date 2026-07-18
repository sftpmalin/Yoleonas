#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SnapRAID helper routes for the Disk module.

Principe volontairement simple :
- l'onglet SnapRAID choisit uniquement le disque de parité ;
- les disques de données sont les autres disques montés non-système ;
- la planification passe par le gestionnaire de tâches existant.
"""
from __future__ import annotations

from urllib.parse import urlencode


def snapraid_bin(conf: Dict[str, str]) -> str:
    return str(conf.get("SNAPRAID_BIN") or "snapraid").strip() or "snapraid"


def snapraid_config_file(conf: Dict[str, str]) -> str:
    return disk_conf_resolve_path(str(conf.get("SNAPRAID_CONFIG_FILE") or "/etc/snapraid.conf"))


def snapraid_state_file(conf: Dict[str, str]) -> str:
    return disk_conf_resolve_path(str(conf.get("SNAPRAID_STATE_FILE") or "/var/lib/yoleo/disk/snapraid.json"))


def snapraid_load_state(conf: Dict[str, str]) -> Dict[str, Any]:
    path = snapraid_state_file(conf)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def snapraid_save_state(conf: Dict[str, str], state: Dict[str, Any]) -> None:
    path = snapraid_state_file(conf)
    parent = os.path.dirname(path.rstrip("/")) or "."
    os.makedirs(parent, exist_ok=True)
    payload = dict(state or {})
    payload["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass


def snapraid_block_size_bytes(device: str) -> int:
    name = os.path.basename(str(device or "").strip())
    if not name:
        return 0
    try:
        return int(sysfs_block_size_bytes(name))
    except Exception:
        return 0


def snapraid_safe_disk_name(device: str, mountpoint: str, used: set) -> str:
    base = os.path.basename(str(mountpoint or "").rstrip("/")) or os.path.basename(str(device or "")) or "disk"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", base).strip("._-") or "disk"
    if safe.lower() in {"parity", "content", "data", "snapraid"}:
        safe = f"data_{safe}"
    candidate = safe
    index = 2
    while candidate in used:
        candidate = f"{safe}_{index}"
        index += 1
    used.add(candidate)
    return candidate


def snapraid_data_disks() -> List[Dict[str, Any]]:
    """Disques montés non-système utilisés comme disques de données SnapRAID."""
    inventory = augment_maintenance_data(collect_disks())
    out: List[Dict[str, Any]] = []
    for row in inventory.get("disks", []) or []:
        device = str(row.get("device") or "").strip()
        mountpoint = str(row.get("mountpoint") or "").strip()
        if not device or not mountpoint:
            continue
        if row.get("maintenance_protected") or row.get("is_system_disk"):
            continue
        if mountpoint == "/" or mountpoint.startswith("/boot"):
            continue
        if not SAFE_DISK_RE.match(device):
            continue
        size_bytes = snapraid_block_size_bytes(device)
        out.append({
            "device": device,
            "display_device": row.get("display_device") or device,
            "mountpoint": mountpoint,
            "mounted": True,
            "model": row.get("model") or "—",
            "serial": row.get("serial") or "",
            "media": row.get("media") or "",
            "transport": row.get("transport") or "",
            "size": row.get("size") or human_bytes(size_bytes),
            "size_bytes": size_bytes,
            "status_label": row.get("status_label") or "",
            "power_state": row.get("power_state") or "",
        })
    out.sort(key=lambda item: (str(item.get("mountpoint") or ""), str(item.get("device") or "")))
    return out


def snapraid_candidate_disks() -> List[Dict[str, Any]]:
    """Disques candidats à la parité SnapRAID.

    Cohérence avec la page RAID : on ne propose ici que des disques physiques
    vraiment vides, non montés, non partitionnés, non formatés, sans signature
    blkid/wipefs, non système et non protégés. Les disques déjà montés servent
    de disques de données ; ils ne doivent pas être proposés comme parité à
    écraser depuis cette page.
    """
    out: List[Dict[str, Any]] = []
    for row in raid_empty_disk_candidates():
        device = str(row.get("device") or "").strip()
        if not device or not SAFE_DISK_RE.match(device):
            continue
        size_bytes = snapraid_block_size_bytes(device)
        out.append({
            "device": device,
            "display_device": row.get("display_device") or device,
            "mountpoint": "",
            "mounted": False,
            "model": row.get("model") or "—",
            "serial": row.get("serial") or "",
            "media": row.get("media") or "",
            "transport": row.get("transport") or "",
            "size": row.get("size") or human_bytes(size_bytes),
            "size_bytes": size_bytes,
            "status_label": row.get("status_label") or "vide",
            "power_state": row.get("power_state") or "",
        })
    out.sort(key=lambda item: (-int(item.get("size_bytes") or 0), str(item.get("device") or "")))
    return out


def snapraid_mounted_disks() -> List[Dict[str, Any]]:
    return snapraid_data_disks()


def snapraid_annotate_candidates(disks: List[Dict[str, Any]], data_disks: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    annotated: List[Dict[str, Any]] = []
    data_list = data_disks if data_disks is not None else snapraid_data_disks()
    data_sizes = [int(other.get("size_bytes") or 0) for other in data_list]
    max_data = max(data_sizes) if data_sizes else 0

    for disk in disks:
        size_bytes = int(disk.get("size_bytes") or 0)
        eligible = bool(data_sizes and max_data > 0 and size_bytes >= max_data)
        if eligible:
            note = "OK"
        elif not data_sizes:
            note = "aucun disque de données"
        else:
            note = "trop petit"

        item = dict(disk)
        item["eligible_parity"] = bool(eligible)
        item["parity_note"] = note
        item["max_data_size"] = human_bytes(max_data) if max_data else "—"
        annotated.append(item)
    return annotated


def snapraid_find_disk(disks: List[Dict[str, Any]], device: str) -> Optional[Dict[str, Any]]:
    wanted = str(device or "").strip()
    for disk in disks:
        if str(disk.get("device") or "") == wanted:
            return disk
    return None


def snapraid_enrich_data_disk_names(data_disks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ajoute le nom logique SnapRAID utilisé par la directive disk.

    Ce nom est indispensable pour préparer une récupération ciblée :
    snapraid -d <nom_logique> fix
    """
    used: set = set()
    enriched: List[Dict[str, Any]] = []
    for disk in data_disks or []:
        item = dict(disk or {})
        current = str(item.get("snapraid_name") or "").strip()
        if current and current not in used:
            name = current
            used.add(name)
        else:
            name = snapraid_safe_disk_name(str(item.get("device") or ""), str(item.get("mountpoint") or ""), used)
        item["snapraid_name"] = name
        enriched.append(item)
    return enriched


def snapraid_enrich_state_for_ui(state: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(state or {})
    if isinstance(out.get("data_disks"), list):
        out["data_disks"] = snapraid_enrich_data_disk_names(out.get("data_disks") or [])
    return out


def snapraid_build_config(conf: Dict[str, str], parity_device: str) -> Tuple[bool, str, str, Dict[str, Any]]:
    data_all = snapraid_data_disks()
    parity = snapraid_find_disk(data_all, parity_device)

    # Cas 1 : l'admin choisit un disque vierge depuis le tableau SnapRAID.
    # On le mémorise seulement. SnapRAID ne peut pas encore écrire sa parité
    # sur un disque brut : il faudra d'abord le formater/monter dans Maintenance.
    if not parity:
        candidates = snapraid_annotate_candidates(snapraid_candidate_disks(), data_all)
        raw_parity = snapraid_find_disk(candidates, parity_device)
        if not raw_parity:
            return False, "Disque de parité introuvable ou refusé : il doit être vide, non monté, non partitionné, non formaté et sans signature.", "", {}
        if not raw_parity.get("eligible_parity"):
            return False, "Le disque de parité doit être au moins aussi grand que le plus grand disque de données monté.", "", {}
        state = {
            "enabled": False,
            "pending": True,
            "parity_device": raw_parity.get("device"),
            "parity_mountpoint": "",
            "parity_file": "",
            "data_disks": data_all,
            "config_file": snapraid_config_file(conf),
            "content_files": [],
        }
        return True, "Disque de parité sélectionné. Formate/monte-le dans Maintenance, puis reviens valider SnapRAID pour écrire la configuration.", "", state

    # Cas 2 : le disque de parité précédemment choisi est maintenant monté.
    # On peut écrire la vraie configuration SnapRAID.
    data_disks = [disk for disk in data_all if disk.get("device") != parity.get("device")]
    if not data_disks:
        return False, "Aucun disque de données disponible en dehors du disque de parité.", "", {}
    data_disks = snapraid_enrich_data_disk_names(data_disks)

    max_data = max(int(disk.get("size_bytes") or 0) for disk in data_disks)
    if int(parity.get("size_bytes") or 0) < max_data:
        return False, "Le disque de parité doit être au moins aussi grand que chaque disque de données.", "", {}

    parity_mount = str(parity.get("mountpoint") or "").rstrip("/")
    parity_file = f"{parity_mount}/snapraid.parity"
    content_files = [f"{parity_mount}/snapraid.content"]
    for disk in data_disks:
        data_mount = str(disk.get("mountpoint") or "").rstrip("/")
        if data_mount:
            content_files.append(f"{data_mount}/.snapraid.content")

    lines: List[str] = [
        "# snapraid.conf généré par Yoleo - module Stockage / SnapRAID",
        f"# Généré le {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "# Le disque de parité doit être au moins aussi grand que chaque disque de données.",
        "",
        f"parity {parity_file}",
    ]
    for content in content_files:
        lines.append(f"content {content}")
    lines.append("")
    for disk in data_disks:
        name = str(disk.get("snapraid_name") or "").strip() or snapraid_safe_disk_name(str(disk.get("device") or ""), str(disk.get("mountpoint") or ""), set())
        lines.append(f"disk {name} {str(disk.get('mountpoint') or '').rstrip('/')}")
    lines.extend([
        "",
        "exclude *.unrecoverable",
        "exclude /tmp/",
        "exclude /lost+found/",
        "exclude /.snapraid.content",
        "exclude .snapraid.content",
        "exclude /snapraid.content",
        "",
    ])

    state = {
        "enabled": True,
        "pending": False,
        "parity_device": parity.get("device"),
        "parity_mountpoint": parity.get("mountpoint"),
        "parity_file": parity_file,
        "data_disks": data_disks,
        "config_file": snapraid_config_file(conf),
        "content_files": content_files,
    }
    return True, "Configuration SnapRAID prête.", "\n".join(lines), state


def snapraid_write_config(conf: Dict[str, str], config_text: str) -> Tuple[bool, str]:
    path = snapraid_config_file(conf)
    parent = os.path.dirname(path.rstrip("/")) or "."
    try:
        os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(config_text.rstrip() + "\n")
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
        return True, path
    except Exception as exc:
        return False, f"Écriture impossible {path}: {exc}"


def snapraid_command(conf: Dict[str, str], action: str = "sync", disk_name: str = "") -> str:
    parts = [snapraid_bin(conf), "-c", snapraid_config_file(conf)]
    clean_disk = str(disk_name or "").strip()
    if clean_disk:
        parts.extend(["-d", clean_disk])
    parts.append(str(action or "sync"))
    return " ".join(shlex.quote(str(part)) for part in parts)


def snapraid_task_url(conf: Dict[str, str], schedule: bool = False, action: str = "sync", disk_name: str = "", label: str = "") -> str:
    clean_action = str(action or "sync").strip().lower()
    clean_disk = str(disk_name or "").strip()
    if clean_action == "fix":
        title = "SnapRAID - récupération" + (f" {label or clean_disk}" if (label or clean_disk) else "")
        description = (
            "Récupération SnapRAID générée depuis Stockage > SnapRAID. "
            "La tâche lance snapraid fix ; vérifier les disques/montages avant exécution."
        )
        schedule_type = "manual"
        enabled = "0"
    elif clean_action == "check":
        title = "SnapRAID - vérification"
        description = "Vérification SnapRAID générée depuis Stockage > SnapRAID."
        schedule_type = "manual"
        enabled = "0"
    else:
        title = "SnapRAID - synchronisation programmée" if schedule else "SnapRAID - synchronisation manuelle"
        description = (
            "Synchronisation SnapRAID générée depuis Stockage > SnapRAID. "
            "La commande reste dans le gestionnaire de tâches pour éviter de réinventer la planification dans chaque module."
        )
        schedule_type = "daily" if schedule else "manual"
        enabled = "1" if schedule else "0"
    params = {
        "title": title,
        "description": description,
        "command_1": snapraid_command(conf, clean_action, clean_disk),
        "chain_mode": "and",
        "notify_success": "1",
        "enabled": enabled,
        "schedule_type": schedule_type,
        "time_hour": "3",
        "time_minute": "0",
    }
    return "/system/task/create?" + urlencode(params)


def snapraid_runtime_status(conf: Dict[str, str], state: Dict[str, Any]) -> Dict[str, Any]:
    config_file = snapraid_config_file(conf)
    binary = snapraid_bin(conf)
    binary_path = shutil.which(binary) if not os.path.isabs(binary) else (binary if os.path.exists(binary) else "")
    data_disks = state.get("data_disks") if isinstance(state.get("data_disks"), list) else []
    missing_mounts: List[str] = []
    for item in data_disks:
        mp = str((item or {}).get("mountpoint") or "")
        if mp and not os.path.ismount(mp):
            missing_mounts.append(mp)
    parity_mount = str(state.get("parity_mountpoint") or "")
    if parity_mount and not os.path.ismount(parity_mount):
        missing_mounts.append(parity_mount)
    pending = bool(state.get("pending") and state.get("parity_device") and not state.get("enabled"))
    configured = bool(state.get("enabled") and state.get("parity_device") and os.path.exists(config_file))
    active = configured and not missing_mounts
    if pending and not configured:
        label = "parité à préparer"
        badge = "warn"
    elif not configured:
        label = "non configuré"
        badge = "gray"
    elif missing_mounts:
        label = "montage manquant"
        badge = "bad"
    elif not binary_path:
        label = "snapraid absent"
        badge = "warn"
    else:
        label = "prêt"
        badge = "ok"
    return {
        "configured": configured,
        "pending": pending,
        "active": active,
        "label": label,
        "badge": badge,
        "binary": binary,
        "binary_path": binary_path,
        "config_file": config_file,
        "state_file": snapraid_state_file(conf),
        "missing_mounts": missing_mounts,
        "task_ready": active and bool(binary_path),
        "task_block_reason": (
            "Monte les points SnapRAID manquants avant de créer une tâche."
            if missing_mounts else
            ("Installe snapraid avant de créer une tâche." if configured and not binary_path else "")
        ),
    }


@disk_bp.route("/disk/api/snapraid")
def disk_snapraid_api():
    conf = get_config()
    data_disks = snapraid_data_disks()
    annotated = snapraid_annotate_candidates(snapraid_candidate_disks(), data_disks)
    disks = [disk for disk in annotated if disk.get("eligible_parity")]
    state = snapraid_enrich_state_for_ui(snapraid_load_state(conf))
    status = snapraid_runtime_status(conf, state)
    max_data_size = max([int(disk.get("size_bytes") or 0) for disk in data_disks] or [0])
    recovery_urls: Dict[str, str] = {}
    for disk in state.get("data_disks") or []:
        name = str((disk or {}).get("snapraid_name") or "").strip()
        if name:
            recovery_urls[name] = snapraid_task_url(
                conf,
                action="fix",
                disk_name=name,
                label=str((disk or {}).get("mountpoint") or (disk or {}).get("device") or name),
            )
    return jsonify({
        "ok": True,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "disks": disks,
        "data_disks": data_disks,
        "data_disk_count": len(data_disks),
        "max_data_size": human_bytes(max_data_size) if max_data_size else "—",
        "state": state,
        "status": status,
        "manual_task_url": snapraid_task_url(conf, schedule=False),
        "schedule_task_url": snapraid_task_url(conf, schedule=True),
        "check_task_url": snapraid_task_url(conf, action="check"),
        "fix_task_url": snapraid_task_url(conf, action="fix"),
        "recovery_task_urls": recovery_urls,
        "sync_command": snapraid_command(conf, "sync"),
    })


@disk_bp.route("/disk/api/action/snapraid/configure", methods=["POST"])
def disk_snapraid_configure_api():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    current_state = snapraid_load_state(conf)
    parity_device = str(payload.get("parity_device") or current_state.get("parity_device") or "").strip()
    ok, msg, config_text, state = snapraid_build_config(conf, parity_device)
    if not ok:
        return json_response(False, msg)
    if not config_text:
        state["parity_selected_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        snapraid_save_state(conf, state)
        return json_response(True, msg, state=state, output="")
    ok_write, write_msg = snapraid_write_config(conf, config_text)
    if not ok_write:
        return json_response(False, write_msg)
    state["config_written_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    snapraid_save_state(conf, state)
    return json_response(True, f"Configuration SnapRAID écrite : {write_msg}", state=state, output=config_text[-5000:])


@disk_bp.route("/disk/api/action/snapraid/clear", methods=["POST"])
def disk_snapraid_clear_api():
    conf = get_config()
    state_path = snapraid_state_file(conf)
    try:
        if os.path.exists(state_path):
            os.unlink(state_path)
    except Exception as exc:
        return json_response(False, f"Suppression état SnapRAID impossible : {exc}")
    return json_response(True, "État SnapRAID supprimé. Le fichier /etc/snapraid.conf n'est pas effacé automatiquement.")
