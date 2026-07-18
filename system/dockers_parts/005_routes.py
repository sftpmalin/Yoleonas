DOCKERS_ACTIVE_TABS = {"docker", "stacks", "env", "images", "docker_run", "docker_lan", "yml", "options"}
DOCKERS_TAB_TEMPLATES = {
    "docker": "dockers_containers.html",
    "stacks": "dockers_stacks.html",
    "env": "dockers_env.html",
    "images": "dockers_images.html",
    "docker_run": "dockers_run.html",
    "docker_lan": "dockers_networks.html",
    "yml": "dockers_yml.html",
    "options": "dockers_options.html",
}

DOCKERS_TAB_ALIASES = {
    "containers": "docker",
    "compose": "docker",
    "container": "docker",
    "networks": "docker_lan",
    "network": "docker_lan",
    "image": "images",
    "images_docker": "images",
    "docker-images": "images",
    "docker_run": "docker_run",
    "docker-run": "docker_run",
    "run": "docker_run",
    "lan": "docker_lan",
    "docker-lan": "docker_lan",
    "stack": "stacks",
    "systeme": "options",
    "system": "options",
    "options": "options",
}

# Sous-routes internes de Docker Run.
# Objectif : une vraie URL par panneau, sans onglets JavaScript :
# /docker/run/main, /docker/run/log, /docker/run/edit, /docker/run/info
DOCKER_RUN_ACTIVE_SUBTABS = {"main", "log", "edit", "info"}
DOCKER_RUN_SUBTAB_ALIASES = {
    "library": "main",
    "bibliotheque": "main",
    "bibliothèque": "main",
    "lib": "main",
    "index": "main",
    "home": "main",
    "run": "log",
    "execution": "log",
    "exécution": "log",
    "terminal": "log",
    "exec": "log",
    "logs": "log",
    "editor": "edit",
    "editeur": "edit",
    "éditeur": "edit",
    "conf": "edit",
    "config": "edit",
    "infos": "info",
}


def normalize_dockers_tab(tab: str = "docker") -> str:
    tab = str(tab or "docker").strip().lower().replace("-", "_")
    tab = DOCKERS_TAB_ALIASES.get(tab, tab)
    return tab if tab in DOCKERS_ACTIVE_TABS else "docker"


def normalize_docker_run_subtab(subtab: str = "main") -> str:
    subtab = str(subtab or "main").strip().lower().replace("-", "_")
    subtab = DOCKER_RUN_SUBTAB_ALIASES.get(subtab, subtab)
    return subtab if subtab in DOCKER_RUN_ACTIVE_SUBTABS else "main"


def docker_run_canonical_url(subtab: str = "main", **query: Any) -> str:
    """Construit toujours l'URL canonique /docker/run/<section>."""
    safe_subtab = normalize_docker_run_subtab(subtab)
    clean_query = {str(k): v for k, v in query.items() if v not in (None, "")}
    suffix = f"?{urlencode(clean_query)}" if clean_query else ""
    return f"/docker/run/{safe_subtab}{suffix}"


def docker_tab_canonical_url(tab: str = "docker", **query: Any) -> str:
    """Construit toujours une URL canonique /docker/... sans repasser par /dockers?tab=."""
    safe_tab = normalize_dockers_tab(tab)
    if safe_tab == "docker_run":
        return docker_run_canonical_url(query.pop("subtab", "main"), **query)
    path_map = {
        "docker": "/docker/containers",
        "stacks": "/docker/stacks",
        "env": "/docker/env",
        "images": "/docker/images",
        "docker_lan": "/docker/networks",
        "yml": "/docker/yml",
        "options": "/docker/options",
    }
    clean_query = {str(k): v for k, v in query.items() if v not in (None, "")}
    suffix = f"?{urlencode(clean_query)}" if clean_query else ""
    return f"{path_map.get(safe_tab, '/docker/containers')}{suffix}"


def render_stacks_page(active_tab: str = "docker", active_subtab: str = "", yml_action: str = "", yml_filename: str = ""):
    conf = get_config()
    docker_setup = dockers_setup_status(conf, config_exists=conf.get("DOCKERS_CONFIG_EXISTS") == "1")
    active_tab = normalize_dockers_tab(active_tab)
    if docker_setup.get("required"):
        active_tab = "options"
    active_subtab = normalize_docker_run_subtab(active_subtab) if active_tab == "docker_run" else ""
    stacks = parse_stacks_conf(conf.get("STACKS_FILE", ""))
    env_rows = parse_env_file(conf.get("ENV_FILE", ""))
    images_docker_data = images_docker_payload_from_conf(conf)
    docker_run_data = docker_run_payload_from_conf(conf)

    if active_tab == "docker_run" and active_subtab == "edit":
        requested_edit_filename = request.args.get("filename", "")
        if requested_edit_filename:
            try:
                edit_filename, edit_command = docker_run_read_file(conf, requested_edit_filename)
                docker_run_data["docker_run_edit_filename"] = edit_filename
                docker_run_data["docker_run_edit_command"] = edit_command
            except FileNotFoundError:
                flash(f"❌ Fichier Docker Run introuvable : {docker_run_safe_filename(requested_edit_filename)}", "error")
            except Exception as exc:
                flash(f"❌ Impossible d'ouvrir le fichier Docker Run : {exc}", "error")

    if active_tab == "docker_run" and active_subtab == "log":
        requested_run_filename = request.args.get("filename", "")
        run_filename = docker_run_safe_filename(requested_run_filename)
        should_run = str(request.args.get("run", "")).strip().lower() in {"1", "true", "yes", "on"}

        if should_run and run_filename:
            try:
                started_filename, _log_path = docker_run_start_background(conf, run_filename)
                docker_run_data["docker_run_log_filename"] = started_filename
                docker_run_data["docker_run_log_content"] = docker_run_read_log_tail(conf, started_filename)
                docker_run_data["docker_run_log_running"] = True
            except Exception as exc:
                flash(f"❌ Impossible d'exécuter {run_filename} : {exc}", "error")
                docker_run_data["docker_run_log_filename"] = run_filename
                docker_run_data["docker_run_log_content"] = docker_run_read_log_tail(conf, run_filename)
        else:
            log_filename = run_filename or docker_run_latest_log_filename(conf)
            if log_filename:
                docker_run_data["docker_run_log_filename"] = log_filename
                docker_run_data["docker_run_log_content"] = docker_run_read_log_tail(conf, log_filename)
                state = docker_run_load_state(conf)
                docker_run_data["docker_run_log_running"] = (
                    docker_run_safe_filename(str(state.get("filename", ""))) == log_filename
                    and state.get("status") == "running"
                    and docker_run_pid_running(state.get("pid"))
                )

    docker_lan_data = docker_lan_payload_from_conf(conf)
    yml_data = yml_payload_from_conf(conf)

    if active_tab == "yml":
        yml_data.update({
            "yml_mode": "list",
            "yml_current_name": "",
            "yml_original_name": "",
            "yml_current_content": "",
            "yml_is_new": False,
        })

        requested_yml_filename = yml_filename or request.args.get("file", "")
        requested_new_yml = (
            yml_action == "new"
            or str(request.args.get("new", "")).strip().lower() in {"1", "true", "yes", "on"}
        )

        if requested_new_yml:
            yml_data.update({
                "yml_mode": "edit",
                "yml_current_name": "nouveau.yml",
                "yml_original_name": "",
                "yml_current_content": "services:\n",
                "yml_is_new": True,
            })
        elif requested_yml_filename:
            try:
                yml_path, safe_yml_name = yml_resolve_file_path(conf["YML_FOLDER"], requested_yml_filename)
                with open(yml_path, "r", encoding="utf-8") as handle:
                    yml_content = handle.read()
                yml_data.update({
                    "yml_mode": "edit",
                    "yml_current_name": safe_yml_name,
                    "yml_original_name": safe_yml_name,
                    "yml_current_content": yml_content,
                    "yml_is_new": False,
                })
            except FileNotFoundError:
                flash(f"❌ Fichier YAML introuvable : {requested_yml_filename}", "error")
            except Exception as exc:
                flash(f"❌ Impossible d'ouvrir le fichier YAML : {exc}", "error")

    docker_data = docker_tab_payload()
    template_name = DOCKERS_TAB_TEMPLATES.get(active_tab, "dockers_containers.html")
    return render_template(
        template_name,
        conf=conf,
        docker_setup=docker_setup,
        config_file=CONFIG_FILE,
        stacks=stacks,
        stack_summary=stacks_summary(stacks),
        env_rows=env_rows,
        env_summary=env_summary(env_rows),
        allowed_roots=allowed_roots(conf),
        active_tab=active_tab,
        active_subtab=active_subtab,
        active_docker_run_subtab=active_subtab,
        **docker_data,
        **images_docker_data,
        **docker_run_data,
        **docker_lan_data,
        **yml_data,
    )
@dockers_bp.route("/docker", methods=["GET"])
def stacks_index():
    tab = normalize_dockers_tab(request.args.get("tab", "docker"))
    subtab = request.args.get("subtab", "")
    args = request.args.to_dict(flat=True)
    args.pop("tab", None)
    args.pop("subtab", None)
    if tab == "docker_run" and subtab:
        args["subtab"] = subtab
    return redirect(docker_tab_canonical_url(tab, **args))


@dockers_bp.route("/docker/containers", methods=["GET"])
def docker_containers_page():
    return render_stacks_page("docker")


@dockers_bp.route("/docker/stacks", methods=["GET"])
def docker_stacks_page():
    return render_stacks_page("stacks")


@dockers_bp.route("/docker/yml", methods=["GET"])
def docker_yml_page():
    # URL canonique : la liste reste /docker/yml, l'éditeur devient /docker/yml/edit[/fichier].
    # Les anciens liens ?new=1 et ?file=... sont seulement redirigés vers la route propre.
    if str(request.args.get("new", "")).strip().lower() in {"1", "true", "yes", "on"}:
        return redirect(url_for("dockers_bp.docker_yml_edit_new_page"))

    requested_file = (request.args.get("file", "") or "").strip()
    if requested_file:
        return redirect(url_for("dockers_bp.docker_yml_edit_file_page", filename=yml_safe_filename(requested_file)))

    return render_stacks_page("yml")


@dockers_bp.route("/docker/yml/edit", methods=["GET"])
def docker_yml_edit_new_page():
    return render_stacks_page("yml", yml_action="new")


@dockers_bp.route("/docker/yml/edit/<path:filename>", methods=["GET"])
def docker_yml_edit_file_page(filename: str):
    return render_stacks_page("yml", yml_action="edit", yml_filename=filename)


@dockers_bp.route("/docker/env", methods=["GET"])
def docker_env_page():
    return render_stacks_page("env")


@dockers_bp.route("/docker/images", methods=["GET"])
def docker_images_page():
    return render_stacks_page("images")


@dockers_bp.route("/docker/networks", methods=["GET"])
def docker_networks_page():
    return render_stacks_page("docker_lan")


@dockers_bp.route("/docker/run", methods=["GET"])
def docker_run_main_redirect():
    # La page principale canonique est /docker/run/main.
    requested_subtab = normalize_docker_run_subtab(request.args.get("subtab", "main"))
    args = request.args.to_dict(flat=True)
    args.pop("subtab", None)
    return redirect(docker_run_canonical_url(requested_subtab, **args))


@dockers_bp.route("/docker/run/start", methods=["POST"])
def docker_run_start_api():
    conf = get_config()
    json_payload = request.get_json(silent=True) if request.is_json else {}
    requested_filename = request.form.get("filename", "") or (json_payload or {}).get("filename", "")
    filename = docker_run_safe_filename(requested_filename)

    if not filename:
        return jsonify({"ok": False, "message": "Nom de fichier invalide.", "status": "error"}), 400

    try:
        started_filename, _log_path = docker_run_start_background(conf, filename)
        status = docker_run_status_view(conf, started_filename)
        return jsonify({"ok": True, **status})
    except FileNotFoundError:
        return jsonify({"ok": False, "filename": filename, "message": "Fichier Docker Run introuvable.", "status": "error", "label": "Erreur", "progress": 100, "running": False, "class": "error"}), 404
    except Exception as exc:
        return jsonify({"ok": False, "filename": filename, "message": str(exc), "status": "error", "label": "Erreur", "progress": 100, "running": False, "class": "error"}), 500


@dockers_bp.route("/docker/run/status", methods=["GET"])
def docker_run_status_api():
    conf = get_config()
    filename = docker_run_safe_filename(request.args.get("filename", ""))
    if filename:
        return jsonify({"ok": True, **docker_run_status_view(conf, filename)})

    statuses = {}
    for item in docker_run_read_runs(conf):
        statuses[item["filename"]] = {
            "filename": item["filename"],
            "status": item["run_status"],
            "label": item["run_status_label"],
            "progress": item["run_progress"],
            "running": item["run_running"],
            "class": item["run_status_class"],
        }
    return jsonify({"ok": True, "statuses": statuses})


@dockers_bp.route("/docker/run/<run_subroute>", methods=["GET"])
def docker_run_page(run_subroute: str = "main"):
    # Vraies sous-routes canoniques /docker/run/main|log|edit|info.
    requested_subtab = normalize_docker_run_subtab(request.args.get("subtab", run_subroute))
    if requested_subtab != run_subroute:
        args = request.args.to_dict(flat=True)
        args.pop("subtab", None)
        return redirect(docker_run_canonical_url(requested_subtab, **args))
    return render_stacks_page("docker_run", requested_subtab)


@dockers_bp.route("/docker/options", methods=["GET"])
def docker_options_page():
    return render_stacks_page("options")



@dockers_bp.route("/docker/compose/ttyd", methods=["GET"])
def docker_compose_ttyd():
    try:
        data = _docker_ttyd_start("compose_logs", "compose")
        return jsonify({"ok": True, **data})
    except Exception as exc:
        return jsonify({"ok": False, "message": clean_docker_error(exc)}), 400


@dockers_bp.route("/docker/containers/ttyd", methods=["GET"])
def docker_container_ttyd():
    kind = request.args.get("kind", "logs")
    container = request.args.get("container", "")
    try:
        data = _docker_ttyd_start(kind, container)
        return jsonify({"ok": True, **data})
    except NotFound:
        return jsonify({"ok": False, "message": "Conteneur Docker introuvable."}), 404
    except Exception as exc:
        return jsonify({"ok": False, "message": clean_docker_error(exc)}), 400


@dockers_bp.route("/docker/action", methods=["GET", "POST"])
def stacks_docker():
    if request.method == "GET":
        return redirect(docker_tab_canonical_url("docker"))

    action = request.form.get("action", "").strip()

    # Les actions systemd doivent fonctionner même quand Docker est complètement arrêté.
    if action in DOCKER_SERVICE_ACTIONS:
        payload, status_code = do_docker_service_action(action)
        return jsonify(payload), status_code

    if docker is None:
        return jsonify({
            "status": "error",
            "message": "Module Python docker introuvable. Installe python3-docker ou docker dans le venv.",
        }), 500

    try:
        client = get_docker_client()
    except DockerException as exc:
        msg = f"Impossible de se connecter à Docker : {clean_docker_error(exc)}"
        print(msg)
        return jsonify({"status": "error", "message": msg}), 500

    try:
        c_id = request.form.get("id", "").strip()
        payload, status_code = do_action(client, c_id, action)
        return jsonify(payload), status_code
    finally:
        try:
            client.close()
        except Exception:
            pass


@dockers_bp.route("/docker/images_docker", methods=["POST"])
def stacks_images_docker():
    conf = get_config()
    sync_images_docker_config(conf)

    if docker is None:
        flash("Module Python docker introuvable. Installe le paquet python3-docker ou docker dans le venv.", "error")
        return redirect(docker_tab_canonical_url("images"))

    try:
        client = docker.from_env()
        client.ping()
    except DockerException as exc:
        flash(f"Connexion Docker impossible : {exc}", "error")
        return redirect(docker_tab_canonical_url("images"))

    action = request.form.get("action", "").strip()

    if action == "delete_one":
        image_id = request.form.get("del_img", "").strip()
        image_label = request.form.get("del_tag", "").strip() or image_id[:12]
        if not image_id:
            flash("Aucune image sélectionnée.", "error")
            return redirect(docker_tab_canonical_url("images"))
        try:
            delete_single_image(client, image_id)
            flash(f"✅ Image supprimée : {image_label}", "success")
        except Exception as exc:
            flash(f"❌ Erreur suppression de {image_label} : {exc}", "error")
        return redirect(docker_tab_canonical_url("images"))

    if action == "delete_unused_all":
        removed_count, removed_size, failed = delete_all_unused_images(client)
        if removed_count:
            flash(
                f"✅ {removed_count} image(s) inutilisée(s) supprimée(s) — espace logique libéré : {format_size(removed_size)}",
                "success",
            )
        else:
            flash("ℹ️ Aucune image inutilisée à supprimer.", "success")

        if failed:
            preview = " | ".join(failed[:3])
            if len(failed) > 3:
                preview += f" | +{len(failed) - 3} autre(s) erreur(s)"
            flash(f"⚠️ Certaines suppressions ont échoué : {preview}", "error")
        return redirect(docker_tab_canonical_url("images"))

    if action == "docker_maintenance":
        maintenance_action = request.form.get("maintenance_action", "").strip()
        try:
            deleted_count, reclaimed_size, details = run_docker_maintenance(client, maintenance_action)
            flash(
                f"✅ Nettoyage Docker terminé : {deleted_count} élément(s) traité(s), espace libéré : {format_size(reclaimed_size)} — {details}",
                "success",
            )
        except Exception as exc:
            flash(f"❌ Erreur nettoyage Docker : {exc}", "error")
        return redirect(docker_tab_canonical_url("images"))

    flash("❌ Action Images Docker inconnue.", "error")
    return redirect(docker_tab_canonical_url("images"))



@dockers_bp.route("/docker/docker_run", methods=["POST"])
def stacks_docker_run():
    conf = get_config()
    run_dir = conf["DOCKER_RUN_DIR"]
    next_subtab = normalize_docker_run_subtab(request.form.get("active_subtab", "edit"))
    redirect_filename = ""

    if "save_run" in request.form or "save_run_close" in request.form:
        command = request.form.get("command", "")
        requested_filename = request.form.get("existing_filename", "")
        filename = docker_run_safe_filename(requested_filename) or f"run_{int(time.time())}.conf"
        file_path = os.path.join(run_dir, filename)

        try:
            os.makedirs(run_dir, exist_ok=True)
            with open(file_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(command)
            try:
                os.chmod(file_path, 0o644)
            except OSError:
                pass
            flash(f"✅ Fichier {filename} sauvegardé.", "success")
        except Exception as exc:
            flash(f"❌ Impossible de sauvegarder {filename} : {exc}", "error")

        if "save_run_close" in request.form:
            next_subtab = "main"
        else:
            # Sauvegarder tout court doit rester sur le fichier courant.
            # Avant, on redirigeait vers /docker/run/edit sans filename :
            # l'interface croyait ouvrir un nouveau fichier et ajoutait
            # l'onglet temporaire "Nouveau fichier".
            next_subtab = "edit"
            redirect_filename = filename

    elif "delete_run" in request.form:
        next_subtab = "main"
        requested_filename = request.form.get("filename", "")
        filename = docker_run_safe_filename(requested_filename)

        if not filename:
            flash("❌ Nom de fichier invalide.", "error")
        else:
            file_path = os.path.join(run_dir, filename)
            try:
                os.remove(file_path)
                flash(f"🗑️ Fichier {filename} supprimé.", "success")
            except FileNotFoundError:
                flash(f"❌ Fichier introuvable : {filename}", "error")
            except Exception as exc:
                flash(f"❌ Impossible de supprimer {filename} : {exc}", "error")

    if next_subtab == "edit" and redirect_filename:
        return redirect(docker_run_canonical_url(next_subtab, filename=redirect_filename))
    return redirect(docker_run_canonical_url(next_subtab))


@dockers_bp.route("/docker/exec_docker_run")  # compat ancien lien /docker
def stacks_exec_docker_run():
    requested_filename = request.args.get("filename", "")
    filename = docker_run_safe_filename(requested_filename)
    conf = get_config()

    if not filename:
        return Response("Nom de fichier invalide.\n", mimetype="text/plain", status=400)

    def stream_local() -> Iterator[str]:
        try:
            started_filename, _log_path = docker_run_start_background(conf, filename)
            yield from docker_run_stream_log(conf, started_filename, follow=True)
        except FileNotFoundError:
            yield "❌ Fichier introuvable.\n"
        except Exception as exc:
            yield f"❌ Erreur d'exécution locale : {exc}\n"

    response = Response(stream_with_context(stream_local()), mimetype="text/plain; charset=utf-8")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@dockers_bp.route("/docker/run/log_stream")
def docker_run_log_stream():
    requested_filename = request.args.get("filename", "")
    filename = docker_run_safe_filename(requested_filename)
    conf = get_config()

    if not filename:
        filename = docker_run_latest_log_filename(conf)
    if not filename:
        return Response("Aucun log Docker Run disponible.\n", mimetype="text/plain; charset=utf-8")

    response = Response(stream_with_context(docker_run_stream_log(conf, filename, follow=True)), mimetype="text/plain; charset=utf-8")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@dockers_bp.route("/docker/docker_lan", methods=["POST"])
def stacks_docker_lan():
    conf = get_config()
    state = docker_lan_load_state(conf)
    networks = state.setdefault("networks", {})
    action = request.form.get("action", "").strip()

    if action == "save_network":
        old_name, entry, errors = docker_lan_collect_form()
        apply_mode = request.form.get("apply_mode", "apply")
        recreate = apply_mode == "apply"

        if errors:
            for err in errors:
                flash("❌ " + err, "error")
            return redirect(docker_tab_canonical_url("docker_lan"))

        if old_name and old_name != entry["name"]:
            networks.pop(old_name, None)

        networks[entry["name"]] = entry
        try:
            docker_lan_save_state(conf, state)
            flash(f"✅ Réseau {entry['name']} enregistré dans le JSON.", "success")
        except Exception as exc:
            flash(f"❌ Impossible d'enregistrer le JSON LAN Docker : {exc}", "error")
            return redirect(docker_tab_canonical_url("docker_lan"))

        if entry.get("enabled") and apply_mode == "apply":
            try:
                if old_name and old_name != entry["name"]:
                    client = docker_lan_get_client_or_flash()
                    if client is not None:
                        try:
                            docker_lan_remove_network(client, old_name)
                        finally:
                            try:
                                client.close()
                            except Exception:
                                pass
                docker_lan_apply_network(entry, recreate=recreate)
                flash(f"✅ Réseau Docker appliqué : {entry['name']}", "success")
            except Exception as exc:
                flash(f"❌ Réseau enregistré mais application Docker impossible : {clean_docker_error(exc)}", "error")

        return redirect(docker_tab_canonical_url("docker_lan"))

    name = docker_lan_safe_name(request.form.get("name", ""))
    if not name:
        flash("❌ Nom de réseau invalide.", "error")
        return redirect(docker_tab_canonical_url("docker_lan"))

    if action == "disable_network":
        if name in DOCKER_LAN_PROTECTED_NAMES:
            flash(f"❌ Le réseau système {name} est protégé.", "error")
            return redirect(docker_tab_canonical_url("docker_lan"))
        entry = docker_lan_normalize_entry({**networks.get(name, {}), "name": name, "enabled": False})
        try:
            client = docker_lan_get_client_or_flash()
            if client is not None:
                try:
                    existing_network = docker_lan_get_network(client, name)
                    if existing_network is not None:
                        detected = docker_lan_extract_network_info(existing_network)
                        detected.update({"name": name, "enabled": False, "managed": True})
                        entry = docker_lan_normalize_entry(detected)
                    docker_lan_remove_network(client, name)
                finally:
                    try:
                        client.close()
                    except Exception:
                        pass
            entry["enabled"] = False
            entry["updated_at"] = docker_lan_now()
            networks[name] = entry
            docker_lan_save_state(conf, state)
            flash(f"⏸️ Réseau {name} désactivé et conservé dans le JSON.", "success")
        except Exception as exc:
            flash(f"❌ Impossible de désactiver {name} : {clean_docker_error(exc)}", "error")
        return redirect(docker_tab_canonical_url("docker_lan"))

    if action == "enable_network":
        entry = docker_lan_normalize_entry(networks.get(name, {"name": name, "enabled": True}))
        entry["enabled"] = True
        entry["updated_at"] = docker_lan_now()
        networks[name] = entry
        try:
            docker_lan_save_state(conf, state)
            docker_lan_apply_network(entry, recreate=False)
            flash(f"▶️ Réseau {name} activé/appliqué.", "success")
        except Exception as exc:
            flash(f"❌ Impossible d'activer {name} : {clean_docker_error(exc)}", "error")
        return redirect(docker_tab_canonical_url("docker_lan"))

    if action == "delete_network":
        if name in DOCKER_LAN_PROTECTED_NAMES:
            flash(f"❌ Le réseau système {name} est protégé.", "error")
            return redirect(docker_tab_canonical_url("docker_lan"))
        try:
            client = docker_lan_get_client_or_flash()
            if client is not None:
                try:
                    docker_lan_remove_network(client, name)
                finally:
                    try:
                        client.close()
                    except Exception:
                        pass
            networks.pop(name, None)
            docker_lan_save_state(conf, state)
            flash(f"🗑️ Réseau {name} supprimé du Docker et du JSON.", "success")
        except Exception as exc:
            flash(f"❌ Impossible de supprimer {name} : {clean_docker_error(exc)}", "error")
        return redirect(docker_tab_canonical_url("docker_lan"))

    flash("❌ Action LAN Docker inconnue.", "error")
    return redirect(docker_tab_canonical_url("docker_lan"))




@dockers_bp.route("/docker/yml/read", methods=["GET"])
def stacks_yml_read():
    conf = get_config()
    filename = request.args.get("filename", "")

    try:
        path, safe_name = yml_resolve_file_path(conf["YML_FOLDER"], filename)
        with open(path, "r", encoding="utf-8") as handle:
            return jsonify({
                "ok": True,
                "filename": safe_name,
                "content": handle.read(),
            })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@dockers_bp.route("/docker/yml/save", methods=["POST"])
def stacks_yml_save():
    conf = get_config()
    filename = request.form.get("filename", "")
    original_filename = request.form.get("original_filename", "")
    content = request.form.get("content", "")

    try:
        path, safe_name = yml_resolve_file_path(conf["YML_FOLDER"], filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)

        # Renommage simple : si l'utilisateur a changé le nom dans le champ,
        # l'ancien fichier est supprimé après écriture réussie du nouveau.
        if original_filename:
            old_path, old_safe_name = yml_resolve_file_path(conf["YML_FOLDER"], original_filename)
            if old_safe_name != safe_name and os.path.exists(old_path):
                os.remove(old_path)
                flash(f"✅ Fichier renommé en {safe_name} et enregistré.", "success")
            else:
                flash(f"✅ Fichier {safe_name} enregistré.", "success")
        else:
            flash(f"✅ Fichier {safe_name} enregistré.", "success")

        return redirect(url_for("dockers_bp.docker_yml_edit_file_page", filename=safe_name))

    except Exception as exc:
        flash(f"❌ Erreur sauvegarde YAML : {exc}", "error")

    return redirect(url_for("dockers_bp.docker_yml_page"))


@dockers_bp.route("/docker/yml/import", methods=["POST"])
def stacks_yml_import():
    conf = get_config()
    uploaded_files = request.files.getlist("yaml_files")

    if not uploaded_files:
        flash("⚠️ Aucun fichier YAML sélectionné.", "error")
        return redirect(url_for("dockers_bp.docker_yml_page"))

    imported: List[str] = []
    refused: List[str] = []
    yml_folder = conf["YML_FOLDER"]

    try:
        os.makedirs(yml_folder, exist_ok=True)
    except Exception as exc:
        flash(f"❌ Impossible de créer le dossier YAML {yml_folder} : {exc}", "error")
        return redirect(url_for("dockers_bp.docker_yml_page"))

    for storage in uploaded_files:
        raw_name = (getattr(storage, "filename", "") or "").strip()
        display_name = os.path.basename(raw_name) or "fichier sans nom"

        if not raw_name:
            continue

        if not raw_name.lower().endswith((".yml", ".yaml")):
            refused.append(f"{display_name} : extension refusée")
            continue

        try:
            path, safe_name = yml_resolve_file_path(yml_folder, raw_name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            storage.save(path)
            imported.append(safe_name)
        except Exception as exc:
            refused.append(f"{display_name} : {exc}")

    if imported:
        sample = ", ".join(imported[:8])
        if len(imported) > 8:
            sample += f", +{len(imported) - 8} autre(s)"
        flash(f"✅ Import YAML terminé : {len(imported)} fichier(s) importé(s) dans {yml_folder} : {sample}", "success")

    if refused:
        flash("❌ Fichier(s) non importé(s) : " + " ; ".join(refused[:8]), "error")

    if not imported and not refused:
        flash("⚠️ Aucun fichier YAML utilisable sélectionné.", "error")

    return redirect(url_for("dockers_bp.docker_yml_page"))


@dockers_bp.route("/docker/yml/delete", methods=["POST"])
def stacks_yml_delete():
    conf = get_config()
    filename = request.form.get("filename", "")

    try:
        path, safe_name = yml_resolve_file_path(conf["YML_FOLDER"], filename)
        if os.path.exists(path):
            os.remove(path)
            flash(f"🗑️ {safe_name} supprimé.", "success")
        else:
            flash("⚠️ Fichier YAML introuvable.", "error")
    except Exception as exc:
        flash(f"❌ Erreur suppression YAML : {exc}", "error")

    return redirect(url_for("dockers_bp.docker_yml_page"))


@dockers_bp.route("/docker/yml/validate", methods=["POST"])
def stacks_yml_validate():
    content = request.form.get("content", "")
    result = yml_validate_content(content)
    return jsonify(result), 200 if result.get("ok") else 400


@dockers_bp.route("/docker/yml/files", methods=["GET"])
def stacks_yml_files():
    conf = get_config()
    try:
        return jsonify({"ok": True, "files": yml_list_files(conf["YML_FOLDER"])})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@dockers_bp.route("/snacks", methods=["GET"])
def stacks_legacy_index():
    args = request.args.to_dict(flat=True)
    tab = args.pop("tab", "docker")
    subtab = args.pop("subtab", "")
    if normalize_dockers_tab(tab) == "docker_run" and subtab:
        args["subtab"] = subtab
    return redirect(docker_tab_canonical_url(tab, **args))


# ---------------------------------------------------------------------------
# Navigateur de dossiers pour l'onglet Système Docker
# ---------------------------------------------------------------------------
def dockers_browse_normalize_path(raw: str) -> str:
    raw = strip_quotes(str(raw or "")).strip()
    if not raw:
        raw = "/"
    raw = os.path.expanduser(os.path.expandvars(raw))
    if not os.path.isabs(raw):
        raw = dockers_conf_resolve_path(raw)
    return os.path.abspath(os.path.normpath(raw))


def dockers_browse_nearest_existing_dir(path: str) -> Tuple[str, str]:
    requested = dockers_browse_normalize_path(path)
    current = requested
    while current and current != "/" and not os.path.exists(current):
        parent = os.path.dirname(current.rstrip("/")) or "/"
        if parent == current:
            break
        current = parent
    if not current or not os.path.exists(current) or not os.path.isdir(current):
        current = "/"
    return os.path.abspath(current), requested


@dockers_bp.route("/docker/api/browse_dirs", methods=["GET"])
def dockers_browse_dirs():
    path, requested_path = dockers_browse_nearest_existing_dir(request.args.get("path", "/"))
    warning = ""
    if requested_path != path:
        warning = f"Le dossier demandé n'existe pas encore : {requested_path}\nAffichage du parent existant : {path}"
    dirs: List[Dict[str, str]] = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if entry.name in {".", ".."}:
                        continue
                    dirs.append({"name": entry.name, "path": os.path.join(path, entry.name)})
                except OSError:
                    continue
    except PermissionError:
        return jsonify({"ok": False, "message": f"Permission refusée : {path}", "path": path, "dirs": []}), 403
    except OSError as exc:
        return jsonify({"ok": False, "message": f"Impossible de lire le dossier : {path}\n{exc}", "path": path, "dirs": []}), 500
    dirs.sort(key=lambda item: item["name"].lower())
    parent = os.path.dirname(path.rstrip("/")) or "/"
    payload = {"ok": True, "path": path, "parent": parent, "dirs": dirs, "requested_path": requested_path}
    if warning:
        payload["message"] = warning
    return jsonify(payload)


def dockers_safe_new_dir_name(name: str) -> Tuple[bool, str]:
    name = strip_quotes(str(name or "")).strip()
    if not name:
        return False, "Nom de dossier vide."
    if name in {".", ".."}:
        return False, "Nom de dossier interdit."
    if "/" in name or "\\" in name or "\x00" in name:
        return False, "Le nom ne doit pas contenir de slash."
    if len(name) > 120:
        return False, "Nom de dossier trop long."
    return True, name


@dockers_bp.route("/docker/api/mkdir_dir", methods=["POST"])
def dockers_mkdir_dir():
    payload = request.get_json(silent=True) or {}
    parent = dockers_browse_normalize_path(payload.get("parent", "/"))
    ok, name_or_error = dockers_safe_new_dir_name(payload.get("name", ""))
    if not ok:
        return jsonify({"ok": False, "message": name_or_error}), 400
    if not os.path.isdir(parent):
        return jsonify({"ok": False, "message": f"Parent introuvable : {parent}"}), 404
    target = os.path.abspath(os.path.join(parent, name_or_error))
    if os.path.exists(target) and not os.path.isdir(target):
        return jsonify({"ok": False, "message": f"Un fichier existe déjà avec ce nom : {target}"}), 409
    try:
        os.makedirs(target, exist_ok=True)
    except PermissionError:
        return jsonify({"ok": False, "message": f"Permission refusée : {target}"}), 403
    except OSError as exc:
        return jsonify({"ok": False, "message": f"Création impossible : {exc}"}), 500
    return jsonify({"ok": True, "path": target})

@dockers_bp.route("/docker/config", methods=["POST"])
def stacks_config_save():
    # On sauvegarde le dockers.conf brut, sans réécrire les chemins résolus
    # en absolu. Les clés non affichées dans l'UI restent présentes dans le
    # fichier, mais ne sont plus modifiables depuis la page Système.
    conf = DEFAULT_CONFIG.copy()
    if dockers_conf_exists():
        conf.update(read_kv_file(CONFIG_FILE))

    for key in DOCKERS_EDITABLE_CONFIG_KEYS:
        if key in request.form:
            conf[key] = request.form.get(key, "").strip()

    conf = _dockers_migrate_hidden_legacy_values(conf)

    conf["BACKUP_DIR"] = conf.get("BACKUP_DIR") or DEFAULT_CONFIG["BACKUP_DIR"]
    conf["SYSTEM_LOG_FILE"] = conf.get("SYSTEM_LOG_FILE") or DEFAULT_CONFIG["SYSTEM_LOG_FILE"]
    conf["STACKS_FILE"] = conf.get("STACKS_FILE") or DEFAULT_CONFIG["STACKS_FILE"]
    conf["SYSTEM_STACKS_CONF_FILE"] = conf.get("SYSTEM_STACKS_CONF_FILE") or DEFAULT_CONFIG["SYSTEM_STACKS_CONF_FILE"]

    if not str(conf.get("YML_FOLDER", "")).strip():
        flash("❌ Choisis d'abord le dossier YML.", "error")
        return redirect(docker_tab_canonical_url("options"))
    if not str(conf.get("DOCKER_RUN_DIR", "")).strip():
        flash("❌ Choisis d'abord le dossier Docker Run.", "error")
        return redirect(docker_tab_canonical_url("options"))

    # Champs cachés / internes dérivés automatiquement.
    # L'utilisateur choisit seulement le dossier YML et le dossier Docker Run.
    conf = _dockers_derive_hidden_config_values(conf)
    conf["SYSTEM_STACKS_CONF_FILE"] = conf.get("SYSTEM_STACKS_CONF_FILE") or conf.get("STACKS_FILE", DEFAULT_CONFIG["STACKS_FILE"])

    write_module_conf(CONFIG_FILE, conf)
    runtime_conf = get_config()
    _dockers_make_runtime_dirs(runtime_conf)
    flash("✅ Configuration du module Docker enregistrée. .env, point de départ et répertoire de travail ont été ajustés automatiquement.", "success")
    return redirect(docker_tab_canonical_url(request.form.get("active_tab", "stacks")))


def _dockers_resolve_stack_yml_file(conf: Dict[str, str], yml: str) -> str:
    clean = strip_quotes(str(yml or "")).strip().replace("\\", "/")
    yml_folder = os.path.realpath(normalize_path(conf.get("YML_FOLDER", "")) or ".")
    if os.path.isabs(clean):
        return os.path.realpath(clean)
    return os.path.realpath(os.path.join(yml_folder, clean))


def _dockers_stack_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _dockers_rel_stack_yml(conf: Dict[str, str], path: str) -> str:
    yml_folder = os.path.realpath(normalize_path(conf.get("YML_FOLDER", "")) or ".")
    real_path = os.path.realpath(path)
    try:
        rel = os.path.relpath(real_path, yml_folder)
        if not rel.startswith(".."):
            return rel.replace("\\", "/")
    except Exception:
        pass
    return real_path


def _dockers_known_yml_files(conf: Dict[str, str]) -> List[str]:
    yml_folder = os.path.realpath(normalize_path(conf.get("YML_FOLDER", "")) or ".")
    out: List[str] = []
    if not os.path.isdir(yml_folder):
        return out
    for root, _dirs, files in os.walk(yml_folder):
        for filename in files:
            if filename.lower().endswith((".yml", ".yaml")):
                out.append(os.path.realpath(os.path.join(root, filename)))
    return out


def _dockers_repair_stack_yml(conf: Dict[str, str], yml: str) -> Tuple[str, str]:
    """Retourne (yaml_valide, avertissement).

    Un vieux import peut contenir "nginxproxymanager" ou "Nginx Proxy Manager"
    au lieu de nginxproxymanager.yml. On tente de retrouver le fichier existant
    dans le dossier YAML, puis on ignore uniquement l'entree impossible.
    """
    clean = re.sub(r"[\r\n\0]+", " ", strip_quotes(str(yml or ""))).strip().replace("\\", "/")
    if not clean:
        return "", ""

    roots = allowed_roots(conf)
    full_path = _dockers_resolve_stack_yml_file(conf, clean)
    clean_has_ext = clean.lower().endswith((".yml", ".yaml"))
    if clean_has_ext and is_under_allowed(full_path, roots) and os.path.isfile(full_path):
        return _dockers_rel_stack_yml(conf, full_path), ""

    candidates: List[str] = []
    if not clean_has_ext:
        candidates.extend([clean + ".yml", clean + ".yaml"])
        base_name = os.path.basename(clean)
        token = _dockers_stack_token(os.path.splitext(base_name)[0])
        if token:
            for path in _dockers_known_yml_files(conf):
                stem_token = _dockers_stack_token(os.path.splitext(os.path.basename(path))[0])
                rel_token = _dockers_stack_token(os.path.splitext(_dockers_rel_stack_yml(conf, path))[0])
                if token and token in {stem_token, rel_token}:
                    candidates.append(_dockers_rel_stack_yml(conf, path))

    for candidate in candidates:
        candidate_path = _dockers_resolve_stack_yml_file(conf, candidate)
        if is_under_allowed(candidate_path, roots) and os.path.isfile(candidate_path):
            repaired = _dockers_rel_stack_yml(conf, candidate_path)
            return repaired, f"YAML réparé : {clean} -> {repaired}"

    if clean_has_ext:
        if not is_under_allowed(full_path, roots):
            return "", f"YAML ignoré hors dossier autorisé : {clean}"
        return "", f"YAML ignoré car introuvable : {clean}"
    return "", f"YAML ignoré car aucun fichier .yml/.yaml correspondant n'a été trouvé : {clean}"


def _dockers_sanitize_stacks_for_save(conf: Dict[str, str], stacks: List[StackBlock]) -> Tuple[List[StackBlock], List[str]]:
    warnings: List[str] = []
    cleaned: List[StackBlock] = []
    for stack in normalize_stack_blocks(stacks):
        name = strip_quotes(str(stack.name or "")).strip()
        if not name:
            name = f"stack_{len(cleaned) + 1}"
            warnings.append(f"Stack sans nom renommée en {name}.")
        ymls: List[str] = []
        seen: set[str] = set()
        for raw_yml in stack.ymls or []:
            fixed, warning = _dockers_repair_stack_yml(conf, raw_yml)
            if warning:
                warnings.append(f"{name} : {warning}")
            if fixed and fixed not in seen:
                seen.add(fixed)
                ymls.append(fixed)
        cleaned.append(StackBlock(index=len(cleaned) + 1, name=name, ymls=ymls))
    return cleaned, warnings


def _dockers_stacks_payload(stacks: List[StackBlock]) -> List[Dict[str, Any]]:
    return [{"name": stack.name, "ymls": list(stack.ymls or [])} for stack in stacks]


def _dockers_validate_stack_yml_files(conf: Dict[str, str], stacks: List[StackBlock]) -> str:
    roots = allowed_roots(conf)
    for stack in stacks:
        stack_name = (stack.name or "Stack").strip() or "Stack"
        for yml in stack.ymls:
            clean = strip_quotes(str(yml or "")).strip()
            if not clean:
                continue
            if not clean.lower().endswith((".yml", ".yaml")):
                return f"Le fichier « {clean} » de la stack « {stack_name} » n'est pas un YAML .yml/.yaml."
            full_path = _dockers_resolve_stack_yml_file(conf, clean)
            if not is_under_allowed(full_path, roots):
                return f"Le YAML « {clean} » de la stack « {stack_name} » est hors du dossier YAML autorisé."
            if not os.path.isfile(full_path):
                return f"Le YAML « {clean} » de la stack « {stack_name} » n'existe pas. Utilise Parcourir pour choisir un fichier existant."
    return ""


@dockers_bp.route("/docker/stacks/save", methods=["POST"])
def stacks_stacks_save():
    conf = get_config()
    try:
        stacks, warnings = _dockers_sanitize_stacks_for_save(conf, collect_stacks_from_form())
        if not stacks:
            flash("❌ Aucun stack à enregistrer.", "error")
            return redirect(docker_tab_canonical_url("stacks"))
        validation_error = _dockers_validate_stack_yml_files(conf, stacks)
        if validation_error:
            flash("❌ " + validation_error, "error")
            return redirect(docker_tab_canonical_url("stacks"))
        backup = backup_file(conf["STACKS_FILE"], conf.get("BACKUP_DIR", DEFAULT_CONFIG["BACKUP_DIR"]))
        write_text(conf["STACKS_FILE"], serialize_stacks(stacks))
        for warning in warnings[:8]:
            flash("⚠️ " + warning, "error")
        flash(f"✅ stacks.conf sauvegardé. Backup : {backup}", "success")
    except Exception as exc:
        flash(f"❌ Erreur sauvegarde stacks.conf : {exc}", "error")
    return redirect(docker_tab_canonical_url("stacks"))


@dockers_bp.route("/docker/stacks/save-json", methods=["POST"])  # API navigateur stacks
def stacks_stacks_save_json():
    conf = get_config()
    try:
        payload = request.get_json(silent=True) or {}
        raw_stacks = payload.get("stacks", [])
        if not isinstance(raw_stacks, list):
            return jsonify({"ok": False, "message": "Payload stacks invalide."}), 400

        stacks: List[StackBlock] = []
        for raw_stack in raw_stacks:
            if not isinstance(raw_stack, dict):
                continue
            name = strip_quotes(str(raw_stack.get("name", ""))).strip()
            raw_ymls = raw_stack.get("ymls", [])
            if not isinstance(raw_ymls, list):
                raw_ymls = []
            ymls: List[str] = []
            for raw_yml in raw_ymls:
                clean = strip_quotes(str(raw_yml or "")).strip()
                if not clean:
                    continue
                if "\x00" in clean or "\r" in clean or "\n" in clean:
                    return jsonify({"ok": False, "message": f"Nom YAML invalide : {clean}"}), 400
                ymls.append(clean)
            if not name and not ymls:
                continue
            if not name:
                return jsonify({"ok": False, "message": "Une stack a un nom vide."}), 400
            if "\x00" in name or "\r" in name or "\n" in name:
                return jsonify({"ok": False, "message": f"Nom de stack invalide : {name}"}), 400
            stacks.append(StackBlock(index=len(stacks) + 1, name=name, ymls=ymls))

        stacks, warnings = _dockers_sanitize_stacks_for_save(conf, stacks)

        if not stacks:
            return jsonify({"ok": False, "message": "Aucun stack à enregistrer."}), 400

        validation_error = _dockers_validate_stack_yml_files(conf, stacks)
        if validation_error:
            return jsonify({"ok": False, "message": validation_error}), 400

        backup = backup_file(conf["STACKS_FILE"], conf.get("BACKUP_DIR", DEFAULT_CONFIG["BACKUP_DIR"]))
        write_text(conf["STACKS_FILE"], serialize_stacks(stacks))
        message = "stacks.conf sauvegardé."
        if warnings:
            message += " " + " ".join(warnings[:3])
            if len(warnings) > 3:
                message += f" +{len(warnings) - 3} autre(s) correction(s)."
        return jsonify({
            "ok": True,
            "message": message,
            "path": conf["STACKS_FILE"],
            "backup": backup,
            "summary": stacks_summary(stacks),
            "stacks": _dockers_stacks_payload(stacks),
            "warnings": warnings,
        })
    except Exception as exc:
        return jsonify({"ok": False, "message": f"Erreur sauvegarde stacks.conf : {exc}"}), 500


@dockers_bp.route("/docker/env/save", methods=["POST"])
def stacks_env_save():
    conf = get_config()
    wants_json = (
        request.headers.get("X-Requested-With") == "fetch"
        or "application/json" in (request.headers.get("Accept") or "")
    )
    try:
        rows = collect_env_rows_from_form()
        content, errors = serialize_env_rows(rows)
        if errors:
            message = "\n".join(errors)
            if wants_json:
                return jsonify({"ok": False, "message": message, "errors": errors}), 400
            for err in errors:
                flash("❌ " + err, "error")
            return redirect(docker_tab_canonical_url("env"))
        backup = backup_file(conf["ENV_FILE"], conf.get("BACKUP_DIR", DEFAULT_CONFIG["BACKUP_DIR"]))
        write_text(conf["ENV_FILE"], content)
        if wants_json:
            return jsonify({
                "ok": True,
                "message": ".env sauvegardé.",
                "path": conf["ENV_FILE"],
                "backup": backup,
                "summary": env_summary(rows),
            })
        flash(f"✅ .env sauvegardé. Backup : {backup}", "success")
    except Exception as exc:
        if wants_json:
            return jsonify({"ok": False, "message": f"Erreur sauvegarde .env : {exc}"}), 500
        flash(f"❌ Erreur sauvegarde .env : {exc}", "error")
    return redirect(docker_tab_canonical_url("env"))


@dockers_bp.route("/docker/api/browse", methods=["GET"])
def stacks_browse():
    conf = get_config()
    roots = allowed_roots(conf)
    yml_folder = os.path.realpath(normalize_path(conf.get("YML_FOLDER", "/dockers/yml")))
    requested = normalize_path(request.args.get("path") or yml_folder)
    if requested and not os.path.isabs(requested):
        requested = os.path.join(yml_folder, requested)
    requested = os.path.realpath(requested or yml_folder)
    if not is_under_allowed(requested, roots):
        requested = yml_folder
    if not os.path.isdir(requested):
        return jsonify({"ok": False, "path": requested, "error": "Dossier introuvable ou non accessible.", "items": []}), 404
    items: List[Dict[str, str]] = []
    try:
        if requested not in [os.path.realpath(r) for r in roots]:
            parent = os.path.dirname(requested.rstrip("/")) or "/"
            if is_under_allowed(parent, roots):
                items.append({"type": "parent", "name": "..", "path": parent, "value": ""})
        for name in sorted(os.listdir(requested), key=lambda x: (not os.path.isdir(os.path.join(requested, x)), x.lower())):
            if name.startswith("."):
                continue
            path = os.path.join(requested, name)
            if os.path.isdir(path):
                items.append({"type": "dir", "name": name, "path": path, "value": ""})
                continue
            if name.lower().endswith((".yml", ".yaml")):
                try:
                    value = os.path.relpath(path, yml_folder)
                    if value.startswith(".."):
                        value = path
                except Exception:
                    value = path
                items.append({"type": "file", "name": name, "path": path, "value": value})
    except Exception as exc:
        return jsonify({"ok": False, "path": requested, "error": str(exc), "items": []}), 500
    return jsonify({"ok": True, "path": requested, "items": items})


@dockers_bp.route("/docker/run_stream")  # compat ancien lien /docker
def stacks_run_stream():
    conf = get_config()
    action = system_normalize_compose_action(request.args.get("action", "").strip())
    if not action.startswith("system_stacks_"):
        if action:
            return Response("❌ Action inconnue.\n", status=400, mimetype="text/plain; charset=utf-8")
        generator = stream_system_action_log(conf, "")
    else:
        generator = stream_system_action_log(conf, action)
    response = Response(stream_with_context(generator), mimetype="text/plain; charset=utf-8")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@dockers_bp.route("/docker/run_status")
def stacks_run_status():
    return jsonify(system_compose_log_status(get_config()))
