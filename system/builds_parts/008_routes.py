def builds_template_context(conf: Optional[Dict[str, str]] = None, active_section: str = "tarbuild") -> Dict[str, object]:
    """Contexte partagé par les sous-pages Build découpées en iframes."""
    if conf is None:
        conf = get_config()
    active_section = normalize_build_section(active_section)

    setup = build_setup_status(conf)
    if setup.get("required"):
        builds = []
        summary = {
            "total": 0,
            "projects": 0,
            "dockerfiles": 0,
            "tars": 0,
            "registry": 0,
            "registry_missing": 0,
            "platforms_missing": 0,
                "meta_missing": 0,
            "amd64": 0,
            "arm64": 0,
            "default_platforms": normalize_platforms(conf.get("DEFAULT_PLATFORMS", "linux/amd64")),
        }
        warning = "Configuration Build initiale requise : " + " | ".join(setup.get("reasons", []))
        registry_images, registry_total_tags = [], 0
        registry_storage = {"size": 0, "size_h": "0 o"}
    else:
        builds, summary, warning = build_inventory(conf)
        if active_section == "registry":
            registry_images, registry_total_tags = registry_build_repo_payload(conf)
        else:
            registry_images, registry_total_tags = [], 0
        if active_section in {"registry", "system", "options", "info"}:
            registry_storage = registry_storage_payload(conf)
        else:
            registry_storage = {"size": 0, "size_h": "0 o"}

    registry_service = registry_host_status_payload(conf) if active_section in {"system", "options", "info"} else {}
    build_options = build_options_payload(conf)
    build_cache = build_cache_info(conf)
    return {
        "builds": builds,
        "summary": summary,
        "conf": conf,
        "ssh_error": warning,
        "build_setup": setup,
        "registry_images": registry_images,
        "registry_total_repos": len(registry_images),
        "registry_total_tags": registry_total_tags,
        "registry_url": registry_browser_url(conf),
        "registry_message": request.args.get("msg"),
        "registry_message_type": request.args.get("msg_type", "success"),
        "registry_service": registry_service,
        "registry_storage": registry_storage,
        "build_options": build_options,
        "build_cache": build_cache,
    }


def normalize_builds_tab(tab: str) -> str:
    aliases = {
        "": "builds_tar",
        "build": "builds_tar",
        "tar": "builds_tar",
        "registry": "tar_registre",
        "database": "base_de_donnees",
        "registry-browser": "registre",
        "registry-system": "systeme",
        "system": "systeme",
    }
    tab = str(tab or "").strip()
    return aliases.get(tab, tab or "builds_tar")


def handle_builds_form_post(conf: Dict[str, str], redirect_endpoint: str):
    form_action = request.form.get("form_action", "").strip()
    if form_action == "save_database":
        names = request.form.getlist("db_name")
        registry_values = request.form.getlist("db_registry")
        platform_values = request.form.getlist("db_platforms")
        mode_values = request.form.getlist("db_mode")

        if not (len(names) == len(registry_values) == len(platform_values) == len(mode_values)):
            flash("Formulaire base de données incomplet : colonnes de taille différente.", "error")
            return redirect(url_for(redirect_endpoint))

        rows, invalid_names = normalize_database_rows(zip(names, registry_values, platform_values, mode_values))
        if invalid_names:
            flash("Noms Docker invalides : " + ", ".join(invalid_names[:10]), "error")
            return redirect(url_for(redirect_endpoint))

        changed_names = database_changed_names(conf, rows)
        ok, message = write_database_rows(conf, rows)
        if ok:
            if changed_names.get("platforms"):
                remove_build_platform_state_files(conf, changed_names["platforms"])
                clear_registry_import_state(conf, changed_names["platforms"])
            if changed_names.get("mode"):
                clear_registry_import_state(conf, changed_names["mode"])
            cache_ok = refresh_build_cache_silent(get_config(), source="database-form", check_registry=True)
            if not cache_ok:
                message += " Cache Build non rafraîchi automatiquement : lance Mise à jour du cache."
        flash(message, "success" if ok else "error")
        return redirect(url_for(redirect_endpoint))

    if form_action == "save_options":
        ok, lines = save_build_options(conf, request.form)
        for line in lines:
            line_s = str(line or "").strip()
            if not line_s:
                continue
            line_l = line_s.lower()
            is_error_line = line_l.startswith(("erreur", "impossible", "ko", "error")) or " erreur" in line_l or " impossible" in line_l
            flash(line_s, "error" if is_error_line else "success")
        if not lines:
            flash("Options enregistrées." if ok else "Erreur options.", "success" if ok else "error")
        if ok:
            # Options est chargé dans une iframe. Avec target="_top" côté HTML,
            # ce redirect recharge la page Build complète après l'enregistrement,
            # donc plus besoin de faire F5 pour débloquer les autres onglets.
            return redirect(url_for("builds_bp.builds_main_route"))
        return redirect(url_for(redirect_endpoint))

    return None


BUILD_SECTION_ROUTES = {
    "main": "main",
    "build_main": "main",
    "build-main": "main",
    "builds_main": "main",
    "builds_tar": "tarbuild",
    "tarbuild": "tarbuild",
    "tar": "tarbuild",
    "build": "tarbuild",
    "tar_registre": "tar_registry",
    "tar-registry": "tar_registry",
    "tar_registry": "tar_registry",
    "push": "tar_registry",
    "base_de_donnees": "database",
    "database": "database",
    "db": "database",
    "registre": "registry",
    "registry-browser": "registry",
    "registry": "registry",
    "systeme": "system",
    "registry-system": "system",
    "system": "system",
    "info": "info",
    "logs": "logs",
    "options": "options",
}

BUILD_LEGACY_TABS = {
    "main": "builds_main",
    "tarbuild": "builds_tar",
    "tar_registry": "tar_registre",
    "database": "base_de_donnees",
    "registry": "registre",
    "system": "systeme",
    "info": "info",
    "logs": "logs",
    "options": "options",
}


def normalize_build_section(value: str) -> str:
    value = str(value or "").strip()
    return BUILD_SECTION_ROUTES.get(value, BUILD_SECTION_ROUTES.get(value.replace("_", "-"), "tarbuild"))


def render_build_page(conf: Dict[str, str], active_section: str = "tarbuild"):
    active_section = normalize_build_section(active_section)
    setup = build_setup_status(conf)
    if setup.get("required") and active_section not in {"options", "info"}:
        active_section = "options"

    # Important performance : l'affichage des pages Build lit uniquement le cache.
    # Les changements faits hors interface (FileZilla, terminal, suppression directe)
    # ne déclenchent aucun scan ici. Ils sont pris en compte uniquement via
    # « Mise à jour du cache » ou une action effectuée par l'interface.
    context = builds_template_context(conf, active_section)
    context.update(
        active_build_section=active_section,
        active_builds_tab=BUILD_LEGACY_TABS.get(active_section, "builds_tar"),
        build_setup=setup,
    )
    template_name = {
        "main": "build_main.html",
        "tarbuild": "build_tarbuild.html",
        "tar_registry": "build_tar_registry.html",
        "database": "build_database.html",
        "registry": "build_registry.html",
        "system": "build_systeme.html",
        "logs": "build_logs.html",
        "info": "build_info.html",
        "options": "build_options.html",
    }.get(active_section, "build_tarbuild.html")
    return render_template(template_name, **context)


@builds_bp.route("/builds", methods=["GET", "POST"])
@builds_bp.route("/build", methods=["GET", "POST"])
def show():
    conf = get_config()
    if request.method == "POST":
        posted = handle_builds_form_post(conf, "builds_bp.show")
        if posted is not None:
            return posted
    wanted_section = normalize_build_section(request.args.get("tab") or request.args.get("section") or "tarbuild")
    return render_build_page(conf, wanted_section)


@builds_bp.route("/builds/main")
@builds_bp.route("/build/main")
def builds_main_route():
    conf = get_config()
    redirect_response = build_setup_redirect_if_needed(conf, "main")
    if redirect_response is not None:
        return redirect_response
    return render_build_page(conf, "main")


@builds_bp.route("/builds/builds_tar")
@builds_bp.route("/build/tarbuild")
def builds_builds_tar_route():
    conf = get_config()
    redirect_response = build_setup_redirect_if_needed(conf, "builds_tar")
    if redirect_response is not None:
        return redirect_response
    return render_build_page(conf, "tarbuild")


@builds_bp.route("/builds/tar_registre")
@builds_bp.route("/build/tar-registry")
def builds_tar_registre_route():
    conf = get_config()
    redirect_response = build_setup_redirect_if_needed(conf, "tar_registre")
    if redirect_response is not None:
        return redirect_response
    return render_build_page(conf, "tar_registry")


@builds_bp.route("/builds/base_de_donnees", methods=["GET", "POST"])
@builds_bp.route("/build/database", methods=["GET", "POST"])
def builds_base_de_donnees_route():
    conf = get_config()
    redirect_response = build_setup_redirect_if_needed(conf, "base_de_donnees")
    if redirect_response is not None:
        return redirect_response
    if request.method == "POST":
        posted = handle_builds_form_post(conf, "builds_bp.builds_base_de_donnees_route")
        if posted is not None:
            return posted
    return render_build_page(conf, "database")


@builds_bp.route("/builds/registre")
@builds_bp.route("/build/registry")
def builds_registre_route():
    conf = get_config()
    redirect_response = build_setup_redirect_if_needed(conf, "registre")
    if redirect_response is not None:
        return redirect_response
    return render_build_page(conf, "registry")


@builds_bp.route("/builds/systeme")
@builds_bp.route("/build/system")
def builds_systeme_route():
    # La page Système registre est maintenant fusionnée dans Options Build.
    # On garde les anciennes routes comme raccourcis propres, sans dupliquer l'UI.
    return redirect(url_for("builds_bp.builds_options_route"))


@builds_bp.route("/builds/info")
@builds_bp.route("/build/info")
def builds_info_route():
    conf = get_config()
    return render_build_page(conf, "info")


@builds_bp.route("/builds/options", methods=["GET", "POST"])
@builds_bp.route("/build/options", methods=["GET", "POST"])
def builds_options_route():
    conf = get_config()
    if request.method == "POST":
        posted = handle_builds_form_post(conf, "builds_bp.builds_options_route")
        if posted is not None:
            return posted
    return render_build_page(conf, "options")


@builds_bp.route("/builds/logs/log-level", methods=["POST"])
@builds_bp.route("/build/logs/log-level", methods=["POST"])
def build_log_level_route():
    payload = request.get_json(silent=True) or request.form
    log_level = strip_quotes(str(payload.get("log_level", "info"))).strip() or "info"
    allowed = {"debug", "info", "warn", "warning", "error", "fatal", "panic"}
    if log_level not in allowed:
        return jsonify({"ok": False, "error": "Niveau de log invalide."}), 400

    conf = get_config()
    settings = registry_host_settings(conf)
    ok, err = write_kv_file_preserve(settings["human_conf"], {"LOG_LEVEL": log_level})
    if not ok:
        return jsonify({"ok": False, "error": f"Erreur registry.conf : {err}"}), 500
    return jsonify({"ok": True, "message": f"Niveau de log enregistré : {log_level}"})


@builds_bp.route("/builds/logs")
@builds_bp.route("/build/logs")
def builds_logs_route():
    conf = get_config()
    return render_build_page(conf, "logs")




@builds_bp.route("/build/scripts")
def builds_scripts_route():
    # Ancienne entrée de menu : il n'y avait pas de vraie page Scripts,
    # seulement le JS commun. On garde une route propre pour éviter une 404.
    return redirect(url_for("builds_bp.builds_logs_route"))

@builds_bp.route("/builds/registry", methods=["POST"])
@builds_bp.route("/build/registry", methods=["POST"])
def registry_browser_action():
    conf = get_config()
    action = request.form.get("action", "").strip()
    repo = request.form.get("repo", "").strip()
    tag = request.form.get("tag", "").strip()
    wants_json = (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in request.headers.get("Accept", "")
    )

    def answer(message: str, message_type: str = "success", status_code: int = 200):
        if wants_json:
            payload = registry_browser_json_payload(conf, message, message_type)
            response = jsonify(payload)
            response.headers["Cache-Control"] = "no-store"
            return response, status_code
        return redirect(url_for("builds_bp.builds_registre_route", msg=message, msg_type=message_type))

    if action != "delete":
        return answer("Action inconnue.", "error", 400)

    if not repo or not tag:
        return answer("Repo ou tag manquant.", "error", 400)

    digest = registry_get_digest(conf, repo, tag)
    if not digest:
        # Suppression idempotente pour l'interface AJAX : si la popup pointe vers
        # un tag déjà supprimé ou une liste stale, on rafraîchit et on considère
        # l'action terminée au lieu de laisser une confirmation bloquée.
        if wants_json:
            refresh_build_cache_silent(conf, source="registry-delete-missing", check_registry=True)
            return answer(f"Tag deja absent : {repo}:{tag}", "success", 200)
        return answer("Digest introuvable.", "error", 404)

    response = registry_catalog_request(conf, f"{repo}/manifests/{digest}", method="DELETE")
    if response is not None and response.status_code in (200, 202):
        refresh_build_cache_silent(conf, source="registry-delete", check_registry=True)
        if wants_json:
            _REGISTRY_ARCH_CACHE.pop(f"{registry_browser_url(conf)}|{repo}:{tag}", None)
            return answer(f"Tag supprime : {repo}:{tag}", "success", 200)
        return redirect(url_for("builds_bp.show", tab="registry-browser", msg=f"Tag supprimé : {repo}:{tag}", msg_type="success"))

    detail = ""
    if response is not None:
        detail = f" (HTTP {response.status_code})"
    if wants_json:
        return answer(f"Erreur technique suppression{detail}.", "error", 500)
    return redirect(url_for("builds_bp.show", tab="registry-browser", msg=f"Erreur technique suppression{detail}.", msg_type="error"))


@builds_bp.route("/builds/registry_json")
@builds_bp.route("/build/registry_json")
def registry_browser_json():
    conf = get_config()
    response = jsonify(registry_browser_json_payload(conf))
    response.headers["Cache-Control"] = "no-store"
    return response


@builds_bp.route("/builds/database_autofill_json", methods=["POST"])
@builds_bp.route("/build/database_autofill_json", methods=["POST"])
def database_autofill_json():
    conf = get_config()
    # Bouton volontaire : on aligne la base sur les dossiers build, mais on ne
    # supprime pas les TAR ici. Le nettoyage complet dossiers/base/TAR est fait
    # par « Mise à jour du cache » pour garder une action claire et contrôlée.
    ok, message, stats = sync_build_database_and_optional_tars_from_dirs(conf, remove_orphan_tars=False)
    cache_ok = False
    invalidated = int(stats.get("state_removed") or 0) if isinstance(stats, dict) else 0
    names = []
    count = 0
    if isinstance(stats, dict):
        names = list(stats.get("added_db") or []) + list(stats.get("removed_db") or [])
        count = len(names)
    if ok:
        cache_ok = refresh_build_cache_silent(get_config(), source="database-autofill-button", check_registry=True)
        if not cache_ok:
            message += " Cache Build non rafraîchi automatiquement : lance Mise à jour du cache."
    return jsonify({
        "ok": ok,
        "message": message,
        "count": count,
        "names": names,
        "cache_refreshed": cache_ok,
        "state_invalidated": invalidated,
    }), (200 if ok else 500)


@builds_bp.route("/builds/database_json", methods=["POST"])
@builds_bp.route("/build/database_json", methods=["POST"])
def database_json():
    conf = get_config()
    payload = request.get_json(silent=True) or {}
    rows, invalid_names = normalize_database_rows(payload.get("rows", []))

    if invalid_names:
        return jsonify({
            "ok": False,
            "message": "Noms Docker invalides : " + ", ".join(invalid_names[:10]),
        }), 400

    if not rows:
        return jsonify({"ok": False, "message": "Aucune ligne à enregistrer."}), 400

    changed_names = database_changed_names(conf, rows)
    ok, message = write_database_rows(conf, rows)
    cache_ok = False
    invalidated = 0
    if ok:
        if changed_names.get("platforms"):
            invalidated = remove_build_platform_state_files(conf, changed_names["platforms"])
            clear_registry_import_state(conf, changed_names["platforms"])
        if changed_names.get("mode"):
            clear_registry_import_state(conf, changed_names["mode"])
        cache_ok = refresh_build_cache_silent(get_config(), source="database-json", check_registry=True)
        if not cache_ok:
            message += " Cache Build non rafraîchi automatiquement : lance Mise à jour du cache."
    return jsonify({"ok": ok, "message": message, "count": len(rows), "cache_refreshed": cache_ok, "state_invalidated": invalidated}), (200 if ok else 500)


@builds_bp.route("/builds/registry_service_status_json")
@builds_bp.route("/build/registry_service_status_json")
def registry_service_status_json():
    conf = get_config()
    response = jsonify(registry_host_status_payload(conf))
    response.headers["Cache-Control"] = "no-store"
    return response


@builds_bp.route("/builds/status_json")
@builds_bp.route("/build/status_json")
def status_json():
    conf = get_config()
    kind = request.args.get("kind", "").strip().lower()
    name = normalize_item_name(request.args.get("name", ""))

    if not is_valid_name(name):
        return jsonify({"ok": False, "kind": kind, "name": name, "state": "error", "label": "Nom invalide", "message": "Nom Docker invalide.", "can_run": False, "needs_action": False}), 400

    try:
        if kind == "build":
            payload = build_status_for(conf, name)
        elif kind == "registry":
            payload = registry_status_for(conf, name)
        else:
            return jsonify({"ok": False, "message": "Type de statut inconnu."}), 400
    except Exception as exc:
        payload = make_status_payload(kind or "unknown", name, "error", "Erreur", f"Erreur statut : {exc}", True, True)

    # Même quand seul le bouton est recalculé en AJAX, on renvoie aussi
    # l'état du TAR pour rafraîchir la cellule taille/date/sha sans F5.
    try:
        payload["tar"] = tar_ui_payload(conf, name)
    except Exception as exc:
        payload["tar"] = {
            "exists": False,
            "size": None,
            "size_h": "—",
            "mtime": "",
            "sha": False,
            "sha_hash": "",
            "arches": [],
            "arches_label": "aucune",
            "error": str(exc),
        }

    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response




@builds_bp.route("/builds/options/browse_dirs")
@builds_bp.route("/build/options/browse_dirs")
def builds_options_browse_dirs():
    path = request.args.get("path", "/")
    ok, payload, status = builds_browse_list_dirs(path)
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response, status


@builds_bp.route("/builds/options/mkdir_dir", methods=["POST"])
@builds_bp.route("/build/options/mkdir_dir", methods=["POST"])
def builds_options_mkdir_dir():
    payload = request.get_json(silent=True) or request.form or {}
    base_path = builds_browse_normalize_path(str(payload.get("base_path", "/")))
    ok_name, result = builds_safe_new_dir_name(str(payload.get("name", "")))
    if not ok_name:
        return jsonify({"ok": False, "message": result}), 400
    name = result

    if not os.path.exists(base_path):
        return jsonify({"ok": False, "message": f"Dossier courant introuvable : {base_path}"}), 404
    if not os.path.isdir(base_path):
        return jsonify({"ok": False, "message": f"Le chemin courant n'est pas un dossier : {base_path}"}), 400

    new_path = os.path.abspath(os.path.normpath(os.path.join(base_path, name)))
    # Sécurité : le dossier créé doit rester enfant direct du dossier affiché.
    if os.path.dirname(new_path) != os.path.abspath(base_path):
        return jsonify({"ok": False, "message": "Chemin de création refusé."}), 400

    try:
        os.makedirs(new_path, exist_ok=True)
    except PermissionError:
        return jsonify({"ok": False, "message": f"Permission refusée : {new_path}"}), 403
    except OSError as exc:
        return jsonify({"ok": False, "message": f"Impossible de créer : {new_path}\n{exc}"}), 500

    response = jsonify({"ok": True, "path": new_path, "message": f"Dossier créé : {new_path}"})
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Import de dossiers depuis le PC client vers le dossier builds configuré
# ---------------------------------------------------------------------------
def _safe_client_upload_relpath(raw: str) -> Tuple[bool, str]:
    raw = strip_quotes(str(raw or "")).replace("\\", "/").strip().lstrip("/")
    if not raw:
        return False, "Chemin de fichier vide."
    parts = []
    for part in raw.split("/"):
        part = part.strip()
        if not part:
            continue
        if part in {".", ".."}:
            return False, f"Chemin refusé : {raw}"
        if "\x00" in part:
            return False, f"Chemin refusé : {raw}"
        parts.append(part)
    if len(parts) < 2:
        return False, "L'import attend un dossier complet, pas un fichier isolé."
    return True, os.path.join(*parts)


@builds_bp.route("/builds/import_client_dirs", methods=["POST"])
@builds_bp.route("/build/import_client_dirs", methods=["POST"])
def import_client_dirs():
    conf = get_config()
    builds_dir = conf.get("DOCKER_BUILDS_DIR") or conf.get("HOST_BUILDS_DIR") or ""
    if not builds_dir:
        return jsonify({"ok": False, "message": "Dossier builds non configuré."}), 400
    if os.path.exists(builds_dir) and not os.path.isdir(builds_dir):
        return jsonify({"ok": False, "message": f"Le chemin builds n'est pas un dossier : {builds_dir}"}), 400
    try:
        os.makedirs(builds_dir, exist_ok=True)
    except OSError as exc:
        return jsonify({"ok": False, "message": f"Impossible de créer le dossier builds : {builds_dir}\n{exc}"}), 500

    files = request.files.getlist("files[]")
    rel_paths = request.form.getlist("relative_paths[]")
    if not files:
        return jsonify({"ok": False, "message": "Aucun fichier reçu. Choisis un dossier depuis le PC client."}), 400

    saved = 0
    folders = set()
    errors: List[str] = []
    total_bytes = 0

    for idx, upload in enumerate(files):
        raw_rel = rel_paths[idx] if idx < len(rel_paths) and rel_paths[idx] else upload.filename
        ok, rel_or_msg = _safe_client_upload_relpath(raw_rel)
        if not ok:
            errors.append(rel_or_msg)
            continue
        rel_path = rel_or_msg
        dest = os.path.abspath(os.path.normpath(os.path.join(builds_dir, rel_path)))
        builds_root = os.path.abspath(builds_dir)
        if dest != builds_root and not dest.startswith(builds_root.rstrip(os.sep) + os.sep):
            errors.append(f"Chemin refusé : {raw_rel}")
            continue
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            upload.save(dest)
            saved += 1
            folders.add(rel_path.split(os.sep, 1)[0])
            try:
                total_bytes += os.path.getsize(dest)
            except OSError:
                pass
        except OSError as exc:
            errors.append(f"{raw_rel}: {exc}")

    if saved == 0:
        message = "Aucun fichier importé."
        if errors:
            message += "\n" + "\n".join(errors[:10])
        return jsonify({"ok": False, "message": message}), 400

    size_mb = total_bytes / 1024 / 1024
    message = f"Import terminé : {saved} fichier(s), {len(folders)} dossier(s), {size_mb:.1f} Mo vers {builds_dir}."
    if errors:
        message += f"\nAvertissements : {len(errors)} fichier(s) ignoré(s)."
        message += "\n" + "\n".join(errors[:8])
    refresh_build_cache_silent(conf, source="import-client", check_registry=True)
    response = jsonify({"ok": True, "message": message, "saved": saved, "folders": sorted(folders), "bytes": total_bytes})
    response.headers["Cache-Control"] = "no-store"
    return response


@builds_bp.route("/builds/delete_project", methods=["POST"])
@builds_bp.route("/build/delete_project", methods=["POST"])
def delete_project_route():
    conf = get_config()
    payload = request.get_json(silent=True) or request.form or {}
    name = normalize_item_name(str(payload.get("name", "")))
    ok, message, data = delete_build_project_for_name(conf, name)
    if ok:
        refresh_build_cache_silent(conf, source="delete-project", check_registry=True)
    response = jsonify({"ok": ok, "message": message, **data})
    response.headers["Cache-Control"] = "no-store"
    return response, (200 if ok else 400)


@builds_bp.route("/builds/delete_tar", methods=["POST"])
@builds_bp.route("/build/delete_tar", methods=["POST"])
def delete_tar_route():
    conf = get_config()
    payload = request.get_json(silent=True) or request.form or {}
    name = normalize_item_name(str(payload.get("name", "")))
    ok, message, removed = delete_tar_files_for_name(conf, name)
    if ok:
        refresh_build_cache_silent(conf, source="delete-tar", check_registry=True)
    response = jsonify({"ok": ok, "message": message, "name": name, "removed": removed})
    response.headers["Cache-Control"] = "no-store"
    return response, (200 if ok else 400)


@builds_bp.route("/builds/icon/<name>")
@builds_bp.route("/build/icon/<name>")
def icon_file(name: str):
    # Ancienne route conservée pour compatibilité d'URL, mais la base icônes
    # n'est plus utilisée par le module Build.
    abort(404)


@builds_bp.route("/builds/run_start", methods=["POST"])
@builds_bp.route("/build/run_start", methods=["POST"])
def run_start():
    conf = get_config()
    payload = request.get_json(silent=True) or request.form or request.args or {}
    action = str(payload.get("action", "")).strip()
    name = normalize_item_name(str(payload.get("name", "")))
    mode = str(payload.get("mode", "normal")).strip() or "normal"

    error = validate_build_background_request(action, name)
    if error:
        return jsonify({"ok": False, "message": error}), 400

    state = start_build_background_job(conf, action, name, mode)
    response = jsonify({"ok": True, "job": state})
    response.headers["Cache-Control"] = "no-store"
    return response



@builds_bp.route("/build/main/ttyd")
def build_main_ttyd_route():
    conf = get_config()
    log_dir = conf.get("DOCKER_LOG_DIR") or tempfile.gettempdir()
    log_path = os.path.join(log_dir, "build_main.log")
    try:
        os.makedirs(log_dir, exist_ok=True)
        open(log_path, "a", encoding="utf-8").close()
    except Exception:
        pass

    try:
        import terminal as yoleo_terminal
        term_conf = yoleo_terminal.get_config()
        ok, message = yoleo_terminal.ensure_terminal_url_args(term_conf)
        if not ok:
            return jsonify({"ok": False, "message": message or "Terminal ttyd indisponible."}), 500
        fresh_conf = yoleo_terminal.get_config()
        return jsonify({
            "ok": True,
            "title": "Log Build Main",
            "url": yoleo_terminal.ttyd_url_with_args(fresh_conf, ["tail-log", log_path]),
            "log_path": log_path,
        })
    except Exception as exc:
        return jsonify({"ok": False, "message": f"Impossible d'ouvrir le log Build Main dans ttyd : {exc}"}), 500


@builds_bp.route("/builds/run_status")
@builds_bp.route("/build/run_status")
def run_status():
    conf = get_config()
    state = read_build_background_state(conf)
    response = jsonify({"ok": True, "job": state})
    response.headers["Cache-Control"] = "no-store"
    return response


@builds_bp.route("/builds/run_stream")
@builds_bp.route("/build/run_stream")
def run_stream():
    conf = get_config()
    action = request.args.get("action", "").strip()
    name = normalize_item_name(request.args.get("name", ""))
    mode = request.args.get("mode", "normal").strip()

    error = validate_build_background_request(action, name)
    if error:
        abort(400, error)
    if action in BUILD_TMUX_ACTIONS:
        generator = stream_build_tmux_status(conf, action, name, mode)
    else:
        generator = build_background_generator(conf, action, name, mode)

    response = Response(stream_with_context(generator), mimetype="text/plain; charset=utf-8")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Accel-Buffering"] = "no"
    return response


def _builds_cli_main() -> int:
    parser = argparse.ArgumentParser(description="Worker interne builds.py")
    parser.add_argument("--build-worker", action="store_true", help="Lance un build Docker détaché depuis tmux")
    parser.add_argument("--job-id", default="")
    parser.add_argument("--action", default="")
    parser.add_argument("--name", default="")
    parser.add_argument("--mode", default="normal")
    args = parser.parse_args()

    if args.build_worker:
        if args.action not in BUILD_TMUX_ACTIONS:
            print(f"Action build tmux invalide : {args.action}", file=sys.stderr)
            return 2
        return run_build_worker_from_cli(
            job_id=args.job_id,
            action=args.action,
            name=normalize_item_name(args.name),
            mode=args.mode or "normal",
        )

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_builds_cli_main())
