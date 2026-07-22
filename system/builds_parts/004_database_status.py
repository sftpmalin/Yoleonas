def normalize_database_rows(raw_rows) -> Tuple[List[Tuple[str, str, str, str]], List[str]]:
    rows: List[Tuple[str, str, str, str]] = []
    invalid_names: List[str] = []
    seen = set()

    for item in raw_rows or []:
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            registry_value = str(item.get("registry", "")).strip()
            raw_platform = str(item.get("platforms", "")).strip()
            raw_mode = str(item.get("mode", "0")).strip()
        else:
            try:
                name, registry_value, raw_platform, raw_mode = item
                name = str(name).strip()
                registry_value = str(registry_value).strip()
                raw_platform = str(raw_platform).strip()
                raw_mode = str(raw_mode).strip()
            except Exception:
                invalid_names.append("<ligne invalide>")
                continue

        name = normalize_item_name(name)
        if not is_valid_name(name):
            invalid_names.append(name or "<vide>")
            continue
        if name in seen:
            continue
        seen.add(name)
        platform_value = normalize_platforms(raw_platform) if raw_platform else ""
        mode_value = registry_mode_db_value(raw_mode) if raw_mode else ""
        rows.append((name, registry_value, platform_value, mode_value))

    return rows, invalid_names


def database_changed_names(conf: Dict[str, str], rows: List[Tuple[str, str, str, str]]) -> Dict[str, List[str]]:
    """Retourne les entrées modifiées avant réécriture des mini-bases.

    Le cache Build est volontairement rapide. Quand on change une plateforme
    depuis la base de données, il faut invalider l'état local de build pour que
    Build → TAR repropose immédiatement le build sans devoir cliquer sur
    "Mise à jour du cache".
    """
    old_registry = normalize_named_map(parse_kv(local_read_text(conf["DOCKER_REGISTRY_FILE"])))
    old_platforms = normalize_named_map(parse_kv(local_read_text(conf["DOCKER_PLATFORMS_FILE"])))
    old_modes = normalize_named_map(parse_kv(local_read_text(effective_mode_file(conf))))

    changed = {"registry": [], "platforms": [], "mode": []}
    for name, registry_value, platform_value, mode_value in rows:
        if old_registry.get(name, "").strip() != registry_value.strip():
            changed["registry"].append(name)
        if normalize_platforms(old_platforms.get(name, "")) != normalize_platforms(platform_value):
            changed["platforms"].append(name)
        if registry_mode_db_value(old_modes.get(name, old_modes.get("_default", "0"))) != registry_mode_db_value(mode_value):
            changed["mode"].append(name)
    return changed


def remove_build_platform_state_files(conf: Dict[str, str], names: Iterable[str]) -> int:
    """Supprime seulement l'état plateforme pour les entrées modifiées.

    On ne supprime pas le TAR ni la base : au prochain scan/cache, Python lit le
    TAR réel et décide s'il contient déjà les plateformes demandées.
    """
    removed = 0
    for raw_name in dict.fromkeys(names or []):
        name = normalize_item_name(str(raw_name or ""))
        if not is_valid_name(name):
            continue
        for path in _state_paths(conf, name, ".platforms"):
            try:
                if path and os.path.isfile(path):
                    os.unlink(path)
                    removed += 1
            except OSError:
                pass
    return removed


def write_database_rows(conf: Dict[str, str], rows: List[Tuple[str, str, str, str]]) -> Tuple[bool, str]:
    targets = [
        (conf["DOCKER_REGISTRY_FILE"], 1),
        (conf["DOCKER_PLATFORMS_FILE"], 2),
        (effective_mode_file(conf), 3),
    ]
    errors: List[str] = []
    for path, value_index in targets:
        text = local_read_text(path)
        for row in rows:
            text = update_kv_text(text, row[0], row[value_index])
        ok, out = local_write_text(path, text)
        if not ok:
            errors.append(f"{path}: {out.strip()}")
    if errors:
        return False, "Écriture impossible : " + " | ".join(errors)
    return True, f"Base de données mise à jour : {len(rows)} ligne(s)."


def autofill_database_from_build_dirs(conf: Dict[str, str]) -> Tuple[bool, str, int, List[str]]:
    """Complète automatiquement la mini-base Build depuis les dossiers de build.

    Règle volontairement simple : quand un dossier Docker existe mais qu'il manque
    dans registre.conf, platforms.conf ou mode.conf, on ajoute seulement les
    valeurs absentes. Les réglages déjà saisis par l'utilisateur ne sont jamais
    écrasés ici.

    Par défaut demandé pour les nouveaux dossiers :
      - plateforme : linux/amd64
      - mode registre : 0 (HTTP local)
      - image : REGISTRY_PREFIX/<nom>:latest
    """
    builds_dir = conf.get("DOCKER_BUILDS_DIR") or conf.get("HOST_BUILDS_DIR") or ""
    project_names = list_project_names(builds_dir)
    if not project_names:
        return True, "Aucun dossier build détecté.", 0, []

    registry_path = conf.get("DOCKER_REGISTRY_FILE") or conf.get("HOST_REGISTRY_FILE")
    platforms_path = conf.get("DOCKER_PLATFORMS_FILE") or conf.get("HOST_PLATFORMS_FILE")
    mode_path = effective_mode_file(conf)

    registry_text = local_read_text(registry_path)
    platforms_text = local_read_text(platforms_path)
    mode_text = local_read_text(mode_path)

    registry_map = normalize_named_map(parse_kv(registry_text))
    platforms_map = normalize_named_map(parse_kv(platforms_text))
    modes_map = normalize_named_map(parse_kv(mode_text))

    changed_files = {"registry": False, "platforms": False, "mode": False}
    changed_names: List[str] = []

    for name in project_names:
        changed_this = False

        if not registry_map.get(name, "").strip():
            suggested = suggested_registry(conf, name)
            if suggested:
                registry_text = update_kv_text(registry_text, name, suggested)
                registry_map[name] = suggested
                changed_files["registry"] = True
                changed_this = True

        if not platforms_map.get(name, "").strip():
            # Valeur par défaut volontaire : AMD/Intel x86_64 seulement.
            platforms_text = update_kv_text(platforms_text, name, "linux/amd64")
            platforms_map[name] = "linux/amd64"
            changed_files["platforms"] = True
            changed_this = True

        if not modes_map.get(name, "").strip():
            # 0 = HTTP local / TLS désactivé.
            mode_text = update_kv_text(mode_text, name, "0")
            modes_map[name] = "0"
            changed_files["mode"] = True
            changed_this = True

        if changed_this:
            changed_names.append(name)

    if not changed_names:
        return True, "Base déjà complète pour les dossiers build détectés.", 0, []

    errors: List[str] = []
    if changed_files["registry"]:
        ok, out = local_write_text(registry_path, registry_text)
        if not ok:
            errors.append(f"{registry_path}: {out.strip()}")
    if changed_files["platforms"]:
        ok, out = local_write_text(platforms_path, platforms_text)
        if not ok:
            errors.append(f"{platforms_path}: {out.strip()}")
    if changed_files["mode"]:
        ok, out = local_write_text(mode_path, mode_text)
        if not ok:
            errors.append(f"{mode_path}: {out.strip()}")

    if errors:
        return False, "Remplissage automatique impossible : " + " | ".join(errors), 0, changed_names

    return True, f"Remplissage automatique : {len(changed_names)} dossier(s) complété(s).", len(changed_names), changed_names


def sync_build_database_and_optional_tars_from_dirs(
    conf: Dict[str, str],
    remove_orphan_tars: bool = False,
) -> Tuple[bool, str, Dict[str, object]]:
    """Aligne volontairement les petites bases et, sur demande, les TAR sur les dossiers build.

    Règle validée : le dossier des builds est le point zéro. L'interface normale
    lit le cache pour rester rapide ; cette fonction ne doit donc être appelée
    que par une action explicite de l'UI (Mise à jour du cache / bouton base) ou
    après une suppression faite par l'interface.

    Sécurité : si le dossier racine des builds n'est pas accessible, on ne purge
    rien. Cela évite de vider la base ou les TAR si un montage n'est pas prêt.
    """
    builds_dir = conf.get("DOCKER_BUILDS_DIR") or conf.get("HOST_BUILDS_DIR") or ""
    stats: Dict[str, object] = {
        "projects": 0,
        "added_db": [],
        "removed_db": [],
        "removed_tars": [],
        "removed_tar_files": 0,
        "state_removed": 0,
        "db_files_changed": 0,
    }

    if not builds_dir or not os.path.isdir(builds_dir):
        return False, f"Dossier builds introuvable : {builds_dir}. Aucun nettoyage effectué.", stats

    project_names = list_project_names(builds_dir)
    project_set = set(project_names)
    stats["projects"] = len(project_names)

    registry_path = conf.get("DOCKER_REGISTRY_FILE") or conf.get("HOST_REGISTRY_FILE")
    platforms_path = conf.get("DOCKER_PLATFORMS_FILE") or conf.get("HOST_PLATFORMS_FILE")
    mode_path = effective_mode_file(conf)

    files = {
        "registry": registry_path,
        "platforms": platforms_path,
        "mode": mode_path,
    }
    texts = {key: local_read_text(path) for key, path in files.items() if path}
    maps = {
        "registry": normalize_named_map(parse_kv(texts.get("registry", ""))),
        "platforms": normalize_named_map(parse_kv(texts.get("platforms", ""))),
        "mode": normalize_named_map(parse_kv(texts.get("mode", ""))),
    }

    removed_db: List[str] = []
    changed_files = {"registry": False, "platforms": False, "mode": False}

    all_db_names = sorted(
        {
            name
            for data in maps.values()
            for name in data.keys()
            if name != "_default" and is_valid_name(name)
        },
        key=str.lower,
    )
    for name in all_db_names:
        if name in project_set:
            continue
        for key in ("registry", "platforms", "mode"):
            old = texts.get(key, "")
            new = remove_kv_entry_text(old, name)
            if new != old:
                texts[key] = new
                changed_files[key] = True
        removed_db.append(name)

    added_db: List[str] = []
    for name in project_names:
        changed_this = False
        if not maps["registry"].get(name, "").strip():
            suggested = suggested_registry(conf, name)
            if suggested:
                texts["registry"] = update_kv_text(texts.get("registry", ""), name, suggested)
                maps["registry"][name] = suggested
                changed_files["registry"] = True
                changed_this = True
        if not maps["platforms"].get(name, "").strip():
            texts["platforms"] = update_kv_text(texts.get("platforms", ""), name, "linux/amd64")
            maps["platforms"][name] = "linux/amd64"
            changed_files["platforms"] = True
            changed_this = True
        if not maps["mode"].get(name, "").strip():
            texts["mode"] = update_kv_text(texts.get("mode", ""), name, "0")
            maps["mode"][name] = "0"
            changed_files["mode"] = True
            changed_this = True
        if changed_this:
            added_db.append(name)

    errors: List[str] = []
    db_files_changed = 0
    for key, changed in changed_files.items():
        if not changed:
            continue
        path = files.get(key)
        if not path:
            continue
        ok, out = local_write_text(path, texts.get(key, ""))
        if ok:
            db_files_changed += 1
        else:
            errors.append(f"{path}: {out.strip()}")

    stats["added_db"] = added_db
    stats["removed_db"] = removed_db
    stats["db_files_changed"] = db_files_changed

    affected_names = sorted(set(added_db) | set(removed_db), key=str.lower)
    if affected_names:
        try:
            stats["state_removed"] = int(remove_build_platform_state_files(conf, affected_names) or 0)
        except Exception:
            pass
        try:
            clear_registry_import_state(conf, affected_names)
        except Exception:
            pass

    if remove_orphan_tars:
        tar_dir = conf.get("DOCKER_TAR_DIR") or conf.get("HOST_TAR_DIR") or ""
        if tar_dir and os.path.isdir(tar_dir):
            for tar_name in list_tar_names(tar_dir):
                if tar_name in project_set:
                    continue
                ok, msg, removed = delete_tar_files_for_name(conf, tar_name)
                if ok or removed:
                    stats["removed_tars"].append(tar_name)  # type: ignore[index]
                    stats["removed_tar_files"] = int(stats.get("removed_tar_files") or 0) + int(removed or 0)
                else:
                    errors.append(msg)
        elif tar_dir:
            # Pas bloquant : le cache peut rester basé sur les dossiers/builds.
            pass

    if errors:
        return False, "Synchronisation incomplète : " + " | ".join(errors), stats

    parts = [f"{len(project_names)} dossier(s) build"]
    if added_db:
        parts.append(f"{len(added_db)} entrée(s) base ajoutée(s)")
    if removed_db:
        parts.append(f"{len(removed_db)} entrée(s) base supprimée(s)")
    if remove_orphan_tars:
        parts.append(f"{len(stats['removed_tars'])} TAR orphelin(s) supprimé(s)")
    if len(parts) == 1:
        parts.append("base déjà alignée")
    return True, "Synchronisation dossiers → base/cache : " + ", ".join(parts) + ".", stats


def remove_kv_entry_text(original: str, key: str) -> str:
    """Supprime une clé d'un fichier key=value en conservant les commentaires."""
    key = normalize_item_name(key)
    if not key:
        return original or ""
    output: List[str] = []
    for raw_line in (original or "").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            existing_key = line.split("=", 1)[0].strip()
            if same_item_key(existing_key, key):
                continue
        output.append(raw_line)
    return "\n".join(output).rstrip() + ("\n" if output else "")


def remove_database_entry(conf: Dict[str, str], name: str) -> Tuple[bool, str, int]:
    """Retire un Docker des petites bases registre/platforms/mode."""
    name = normalize_item_name(name)
    targets = [
        conf.get("DOCKER_REGISTRY_FILE", ""),
        conf.get("DOCKER_PLATFORMS_FILE", ""),
        effective_mode_file(conf),
    ]
    errors: List[str] = []
    changed = 0
    for path in dict.fromkeys(p for p in targets if p):
        old = local_read_text(path)
        new = remove_kv_entry_text(old, name)
        if new != old:
            ok, out = local_write_text(path, new)
            if ok:
                changed += 1
            else:
                errors.append(f"{path}: {out.strip()}")
    if errors:
        return False, "Nettoyage base impossible : " + " | ".join(errors), changed
    return True, f"Entrée base supprimée : {name}", changed


def is_safe_child_path(root: str, path: str) -> bool:
    root_real = os.path.realpath(os.path.abspath(root))
    path_real = os.path.realpath(os.path.abspath(path))
    return path_real == root_real or path_real.startswith(root_real.rstrip(os.sep) + os.sep)


def remove_build_state_files(conf: Dict[str, str], name: str) -> int:
    """Supprime les petits états .save_state liés à un build/import."""
    name = normalize_item_name(name)
    paths: List[str] = []
    for suffix in (".context.sha256", ".platforms", ".tar.sha256"):
        paths.extend(_state_paths(conf, name, suffix))
    paths.extend(registry_state_paths(conf, name))
    removed = 0
    for path in dict.fromkeys(paths):
        try:
            if path and os.path.isfile(path):
                os.unlink(path)
                removed += 1
        except OSError:
            pass
    return removed


def delete_tar_files_for_name(conf: Dict[str, str], name: str) -> Tuple[bool, str, int]:
    name = normalize_item_name(name)
    if not is_valid_name(name):
        return False, "Nom Docker invalide.", 0
    tar_dir = conf.get("DOCKER_TAR_DIR") or conf.get("HOST_TAR_DIR") or ""
    if not tar_dir:
        return False, "Dossier TAR non configuré.", 0
    tar_root = os.path.abspath(tar_dir)
    targets = [
        os.path.join(tar_root, f"{name}.tar"),
        os.path.join(tar_root, f"{name}.tar.sha256"),
        os.path.join(tar_root, f"{name}.tar.tmp"),
        os.path.join(tar_root, f"{name}.tmp"),
    ]
    removed = 0
    for target in targets:
        if not is_safe_child_path(tar_root, target):
            return False, f"Chemin TAR refusé : {target}", removed
        try:
            if os.path.isfile(target):
                os.unlink(target)
                removed += 1
        except OSError as exc:
            return False, f"Suppression impossible : {target}\n{exc}", removed
    clear_registry_import_state(conf, [name])
    remove_build_state_files(conf, name)
    if removed == 0:
        return False, f"Aucun TAR à supprimer pour {name}.", 0
    return True, f"TAR supprimé : {name} ({removed} fichier(s)).", removed


def delete_build_project_for_name(conf: Dict[str, str], name: str) -> Tuple[bool, str, Dict[str, object]]:
    """Suppression complète d'un Docker : dossier build + TAR + lignes base."""
    name = normalize_item_name(name)
    if not is_valid_name(name):
        return False, "Nom Docker invalide.", {"name": name}
    builds_dir = conf.get("DOCKER_BUILDS_DIR") or conf.get("HOST_BUILDS_DIR") or ""
    if not builds_dir:
        return False, "Dossier builds non configuré.", {"name": name}
    builds_root = os.path.abspath(builds_dir)
    project_dir = os.path.abspath(os.path.join(builds_root, name))
    if not is_safe_child_path(builds_root, project_dir):
        return False, f"Chemin build refusé : {project_dir}", {"name": name}
    if not os.path.isdir(project_dir):
        return False, f"Dossier build introuvable : {project_dir}", {"name": name, "project_dir": project_dir}
    try:
        shutil.rmtree(project_dir)
    except OSError as exc:
        return False, f"Impossible de supprimer le dossier build : {project_dir}\n{exc}", {"name": name, "project_dir": project_dir}

    db_ok, db_msg, db_changed = remove_database_entry(conf, name)
    tar_ok, tar_msg, tar_removed = delete_tar_files_for_name(conf, name)
    state_removed = remove_build_state_files(conf, name)
    # tar_ok peut être faux simplement parce qu'il n'y avait pas encore de TAR : ce n'est pas bloquant.
    if not db_ok:
        return False, db_msg, {"name": name, "project_dir": project_dir}
    message = f"Docker supprimé : {name}. Dossier build supprimé."
    message += f"\nBase nettoyée : {db_changed} fichier(s)."
    if tar_removed:
        message += f"\nTAR supprimé : {tar_removed} fichier(s)."
    if state_removed:
        message += f"\nÉtats supprimés : {state_removed} fichier(s)."
    return True, message, {"name": name, "project_dir": project_dir, "db_changed": db_changed, "tar_removed": tar_removed, "state_removed": state_removed}



# ---------------------------------------------------------------------------
# Statuts intelligents des boutons Build/TAR -> Registre.
# Objectif : l'affichage de la page reste immédiat. Le navigateur interroge
# ces routes en arrière-plan, et les boutons individuels se grisent au fil de
# l'eau quand le TAR ou le registre est déjà à jour.
# ---------------------------------------------------------------------------

def _first_plain_value(paths: Iterable[str]) -> str:
    for path in paths:
        value = local_read_text(path).strip()
        if value:
            return value.split()[0]
    return ""


def _state_paths(conf: Dict[str, str], name: str, suffix: str) -> List[str]:
    """Compat : état historique dans STATE_DIR, et état possible à côté des TAR."""
    tar_dir = conf.get("DOCKER_TAR_DIR", "").rstrip("/")
    state_dir = (conf.get("STATE_DIR") or os.path.join(conf.get("DOCKER_CONF_DIR", ""), ".save_state")).rstrip("/")
    candidates = []
    if state_dir:
        candidates.append(os.path.join(state_dir, f"{name}{suffix}"))
    if tar_dir:
        candidates.append(os.path.join(tar_dir, f"{name}{suffix}"))
        candidates.append(os.path.join(tar_dir, f"{name}.tar{suffix}"))
    return list(dict.fromkeys(candidates))


def _saved_tar_hash_only(tar_file: str) -> str:
    """Lit le .sha256 existant sans relire tout le TAR de 1 Go ou plus."""
    sha_file = f"{tar_file}.sha256"
    if not os.path.isfile(tar_file) or not os.path.isfile(sha_file):
        return ""
    return saved_sha_hash(sha_file)


def tar_ui_payload(conf: Dict[str, str], name: str) -> Dict[str, object]:
    """État TAR léger renvoyé à l'UI pour rafraîchir les cellules sans F5.

    Important : on évite de recalculer le SHA du TAR ici. Le but est de
    mettre à jour l'affichage après un build : existence, taille, date,
    présence du .sha256 et architectures OCI lues dans index.json.
    """
    name = normalize_item_name(name)
    tar_file = os.path.join(conf.get("DOCKER_TAR_DIR", ""), f"{name}.tar")
    sha_file = f"{tar_file}.sha256"
    exists = os.path.isfile(tar_file)
    size = os.path.getsize(tar_file) if exists else None
    mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(tar_file))) if exists else ""
    sha_exists = os.path.isfile(sha_file)
    sha_hash = saved_sha_hash(sha_file) if sha_exists else ""
    arches: List[str] = []
    if exists:
        try:
            arches = sorted(local_tar_arches(tar_file))
        except Exception:
            arches = []
    return {
        "exists": bool(exists),
        "path": tar_file,
        "size": size,
        "size_h": human_size(size),
        "mtime": mtime,
        "sha": bool(sha_exists),
        "sha_hash": sha_hash,
        "arches": arches,
        "arches_label": format_arches(arches),
    }


def make_status_payload(kind: str, name: str, state: str, label: str, message: str, can_run: bool, needs_action: bool, **extra) -> Dict[str, object]:
    payload: Dict[str, object] = {
        "ok": state != "error",
        "kind": kind,
        "name": name,
        "state": state,
        "label": label,
        "message": message,
        "can_run": bool(can_run),
        "needs_action": bool(needs_action),
    }
    payload.update(extra)
    return payload


def build_status_for(conf: Dict[str, str], name: str) -> Dict[str, object]:
    name = normalize_item_name(name)
    if not is_valid_name(name):
        return make_status_payload("build", name, "error", "Nom invalide", "Nom Docker invalide.", False, False)

    platforms_map = normalize_named_map(parse_kv(local_read_text(conf["DOCKER_PLATFORMS_FILE"])))
    platforms = get_platforms_for(conf, name, platforms_map)
    context_dir = os.path.join(conf["DOCKER_BUILDS_DIR"], name)
    dockerfile = os.path.join(context_dir, "Dockerfile")
    if not os.path.isfile(dockerfile):
        dockerfile = os.path.join(context_dir, "dockerfile")
    tar_file = os.path.join(conf["DOCKER_TAR_DIR"], f"{name}.tar")

    if not os.path.isdir(context_dir):
        return make_status_payload("build", name, "disabled", "Absent", "Dossier de build absent.", False, False, platforms=platforms)
    if not os.path.isfile(dockerfile):
        return make_status_payload("build", name, "disabled", "Sans Dockerfile", "Dockerfile absent.", False, False, platforms=platforms)
    if not os.path.isfile(tar_file):
        return make_status_payload("build", name, "needed", "Build", "Pas de TAR : build nécessaire.", True, True, platforms=platforms)

    saved_tar_hash = _saved_tar_hash_only(tar_file)
    if not saved_tar_hash:
        return make_status_payload("build", name, "needed", "Build", "TAR présent, mais .sha256 absent/illisible : build conseillé.", True, True, platforms=platforms)

    try:
        context_hash = hash_context(context_dir)
    except Exception as exc:
        return make_status_payload("build", name, "error", "Erreur", f"Hash du contexte impossible : {exc}", True, True, platforms=platforms)

    old_context_hash = _first_plain_value(_state_paths(conf, name, ".context.sha256"))
    old_platforms = _first_plain_value(_state_paths(conf, name, ".platforms"))
    old_tar_hash = _first_plain_value(_state_paths(conf, name, ".tar.sha256")) or saved_tar_hash

    if old_context_hash and old_platforms and context_hash == old_context_hash and platforms == old_platforms:
        try:
            platforms_ok = tar_matches_platforms(tar_file, platforms)
        except Exception:
            platforms_ok = False
        if platforms_ok:
            return make_status_payload(
                "build", name, "current", "À jour",
                "Contexte inchangé + plateformes identiques + .tar.sha256 présent.",
                True, False, platforms=platforms, context_hash=context_hash, tar_hash=old_tar_hash,
            )
        return make_status_payload(
            "build", name, "needed", "Build",
            "Build nécessaire : le TAR ne contient pas toutes les plateformes demandées.",
            True, True, platforms=platforms, context_hash=context_hash, tar_hash=saved_tar_hash,
        )

    if not old_context_hash and not old_platforms:
        # Compat première adoption : on lit seulement l'index OCI du TAR, pas les couches.
        try:
            if tar_matches_platforms(tar_file, platforms):
                return make_status_payload(
                    "build", name, "current", "À jour",
                    "TAR OCI existant + .tar.sha256 présent : rien à rebuild pour l'affichage.",
                    True, False, platforms=platforms, context_hash=context_hash, tar_hash=saved_tar_hash,
                )
        except Exception:
            pass

    reasons: List[str] = []
    if old_context_hash and context_hash != old_context_hash:
        reasons.append("contexte modifié")
    elif not old_context_hash:
        reasons.append("état contexte absent")
    if old_platforms and platforms != old_platforms:
        reasons.append("plateformes modifiées")
    elif not old_platforms:
        reasons.append("état plateformes absent")
    if old_tar_hash and saved_tar_hash and old_tar_hash != saved_tar_hash:
        reasons.append("hash TAR différent")

    return make_status_payload(
        "build", name, "needed", "Build",
        "Build nécessaire : " + ", ".join(reasons or ["état incomplet"]),
        True, True, platforms=platforms, context_hash=context_hash, tar_hash=saved_tar_hash,
    )


def _oci_index_bytes(tar_file: str) -> bytes:
    with tarfile.open(tar_file, "r") as tar:
        member = tar.getmember("index.json")
        extracted = tar.extractfile(member)
        if extracted is None:
            return b""
        return extracted.read()


def local_oci_digests(tar_file: str) -> List[str]:
    """Retourne les digests locaux comparables au registre sans relire les layers."""
    raw = _oci_index_bytes(tar_file)
    if not raw:
        return []
    digests: List[str] = ["sha256:" + hashlib.sha256(raw).hexdigest()]
    try:
        index = json.loads(raw.decode("utf-8", errors="replace"))
        for manifest in index.get("manifests", []) or []:
            digest = str((manifest or {}).get("digest", "")).strip()
            if digest and digest not in digests:
                digests.append(digest)
    except Exception:
        pass
    return digests


def platforms_to_arches(platforms: str) -> set:
    """Convertit linux/amd64,linux/arm64 en ensemble {amd64, arm64}."""
    arches = set()
    normalized = normalize_platforms(platforms or "")
    for part in normalized.split(","):
        item = part.strip().lower()
        if not item:
            continue
        if "/" in item:
            arch = item.rsplit("/", 1)[-1].strip()
        else:
            arch = item.strip()
        arch = _clean_arch_name(arch)
        if arch:
            arches.add(arch)
    return arches


def local_tar_arches(tar_file: str) -> set:
    """Lit les architectures réellement présentes dans le TAR OCI local.

    Avec buildx --output type=oci, index.json ne contient pas toujours les
    plateformes directement. Il peut pointer vers un index/manifests dans
    blobs/sha256, puis vers un blob config où se trouve architecture/os.
    C'est ce cas qui faisait rester Build → TAR en mode "Build" pour les TAR
    déjà à jour en linux/amd64,linux/arm64.
    """
    try:
        with tarfile.open(tar_file, "r") as tar:
            try:
                member = tar.getmember("index.json")
                extracted = tar.extractfile(member)
                if extracted is None:
                    return set()
                index = json.loads(extracted.read().decode("utf-8", errors="replace"))
            except Exception:
                return set()

            def loader(digest: str) -> Optional[dict]:
                return _load_oci_blob_json(tar, digest)

            return arches_from_manifest_payload(index, blob_loader=loader)
    except Exception:
        return set()


def format_arches(arches: Iterable[str]) -> str:
    values = sorted({str(item).strip() for item in arches if str(item).strip()})
    return ", ".join(values) if values else "aucune"


def registry_target_repo_tag(target: str) -> Tuple[str, str]:
    """Extrait repo/tag depuis host/repo:tag ou repo:tag."""
    value = (target or "").strip().removeprefix("http://").removeprefix("https://")
    value = value.split("@", 1)[0].strip().strip("/")
    if not value:
        return "", "latest"

    # Si une partie host est présente, on la retire pour l'API Registry /v2/<repo>/...
    if "/" in value:
        first, rest = value.split("/", 1)
        # Docker considère first comme registre si host:port, localhost ou domaine.
        if "." in first or ":" in first or first == "localhost":
            value = rest

    if not value:
        return "", "latest"

    last_slash = value.rfind("/")
    last_colon = value.rfind(":")
    if last_colon > last_slash:
        return value[:last_colon], value[last_colon + 1:] or "latest"
    return value, "latest"


def registry_status_for(conf: Dict[str, str], name: str) -> Dict[str, object]:
    name = normalize_item_name(name)
    if not is_valid_name(name):
        return make_status_payload("registry", name, "error", "Nom invalide", "Nom Docker invalide.", False, False)

    registry = normalize_named_map(parse_kv(local_read_text(conf["DOCKER_REGISTRY_FILE"])))
    platforms_map = normalize_named_map(parse_kv(local_read_text(conf["DOCKER_PLATFORMS_FILE"])))
    target = registry.get(name, "").strip()
    desired_platforms = get_platforms_for(conf, name, platforms_map)
    desired_arches = platforms_to_arches(desired_platforms)
    tar_file = os.path.join(conf["DOCKER_TAR_DIR"], f"{name}.tar")

    if not os.path.isfile(tar_file):
        return make_status_payload("registry", name, "disabled", "Manquant", "TAR absent.", False, False, target=target, desired_arches=sorted(desired_arches))
    if not target:
        return make_status_payload("registry", name, "disabled", "Sans registre", "Ligne registre manquante.", False, False, target=target, desired_arches=sorted(desired_arches))

    saved_tar_hash = _saved_tar_hash_only(tar_file)
    if not saved_tar_hash:
        return make_status_payload("registry", name, "needed", "Envoyer", ".tar.sha256 absent/illisible : envoi autorisé.", True, True, target=target, desired_arches=sorted(desired_arches))

    try:
        tar_arches = local_tar_arches(tar_file)
    except Exception:
        tar_arches = set()

    if tar_arches and desired_arches and tar_arches != desired_arches:
        return make_status_payload(
            "registry", name, "disabled", "Build d'abord",
            f"Le TAR local contient {format_arches(tar_arches)}, mais la base demande {format_arches(desired_arches)}. Rebuild le TAR avant l'envoi registre.",
            False, False, target=target, tar_hash=saved_tar_hash, local_arches=sorted(tar_arches), desired_arches=sorted(desired_arches),
        )

    repo, tag = registry_target_repo_tag(target)
    exists, registry_digest, registry_payload, registry_msg = registry_v2_manifest_status(conf, name, target)
    registry_arches = arches_from_manifest_payload(registry_payload)
    if not exists:
        detail = registry_msg or "vérification impossible"
        return make_status_payload(
            "registry", name, "needed", "Envoyer",
            f"Registre à mettre à jour ou vérification impossible : {detail}.",
            True, True, target=target, repo=repo, tag=tag, tar_hash=saved_tar_hash, local_arches=sorted(tar_arches), desired_arches=sorted(desired_arches), registry_arches=sorted(registry_arches),
        )

    # L'etat "À jour" repose uniquement sur le digest réellement lu dans le
    # registre. Des architectures identiques ne prouvent pas que l'image est
    # identique : le contenu des layers peut avoir changé.
    try:
        local_digests = local_oci_digests(tar_file)
    except Exception:
        local_digests = []

    if registry_digest and registry_digest in local_digests:
        return make_status_payload(
            "registry", name, "current", "À jour",
            "Tag présent dans le registre avec un digest SHA-256 identique au TAR OCI local.",
            True, False, target=target, repo=repo, tag=tag, tar_hash=saved_tar_hash,
            registry_digest=registry_digest, local_digests=local_digests,
            local_arches=sorted(tar_arches), desired_arches=sorted(desired_arches), registry_arches=sorted(registry_arches),
        )

    if desired_arches and registry_arches and registry_arches != desired_arches:
        return make_status_payload(
            "registry", name, "needed", "Envoyer",
            f"Plateformes registre {format_arches(registry_arches)} ≠ plateformes demandées {format_arches(desired_arches)}.",
            True, True, target=target, repo=repo, tag=tag, tar_hash=saved_tar_hash,
            registry_digest=registry_digest, local_digests=local_digests,
            local_arches=sorted(tar_arches), desired_arches=sorted(desired_arches), registry_arches=sorted(registry_arches),
        )

    return make_status_payload(
        "registry", name, "needed", "Envoyer",
        "Tag présent, mais digest SHA-256 différent du TAR OCI local : envoi nécessaire.",
        True, True, target=target, repo=repo, tag=tag, tar_hash=saved_tar_hash,
        registry_digest=registry_digest, local_digests=local_digests,
        local_arches=sorted(tar_arches), desired_arches=sorted(desired_arches), registry_arches=sorted(registry_arches),
    )

# ============================================================
# Onglet Registre intégré dans Build
# Ancien module registry.py fusionné ici : catalogue, tags, arch, suppression.
# ============================================================
