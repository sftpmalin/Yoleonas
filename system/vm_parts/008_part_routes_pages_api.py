#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# PART 008 - Routes API et rendu des pages VM



def read_tail(path: str, limit: int = 180) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-limit:]
        return "".join(lines)
    except Exception as exc:
        return f"Impossible de lire {path}: {exc}\n"


KVM_UDEV_RULE_PATH = "/etc/udev/rules.d/60-kvm.rules"
KVM_UDEV_RULE_LINE = 'KERNEL=="kvm", GROUP="kvm", MODE="0660"'


def _read_text(path: str, limit: int = 200000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except Exception:
        return ""


def _group_name(gid: int) -> str:
    try:
        import grp
        return grp.getgrgid(gid).gr_name
    except Exception:
        return str(gid)


def _owner_name(uid: int) -> str:
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except Exception:
        return str(uid)


def _loaded_kvm_modules() -> List[str]:
    text = _read_text("/proc/modules")
    if not text:
        rc, text = run_cmd(["lsmod"], timeout=8)
        if rc != 0:
            text = ""
    modules = []
    for raw in text.splitlines():
        name = raw.split()[0] if raw.split() else ""
        if name == "kvm" or name.startswith("kvm_"):
            modules.append(name)
    return sorted(set(modules))


def _unit_exists(unit: str) -> bool:
    if not shutil.which("systemctl"):
        return False
    rc, out = run_cmd(["systemctl", "list-unit-files", unit, "--no-legend", "--no-pager"], timeout=8)
    if rc == 0 and unit in out:
        return True
    rc, out = run_cmd(["systemctl", "status", unit, "--no-pager"], timeout=8)
    return rc == 0 or "Loaded: loaded" in out


def detect_libvirt_services() -> List[str]:
    services: List[str] = []
    if _unit_exists("virtqemud.service"):
        services.append("virtqemud")
    elif _unit_exists("libvirtd.service"):
        services.append("libvirtd")
    else:
        services.append("libvirtd")
    if _unit_exists("virtlogd.service"):
        services.append("virtlogd")
    return services


def collect_kvm_diagnostic() -> Dict[str, object]:
    issues: List[str] = []
    warnings: List[str] = []
    dev = {"path": "/dev/kvm", "exists": os.path.exists("/dev/kvm")}

    if dev["exists"]:
        try:
            st = os.stat("/dev/kvm")
            mode = format(st.st_mode & 0o777, "04o")
            group = _group_name(st.st_gid)
            owner = _owner_name(st.st_uid)
            dev.update({"owner": owner, "group": group, "mode": mode})
            if group != "kvm":
                issues.append(
                    "KVM detecte mais droits incorrects : /dev/kvm n'appartient pas au groupe kvm. "
                    f"Groupe actuel : {group}. Les VM KVM peuvent echouer au demarrage."
                )
            if mode != "0660":
                issues.append(
                    f"KVM detecte mais permissions incorrectes : /dev/kvm est en {mode}, attendu 0660."
                )
        except Exception as exc:
            issues.append(f"Impossible de lire les droits de /dev/kvm : {exc}")
    else:
        issues.append("/dev/kvm est introuvable. Active la virtualisation dans le BIOS et verifie les modules KVM.")

    modules = _loaded_kvm_modules()
    if "kvm" not in modules:
        issues.append("Module kvm non charge.")
    if not any(item in modules for item in ("kvm_amd", "kvm_intel")):
        issues.append("Module KVM CPU non charge : kvm_amd ou kvm_intel absent.")

    cpuinfo = _read_text("/proc/cpuinfo")
    cpu_flags = {
        "svm": bool(re.search(r"\bsvm\b", cpuinfo)),
        "vmx": bool(re.search(r"\bvmx\b", cpuinfo)),
    }
    if not (cpu_flags["svm"] or cpu_flags["vmx"]):
        issues.append("Le CPU n'expose pas svm/vmx. La virtualisation BIOS/UEFI semble desactivee.")

    rule_text = _read_text(KVM_UDEV_RULE_PATH)
    udev = {
        "path": KVM_UDEV_RULE_PATH,
        "exists": os.path.exists(KVM_UDEV_RULE_PATH),
        "ok": KVM_UDEV_RULE_LINE in rule_text,
        "expected": KVM_UDEV_RULE_LINE,
    }
    if not udev["ok"]:
        warnings.append(f"Regle udev KVM persistante absente ou differente : {KVM_UDEV_RULE_PATH}")

    services = detect_libvirt_services()
    return {
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "dev": dev,
        "modules": modules,
        "cpu_flags": cpu_flags,
        "udev": udev,
        "services": services,
    }


def render_kvm_diagnostic_text(diag: Dict[str, object]) -> str:
    dev = diag.get("dev", {}) if isinstance(diag.get("dev"), dict) else {}
    cpu_flags = diag.get("cpu_flags", {}) if isinstance(diag.get("cpu_flags"), dict) else {}
    udev = diag.get("udev", {}) if isinstance(diag.get("udev"), dict) else {}
    lines = [
        "Diagnostic KVM : " + ("OK" if diag.get("ok") else "PROBLEME"),
        f"/dev/kvm : {'present' if dev.get('exists') else 'absent'}",
    ]
    if dev.get("exists"):
        lines.append(f"Droits /dev/kvm : {dev.get('owner', '?')}:{dev.get('group', '?')} mode {dev.get('mode', '?')}")
    lines.append("Modules charges : " + (", ".join(diag.get("modules") or []) or "aucun module kvm detecte"))
    lines.append(f"Flags CPU : svm={'oui' if cpu_flags.get('svm') else 'non'} / vmx={'oui' if cpu_flags.get('vmx') else 'non'}")
    lines.append(f"Regle udev : {udev.get('path', KVM_UDEV_RULE_PATH)} -> {'OK' if udev.get('ok') else 'a corriger'}")
    lines.append("Service libvirt detecte : " + ", ".join(diag.get("services") or ["libvirtd"]))
    if diag.get("issues"):
        lines.append("")
        lines.append("Erreurs :")
        lines.extend("- " + str(item) for item in diag.get("issues") or [])
    if diag.get("warnings"):
        lines.append("")
        lines.append("Avertissements :")
        lines.extend("- " + str(item) for item in diag.get("warnings") or [])
    return "\n".join(lines)


def _vm_repair_blockers(conf: Dict[str, str]) -> Tuple[List[str], Optional[str]]:
    names, err = list_vm_names(conf)
    if err:
        return [], err
    blockers: List[str] = []
    for name in names:
        current = get_vm_state(conf, name)
        if state_class(current) != "stopped":
            blockers.append(f"{name} ({current or 'unknown'})")
    return blockers, None


def repair_kvm_host(conf: Dict[str, str]) -> Tuple[Dict[str, object], int]:
    blockers, err = _vm_repair_blockers(conf)
    if err:
        return {"ok": False, "message": "Impossible de verifier l'etat des VM : " + err}, 500
    if blockers:
        return {
            "ok": False,
            "message": "Erreur : toutes les VM doivent etre arretees pour lancer la reparation KVM. VM non arretees : " + ", ".join(blockers),
        }, 409

    output: List[str] = []
    rc_group, out_group = run_cmd(["getent", "group", "kvm"], timeout=8)
    output.append("$ getent group kvm\n" + (out_group or ""))
    if rc_group != 0:
        return {"ok": False, "message": "Groupe kvm introuvable. Installe/verifie les paquets KVM/libvirt avant la reparation.", "output": "\n".join(output)}, 500

    try:
        os.makedirs(os.path.dirname(KVM_UDEV_RULE_PATH), exist_ok=True)
        current_rule = _read_text(KVM_UDEV_RULE_PATH)
        if KVM_UDEV_RULE_LINE not in current_rule:
            with open(KVM_UDEV_RULE_PATH, "w", encoding="utf-8") as handle:
                handle.write(KVM_UDEV_RULE_LINE + "\n")
            output.append(f"Ecriture {KVM_UDEV_RULE_PATH} : {KVM_UDEV_RULE_LINE}")
        else:
            output.append(f"Regle udev deja presente : {KVM_UDEV_RULE_PATH}")
    except Exception as exc:
        return {"ok": False, "message": f"Impossible d'ecrire {KVM_UDEV_RULE_PATH} : {exc}", "output": "\n".join(output)}, 500

    if os.path.exists("/dev/kvm"):
        for cmd in (["chgrp", "kvm", "/dev/kvm"], ["chmod", "660", "/dev/kvm"]):
            rc, out = run_cmd(cmd, timeout=12)
            output.append("$ " + " ".join(cmd) + "\n" + (out or f"code={rc}"))
            if rc != 0:
                return {"ok": False, "message": "Correction immediate de /dev/kvm impossible.", "output": "\n".join(output)}, 500
    else:
        output.append("/dev/kvm absent : la regle udev est preparee, mais le peripherique doit exister apres chargement KVM.")

    for cmd in (["udevadm", "control", "--reload-rules"], ["udevadm", "trigger", "--name-match=kvm"]):
        rc, out = run_cmd(cmd, timeout=20)
        output.append("$ " + " ".join(cmd) + "\n" + (out or f"code={rc}"))

    services = detect_libvirt_services()
    if shutil.which("systemctl"):
        cmd = ["systemctl", "restart", *services]
        rc, out = run_cmd(cmd, timeout=60)
        output.append("$ " + " ".join(cmd) + "\n" + (out or f"code={rc}"))
        if rc != 0:
            return {"ok": False, "message": "Redemarrage libvirt impossible.", "output": "\n".join(output)}, 500
    else:
        output.append("systemctl introuvable : services libvirt non redemarres.")

    rc, out = run_cmd(["ls", "-l", "/dev/kvm"], timeout=8)
    output.append("$ ls -l /dev/kvm\n" + (out or f"code={rc}"))
    diag = collect_kvm_diagnostic()
    text = render_kvm_diagnostic_text(diag)
    status = 200 if diag.get("ok") else 500
    message = "Reparation KVM terminee." if diag.get("ok") else "Reparation KVM executee, mais le diagnostic indique encore un probleme."
    return {"ok": bool(diag.get("ok")), "message": message + "\n\n" + text, "diagnostic": diag, "output": "\n".join(output)}, status


def collect_vm_logs(conf: Dict[str, str]) -> Dict[str, str]:
    paths = {
        "vm_log": vm_log_path(conf),
        "console_log": str(conf.get("CONSOLE_LOG_FILE", "") or ""),
        "config": get_config_path(),
    }
    journal_cmds = [["journalctl", "-u", service, "-b", "-n", "80", "--no-pager"] for service in detect_libvirt_services()]
    journal_parts = []
    for cmd in journal_cmds:
        rc, out = run_cmd(cmd, timeout=8)
        if rc == 0 and out.strip():
            journal_parts.append("$ " + " ".join(cmd) + "\n" + out)
    return {
        "paths": json.dumps(paths, ensure_ascii=False, indent=2),
        "kvm": render_kvm_diagnostic_text(collect_kvm_diagnostic()),
        "vm_log": read_tail(paths["vm_log"]),
        "console_log": read_tail(paths["console_log"]) if paths["console_log"] else "",
        "config": read_tail(paths["config"], limit=120),
        "journal": "\n\n".join(journal_parts) or "Aucun journal libvirt lisible.",
    }


@vm_bp.route("/vm/ping")
def vm_ping():
    return jsonify({"ok": True, "module": "vm", "route": "/vm"})

@vm_bp.route("/vm/host_disks")
def host_disks():
    conf = get_config()
    try:
        return jsonify({"ok": True, "disks": list_host_byid_disks(conf)})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc), "disks": []}), 500


@vm_bp.route("/vm/disk_action", methods=["POST"])
def disk_action():
    conf = get_config()
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    try:
        result, status = do_vm_disk_action(conf, payload)
        return jsonify(result), status
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@vm_bp.route("/vm/device_action", methods=["POST"])
def vm_device_action():
    conf = get_config()
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    try:
        result, status = do_vm_device_action(conf, payload)
        return jsonify(result), status
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@vm_bp.route("/vm/update", methods=["POST"])
def vm_update():
    conf = get_config()
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    try:
        result, status = do_vm_update(conf, payload)
        return jsonify(result), status
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@vm_bp.route("/vm/storage_action", methods=["POST"])
def vm_storage_action():
    conf = get_config()
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    try:
        result, status = do_storage_action(conf, payload)
        return jsonify(result), status
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@vm_bp.route("/vm/network_action", methods=["POST"])
def vm_network_action():
    conf = get_config()
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    try:
        result, status = do_network_action(conf, payload)
        return jsonify(result), status
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@vm_bp.route("/vm/create", methods=["POST"])
def vm_create():
    conf = get_config()
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    try:
        result, status = do_create_vm(conf, payload)
        return jsonify(result), status
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@vm_bp.route("/vm/serial_ttyd", methods=["POST"])
def vm_serial_ttyd():
    conf = get_config()
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    try:
        result = start_vm_serial_ttyd(conf, str(payload.get("name", "") or ""))
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@vm_bp.route("/vm/api/logs")
def vm_logs_api():
    conf = get_config()
    return jsonify({"ok": True, "logs": collect_vm_logs(conf)})


@vm_bp.route("/vm/api/kvm/diagnostic")
def vm_kvm_diagnostic_api():
    try:
        diag = collect_kvm_diagnostic()
        return jsonify({"ok": bool(diag.get("ok")), "diagnostic": diag, "message": render_kvm_diagnostic_text(diag)})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@vm_bp.route("/vm/api/kvm/repair", methods=["POST"])
def vm_kvm_repair_api():
    conf = get_config()
    try:
        result, status = repair_kvm_host(conf)
        return jsonify(result), status
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


VM_SECTIONS = {
    "general": ("Virtual Machine", "Vue synthetique des machines virtuelles libvirt/KVM : etat, console, XML et actions directes depuis le tableau."),
    "storage": ("VM - Stockage", "Pools libvirt, volumes, ISO detectes et disques physiques stables by-id."),
    "network": ("VM - Reseau", "Reseaux libvirt, bridge hote, NAT, autostart et suppression."),
    "logs": ("VM - Logs", "Journal du module VM, console noVNC, vm.conf et derniers logs libvirt."),
    "networks": ("VM - Reseau", "Reseaux libvirt, bridge hote, NAT, autostart et suppression."),
}


VM_SECTION_ALIASES = {
    "": "general",
    "general": "general",
    "home": "general",
    "storage": "storage",
    "network": "network",
    "networks": "network",
    "logs": "logs",
    "log": "logs",
}


def _render_vm_page(section: str = "general"):
    section = VM_SECTION_ALIASES.get((section or "general").strip().lower(), "general")
    conf = get_config()
    vms, summary, error = collect_inventory(conf)
    extra = collect_vm_extra(conf)
    template_name = {
        "general": "vm_general.html",
        "storage": "vm_storage.html",
        "network": "vm_network.html",
        "logs": "vm_logs.html",
    }.get(section, "vm_general.html")
    return render_template(
        template_name,
        conf=conf,
        vms=vms,
        summary=summary,
        error=error,
        refresh_seconds=int(conf.get("REFRESH_SECONDS", "8") or "8"),
        active_section=section,
        vm_current_title=VM_SECTIONS.get(section, VM_SECTIONS["general"]),
        **extra,
    )


# Routes HTML canoniques du module VM.
# La page principale est physiquement /vms/main afin que /vms/storage
# ne puisse plus activer en même temps l'entrée principale du menu.
# Les routes techniques /vm/... restent dédiées aux API, noVNC, XML et édition.
@vm_bp.route("/vms/main", defaults={"section": "general"})
@vm_bp.route("/vms/storage", defaults={"section": "storage"})
@vm_bp.route("/vms/networks", defaults={"section": "network"})
@vm_bp.route("/vms/logs", defaults={"section": "logs"})
def show_section(section: str):
    return _render_vm_page(section)


@vm_bp.route("/vm/data")
def data():
    conf = get_config()
    vms, summary, error = collect_inventory(conf)
    extra = collect_vm_extra(conf)
    return jsonify({"ok": not bool(error), "vms": vms, "summary": summary, "error": error or "", **extra})


def _runtime_summary(vms):
    return {
        "total": len(vms),
        "running": sum(1 for vm in vms if vm.get("state_class") == "running"),
        "stopped": sum(1 for vm in vms if vm.get("state_class") == "stopped"),
        "paused": sum(1 for vm in vms if vm.get("state_class") == "paused"),
    }


@vm_bp.route("/vm/api/runtime")
def vm_runtime_api():
    conf = get_config()
    try:
        names, err = list_vm_names(conf)
        if err:
            return jsonify({"ok": False, "message": err, "vms": [], "summary": _runtime_summary([])}), 500
        vms = []
        for name in names:
            current_state = get_vm_state(conf, name)
            current_class = state_class(current_state)
            vms.append({
                "name": name,
                "state": current_state,
                "state_class": current_class,
                "running": current_class == "running",
            })
        return jsonify({"ok": True, "vms": vms, "summary": _runtime_summary(vms)})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc), "vms": [], "summary": _runtime_summary([])}), 500


@vm_bp.route("/vm/api/state")
def vm_state_api():
    conf = get_config()
    name = str(request.args.get("name", "")).strip()
    try:
        name = clean_vm_name(name)
        names, err = list_vm_names(conf)
        if err:
            return jsonify({"ok": False, "message": err}), 500
        if name not in names:
            return jsonify({"ok": False, "message": f"VM introuvable : {name}"}), 404
        vm = collect_one_vm(conf, name)
        return jsonify({"ok": not bool(vm.get("error")), "vm": vm, "message": vm.get("error", "")})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500




@vm_bp.route("/vm/api/host-stats")
def vm_host_stats_api():
    try:
        return jsonify({"ok": True, "host": collect_host_live_stats()})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500

@vm_bp.route("/vm/action", methods=["POST"])
def action():
    conf = get_config()
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    name = str(payload.get("name", "")).strip()
    action_name = str(payload.get("action", "")).strip()
    try:
        result, status = do_vm_action(conf, name, action_name)
        return jsonify(result), status
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@vm_bp.route("/vm/api/xml")
def xml():
    conf = get_config()
    name = str(request.args.get("name", "")).strip()
    try:
        name = clean_vm_name(name)
        names, err = list_vm_names(conf)
        if err:
            return jsonify({"ok": False, "message": err}), 500
        if name not in names:
            return jsonify({"ok": False, "message": f"VM introuvable : {name}"}), 404
        rc, xml_text = virsh(conf, "dumpxml", name, timeout=25)
        if rc != 0:
            return jsonify({"ok": False, "message": xml_text.strip() or "dumpxml a echoue"}), 500
        return jsonify({"ok": True, "name": name, "xml": xml_text})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@vm_bp.route("/vm/export_xml", methods=["POST"])
def export_xml():
    conf = get_config()
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    name = str(payload.get("name", "")).strip()
    try:
        result, status = export_vm_xml(conf, name)
        return jsonify(result), status
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@vm_bp.route("/vm/import_xml_server", methods=["POST"])
def vm_import_xml_server():
    conf = get_config()
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    path = str(payload.get("path", "") or "").strip()
    try:
        result, status = import_vm_xml_from_server(conf, path)
        return jsonify(result), status
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@vm_bp.route("/vm/import_xml_upload", methods=["POST"])
def vm_import_xml_upload():
    conf = get_config()
    upload = request.files.get("xml_file")
    if not upload or not upload.filename:
        return jsonify({"ok": False, "message": "Fichier XML manquant."}), 400
    try:
        raw = upload.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            return jsonify({"ok": False, "message": "XML trop volumineux pour une définition VM."}), 413
        xml_text = raw.decode("utf-8", errors="replace")
        result, status = import_vm_xml_text(conf, xml_text, upload.filename)
        return jsonify(result), status
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@vm_bp.route("/vm/export_xml_server", methods=["POST"])
def vm_export_xml_server():
    conf = get_config()
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    name = str(payload.get("name", "") or "").strip()
    folder = str(payload.get("folder", "") or "").strip()
    try:
        result, status = export_vm_xml_to_folder(conf, name, folder)
        return jsonify(result), status
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@vm_bp.route("/vm/export_xml_download")
def vm_export_xml_download():
    conf = get_config()
    name = str(request.args.get("name", "") or "").strip()
    try:
        ok, xml_text, status = dump_vm_xml_text(conf, name)
        if not ok:
            return Response(xml_text, status=status, mimetype="text/plain; charset=utf-8")
        filename = safe_filename(name) + ".xml"
        response = Response(xml_text, mimetype="application/xml; charset=utf-8")
        response.headers["Content-Disposition"] = f"attachment; filename={filename}"
        return response
    except Exception as exc:
        return Response(str(exc), status=500, mimetype="text/plain; charset=utf-8")
