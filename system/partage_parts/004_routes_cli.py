@partage_bp.before_app_request
def partage_nfs_client_startup_hook():
    startup_nfs_client_automount_once()


def state_payload() -> dict:
    cfg = load_samba_config()
    try:
        seed_samba_share_rights_state_if_missing(cfg)
    except Exception:
        pass
    try:
        sync_active_exports_to_nfs_conf()
    except Exception:
        pass
    nfs_entries = read_nfs_entries()
    nfs_conf_file = get_nfs_server_conf_file()
    nfs_linux_file = get_nfs_exports_file()
    services = [service_state(s) for s in (
        *SAMBA_SERVICES,
        SAMBA_APPLY_SERVICE,
        *DISTRO_WSDD_SERVICES,
        *NFS_SERVICES,
    )]
    module_conf = read_partage_config()
    return {
        "version": VERSION,
        "partage_conf": str(get_partage_config_path()),
        "partage": {
            "conf_path": str(get_partage_config_path()),
            "settings": module_conf,
            "settings_rows": partage_settings_rows(),
        },
        "browse_start": str(browse_start_path()),
        "linux_users": enrich_samba_users_for_ui(cfg),
        "samba_owner_users": list_samba_owner_users_for_ui(),
        "samba": to_samba_dict(cfg),
        "nfs": {
            "exports_file": str(nfs_conf_file),
            "config_file": str(nfs_conf_file),
            "linux_exports_file": str(nfs_linux_file),
            "default_client": module_conf.get("NFS_DEFAULT_CLIENT", nfs_default_client()),
            "default_options": normalize_nfs_options(module_conf.get("NFS_DEFAULT_OPTIONS", nfs_default_options())),
            "entries": nfs_entries_view(nfs_entries),
            "active_exports": [
                {"path": p, "client": c, "options": o}
                for p, c, o in active_exports()
            ],
        },
        "services": services,
        "raw_samba": read_text(cfg.conf_path),
        "raw_nfs": read_text(nfs_conf_file),
    }


def logs_for(kind: str, lines: int = 200) -> str:
    lines = max(20, min(int(lines or 200), 1000))
    if kind in {"nfs_client", "nfs-client", "client"}:
        text = tail_file(NFS_CLIENT_LOG_FILE, lines)
        return f"===== {NFS_CLIENT_LOG_FILE} =====\n" + (text or "Aucun log NFS client pour le moment.")

    if kind == "samba":
        units = [*SAMBA_SERVICES, *DISTRO_WSDD_SERVICES, SAMBA_APPLY_SERVICE]
    elif kind == "nfs":
        units = [*NFS_SERVICES]
    else:
        units = [*SAMBA_SERVICES, *DISTRO_WSDD_SERVICES, SAMBA_APPLY_SERVICE, *NFS_SERVICES]

    chunks: list[str] = []
    if command_exists("journalctl"):
        cmd = ["journalctl"]
        for unit in units:
            cmd.extend(["-u", unit])
        cmd.extend(["-n", str(lines), "--no-pager"])
        res = run_capture(cmd)
        if res.stdout.strip():
            chunks.append("$ " + shell_join(cmd) + "\n" + res.stdout)

    # Le log NFS client fait partie de la catégorie Partage, mais il n'est pas
    # dans journalctl : c'est un fichier applicatif dédié à cette route.
    if kind == "all":
        client_log = tail_file(NFS_CLIENT_LOG_FILE, lines)
        if client_log:
            chunks.append(f"===== {NFS_CLIENT_LOG_FILE} =====\n" + client_log)

    # Fallback fichiers Samba classiques.
    if not chunks:
        for path in [Path("/var/log/samba/log.smbd"), Path("/var/log/samba/log.nmbd"), Path("/var/log/syslog")]:
            if path.exists():
                content = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
                chunks.append(f"===== {path} =====\n" + "\n".join(content))
    return "\n\n".join(chunks) if chunks else "Aucun log lisible trouvé."


def make_stream_response(generator: Iterator[str]) -> Response:
    def wrapped() -> Iterator[str]:
        yield f"Partage.py {VERSION}\n"
        yield "=" * 72 + "\n"
        for chunk in generator:
            if chunk:
                yield chunk
                if not chunk.endswith("\n"):
                    yield "\n"
        yield "=" * 72 + "\n"
        yield "FIN\n"

    response = Response(stream_with_context(wrapped()), mimetype="text/plain; charset=utf-8")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Accel-Buffering"] = "no"
    return response


def render_partage_page(section: str = "samba", samba_tab: str = "main"):
    allowed = {"samba", "nfs", "nfs_client", "logs"}
    allowed_samba_tabs = {"main", "user", "shares", "settings", "info"}
    section = section if section in allowed else "samba"
    samba_tab = samba_tab if samba_tab in allowed_samba_tabs else "main"
    if section == "samba":
        template_name = {
            "main": "partage_samba_main.html",
            "user": "partage_samba_user.html",
            "shares": "partage_samba_shares.html",
            "settings": "partage_samba_settings.html",
            "info": "partage_samba_info.html",
        }.get(samba_tab, "partage_samba_main.html")
    else:
        template_name = {
            "nfs": "partage_nfs.html",
            "nfs_client": "partage_nfs_client.html",
            "logs": "partage_logs.html",
        }.get(section, "partage_samba_main.html")
    state = state_payload()
    # Les templates Samba découpés utilisent encore la variable courte `sg`
    # pour le bloc [global]. Quand elle était définie seulement dans
    # partage_base.html, elle restait locale au bloc Jinja parent et n'était
    # plus visible dans les templates enfants comme partage_samba_main.html.
    # On la passe donc explicitement depuis Python pour que /partage/samba
    # et les sous-routes Samba restent stables.
    return render_template(
        template_name,
        state=state,
        section=section,
        samba_tab=samba_tab,
        sg=state.get("samba", {}).get("global", {}),
        nfs_client=nfs_client_payload() if section == "nfs_client" else {},
        nfs_client_exports=[],
        nfs_client_machine=(request.values.get("machine") or "").strip(),
    )


@partage_bp.route("/partage")
def show():
    section = (request.args.get("tab") or "samba").strip().lower()
    return render_partage_page(section)


@partage_bp.route("/partage/samba")
def show_samba():
    return render_partage_page("samba", "main")


@partage_bp.route("/partage/samba/main")
def show_samba_main():
    return render_partage_page("samba", "main")


@partage_bp.route("/partage/samba/user")
def show_samba_user():
    # Les utilisateurs et mots de passe se gèrent désormais au seul endroit
    # logique du NAS : le module Utilisateurs Linux.
    return redirect("/users")


@partage_bp.route("/partage/samba/shares")
def show_samba_shares():
    return render_partage_page("samba", "shares")



@partage_bp.route("/partage/samba/settings")
def show_samba_settings():
    return render_partage_page("samba", "settings")


@partage_bp.route("/partage/samba/info")
def show_samba_info():
    return render_partage_page("samba", "info")


@partage_bp.route("/partage/samba/log")
def show_samba_log():
    return redirect(url_for("partage_bp.show_logs"))


@partage_bp.route("/partage/nfs")
def show_nfs():
    return render_partage_page("nfs")


@partage_bp.route("/partage/nfs-client")
def show_nfs_client():
    machine = (request.args.get("machine") or "").strip()
    exports = [
        {"path": export_path, "mount_path": nfs_client_mountpoint_for(machine, export_path)}
        for export_path in (nfs_client_show_exports(machine) if machine else [])
    ]
    state = state_payload()
    return render_template(
        "partage_nfs_client.html",
        state=state,
        section="nfs_client",
        samba_tab="main",
        sg=state.get("samba", {}).get("global", {}),
        nfs_client=nfs_client_payload(),
        nfs_client_exports=exports,
        nfs_client_machine=machine,
    )


@partage_bp.route("/partage/settings")
def show_settings():
    # L'ancienne page Réglages ne rend plus d'écran : les valeurs techniques
    # restent générées automatiquement par partage.py. Redirection provisoire
    # pour éviter une 404 tant que l'entrée existe encore dans le menu local.
    return redirect(url_for("partage_bp.show_nfs"))


@partage_bp.route("/partage/logs")
def show_logs():
    return render_partage_page("logs")


def nfs_client_wants_json() -> bool:
    return (
        request.headers.get("X-Requested-With", "").lower() == "fetch"
        or "application/json" in request.headers.get("Accept", "").lower()
    )


def nfs_client_action_response(ok: bool, message: str, status: int = 200):
    if nfs_client_wants_json():
        payload = nfs_client_payload()
        payload.update({"ok": bool(ok), "message": message})
        return jsonify(payload), status
    return None


@partage_bp.route("/partage/nfs-client/mount", methods=["POST"])
def nfs_client_mount_route():
    machine = (request.form.get("machine") or "").strip()
    options = (request.form.get("options") or nfs_client_general()["default_options"]).strip()
    exports = request.form.getlist("exports")
    auto_mount = bool(request.form.get("auto_mount"))
    if not machine or not exports:
        append_nfs_client_log("[FORM][ERREUR] Machine ou export manquant pour le montage.")
        response = nfs_client_action_response(False, "Machine ou export manquant.", 400)
        if response:
            return response
        return redirect(url_for("partage_bp.show_nfs_client", machine=machine))
    started = start_nfs_client_mount_queue(
        [
            {"machine": machine, "export": export_path, "options": options, "force": False, "auto_mount": auto_mount}
            for export_path in exports
        ],
        reason=f"formulaire {machine}",
    )
    response = nfs_client_action_response(
        started,
        "Montage lancé en arrière-plan." if started else "Une file de montage est déjà en cours.",
        202 if started else 409,
    )
    if response:
        return response
    return redirect(url_for("partage_bp.show_nfs_client", machine=machine))


@partage_bp.route("/partage/nfs-client/mount-known", methods=["POST"])
def nfs_client_mount_known_route():
    section = (request.form.get("section") or "").strip()
    for item in load_nfs_client_mounts():
        if item.get("section") == section:
            started = start_nfs_client_mount_queue(
                [{
                    "machine": item["machine"],
                    "export": item["export"],
                    "options": item.get("options"),
                    "force": True,
                    "auto_mount": item.get("auto_mount", True),
                }],
                reason=f"montage connu {section}",
            )
            response = nfs_client_action_response(
                started,
                "Montage lancé en arrière-plan." if started else "Une file de montage est déjà en cours.",
                202 if started else 409,
            )
            if response:
                return response
            break
    else:
        append_nfs_client_log(f"[FORM][ERREUR] Montage connu introuvable : {section}")
        response = nfs_client_action_response(False, "Montage connu introuvable.", 404)
        if response:
            return response
    return redirect(url_for("partage_bp.show_nfs_client"))


@partage_bp.route("/partage/nfs-client/unmount", methods=["POST"])
def nfs_client_unmount_route():
    section = (request.form.get("section") or "").strip()
    ok = unmount_nfs_client_section(section, remove_empty_dir=True)
    response = nfs_client_action_response(ok, "Démontage demandé." if ok else "Montage introuvable.", 200 if ok else 404)
    if response:
        return response
    return redirect(url_for("partage_bp.show_nfs_client"))


@partage_bp.route("/partage/nfs-client/delete", methods=["POST"])
def nfs_client_delete_route():
    section = (request.form.get("section") or "").strip()
    ok = delete_nfs_client_mount(section, unmount=True, remove_empty_dir=True)
    response = nfs_client_action_response(ok, "Montage supprimé." if ok else "Montage introuvable.", 200 if ok else 404)
    if response:
        return response
    return redirect(url_for("partage_bp.show_nfs_client"))


@partage_bp.route("/partage/nfs-client/toggle-auto", methods=["POST"])
def nfs_client_toggle_auto_route():
    section = (request.form.get("section") or "").strip()
    enabled = bool(request.form.get("auto_mount"))
    set_nfs_client_auto(section, enabled)
    response = nfs_client_action_response(True, "Auto mis à jour.")
    if response:
        return response
    return redirect(url_for("partage_bp.show_nfs_client"))


@partage_bp.route("/partage/nfs-client/refresh")
def nfs_client_refresh_route():
    started = start_nfs_client_refresh_async(force=True, reason="manuel")
    response = nfs_client_action_response(
        started,
        "Rafraîchissement lancé." if started else "Rafraîchissement déjà en cours.",
        202 if started else 409,
    )
    if response:
        return response
    return redirect(url_for("partage_bp.show_nfs_client"))


@partage_bp.route("/partage/api/nfs-client")
def api_nfs_client():
    return jsonify(nfs_client_payload())


@partage_bp.route("/partage/api/state")
def api_state():
    return jsonify(state_payload())


@partage_bp.route("/partage/api/settings/save", methods=["POST"])
def api_settings_save():
    payload = request.get_json(silent=True) or {}
    settings = payload.get("settings") or {}
    if not isinstance(settings, dict):
        return jsonify({"ok": False, "message": "Réglages invalides."}), 400
    path = get_partage_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = backup_file(path, "settings") if path.exists() else None
        path.write_text(render_partage_config(settings), encoding="utf-8")
        path.chmod(0o644)
        msg = f"partage.conf enregistré : {path}"
        if backup:
            msg += f"\nBackup : {backup}"

        # Les champs techniques cachés ne sont pas envoyés par l'UI.
        # On relit donc le partage.conf final pour récupérer restart_scripts.
        final_settings = read_partage_config()
        launched, restart_msg = schedule_restart_scripts(final_settings)
        if launched:
            msg += "\n" + restart_msg
        else:
            msg += "\n" + restart_msg
            msg += "\nLes nouveaux chemins seront pris en compte au prochain rechargement/redémarrage du service Flask."

        return jsonify({"ok": True, "message": msg, "restart_scheduled": launched})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@partage_bp.route("/partage/api/samba/save", methods=["POST"])
def api_samba_save():
    payload = request.get_json(silent=True) or {}
    cfg = samba_config_from_payload(payload)
    errors = validate_samba_config(cfg)
    if errors:
        return jsonify({"ok": False, "message": "\n".join(errors)}), 400
    try:
        cfg.conf_path.parent.mkdir(parents=True, exist_ok=True)
        backup = backup_file(cfg.conf_path, "save") if cfg.conf_path.exists() else None
        cfg.conf_path.write_text(render_samba_source_conf(cfg), encoding="utf-8")
        cfg.conf_path.chmod(0o600)
        return jsonify({"ok": True, "message": f"samba.conf enregistré : {cfg.conf_path}" + (f"\nBackup : {backup}" if backup else "")})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@partage_bp.route("/partage/api/nfs/save", methods=["POST"])
def api_nfs_save():
    payload = request.get_json(silent=True) or {}
    try:
        entries = nfs_entries_from_payload(payload)
        changed, msg = write_nfs_entries(entries)
        return jsonify({"ok": True, "changed": changed, "message": msg})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@partage_bp.route("/partage/api/raw/save", methods=["POST"])
def api_raw_save():
    payload = request.get_json(silent=True) or {}
    kind = (payload.get("kind") or "").strip()
    content = payload.get("content") or ""
    if kind == "samba":
        path = resolve_samba_conf_path()
        mode = 0o600
    elif kind == "nfs":
        path = get_nfs_server_conf_file()
        mode = 0o644
    else:
        return jsonify({"ok": False, "message": "Type raw inconnu."}), 400
    root_error = None
    if root_error:
        return jsonify({"ok": False, "message": root_error}), 403
    try:
        ok, msg = save_raw_file(path, content, mode=mode)
        return jsonify({"ok": ok, "message": msg})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@partage_bp.route("/partage/api/browse")
def api_browse():
    path = request.args.get("path", "")
    payload = browse_directories(path)
    status = 200 if payload.get("ok") else 400
    return jsonify(payload), status


@partage_bp.route("/partage/api/logs")
def api_logs():
    kind = request.args.get("kind", "all")
    lines = int(request.args.get("lines", partage_setting("LOG_LINES", "200")) or partage_setting("LOG_LINES", "200"))
    return Response(logs_for(kind, lines), mimetype="text/plain; charset=utf-8")


@partage_bp.route("/partage/run_stream")
def run_stream():
    action = request.args.get("action", "").strip()
    cfg = load_samba_config()

    if action == "samba_install":
        return make_stream_response(install_samba_stream(cfg))
    if action == "samba_apply":
        return make_stream_response(apply_and_restart_samba_stream(cfg, update_passwords=False))
    if action == "samba_passwords":
        return make_stream_response(apply_and_restart_samba_stream(cfg, update_passwords=True))
    if action == "samba_start":
        return make_stream_response(start_samba_services_stream(cfg, restart=True))
    if action == "samba_stop":
        return make_stream_response(stop_samba_services_stream())
    if action == "samba_remove":
        return make_stream_response(remove_samba_stream(cfg))
    if action == "nfs_reload":
        return make_stream_response(reload_nfs_stream())
    if action == "nfs_sync_active":
        return make_stream_response(sync_active_nfs_stream())
    if action == "nfs_start":
        return make_stream_response(ensure_nfs_service_stream())
    if action == "nfs_stop":
        return make_stream_response(stop_nfs_stream())

    abort(400, "Action inconnue.")


def cli_samba_apply(conf: str | None) -> int:
    cfg = load_samba_config(resolve_samba_conf_path(conf))
    chunks: list[str] = []
    for chunk in apply_samba_stream(cfg, update_passwords=False):
        chunks.append(chunk)
        sys.stdout.write(chunk)
    return 1 if any("ERREUR" in chunk for chunk in chunks) else 0


def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Module Partage Flask host Samba/NFS")
    parser.add_argument("--samba-apply", action="store_true", help="Appliquer Samba depuis la CLI")
    parser.add_argument("--conf", help="Chemin du samba.conf source")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_cli_args()
    if args.samba_apply:
        raise SystemExit(cli_samba_apply(args.conf))
    print("Ce fichier est surtout un module Flask. Ajoute partage_bp dans app.py.")
