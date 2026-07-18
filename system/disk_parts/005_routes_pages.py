@disk_bp.route("/disk/api/sleep")
def disk_sleep_api():
    conf = get_config()
    service = sleep_service_name(conf)
    cfg = read_sleep_config(conf)
    return jsonify({
        "ok": True,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "service": service,
        "service_status": service_status_text(service),
        "systemd": cfg.get("systemd") or systemd_show(service),
        "pid": cfg.get("pid") or "",
        "script": "",
        "config_file": sleep_conf_path(conf),
        "config": cfg,
        "disks": available_sleep_disks(conf),
    })


@disk_bp.route("/disk/api/action/sleep/apply", methods=["POST"])
def disk_sleep_apply_api():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    try:
        minutes = int(payload.get("minutes") or 30)
    except Exception:
        return json_response(False, "Minutes invalides.")
    minutes = max(0, min(330, minutes))

    ok_write, msg = write_sleep_service(conf, [], minutes)
    if not ok_write:
        return json_response(False, "Application hd-idle impossible.", output=msg[-5000:])
    return json_response(True, f"Veille hd-idle appliquée : {minutes} min sur les HDD rotatifs.", output=msg[-5000:])


@disk_bp.route("/disk/api/action/sleep/service", methods=["POST"])
def disk_sleep_service_action_api():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action") or "status").strip().lower()
    service = sleep_service_name(conf)

    if action == "status":
        if shutil.which("systemctl"):
            rc, out = run_cmd(["systemctl", "status", service, "--no-pager", "-l"], timeout=10)
            return json_response(True, "État veille lu via systemctl.", output=(out or service_status_text(service))[-5000:])
        return json_response(False, "systemctl introuvable, impossible de lire hd-idle.")

    if action in {"start", "enable"}:
        cleanup = disable_legacy_sleep_service(conf)
        output = cleanup or ""
        if shutil.which("systemctl"):
            for cmd in (["systemctl", "daemon-reload"], ["systemctl", "enable", "--now", service], ["systemctl", "restart", service]):
                rc, out = run_cmd(list(cmd), timeout=25)
                output += f"\n$ {' '.join(cmd)}" + (("\n" + out.strip()) if out else "")
                if rc != 0 and cmd[1] in {"enable", "restart"}:
                    return json_response(False, "Démarrage hd-idle impossible.", output=output[-5000:])
            return json_response(True, "Service hd-idle démarré/activé.", output=output[-5000:])
        return json_response(False, "systemctl introuvable, impossible de démarrer hd-idle.", output=output[-5000:])

    if action in {"disable", "stop"}:
        outputs: List[str] = []
        try:
            write_hd_idle_defaults(conf, [], 0)
            outputs.append(f"Configuration arrêt écrite : {sleep_conf_path(conf)}")
        except Exception as exc:
            outputs.append(f"Écriture config arrêt impossible : {exc}")
        if shutil.which("systemctl"):
            for cmd in (["systemctl", "disable", "--now", service], ["systemctl", "reset-failed", service]):
                rc, out = run_cmd(list(cmd), timeout=25)
                outputs.append(f"$ {' '.join(cmd)}" + (("\n" + out.strip()) if out else ""))
                if rc != 0 and cmd[1] == "disable":
                    return json_response(False, "Arrêt/désactivation hd-idle impossible.", output="\n".join(outputs)[-5000:])
            return json_response(True, "Service hd-idle arrêté et désactivé.", output="\n".join(outputs)[-5000:])
        return json_response(False, "systemctl introuvable, impossible de désactiver hd-idle.", output="\n".join(outputs)[-5000:])

    if action in {"remove", "delete"}:
        if shutil.which("systemctl"):
            rc, out = run_cmd(["systemctl", "disable", "--now", service], timeout=25)
            return json_response(rc == 0, "Service hd-idle désactivé." if rc == 0 else "Désactivation hd-idle impossible.", output=(out or "")[-5000:])
        return json_response(False, "systemctl introuvable.")

    return json_response(False, "Action veille inconnue.")


@disk_bp.route("/disk/api/logs")
def disk_logs_api():
    conf = get_config()
    try:
        lines = int(request.args.get("lines") or 300)
    except Exception:
        lines = 300
    lines = max(50, min(2000, lines))
    sections: List[str] = []
    log_path = disk_log_file(conf)
    sections.append(f"# disk log: {log_path}")
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
            content_lines = handle.read().splitlines()[-lines:]
        sections.append("\n".join(content_lines) if content_lines else "(vide)")
    except Exception as exc:
        sections.append(f"Lecture impossible : {exc}")
    state_path = mergerfs_state_file(conf)
    sections.append(f"\n# mergerfs profiles: {state_path}")
    try:
        with open(state_path, "r", encoding="utf-8", errors="replace") as handle:
            state_lines = handle.read().splitlines()[-lines:]
        sections.append("\n".join(state_lines) if state_lines else "(vide)")
    except Exception as exc:
        sections.append(f"Lecture impossible : {exc}")

    defaults_path = sleep_conf_path(conf)
    sections.append(f"\n# hd-idle defaults: {defaults_path}")
    try:
        with open(defaults_path, "r", encoding="utf-8", errors="replace") as handle:
            defaults_lines = handle.read().splitlines()[-lines:]
        sections.append("\n".join(defaults_lines) if defaults_lines else "(vide)")
    except Exception as exc:
        sections.append(f"Lecture impossible : {exc}")

    old_defaults_path = "/etc/default/hdd-veille"
    if old_defaults_path != defaults_path:
        sections.append(f"\n# ancien hdd-veille defaults éventuel: {old_defaults_path}")
        try:
            with open(old_defaults_path, "r", encoding="utf-8", errors="replace") as handle:
                old_defaults_lines = handle.read().splitlines()[-lines:]
            sections.append("\n".join(old_defaults_lines) if old_defaults_lines else "(vide)")
        except Exception as exc:
            sections.append(f"Lecture impossible : {exc}")

    if shutil.which("journalctl"):
        service = sleep_service_name(conf)
        rc, out = run_cmd(["journalctl", "-u", service, "-n", str(min(lines, 200)), "--no-pager"], timeout=10)
        sections.append(f"\n# journalctl -u {service}")
        sections.append(out if out else "(aucune ligne)")
        legacy = legacy_sleep_service_name(conf)
        if legacy and legacy != service:
            rc_old, out_old = run_cmd(["journalctl", "-u", legacy, "-n", str(min(lines, 80)), "--no-pager"], timeout=10)
            if out_old:
                sections.append(f"\n# ancien service désactivé normalement: journalctl -u {legacy}")
                sections.append(out_old)
    return jsonify({
        "ok": True,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "text": "\n".join(sections),
    })


def _disk_refresh_seconds() -> int:
    conf = get_config()
    return conf_int(conf, "REFRESH_SECONDS", 10)


def _normalise_disk_section(section: str) -> str:
    section = (section or "main").strip().lower()
    aliases = {
        "main": "general",
        "general": "general",
        "disk": "general",
        "disks": "general",
        "maintenance": "maintenance",
        "mount": "maintenance",
        "format": "maintenance",
        "formatage": "maintenance",
        "mergerfs": "mergerfs",
        "margefs": "mergerfs",
        "mergerfs_info": "mergerfs_info",
        "mergerfs-info": "mergerfs_info",
        "sleep": "spindown",
        "veille": "spindown",
        "spindown": "spindown",
        "logs": "logs",
        "log": "logs",
        "raid": "raid",
        "red": "raid",
        "snapraid": "snapraid",
        "snap": "snapraid",
        "ramdrive": "ramdrive",
        "ram": "ramdrive",
        "tmpfs": "ramdrive",
        "virtual_disk": "virtual_disk",
        "virtual-disk": "virtual_disk",
        "virtualdisk": "virtual_disk",
        "vdisk": "virtual_disk",
    }
    return aliases.get(section, "general")


def _disk_initial_tab(section: str) -> str:
    section = _normalise_disk_section(section)
    if section == "mergerfs":
        return "mergerfs"
    if section == "mergerfs_info":
        return "mergerfs_info"
    if section == "spindown":
        return "sleep"
    if section == "logs":
        return "logs"
    if section == "ramdrive":
        return "ramdrive"
    if section == "virtual_disk":
        return "virtual_disk"
    return section


def _render_disk_section(section: str):
    section = _normalise_disk_section(section)
    template_name = {
        "general": "disk_general.html",
        "maintenance": "disk_maintenance.html",
        "mergerfs": "disk_mergerfs.html",
        "mergerfs_info": "disk_mergerfs_info.html",
        "spindown": "disk_spindown.html",
        "logs": "disk_logs.html",
        "raid": "disk_raid.html",
        "snapraid": "disk_snapraid.html",
        "ramdrive": "disk_ramdrive.html",
        "virtual_disk": "disk_virtual_disk.html",
    }.get(section, "disk_general.html")
    return render_template(
        template_name,
        disk_section=section,
        initial_tab=_disk_initial_tab(section),
        refresh_seconds=_disk_refresh_seconds(),
    )


@disk_bp.route("/disk")
def disk_root_page():
    legacy_tab = request.args.get("tab")
    if legacy_tab:
        return redirect(f"/disk/{_normalise_disk_section(legacy_tab)}")
    return redirect("/disk/main")


@disk_bp.route("/disk/main")
@disk_bp.route("/disk/general")
def disk_page():
    legacy_tab = request.args.get("tab")
    if legacy_tab:
        return redirect(f"/disk/{_normalise_disk_section(legacy_tab)}")
    return _render_disk_section("general")


@disk_bp.route("/disk/maintenance")
def disk_maintenance_page():
    return _render_disk_section("maintenance")


def _mergerfs_mount_command(row):
    sources = row.get("sources") or []
    if not sources and row.get("source"):
        sources = [p for p in str(row.get("source") or "").split(":") if p]
    source_text = ":".join([str(s) for s in sources if str(s)]) or str(row.get("source") or "")
    target = str(row.get("target") or row.get("mount") or "")
    options = str(row.get("options") or "")
    if not source_text or not target:
        return ""
    if options:
        return f"mount -t fuse.mergerfs -o {options} {source_text} {target}"
    return f"mount -t fuse.mergerfs {source_text} {target}"


def _mergerfs_info_context():
    conf = get_config()
    fstab_rows = parse_mergerfs_fstab(conf)
    live_rows = current_mergerfs_live(conf)
    live_targets = {str(item.get("target") or item.get("mount") or "") for item in live_rows}
    state = read_mergerfs_state(conf)
    profiles = state.get("profiles") if isinstance(state, dict) else {}
    if not isinstance(profiles, dict):
        profiles = {}

    rows = []
    declared_targets = set()
    for row in fstab_rows:
        item = dict(row)
        target = str(item.get("target") or "")
        declared_targets.add(target)
        profile = profiles.get(target) if isinstance(profiles.get(target), dict) else {}
        item["profile"] = profile
        item["mounted"] = target in live_targets
        item["disabled"] = bool(item.get("disabled") or profile.get("disabled"))
        item["status_label"] = "désactivé" if item["disabled"] else ("démarré" if item["mounted"] else "arrêté")
        item["mount_command"] = _mergerfs_mount_command(item)
        rows.append(item)

    for live in live_rows:
        target = str(live.get("target") or live.get("mount") or "")
        if target in declared_targets:
            continue
        item = dict(live)
        item["from_fstab"] = False
        item["disabled"] = False
        item["mounted"] = True
        item["status_label"] = "démarré live"
        item["mount_command"] = _mergerfs_mount_command(item)
        rows.append(item)

    fstab_file = fstab_path(conf)
    ok_fstab, fstab_lines, fstab_error = read_fstab_lines(conf)
    fstab_excerpt = []
    if ok_fstab:
        for idx, line in enumerate(fstab_lines, start=1):
            try:
                is_line = is_mergerfs_fstab_line(line)
            except Exception:
                is_line = "mergerfs" in str(line).lower()
            if is_line or "mergerfs" in str(line).lower():
                fstab_excerpt.append(f"{idx}: {line}")
    elif fstab_error:
        fstab_excerpt.append(fstab_error)

    state_file = mergerfs_state_file(conf)
    try:
        with open(state_file, "r", encoding="utf-8", errors="replace") as handle:
            state_excerpt = handle.read().strip()
    except Exception as exc:
        state_excerpt = f"Lecture impossible : {exc}"
    if len(state_excerpt) > 5000:
        state_excerpt = state_excerpt[:5000] + "\n... (tronqué)"

    return {
        "mergerfs_rows": rows,
        "mergerfs_fstab_file": fstab_file,
        "mergerfs_state_file": state_file,
        "mergerfs_fstab_excerpt": "\n".join(fstab_excerpt) if fstab_excerpt else "Aucune ligne MergerFS dans fstab.",
        "mergerfs_state_excerpt": state_excerpt or "Aucun profil MergerFS enregistré.",
        "mergerfs_generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


@disk_bp.route("/disk/mergerfs/info")
@disk_bp.route("/disk/margefs/info")
def disk_mergerfs_info_page():
    ctx = _mergerfs_info_context()
    return render_template(
        "disk_mergerfs_info.html",
        disk_section="mergerfs_info",
        initial_tab="mergerfs_info",
        refresh_seconds=_disk_refresh_seconds(),
        **ctx,
    )


@disk_bp.route("/disk/mergerfs")
@disk_bp.route("/disk/margefs")
def disk_mergerfs_page():
    return _render_disk_section("mergerfs")


@disk_bp.route("/disk/spindown")
@disk_bp.route("/disk/veille")
def disk_spindown_page():
    return _render_disk_section("spindown")


@disk_bp.route("/disk/raid")
@disk_bp.route("/disk/red")
def disk_raid_page():
    return _render_disk_section("raid")


@disk_bp.route("/disk/snapraid")
def disk_snapraid_page():
    return _render_disk_section("snapraid")


@disk_bp.route("/disk/ramdrive")
@disk_bp.route("/disk/ram")
@disk_bp.route("/disk/tmpfs")
def disk_ramdrive_page():
    return _render_disk_section("ramdrive")


@disk_bp.route("/disk/virtual_disk")
@disk_bp.route("/disk/virtual-disk")
@disk_bp.route("/disk/virtualdisk")
@disk_bp.route("/disk/vdisk")
def disk_virtual_disk_page():
    return _render_disk_section("virtual_disk")


@disk_bp.route("/disk/logs")
@disk_bp.route("/disk/log")
def disk_logs_page():
    return _render_disk_section("logs")


@disk_bp.route("/disk/api")
def disk_api():
    return jsonify(collect_disks())
