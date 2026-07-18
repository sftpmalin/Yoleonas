# -*- coding: utf-8 -*-
"""RAMDrive / tmpfs - page Stockage.

Module volontairement simple : créer un tmpfs, le monter à chaud, et
éventuellement écrire la ligne correspondante dans /etc/fstab.
"""
from __future__ import annotations


RAMDRIVE_MARKER = "x-yoleo.ramdrive=1"


def ramdrive_base_path(conf):
    raw = str(conf.get("RAMDRIVE_BASE_PATH") or "/mnt/ramdrive").strip() or "/mnt/ramdrive"
    return os.path.normpath(os.path.expanduser(os.path.expandvars(raw)))


def ramdrive_default_options(conf):
    raw = str(conf.get("RAMDRIVE_DEFAULT_OPTIONS") or "mode=0777,nosuid,nodev,noatime").strip()
    return raw or "mode=0777,nosuid,nodev,noatime"


def ramdrive_safe_name(value):
    value = str(value or "").strip()
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    value = value.strip("._-")
    return value[:48] or "ram1"


def ramdrive_size_limits(conf):
    try:
        min_gb = float(str(conf.get("RAMDRIVE_MIN_GB") or "0.1").replace(",", "."))
    except Exception:
        min_gb = 0.1
    try:
        max_gb = float(str(conf.get("RAMDRIVE_MAX_GB") or "256").replace(",", "."))
    except Exception:
        max_gb = 256.0
    min_gb = max(0.01, min_gb)
    max_gb = max(min_gb, max_gb)
    return min_gb, max_gb


def ramdrive_default_mountpoint(conf, name="ram1"):
    return os.path.join(ramdrive_base_path(conf), ramdrive_safe_name(name))


def ramdrive_parse_size_gb(value, conf):
    raw = str(value or "").strip().replace(",", ".")
    if not raw:
        return False, 0.0, "Taille obligatoire."
    try:
        size = float(raw)
    except Exception:
        return False, 0.0, "Taille invalide."
    min_gb, max_gb = ramdrive_size_limits(conf)
    if size < min_gb:
        return False, 0.0, f"Taille trop petite. Minimum : {min_gb:g} Go."
    if size > max_gb:
        return False, 0.0, f"Taille trop grande. Maximum : {max_gb:g} Go."
    return True, size, ""


def ramdrive_size_option(size_gb):
    if abs(size_gb - round(size_gb)) < 0.0001:
        return f"size={int(round(size_gb))}G"
    return f"size={size_gb:g}G"


def ramdrive_options_split(options):
    return [p.strip() for p in str(options or "").split(",") if p.strip()]


def ramdrive_options_without_size_marker(options):
    out = []
    for opt in ramdrive_options_split(options):
        if opt.startswith("size="):
            continue
        if opt.startswith("mode="):
            # Les nouveaux RAMDrive Yoleo doivent rester simples : 777.
            # On ignore donc un ancien mode=1777 éventuel déjà présent en conf.
            continue
        if opt == RAMDRIVE_MARKER or opt.startswith("x-yoleo.ramdrive"):
            continue
        out.append(opt)
    return out


def ramdrive_options_for_mount(options):
    """Options envoyées au kernel : surtout pas le marqueur x-yoleo.*."""
    out = []
    for opt in ramdrive_options_split(options):
        if opt == RAMDRIVE_MARKER or opt.startswith("x-yoleo.ramdrive"):
            continue
        out.append(opt)
    return ",".join(out) or "defaults"


def ramdrive_build_options(conf, size_gb, with_marker=False):
    parts = [ramdrive_size_option(size_gb), "mode=0777"]
    parts.extend(ramdrive_options_without_size_marker(ramdrive_default_options(conf)))
    if with_marker:
        parts.append(RAMDRIVE_MARKER)
    return ",".join(parts)


def ramdrive_size_label(options):
    m = re.search(r"(?:^|,)size=([^,\s]+)", str(options or ""))
    if not m:
        return "—"
    val = m.group(1).strip()
    return val.replace("G", " Go").replace("g", " Go").replace("M", " Mo").replace("m", " Mo")


def ramdrive_is_mountpoint(path):
    path = os.path.normpath(str(path or ""))
    if not path or path == ".":
        return False
    try:
        if os.path.ismount(path):
            return True
    except Exception:
        pass
    if shutil.which("mountpoint"):
        try:
            rc, _out = run_cmd(["mountpoint", "-q", path], timeout=5)
            return rc == 0
        except Exception:
            return False
    return False


def ramdrive_fstab_file(conf):
    return fstab_path(conf)


def ramdrive_fstab_row_from_parts(parts):
    if len(parts) < 4:
        return False
    spec, mountpoint, fstype, options = parts[:4]
    if fstype != "tmpfs" or spec != "tmpfs":
        return False
    if RAMDRIVE_MARKER in options or "x-yoleo.ramdrive" in options:
        return True
    # Compatibilité : ancien RAMDrive géré sans marqueur sous la base dédiée.
    try:
        base = ramdrive_base_path(get_config()).rstrip("/") + "/"
    except Exception:
        base = "/mnt/ramdrive/"
    return mountinfo_unescape(mountpoint).startswith(base)


def ramdrive_read_fstab_rows(conf):
    path = ramdrive_fstab_file(conf)
    rows = []
    try:
        lines = open(path, "r", encoding="utf-8", errors="replace").read().splitlines()
    except Exception:
        return rows
    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if not ramdrive_fstab_row_from_parts(parts):
            continue
        mountpoint = mountinfo_unescape(parts[1])
        options = parts[3]
        rows.append({
            "id": f"fstab-{idx}",
            "name": os.path.basename(mountpoint.rstrip("/")) or mountpoint,
            "mountpoint": mountpoint,
            "options": options,
            "mount_options": ramdrive_options_for_mount(options),
            "size_label": ramdrive_size_label(options),
            "from_fstab": True,
            "line": idx,
        })
    return rows


def ramdrive_live_rows(conf):
    rows = []
    mountinfo = read_mountinfo(conf)
    for mounts in mountinfo.values():
        for item in mounts:
            if str(item.get("fstype") or "") != "tmpfs":
                continue
            mountpoint = str(item.get("mount") or "")
            source = str(item.get("source") or "")
            if source and source != "tmpfs":
                continue
            opts = ",".join([str(item.get("opts") or ""), str(item.get("super_options") or "")]).strip(",")
            # On remonte tout tmpfs déclaré fstab Yoleo, et les live sous la base RAMDrive.
            base = ramdrive_base_path(conf).rstrip("/") + "/"
            if not (mountpoint.startswith(base) or RAMDRIVE_MARKER in opts or "x-yoleo.ramdrive" in opts):
                # tmpfs système type /run, /dev/shm : ignoré.
                continue
            rows.append({
                "id": "live-" + re.sub(r"[^A-Za-z0-9_.-]+", "_", mountpoint.strip("/")),
                "name": os.path.basename(mountpoint.rstrip("/")) or mountpoint,
                "mountpoint": mountpoint,
                "options": opts,
                "mount_options": ramdrive_options_for_mount(opts),
                "size_label": ramdrive_size_label(opts),
                "mounted": True,
                "from_fstab": False,
            })
    return rows


def ramdrive_rows(conf):
    fstab_rows = ramdrive_read_fstab_rows(conf)
    live_rows = ramdrive_live_rows(conf)
    live_by_mount = {str(r.get("mountpoint") or ""): r for r in live_rows}
    rows = []
    seen = set()
    for row in fstab_rows:
        mountpoint = str(row.get("mountpoint") or "")
        live = live_by_mount.get(mountpoint)
        item = dict(row)
        item["mounted"] = bool(live) or ramdrive_is_mountpoint(mountpoint)
        if live:
            item["live_options"] = live.get("options") or ""
            if item.get("size_label") in {"", "—"}:
                item["size_label"] = live.get("size_label") or "—"
        item["state_label"] = "monté" if item["mounted"] else "arrêté"
        rows.append(item)
        seen.add(mountpoint)
    for row in live_rows:
        mountpoint = str(row.get("mountpoint") or "")
        if mountpoint in seen:
            continue
        item = dict(row)
        item["mounted"] = True
        item["state_label"] = "monté live"
        rows.append(item)
    rows.sort(key=lambda x: str(x.get("mountpoint") or ""))
    return rows


def ramdrive_fstab_escape_path(path):
    value = str(path or "")
    return (
        value.replace("\\", "\\134")
        .replace(" ", "\\040")
        .replace("\t", "\\011")
        .replace("\n", "\\012")
    )


def ramdrive_mountpoint_available(mountpoint):
    mountpoint = os.path.normpath(str(mountpoint or ""))
    if not mountpoint or mountpoint == ".":
        return False, "Point de montage RAMDrive invalide."
    if ramdrive_is_mountpoint(mountpoint):
        return True, ""
    if os.path.exists(mountpoint):
        if not os.path.isdir(mountpoint):
            return False, "Le point de montage existe mais n'est pas un dossier."
        try:
            entries = [x for x in os.listdir(mountpoint) if x not in {".", ".."}]
        except Exception as exc:
            return False, f"Lecture du dossier de montage impossible : {exc}"
        if entries:
            return False, "Le dossier de montage existe déjà et n'est pas vide. Choisis un dossier vide."
    return True, ""


def ramdrive_ensure_mountpoint_dir(mountpoint):
    ok, msg = ramdrive_mountpoint_available(mountpoint)
    if not ok:
        return False, msg
    try:
        os.makedirs(mountpoint, exist_ok=True)
    except Exception as exc:
        return False, f"Création du dossier impossible : {exc}"
    return True, ""


def ramdrive_write_fstab_entry(conf, mountpoint, options):
    ok_mountpoint, mountpoint_msg = ramdrive_ensure_mountpoint_dir(mountpoint)
    if not ok_mountpoint:
        return False, mountpoint_msg
    path = ramdrive_fstab_file(conf)
    try:
        current = open(path, "r", encoding="utf-8", errors="replace").read().splitlines() if os.path.exists(path) else []
    except Exception as exc:
        return False, f"Lecture fstab impossible : {exc}"
    kept = []
    for line in current:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            kept.append(line)
            continue
        parts = stripped.split()
        if len(parts) >= 2 and mountinfo_unescape(parts[1]) == mountpoint:
            # On remplace seulement cette entrée de montage.
            continue
        kept.append(line)
    kept.append(f"tmpfs\t{ramdrive_fstab_escape_path(mountpoint)}\ttmpfs\t{options}\t0\t0")
    try:
        fstab_backup_and_write(conf, kept)
    except Exception as exc:
        return False, f"Écriture fstab impossible : {exc}"
    return True, "Ligne RAMDrive écrite dans /etc/fstab."


def ramdrive_remove_fstab_entry(conf, mountpoint):
    path = ramdrive_fstab_file(conf)
    try:
        current = open(path, "r", encoding="utf-8", errors="replace").read().splitlines() if os.path.exists(path) else []
    except Exception as exc:
        return False, f"Lecture fstab impossible : {exc}"
    kept = []
    removed = 0
    for line in current:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            kept.append(line)
            continue
        parts = stripped.split()
        if len(parts) >= 2 and mountinfo_unescape(parts[1]) == mountpoint and ramdrive_fstab_row_from_parts(parts):
            removed += 1
            continue
        kept.append(line)
    if not removed:
        return True, "Aucune ligne fstab RAMDrive à supprimer."
    try:
        fstab_backup_and_write(conf, kept)
    except Exception as exc:
        return False, f"Écriture fstab impossible : {exc}"
    return True, f"{removed} ligne(s) fstab supprimée(s)."


def ramdrive_apply_permissions(conf, mountpoint):
    """Applique les droits simples attendus pour un RAMDrive neuf.

    Sécurité : on ne chmod qu'après confirmation que le chemin est bien un
    mountpoint. Ainsi, si le montage tmpfs échoue, on ne modifie pas les droits
    d'un dossier local de fallback.
    """
    norm = os.path.normpath(str(mountpoint or ""))
    base = ramdrive_base_path(conf).rstrip("/")
    if not norm or norm == base or not (norm.startswith(base + "/") or norm.startswith("/mnt/")):
        return False, "chmod refusé : chemin RAMDrive hors zone autorisée."
    if not ramdrive_is_mountpoint(norm):
        return False, "chmod refusé : le RAMDrive n'est pas monté."
    cmd = ["chmod", "-R", "0777", norm]
    rc, out = run_cmd(cmd, timeout=30)
    text = "$ " + " ".join(shlex.quote(x) for x in cmd)
    if out:
        text += "\n" + out.strip()
    if rc != 0:
        return False, text
    return True, text


def ramdrive_mount(conf, mountpoint, options):
    ok_mountpoint, mountpoint_msg = ramdrive_ensure_mountpoint_dir(mountpoint)
    if not ok_mountpoint:
        return False, mountpoint_msg
    if ramdrive_is_mountpoint(mountpoint):
        return True, f"Déjà monté : {mountpoint}"
    mount_options = ramdrive_options_for_mount(options)
    cmd = ["mount", "-t", "tmpfs", "-o", mount_options, "tmpfs", mountpoint]
    rc, out = run_cmd(cmd, timeout=25)
    text = "$ " + " ".join(shlex.quote(x) for x in cmd)
    if out:
        text += "\n" + out.strip()
    if rc != 0:
        return False, text
    if not ramdrive_is_mountpoint(mountpoint):
        return False, text + "\nMontage demandé, mais mountpoint non confirmé."
    ok_chmod, chmod_msg = ramdrive_apply_permissions(conf, mountpoint)
    text += "\n" + chmod_msg
    if not ok_chmod:
        return False, text + "\nMontage OK, mais chmod 777 impossible."
    return True, text


def ramdrive_unmount(conf, mountpoint, remove_empty_dir=False):
    outputs = []
    if ramdrive_is_mountpoint(mountpoint):
        rc, out = run_cmd(["umount", mountpoint], timeout=25)
        line = "$ umount " + shlex.quote(mountpoint)
        if out:
            line += "\n" + out.strip()
        outputs.append(line)
        if rc != 0:
            return False, "\n".join(outputs) or "Démontage impossible."
    else:
        outputs.append(f"Déjà démonté : {mountpoint}")
    if ramdrive_is_mountpoint(mountpoint):
        outputs.append("Sécurité : encore monté, dossier conservé.")
        return False, "\n".join(outputs)
    if remove_empty_dir:
        norm = os.path.normpath(mountpoint)
        base = ramdrive_base_path(conf).rstrip("/")
        if norm and norm != base and (norm.startswith(base + "/") or norm.startswith("/mnt/")):
            try:
                os.rmdir(norm)
                outputs.append(f"Dossier vide supprimé : {norm}")
            except OSError as exc:
                outputs.append(f"Dossier conservé : {exc}")
        else:
            outputs.append("Dossier conservé : base ou chemin hors zone RAMDrive.")
    return True, "\n".join(outputs)


def ramdrive_resolve_mountpoint(conf, raw, name="ram1"):
    raw = str(raw or "").strip()
    if not raw:
        raw = ramdrive_default_mountpoint(conf, name)
    ok, path, err = safe_real_mount_path(conf, raw)
    if not ok:
        return False, path, err
    norm = os.path.normpath(path)
    forbidden = {"/mnt", "/media", "/srv", "/data", ramdrive_base_path(conf).rstrip("/")}
    if norm in forbidden:
        return False, norm, "Choisis un sous-dossier de montage, pas la racine elle-même."
    return True, norm, ""


def ramdrive_summary(conf):
    rows = ramdrive_rows(conf)
    return rows, {
        "profiles": len(rows),
        "mounted": sum(1 for r in rows if r.get("mounted")),
        "persistent": sum(1 for r in rows if r.get("from_fstab")),
    }


@disk_bp.route("/disk/api/ramdrive")
def disk_ramdrive_api():
    conf = get_config()
    rows, summary = ramdrive_summary(conf)
    min_gb, max_gb = ramdrive_size_limits(conf)
    return jsonify({
        "ok": True,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_path": ramdrive_base_path(conf),
        "default_mountpoint": ramdrive_default_mountpoint(conf, "ram1"),
        "default_options": ramdrive_default_options(conf),
        "limits": {"min_gb": min_gb, "max_gb": max_gb},
        "summary": summary,
        "rows": rows,
    })


@disk_bp.route("/disk/api/action/ramdrive/create", methods=["POST"])
def disk_ramdrive_create_api():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    name = ramdrive_safe_name(payload.get("name") or "ram1")
    ok_path, mountpoint, path_error = ramdrive_resolve_mountpoint(conf, payload.get("mountpoint"), name)
    if not ok_path:
        return json_response(False, path_error)
    ok_size, size_gb, size_error = ramdrive_parse_size_gb(payload.get("size_gb"), conf)
    if not ok_size:
        return json_response(False, size_error)
    mount_now = bool_from_payload(payload.get("mount_now"))
    add_fstab = bool_from_payload(payload.get("add_fstab"))
    if not mount_now and not add_fstab:
        return json_response(False, "Coche au moins Monter maintenant ou Ajouter dans /etc/fstab.")
    live_options = ramdrive_build_options(conf, size_gb, with_marker=False)
    fstab_options = ramdrive_build_options(conf, size_gb, with_marker=True)
    outputs = []
    if add_fstab:
        ok_fstab, msg = ramdrive_write_fstab_entry(conf, mountpoint, fstab_options)
        outputs.append(msg)
        if not ok_fstab:
            return json_response(False, msg, output="\n".join(outputs)[-5000:])
        if shutil.which("systemctl"):
            rc, out = run_cmd(["systemctl", "daemon-reload"], timeout=12)
            if out:
                outputs.append("$ systemctl daemon-reload\n" + out.strip())
            if rc != 0:
                return json_response(False, "fstab écrit, mais daemon-reload a échoué.", output="\n".join(outputs)[-5000:])
    if mount_now:
        ok_mount, msg = ramdrive_mount(conf, mountpoint, live_options)
        outputs.append(msg)
        if not ok_mount:
            return json_response(False, "RAMDrive enregistré, mais montage impossible." if add_fstab else "Montage RAMDrive impossible.", output="\n".join(outputs)[-5000:])
    return json_response(True, "RAMDrive appliqué.", output="\n".join(outputs)[-5000:])


@disk_bp.route("/disk/api/action/ramdrive/mount", methods=["POST"])
def disk_ramdrive_mount_api():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    ok_path, mountpoint, path_error = ramdrive_resolve_mountpoint(conf, payload.get("mountpoint"), "ram1")
    if not ok_path:
        return json_response(False, path_error)
    rows = ramdrive_rows(conf)
    row = next((r for r in rows if str(r.get("mountpoint") or "") == mountpoint), None)
    options = str((row or {}).get("options") or "")
    if not options:
        ok_size, size_gb, size_error = ramdrive_parse_size_gb(payload.get("size_gb") or "1", conf)
        if not ok_size:
            return json_response(False, size_error)
        options = ramdrive_build_options(conf, size_gb, with_marker=False)
    ok_mount, msg = ramdrive_mount(conf, mountpoint, options)
    return json_response(ok_mount, "RAMDrive monté." if ok_mount else "Montage RAMDrive impossible.", output=msg[-5000:])


@disk_bp.route("/disk/api/action/ramdrive/unmount", methods=["POST"])
def disk_ramdrive_unmount_api():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    ok_path, mountpoint, path_error = ramdrive_resolve_mountpoint(conf, payload.get("mountpoint"), "ram1")
    if not ok_path:
        return json_response(False, path_error)
    ok_unmount, msg = ramdrive_unmount(conf, mountpoint, remove_empty_dir=False)
    return json_response(ok_unmount, "RAMDrive démonté." if ok_unmount else "Démontage RAMDrive incomplet.", output=msg[-5000:])


@disk_bp.route("/disk/api/action/ramdrive/delete", methods=["POST"])
def disk_ramdrive_delete_api():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    ok_path, mountpoint, path_error = ramdrive_resolve_mountpoint(conf, payload.get("mountpoint"), "ram1")
    if not ok_path:
        return json_response(False, path_error)
    outputs = []
    ok_unmount, msg_unmount = ramdrive_unmount(conf, mountpoint, remove_empty_dir=True)
    outputs.append(msg_unmount)
    if not ok_unmount and ramdrive_is_mountpoint(mountpoint):
        return json_response(False, "Suppression refusée : le RAMDrive est encore monté.", output="\n".join(outputs)[-5000:])
    ok_fstab, msg_fstab = ramdrive_remove_fstab_entry(conf, mountpoint)
    outputs.append(msg_fstab)
    if not ok_fstab:
        return json_response(False, "RAMDrive démonté, mais fstab non nettoyé.", output="\n".join(outputs)[-5000:])
    if shutil.which("systemctl"):
        rc, out = run_cmd(["systemctl", "daemon-reload"], timeout=12)
        if out:
            outputs.append("$ systemctl daemon-reload\n" + out.strip())
    return json_response(True, "RAMDrive supprimé.", output="\n".join(outputs)[-5000:])
