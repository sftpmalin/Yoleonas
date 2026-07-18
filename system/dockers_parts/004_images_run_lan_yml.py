YML_DIR = DEFAULT_CONFIG["YML_FOLDER"]
DOCKER_ROOT_DIR = DEFAULT_CONFIG["DOCKER_ROOT_DIR"]


def sync_images_docker_config(conf: Dict[str, str]) -> None:
    """Alimente l'ancien code Images Docker depuis dockers.conf."""
    global YML_DIR, DOCKER_ROOT_DIR
    YML_DIR = (conf.get("YML_FOLDER") or DEFAULT_CONFIG["YML_FOLDER"]).rstrip("/")
    DOCKER_ROOT_DIR = (conf.get("DOCKER_ROOT_DIR") or DEFAULT_CONFIG["DOCKER_ROOT_DIR"]).rstrip("/")


def images_docker_payload_from_conf(conf: Dict[str, str]) -> Dict[str, Any]:
    sync_images_docker_config(conf)

    # Comme l’onglet Docker, on évite de toucher au socket Docker si le service
    # est arrêté, sinon docker.socket peut réveiller le daemon tout seul.
    service_status = get_docker_service_status()
    if service_status.get("active") is False:
        data = empty_stats_payload()
        data["docker_error"] = ""
        data["docker_notice"] = "Service Docker arrêté."
        return data

    if docker is None:
        data = empty_stats_payload()
        data["docker_error"] = "Module Python docker introuvable. Installe le paquet python3-docker ou docker dans le venv."
        return data
    try:
        client = docker.from_env()
        client.ping()
    except DockerException as exc:
        data = empty_stats_payload()
        data["docker_error"] = clean_docker_error(exc)
        return data

    try:
        data = build_images_payload(client)
        data["docker_error"] = ""
        return data
    except Exception as exc:
        data = empty_stats_payload()
        data["docker_error"] = f"Erreur lecture images : {exc}"
        return data

def format_size(size_bytes):
    size_bytes = int(size_bytes or 0)
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    value = float(size_bytes)
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{int(value)} {units[unit_index]}"
    return f"{value:.2f} {units[unit_index]}"




def format_percent(used, total):
    used = int(used or 0)
    total = int(total or 0)
    if total <= 0:
        return '0%'
    return f"{(used / total) * 100:.0f}%"


def get_docker_root_dir(client):
    try:
        info = client.info() or {}
        root_dir = str(info.get('DockerRootDir') or '').strip()
        if root_dir:
            return root_dir
    except Exception:
        pass
    return DOCKER_ROOT_DIR


def get_docker_img_usage(client):
    """
    Taille réelle du docker.img / montage Docker Unraid.
    Équivalent utile de : df -h /var/lib/docker
    """
    root_dir = get_docker_root_dir(client)
    payload = {
        'docker_root_dir': root_dir,
        'docker_img_total': '0 B',
        'docker_img_used': '0 B',
        'docker_img_free': '0 B',
        'docker_img_percent': '0%',
    }

    try:
        usage = shutil.disk_usage(root_dir)
        payload.update({
            'docker_img_total': format_size(usage.total),
            'docker_img_used': format_size(usage.used),
            'docker_img_free': format_size(usage.free),
            'docker_img_percent': format_percent(usage.used, usage.total),
        })
    except Exception:
        pass

    return payload

def empty_stats_payload():
    return {
        'images': [],
        'groups': {'Inutilisées': []},
        'docker_notice': '',
        'docker_error': '',
        'total': 0,
        'used_count': 0,
        'unused_count': 0,
        'unused_size': '0 B',
        'total_size': '0 B',
        'used_size': '0 B',
        'remaining_size': '0 B',
        'docker_root_dir': DOCKER_ROOT_DIR,
        'docker_img_total': '0 B',
        'docker_img_used': '0 B',
        'docker_img_free': '0 B',
        'docker_img_percent': '0%',
        'docker_system_images_size': '0 B',
        'docker_system_reclaimable': '0 B',
        'containers_size': '0 B',
        'dangling_count': 0,
        'dangling_size': '0 B',
        'build_cache_size': '0 B',
        'build_cache_reclaimable': '0 B',
        'volumes_size': '0 B',
        'unused_volumes_size': '0 B',
        'containers_total_count': 0,
        'containers_running_count': 0,
        'stopped_containers_count': 0,
    }


def safe_int(value, default=0):
    try:
        value = int(value or 0)
        return value if value > 0 else 0
    except Exception:
        return default


def build_docker_df_summary(client):
    """Résumé type docker system df, sans faire échouer la page si Docker refuse une info."""
    summary = {
        'docker_system_images_size': '0 B',
        'docker_system_reclaimable': '0 B',
        'containers_size': '0 B',
        'build_cache_size': '0 B',
        'build_cache_reclaimable': '0 B',
        'volumes_size': '0 B',
        'unused_volumes_size': '0 B',
        'containers_total_count': 0,
        'containers_running_count': 0,
        'stopped_containers_count': 0,
    }

    try:
        df_data = client.df() or {}
    except Exception:
        return summary

    layers_size = safe_int(df_data.get('LayersSize'))

    images = df_data.get('Images') or []
    images_reclaimable = 0
    for item in images:
        # Docker considère surtout les images non liées à un conteneur actif comme récupérables.
        # On garde cette valeur séparée de la suppression sécurisée image-par-image.
        containers_count = safe_int(item.get('Containers'))
        if containers_count <= 0:
            images_reclaimable += safe_int(item.get('Size'))

    build_cache = df_data.get('BuildCache') or []
    build_cache_total = 0
    build_cache_reclaimable = 0
    for item in build_cache:
        size = safe_int(item.get('Size'))
        build_cache_total += size
        if not item.get('InUse'):
            build_cache_reclaimable += size

    volumes = df_data.get('Volumes') or []
    volumes_total = 0
    volumes_unused = 0
    for item in volumes:
        usage = item.get('UsageData') or {}
        size = safe_int(usage.get('Size'))
        ref_count = safe_int(usage.get('RefCount'))
        volumes_total += size
        if ref_count == 0:
            volumes_unused += size

    containers = df_data.get('Containers') or []
    containers_size = 0
    running_count = 0
    stopped_count = 0
    for item in containers:
        containers_size += safe_int(item.get('SizeRw'))
        state = str(item.get('State') or '').lower()
        if state == 'running':
            running_count += 1
        else:
            stopped_count += 1

    summary.update({
        'docker_system_images_size': format_size(layers_size),
        'docker_system_reclaimable': format_size(images_reclaimable),
        'containers_size': format_size(containers_size),
        'build_cache_size': format_size(build_cache_total),
        'build_cache_reclaimable': format_size(build_cache_reclaimable),
        'volumes_size': format_size(volumes_total),
        'unused_volumes_size': format_size(volumes_unused),
        'containers_total_count': len(containers),
        'containers_running_count': running_count,
        'stopped_containers_count': stopped_count,
    })
    return summary


def get_space_reclaimed(response):
    if not isinstance(response, dict):
        return 0
    return safe_int(response.get('SpaceReclaimed'))


def count_deleted_objects(response):
    if not isinstance(response, dict):
        return 0
    keys = ('ContainersDeleted', 'ImagesDeleted', 'NetworksDeleted', 'VolumesDeleted', 'CachesDeleted')
    total_deleted = 0
    for key in keys:
        value = response.get(key) or []
        if isinstance(value, list):
            total_deleted += len(value)
    return total_deleted


def prune_build_cache(client, all_cache=True):
    try:
        return client.api.prune_builds(all=all_cache)
    except TypeError:
        return client.api.prune_builds()


def summarize_prune(label, response):
    deleted = count_deleted_objects(response)
    reclaimed = get_space_reclaimed(response)
    return {
        'label': label,
        'deleted': deleted,
        'reclaimed': reclaimed,
    }


def run_docker_maintenance(client, action):
    """Actions de ménage Docker déclenchées par les boutons de l'interface."""
    results = []

    if action == 'prune_build_cache':
        results.append(summarize_prune('cache build', prune_build_cache(client, all_cache=True)))
    elif action == 'prune_stopped_containers':
        results.append(summarize_prune('conteneurs arrêtés', client.containers.prune()))
    elif action == 'prune_dangling_images':
        results.append(summarize_prune('images dangling', client.images.prune(filters={'dangling': True})))
    elif action == 'prune_unused_networks':
        results.append(summarize_prune('réseaux inutilisés', client.networks.prune()))
    elif action == 'prune_unused_volumes':
        results.append(summarize_prune('volumes inutilisés', client.volumes.prune()))
    elif action == 'prune_system_safe':
        results.append(summarize_prune('conteneurs arrêtés', client.containers.prune()))
        results.append(summarize_prune('réseaux inutilisés', client.networks.prune()))
        results.append(summarize_prune('images dangling', client.images.prune(filters={'dangling': True})))
        results.append(summarize_prune('cache build', prune_build_cache(client, all_cache=True)))
    elif action == 'prune_system_deep':
        results.append(summarize_prune('conteneurs arrêtés', client.containers.prune()))
        results.append(summarize_prune('réseaux inutilisés', client.networks.prune()))
        results.append(summarize_prune('images inutilisées', client.images.prune(filters={'dangling': False})))
        results.append(summarize_prune('volumes inutilisés', client.volumes.prune()))
        results.append(summarize_prune('cache build', prune_build_cache(client, all_cache=True)))
    else:
        raise ValueError('Action de nettoyage inconnue.')

    total_deleted = sum(item['deleted'] for item in results)
    total_reclaimed = sum(item['reclaimed'] for item in results)
    details = ', '.join(
        f"{item['label']} : {item['deleted']} élément(s), {format_size(item['reclaimed'])}"
        for item in results
    )
    return total_deleted, total_reclaimed, details


def normalize_image_name(name):
    return str(name or '').strip().strip('"\'')


_ICON_REGEX = re.compile(r'net\.unraid\.docker\.icon["\']?\s*[:=]\s*["\']?([^"\'\s]+)', re.IGNORECASE)
_IMAGE_REGEX = re.compile(r'^\s*image:\s*([^\s#]+)', re.IGNORECASE | re.MULTILINE)


def get_image_icons_from_yml():
    """Scanne les fichiers compose pour associer Image -> Icône."""
    mapping = {}
    if not os.path.exists(YML_DIR):
        return mapping

    patterns = [
        os.path.join(YML_DIR, '*.yml'),
        os.path.join(YML_DIR, '*.yaml'),
        os.path.join(YML_DIR, '**', '*.yml'),
        os.path.join(YML_DIR, '**', '*.yaml'),
    ]

    files = []
    for pattern in patterns:
        files.extend(glob.glob(pattern, recursive=True))

    for filepath in sorted(set(files)):
        try:
            with open(filepath, 'r', encoding='utf-8', errors='replace') as fh:
                content = fh.read()

            icon_match = _ICON_REGEX.search(content)
            if not icon_match:
                continue

            icon_url = icon_match.group(1).strip()
            image_matches = _IMAGE_REGEX.findall(content)
            for image_name in image_matches:
                image_name = normalize_image_name(image_name)
                if not image_name:
                    continue
                mapping.setdefault(image_name, icon_url)

                if ':' in image_name:
                    repo_only = image_name.rsplit(':', 1)[0]
                    mapping.setdefault(repo_only, icon_url)
                else:
                    mapping.setdefault(f'{image_name}:latest', icon_url)
        except Exception:
            continue

    return mapping


def prettify_stack_name(value):
    text = str(value or '').strip()
    if not text:
        return 'Autre'
    return text.replace('_', ' ').strip().title()


def build_usage_maps(client):
    used_image_ids = set()
    image_stack_map = defaultdict(set)
    image_icon_map = {}

    containers = client.containers.list(all=True)
    for container in containers:
        image_id = container.attrs.get('Image')
        if not image_id:
            continue

        used_image_ids.add(image_id)

        labels = container.attrs.get('Config', {}).get('Labels') or {}
        stack_name = labels.get('com.docker.compose.project') or labels.get('com.docker.stack.namespace')
        image_stack_map[image_id].add(prettify_stack_name(stack_name))

        # Même logique que le tableau Conteneurs : si un conteneur utilise cette
        # image, on reprend son icône Docker/Unraid au lieu du logo générique.
        try:
            icon_url = get_container_icon(labels)
        except Exception:
            icon_url = ''
        if icon_url and image_id not in image_icon_map:
            image_icon_map[image_id] = icon_url

    return used_image_ids, image_stack_map, image_icon_map


DOCKER_IMAGE_DEFAULT_ICON = '/static/logo/docker1.png'


def resolve_icon_for_tags(tags, icon_map, *, used=False, container_icon=''):
    # Image utilisée : priorité à l'icône réelle du conteneur, exactement comme
    # sur la page Conteneurs Docker.
    if used and container_icon:
        return container_icon

    # Image inutilisée : on ne garde pas un vieux logo applicatif ambigu.
    # On affiche seulement l'icône Docker bleue générique.
    if not used:
        return DOCKER_IMAGE_DEFAULT_ICON

    for tag in tags:
        normalized_tag = normalize_image_name(tag)
        if normalized_tag in icon_map:
            return icon_map[normalized_tag]
        if ':' in normalized_tag:
            repo_only = normalized_tag.rsplit(':', 1)[0]
            if repo_only in icon_map:
                return icon_map[repo_only]
        else:
            latest_tag = f'{normalized_tag}:latest'
            if latest_tag in icon_map:
                return icon_map[latest_tag]
    return DOCKER_IMAGE_DEFAULT_ICON


def choose_display_tag(tags):
    cleaned = [normalize_image_name(tag) for tag in tags if normalize_image_name(tag)]
    if not cleaned:
        return '<none>'
    cleaned.sort(key=str.lower)
    return cleaned[0]


def build_images_payload(client):
    icon_map = get_image_icons_from_yml()
    used_image_ids, image_stack_map, image_icon_map = build_usage_maps(client)

    images = []
    groups = defaultdict(list)

    for image in client.images.list():
        full_id = image.id
        tags = sorted(image.tags or [], key=str.lower)
        display_tag = choose_display_tag(tags)
        size_bytes = int(image.attrs.get('Size', 0) or 0)
        size_human = format_size(size_bytes)
        short_id = full_id.replace('sha256:', '')[:12]
        used = full_id in used_image_ids
        stacks = sorted(stack for stack in image_stack_map.get(full_id, set()) if stack)
        stack_label = ', '.join(stacks) if stacks else ('Inutilisée' if not used else 'Autre')

        if used:
            group_name = stacks[0] if len(stacks) == 1 else ('Multi-stack' if len(stacks) > 1 else 'Autre')
        else:
            group_name = 'Inutilisées'

        item = {
            'id': full_id,
            'short_id': short_id,
            'tags': display_tag,
            'all_tags': tags,
            'size': size_human,
            'size_bytes': size_bytes,
            'used': used,
            'icon': resolve_icon_for_tags(tags or [display_tag], icon_map, used=used, container_icon=image_icon_map.get(full_id, '')),
            'stack': stack_label,
            'group_name': group_name,
            'is_dangling': display_tag == '<none>',
        }
        images.append(item)
        groups[group_name].append(item)

    images.sort(key=lambda item: (
        0 if not item['used'] else 1,
        item['tags'].lower(),
        item['short_id'].lower(),
    ))

    ordered_groups = {}
    used_group_names = sorted(name for name in groups.keys() if name != 'Inutilisées')
    for group_name in used_group_names:
        ordered_groups[group_name] = sorted(groups[group_name], key=lambda item: item['tags'].lower())
    ordered_groups['Inutilisées'] = sorted(groups.get('Inutilisées', []), key=lambda item: item['tags'].lower())

    total = len(images)
    total_size_bytes = sum(image['size_bytes'] for image in images)
    used_images = [image for image in images if image['used']]
    used_count = len(used_images)
    used_size_bytes = sum(image['size_bytes'] for image in used_images)
    unused_images = [image for image in images if not image['used']]
    unused_count = len(unused_images)
    unused_size_bytes = sum(image['size_bytes'] for image in unused_images)
    dangling_images = [image for image in images if image['is_dangling']]
    dangling_size_bytes = sum(image['size_bytes'] for image in dangling_images)

    df_summary = build_docker_df_summary(client)
    docker_system_images_size = df_summary.get('docker_system_images_size') or '0 B'
    if docker_system_images_size == '0 B' and total_size_bytes:
        docker_system_images_size = format_size(total_size_bytes)

    data = {
        'images': images,
        'groups': ordered_groups,
        'total': total,
        'used_count': used_count,
        'unused_count': unused_count,
        'unused_size': format_size(unused_size_bytes),
        # Taille officielle des couches Docker, proche de docker system df.
        # On évite ici la simple addition des tailles par image, qui double-compte les couches partagées.
        'total_size': docker_system_images_size,
        # Ces deux valeurs restent une indication logique pour l'état image-par-image.
        'used_size': format_size(used_size_bytes),
        'remaining_size': format_size(unused_size_bytes),
        'dangling_count': len(dangling_images),
        'dangling_size': format_size(dangling_size_bytes),
    }
    data.update(df_summary)
    data.update(get_docker_img_usage(client))
    return data


def delete_single_image(client, image_id):
    client.images.remove(image=image_id, force=False, noprune=False)


def delete_all_unused_images(client):
    data = build_images_payload(client)
    unused_images = [image for image in data['images'] if not image['used']]

    if not unused_images:
        return 0, 0, []

    removed_count = 0
    removed_size = 0
    failed = []

    for image in unused_images:
        try:
            client.images.remove(image=image['id'], force=False, noprune=False)
            removed_count += 1
            removed_size += image['size_bytes']
        except Exception as exc:
            failed.append(f"{image['tags']} ({image['short_id']}) : {exc}")

    return removed_count, removed_size, failed








# ---------------------------------------------------------------------------
# Onglet Docker Run intégré depuis l'ancien module docker_run.py
# ---------------------------------------------------------------------------
DOCKER_RUN_DEFAULT_ICON_URL = "https://cdn-icons-png.flaticon.com/512/919/919853.png"


def docker_run_safe_filename(raw_name: str) -> str:
    name = (raw_name or "").strip().replace("\\", "/")
    name = name.split("/")[-1].strip()
    name = re.sub(r"\s+", " ", name)

    if not name:
        return ""

    name = re.sub(r"[^A-Za-z0-9._\- ]", "_", name)
    name = name.strip(" .")

    if not name:
        return ""

    if not name.endswith(".conf"):
        name += ".conf"
    return name


def docker_run_extract_image_reference(content: str) -> str:
    if not content:
        return ""

    # Priorité à une vraie image docker de type repo/image:tag.
    image_match = re.findall(r"(?<![=\w./-])([A-Za-z0-9._/-]+:[A-Za-z0-9._-]+)(?![\w./-])", content)
    if image_match:
        return image_match[-1].strip("\"'")

    return ""


def docker_run_get_icons_map(conf: Dict[str, str]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    yml_dir = (conf.get("YML_FOLDER") or nas_root_path("yml")).strip()
    if not yml_dir or not os.path.exists(yml_dir):
        return mapping

    files = (
        glob.glob(os.path.join(yml_dir, "*.yml"))
        + glob.glob(os.path.join(yml_dir, "*.yaml"))
        + glob.glob(os.path.join(yml_dir, "**", "*.yml"), recursive=True)
        + glob.glob(os.path.join(yml_dir, "**", "*.yaml"), recursive=True)
    )

    for filepath in sorted(set(files)):
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read()

            icon_match = re.search(
                r"net\.unraid\.docker\.icon[\"']?\s*[:=]\s*[\"']?([^\"\s']+)",
                content,
                re.IGNORECASE,
            )
            image_match = re.search(r"^\s*image\s*:\s*([^\s]+)", content, re.IGNORECASE | re.MULTILINE)

            if not (icon_match and image_match):
                continue

            icon_url = icon_match.group(1).strip()
            full_image = image_match.group(1).strip('"\'')
            short_name = full_image.split("/")[-1].split(":")[0]

            mapping[full_image] = icon_url
            mapping[short_name] = icon_url
        except Exception:
            continue

    return mapping


def docker_run_detect_icon(content: str, icon_map: Dict[str, str]) -> str:
    direct_icon = re.search(r"net\.unraid\.docker\.icon=([^\s\\]+)", content or "", re.IGNORECASE)
    if direct_icon:
        return direct_icon.group(1).strip('"\'')

    image_ref = docker_run_extract_image_reference(content)
    if image_ref:
        short_name = image_ref.split("/")[-1].split(":")[0]
        return icon_map.get(image_ref) or icon_map.get(short_name) or DOCKER_RUN_DEFAULT_ICON_URL

    return DOCKER_RUN_DEFAULT_ICON_URL


def docker_run_build_preview(content: str) -> str:
    lines = [line.strip() for line in (content or "").splitlines() if line.strip()]
    if not lines:
        return "Fichier vide."
    preview = lines[0]
    return preview[:140] + ("…" if len(preview) > 140 else "")


def docker_run_read_runs(conf: Dict[str, str]) -> List[Dict[str, Any]]:
    runs: List[Dict[str, Any]] = []
    run_dir = conf.get("DOCKER_RUN_DIR", "/data/docker_run")
    icon_map = docker_run_get_icons_map(conf)
    raw_files = glob.glob(os.path.join(run_dir, "*.conf"))

    for filepath in raw_files:
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read()

            stat_result = os.stat(filepath)
            image_ref = docker_run_extract_image_reference(content)
            filename = os.path.basename(filepath)

            run_status = docker_run_status_view(conf, filename)

            runs.append(
                {
                    "filename": filename,
                    "name": os.path.splitext(filename)[0],
                    "command": content,
                    "command_b64": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                    "icon": docker_run_detect_icon(content, icon_map),
                    "image": image_ref or "Image non détectée",
                    "preview": docker_run_build_preview(content),
                    "line_count": len(content.splitlines()) if content else 0,
                    "size_kb": round(stat_result.st_size / 1024, 1),
                    "modified_at": time.strftime("%d/%m/%Y %H:%M", time.localtime(stat_result.st_mtime)),
                    "run_status": run_status["status"],
                    "run_status_label": run_status["label"],
                    "run_status_class": run_status["class"],
                    "run_progress": run_status["progress"],
                    "run_running": run_status["running"],
                }
            )
        except Exception:
            continue

    runs.sort(key=lambda item: item["filename"].lower())
    return runs


def docker_run_payload_from_conf(conf: Dict[str, str]) -> Dict[str, Any]:
    runs = docker_run_read_runs(conf)
    return {
        "docker_run_runs": runs,
        "docker_run_config": conf,
        "docker_run_config_file": CONFIG_FILE,
        "docker_run_yml_dir": conf.get("YML_FOLDER") or nas_root_path("yml"),
        "docker_run_dir": conf.get("DOCKER_RUN_DIR", "/data/docker_run"),
        "docker_run_count": len(runs),
        "docker_run_edit_filename": "",
        "docker_run_edit_command": "",
        "docker_run_autorun_file": "",
        "docker_run_log_filename": "",
        "docker_run_log_content": "",
        "docker_run_log_running": False,
    }


def docker_run_read_file(conf: Dict[str, str], requested_filename: str) -> Tuple[str, str]:
    """Retourne (filename_sain, contenu) pour l'éditeur Docker Run."""
    filename = docker_run_safe_filename(requested_filename)
    if not filename:
        return "", ""

    run_dir = conf.get("DOCKER_RUN_DIR", "/data/docker_run")
    file_path = os.path.join(run_dir, filename)
    if not os.path.isfile(file_path):
        raise FileNotFoundError(filename)

    with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
        return filename, handle.read()


def docker_run_log_dir(conf: Dict[str, str]) -> str:
    run_dir = conf.get("DOCKER_RUN_DIR", "/data/docker_run")
    return os.path.join(run_dir, ".logs")


def docker_run_state_file(conf: Dict[str, str]) -> str:
    return os.path.join(docker_run_log_dir(conf), "docker_run_state.json")


def docker_run_log_file(conf: Dict[str, str], requested_filename: str) -> Tuple[str, str]:
    filename = docker_run_safe_filename(requested_filename)
    if not filename:
        return "", ""
    log_dir = docker_run_log_dir(conf)
    base = os.path.splitext(filename)[0]
    log_name = re.sub(r"[^A-Za-z0-9._\- ]", "_", base).strip(" .") or "docker_run"
    return filename, os.path.join(log_dir, log_name + ".log")


def docker_run_status_file(conf: Dict[str, str], requested_filename: str) -> Tuple[str, str]:
    filename, log_path = docker_run_log_file(conf, requested_filename)
    if not filename:
        return "", ""
    return filename, os.path.splitext(log_path)[0] + ".status.json"


def docker_run_write_run_status(conf: Dict[str, str], filename: str, status: Dict[str, Any]) -> None:
    safe_name, status_path = docker_run_status_file(conf, filename)
    if not safe_name or not status_path:
        return
    os.makedirs(os.path.dirname(status_path), exist_ok=True)
    payload = {
        "filename": safe_name,
        "status": status.get("status", "idle"),
        "pid": status.get("pid"),
        "return_code": status.get("return_code"),
        "started_at": status.get("started_at"),
        "finished_at": status.get("finished_at"),
        "log_path": status.get("log_path", ""),
        "message": status.get("message", ""),
    }
    tmp_path = status_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, status_path)


def docker_run_status_view(conf: Dict[str, str], filename: str) -> Dict[str, Any]:
    safe_name = docker_run_safe_filename(filename)
    if not safe_name:
        return {"filename": "", "status": "idle", "label": "—", "progress": 0, "running": False, "class": "idle"}

    data: Dict[str, Any] = {}
    _safe_name, status_path = docker_run_status_file(conf, safe_name)
    if status_path and os.path.isfile(status_path):
        try:
            with open(status_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if isinstance(raw, dict):
                data = raw
        except Exception:
            data = {}

    # Compat avec l'ancien état global du module Log.
    if not data:
        state = docker_run_load_state(conf)
        if docker_run_safe_filename(str(state.get("filename", ""))) == safe_name:
            data = dict(state)

    status = str(data.get("status", "idle") or "idle").lower()
    pid = data.get("pid")
    running = status == "running" and docker_run_pid_running(pid)

    if status == "running" and not running:
        # Si Flask a perdu le watcher mais que le PID n'existe plus, on affiche une anomalie visible.
        # Le log reste disponible pour comprendre ce qui s'est passé.
        status = "error"
        data["message"] = data.get("message") or "Processus terminé sans retour confirmé."

    if running:
        label = "En cours"
        progress = 55
        css_class = "running"
    elif status == "success":
        label = "Succès"
        progress = 100
        css_class = "success"
    elif status == "error":
        label = "Erreur"
        progress = 100
        css_class = "error"
    else:
        label = "—"
        progress = 0
        css_class = "idle"

    return {
        "filename": safe_name,
        "status": status if status in {"running", "success", "error"} else "idle",
        "label": label,
        "progress": progress,
        "running": running,
        "class": css_class,
        "pid": pid,
        "return_code": data.get("return_code"),
        "message": data.get("message", ""),
    }


def docker_run_load_state(conf: Dict[str, str]) -> Dict[str, Any]:
    try:
        with open(docker_run_state_file(conf), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def docker_run_save_state(conf: Dict[str, str], state: Dict[str, Any]) -> None:
    log_dir = docker_run_log_dir(conf)
    os.makedirs(log_dir, exist_ok=True)
    tmp_path = docker_run_state_file(conf) + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, docker_run_state_file(conf))


def docker_run_pid_running(pid: Any) -> bool:
    try:
        pid_int = int(pid)
        if pid_int <= 0:
            return False
        os.kill(pid_int, 0)
        return True
    except Exception:
        return False


def docker_run_latest_log_filename(conf: Dict[str, str]) -> str:
    state = docker_run_load_state(conf)
    filename = docker_run_safe_filename(str(state.get("filename", "")))
    if filename:
        return filename
    return ""


def docker_run_read_log_tail(conf: Dict[str, str], requested_filename: str, max_bytes: int = 120_000) -> str:
    filename, log_path = docker_run_log_file(conf, requested_filename)
    if not filename or not os.path.isfile(log_path):
        return ""
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            data = handle.read()
        text = data.decode("utf-8", errors="replace")
        if size > max_bytes:
            text = "… log tronqué aux dernières lignes …\n" + text
        return text
    except Exception as exc:
        return f"❌ Impossible de lire le log Docker Run : {exc}\n"


def docker_run_mark_finished(conf: Dict[str, str], filename: str, pid: int, return_code: int, log_path: str) -> None:
    try:
        with open(log_path, "a", encoding="utf-8", buffering=1) as log_handle:
            if return_code == 0:
                log_handle.write("\n✅ Exécution locale terminée sans erreur.\n")
            else:
                log_handle.write(f"\n❌ Fin avec code de retour {return_code}.\n")
    except Exception:
        pass

    finished_payload = {
        "filename": filename,
        "pid": pid,
        "status": "success" if return_code == 0 else "error",
        "return_code": return_code,
        "finished_at": time.time(),
        "log_path": log_path,
        "message": "Succès" if return_code == 0 else f"Erreur : code {return_code}",
    }

    try:
        docker_run_write_run_status(conf, filename, finished_payload)
    except Exception:
        pass

    state = docker_run_load_state(conf)
    if int(state.get("pid") or -1) == int(pid):
        state.update(finished_payload)
        try:
            docker_run_save_state(conf, state)
        except Exception:
            pass


def docker_run_start_background(conf: Dict[str, str], requested_filename: str) -> Tuple[str, str]:
    filename = docker_run_safe_filename(requested_filename)
    if not filename:
        raise ValueError("Nom de fichier invalide.")

    run_dir = conf.get("DOCKER_RUN_DIR", "/data/docker_run")
    file_path = os.path.join(run_dir, filename)
    if not os.path.isfile(file_path):
        raise FileNotFoundError(filename)

    with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
        cmd_content = handle.read()

    if not cmd_content.strip():
        raise ValueError("Commande vide.")

    state = docker_run_load_state(conf)
    if docker_run_safe_filename(str(state.get("filename", ""))) == filename and docker_run_pid_running(state.get("pid")):
        _, existing_log_path = docker_run_log_file(conf, filename)
        return filename, existing_log_path

    log_dir = docker_run_log_dir(conf)
    os.makedirs(log_dir, exist_ok=True)
    filename, log_path = docker_run_log_file(conf, filename)

    shell_bin = conf.get("SHELL_BIN", "/bin/bash")
    workdir = conf.get("WORKDIR", "/")
    safe_workdir = workdir if os.path.isdir(workdir) else "/"

    with open(log_path, "w", encoding="utf-8", buffering=1) as log_handle:
        log_handle.write(f"🚀 Exécution locale de {filename}\n")
        log_handle.write("🖥️ Cible : système hôte local\n")
        log_handle.write(f"🐚 Shell : {shell_bin}\n")
        log_handle.write(f"📁 Dossier : {safe_workdir}\n")
        log_handle.write(f"🕒 Début : {time.strftime('%d/%m/%Y %H:%M:%S')}\n\n")
        log_handle.flush()

        process = subprocess.Popen(
            [shell_bin, "-lc", cmd_content],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=safe_workdir,
            start_new_session=True,
        )

    state = {
        "filename": filename,
        "pid": process.pid,
        "status": "running",
        "started_at": time.time(),
        "log_path": log_path,
        "message": "Exécution en cours",
    }
    docker_run_save_state(conf, state)
    docker_run_write_run_status(conf, filename, state)

    waiter = threading.Thread(
        target=lambda: docker_run_mark_finished(conf, filename, process.pid, process.wait(), log_path),
        daemon=True,
    )
    waiter.start()
    return filename, log_path


def docker_run_stream_log(conf: Dict[str, str], requested_filename: str, *, follow: bool = True) -> Iterator[str]:
    filename, log_path = docker_run_log_file(conf, requested_filename)
    if not filename:
        yield "❌ Nom de fichier invalide.\n"
        return

    last_pos = 0
    empty_ticks = 0
    while True:
        if os.path.isfile(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(last_pos)
                    chunk = handle.read()
                    last_pos = handle.tell()
                if chunk:
                    empty_ticks = 0
                    yield chunk
                else:
                    empty_ticks += 1
            except Exception as exc:
                yield f"\n❌ Impossible de lire le log : {exc}\n"
                return
        else:
            empty_ticks += 1
            if empty_ticks == 1:
                yield "⏳ En attente du fichier log…\n"

        state = docker_run_load_state(conf)
        same_file = docker_run_safe_filename(str(state.get("filename", ""))) == filename
        running = same_file and state.get("status") == "running" and docker_run_pid_running(state.get("pid"))
        if not follow or not running:
            if empty_ticks >= 2:
                break
        time.sleep(0.8)


def docker_run_stop_process_group(process: Optional[subprocess.Popen]) -> None:
    if not process or process.poll() is not None:
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except Exception:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Onglet LAN Docker : réseaux Docker + état persistant JSON
# ---------------------------------------------------------------------------
DOCKER_LAN_ALLOWED_DRIVERS = {"macvlan", "ipvlan", "bridge"}
DOCKER_LAN_PROTECTED_NAMES = {"bridge", "host", "none"}


def docker_lan_safe_name(raw_name: str) -> str:
    name = (raw_name or "").strip()
    name = re.sub(r"\s+", "_", name)
    if not name:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name):
        return ""
    return name


def docker_lan_now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def docker_lan_blank_state() -> Dict[str, Any]:
    return {"version": 1, "networks": {}}


def docker_lan_load_state(conf: Dict[str, str]) -> Dict[str, Any]:
    path = conf.get("DOCKER_LAN_STATE_FILE", nas_conf_file("docker_lan.json"))
    if not path or not os.path.exists(path):
        return docker_lan_blank_state()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return docker_lan_blank_state()
        networks = data.get("networks", {})
        if isinstance(networks, list):
            networks = {item.get("name", ""): item for item in networks if isinstance(item, dict)}
        if not isinstance(networks, dict):
            networks = {}
        data["version"] = 1
        data["networks"] = {name: item for name, item in networks.items() if isinstance(item, dict) and docker_lan_safe_name(name)}
        return data
    except Exception:
        return docker_lan_blank_state()


def docker_lan_save_state(conf: Dict[str, str], state: Dict[str, Any]) -> None:
    path = conf.get("DOCKER_LAN_STATE_FILE", nas_conf_file("docker_lan.json"))
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": docker_lan_now(),
        "networks": state.get("networks", {}),
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")


def docker_lan_normalize_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    name = docker_lan_safe_name(str(entry.get("name", "")))
    driver = str(entry.get("driver", "macvlan")).strip().lower() or "macvlan"
    if driver not in DOCKER_LAN_ALLOWED_DRIVERS:
        driver = "macvlan"
    mode = str(entry.get("mode", "bridge")).strip().lower() or "bridge"
    if mode not in {"bridge", "private", "vepa", "passthru", "l2", "l3"}:
        mode = "bridge" if driver == "macvlan" else "l2"
    normalized = {
        "name": name,
        "driver": driver,
        "parent": str(entry.get("parent", "")).strip(),
        "subnet": str(entry.get("subnet", "")).strip(),
        "netmask": str(entry.get("netmask", "")).strip(),
        "gateway": str(entry.get("gateway", "")).strip(),
        "ip_range": str(entry.get("ip_range", "")).strip(),
        "mode": mode,
        "enabled": bool(entry.get("enabled", True)),
        "managed": bool(entry.get("managed", True)),
        "updated_at": str(entry.get("updated_at", docker_lan_now())),
    }
    return normalized


def docker_lan_detect_defaults() -> Dict[str, str]:
    defaults = {
        "driver": "macvlan",
        "parent": "",
        "subnet": "",
        "netmask": "255.255.255.0",
        "gateway": "",
        "ip_range": "",
        "mode": "bridge",
    }

    try:
        res = subprocess.run(
            ["ip", "-4", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        line = (res.stdout or "").strip().splitlines()[0] if (res.stdout or "").strip() else ""
        gw_match = re.search(r"\bvia\s+([^\s]+)", line)
        dev_match = re.search(r"\bdev\s+([^\s]+)", line)
        if gw_match:
            defaults["gateway"] = gw_match.group(1).strip()
        if dev_match:
            defaults["parent"] = dev_match.group(1).strip()
    except Exception:
        pass

    if defaults["parent"]:
        try:
            res = subprocess.run(
                ["ip", "-4", "addr", "show", "dev", defaults["parent"]],
                capture_output=True,
                text=True,
                timeout=5,
            )
            inet_match = re.search(r"\binet\s+([^\s]+)", res.stdout or "")
            if inet_match:
                iface = ipaddress.ip_interface(inet_match.group(1).strip())
                defaults["subnet"] = str(iface.network)
                defaults["netmask"] = str(iface.network.netmask)
        except Exception:
            pass

    # Fallback courant en LAN domestique si iproute2 ne renvoie rien.
    if not defaults["subnet"] and defaults["gateway"]:
        try:
            ip = ipaddress.ip_address(defaults["gateway"])
            if ip.version == 4:
                parts = str(ip).split(".")
                defaults["subnet"] = f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
                defaults["netmask"] = "255.255.255.0"
        except Exception:
            pass

    return defaults


def docker_lan_validate_entry(entry: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    name = entry.get("name", "")
    driver = entry.get("driver", "")
    parent = entry.get("parent", "")
    subnet = entry.get("subnet", "")
    gateway = entry.get("gateway", "")
    ip_range = entry.get("ip_range", "")

    if not name:
        errors.append("Nom de réseau invalide. Utilise lettres, chiffres, point, tiret ou underscore.")
    if name in DOCKER_LAN_PROTECTED_NAMES:
        errors.append(f"Le réseau Docker système '{name}' est protégé.")
    if driver not in DOCKER_LAN_ALLOWED_DRIVERS:
        errors.append("Type réseau invalide.")
    if driver in {"macvlan", "ipvlan"} and not parent:
        errors.append("Interface parente obligatoire pour macvlan/ipvlan.")
    if driver in {"macvlan", "ipvlan"} and not subnet:
        errors.append("Sous-réseau CIDR obligatoire pour macvlan/ipvlan, exemple 192.168.1.0/24.")
    if subnet:
        try:
            ipaddress.ip_network(subnet, strict=False)
        except Exception:
            errors.append("Sous-réseau CIDR invalide.")
    if gateway:
        try:
            ipaddress.ip_address(gateway)
        except Exception:
            errors.append("Passerelle invalide.")
    if ip_range:
        try:
            ipaddress.ip_network(ip_range, strict=False)
        except Exception:
            errors.append("Plage IP invalide.")
    return errors


def docker_lan_extract_network_info(network: Any) -> Dict[str, Any]:
    attrs = getattr(network, "attrs", {}) or {}
    ipam_configs = ((attrs.get("IPAM", {}) or {}).get("Config", []) or [])
    ipam = ipam_configs[0] if ipam_configs else {}
    options = attrs.get("Options", {}) or {}
    subnet = ipam.get("Subnet", "") or ""
    netmask = ""
    if subnet:
        try:
            netmask = str(ipaddress.ip_network(subnet, strict=False).netmask)
        except Exception:
            netmask = ""
    return {
        "name": attrs.get("Name") or getattr(network, "name", ""),
        "id": attrs.get("Id") or getattr(network, "id", ""),
        "driver": attrs.get("Driver", "") or "",
        "scope": attrs.get("Scope", "") or "",
        "parent": options.get("parent", "") or "",
        "subnet": subnet,
        "netmask": netmask,
        "gateway": ipam.get("Gateway", "") or "",
        "ip_range": ipam.get("IPRange", "") or "",
        "mode": options.get("macvlan_mode") or options.get("ipvlan_mode") or "",
        "options": options,
        "containers_count": len((attrs.get("Containers", {}) or {})),
    }


def docker_lan_list_docker_networks() -> Tuple[List[Dict[str, Any]], str]:
    if docker is None:
        return [], "Module Python docker introuvable. Installe python3-docker ou docker dans le venv."

    docker_service_status = get_docker_service_status()
    if docker_service_status.get("active") is False:
        return [], "Service Docker arrêté. Les réseaux conservés dans le JSON restent visibles."

    client = None
    try:
        client = get_docker_client()
        networks = []
        for network in client.networks.list():
            try:
                network.reload()
            except Exception:
                pass
            networks.append(docker_lan_extract_network_info(network))
        return networks, ""
    except Exception as exc:
        return [], f"Impossible de lire les réseaux Docker : {clean_docker_error(exc)}"
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def docker_lan_payload_from_conf(conf: Dict[str, str]) -> Dict[str, Any]:
    state = docker_lan_load_state(conf)
    state_networks = {
        name: docker_lan_normalize_entry({**item, "name": name})
        for name, item in (state.get("networks", {}) or {}).items()
        if docker_lan_safe_name(name)
    }
    docker_networks, docker_lan_error = docker_lan_list_docker_networks()
    rows: Dict[str, Dict[str, Any]] = {}

    for item in docker_networks:
        name = docker_lan_safe_name(str(item.get("name", "")))
        if not name:
            continue
        managed = name in state_networks
        merged = state_networks.get(name, {}).copy()
        merged.update({k: v for k, v in item.items() if v not in (None, "")})
        merged["name"] = name
        merged["managed"] = managed
        merged["system_active"] = True
        merged["protected"] = name in DOCKER_LAN_PROTECTED_NAMES
        merged["enabled"] = bool(state_networks.get(name, {}).get("enabled", True))
        rows[name] = merged

    for name, item in state_networks.items():
        if name in rows:
            continue
        row = item.copy()
        row["name"] = name
        row["system_active"] = False
        row["protected"] = name in DOCKER_LAN_PROTECTED_NAMES
        row["managed"] = True
        rows[name] = row

    final_rows: List[Dict[str, Any]] = []
    for row in rows.values():
        row = docker_lan_normalize_entry(row)
        row["system_active"] = bool(rows[row["name"]].get("system_active", False))
        row["protected"] = bool(rows[row["name"]].get("protected", row["name"] in DOCKER_LAN_PROTECTED_NAMES))
        row["managed"] = bool(rows[row["name"]].get("managed", False))
        row["containers_count"] = int(rows[row["name"]].get("containers_count", 0) or 0)
        if row["protected"]:
            row["status_label"] = "Système"
            row["status_class"] = "system"
        elif row["system_active"]:
            row["status_label"] = "Actif"
            row["status_class"] = "active"
        elif row.get("enabled"):
            row["status_label"] = "Manquant"
            row["status_class"] = "missing"
        else:
            row["status_label"] = "Désactivé"
            row["status_class"] = "disabled"
        row["origin_label"] = "JSON + Docker" if row["managed"] and row["system_active"] else ("JSON" if row["managed"] else "Docker")
        final_rows.append(row)

    final_rows.sort(key=lambda item: (item.get("protected", False), item.get("name", "").lower()))
    summary = {
        "total": len(final_rows),
        "active": sum(1 for item in final_rows if item.get("system_active")),
        "disabled": sum(1 for item in final_rows if not item.get("system_active") and item.get("managed")),
        "managed": sum(1 for item in final_rows if item.get("managed")),
    }
    return {
        "docker_lan_networks": final_rows,
        "docker_lan_summary": summary,
        "docker_lan_defaults": docker_lan_detect_defaults(),
        "docker_lan_state_file": conf.get("DOCKER_LAN_STATE_FILE", nas_conf_file("docker_lan.json")),
        "docker_lan_error": docker_lan_error,
    }


def docker_lan_get_client_or_flash() -> Optional[Any]:
    if docker is None:
        flash("❌ Module Python docker introuvable. Installe python3-docker ou docker dans le venv.", "error")
        return None
    try:
        return get_docker_client()
    except Exception as exc:
        flash(f"❌ Connexion Docker impossible : {clean_docker_error(exc)}", "error")
        return None


def docker_lan_get_network(client: Any, name: str) -> Optional[Any]:
    try:
        return client.networks.get(name)
    except Exception:
        return None


def docker_lan_remove_network(client: Any, name: str) -> None:
    if name in DOCKER_LAN_PROTECTED_NAMES:
        raise RuntimeError(f"Le réseau système '{name}' est protégé.")
    network = docker_lan_get_network(client, name)
    if network is not None:
        network.remove()


def docker_lan_create_network(client: Any, entry: Dict[str, Any]) -> None:
    errors = docker_lan_validate_entry(entry)
    if errors:
        raise RuntimeError(" | ".join(errors))

    name = entry["name"]
    driver = entry["driver"]
    subnet = entry.get("subnet", "")
    gateway = entry.get("gateway", "")
    ip_range = entry.get("ip_range", "")
    parent = entry.get("parent", "")
    mode = entry.get("mode", "bridge") or ("l2" if driver == "ipvlan" else "bridge")

    options: Dict[str, str] = {}
    if driver in {"macvlan", "ipvlan"}:
        options["parent"] = parent
    if driver == "macvlan":
        options["macvlan_mode"] = mode if mode in {"bridge", "private", "vepa", "passthru"} else "bridge"
    if driver == "ipvlan":
        options["ipvlan_mode"] = mode if mode in {"l2", "l3"} else "l2"

    ipam_config = None
    if subnet:
        pool_kwargs: Dict[str, Any] = {"subnet": subnet}
        if gateway:
            pool_kwargs["gateway"] = gateway
        if ip_range:
            pool_kwargs["iprange"] = ip_range
        ipam_pool = docker.types.IPAMPool(**pool_kwargs)
        ipam_config = docker.types.IPAMConfig(pool_configs=[ipam_pool])

    client.networks.create(
        name=name,
        driver=driver,
        options=options or None,
        ipam=ipam_config,
        check_duplicate=True,
    )


def docker_lan_apply_network(entry: Dict[str, Any], recreate: bool = False) -> None:
    client = docker_lan_get_client_or_flash()
    if client is None:
        raise RuntimeError("Docker indisponible.")
    try:
        existing = docker_lan_get_network(client, entry["name"])
        if existing is not None:
            if not recreate:
                return
            docker_lan_remove_network(client, entry["name"])
        docker_lan_create_network(client, entry)
    finally:
        try:
            client.close()
        except Exception:
            pass


def docker_lan_collect_form() -> Tuple[str, Dict[str, Any], List[str]]:
    old_name = docker_lan_safe_name(request.form.get("old_name", ""))
    name = docker_lan_safe_name(request.form.get("name", ""))
    driver = request.form.get("driver", "macvlan").strip().lower()
    mode = request.form.get("mode", "bridge").strip().lower()
    entry = docker_lan_normalize_entry({
        "name": name,
        "driver": driver,
        "parent": request.form.get("parent", ""),
        "subnet": request.form.get("subnet", ""),
        "netmask": request.form.get("netmask", ""),
        "gateway": request.form.get("gateway", ""),
        "ip_range": request.form.get("ip_range", ""),
        "mode": mode,
        "enabled": request.form.get("enabled", "1") == "1",
        "managed": True,
        "updated_at": docker_lan_now(),
    })
    errors = docker_lan_validate_entry(entry)
    return old_name, entry, errors

def docker_tab_empty_payload(message: str = "") -> Dict[str, Any]:
    return {
        "docker_stacks": {},
        "docker_stats": {"total": 0, "running": 0, "stopped": 0},
        "docker_service_status": get_docker_service_status(),
        "docker_connection_error": message,
        "docker_connection_notice": "",
    }


def docker_tab_payload() -> Dict[str, Any]:
    docker_service_status = get_docker_service_status()

    # Priorité à l'état systemd : quand Docker est volontairement arrêté
    # (backup appdata, maintenance), l'interface doit afficher un état normal
    # et ne surtout pas toucher au socket Docker.
    if docker_service_status.get("active") is False:
        return {
            "docker_stacks": {},
            "docker_stats": {"total": 0, "running": 0, "stopped": 0},
            "docker_service_status": docker_service_status,
            "docker_connection_error": "",
            "docker_connection_notice": "Service Docker arrêté.",
        }

    if docker is None:
        return {
            "docker_stacks": {},
            "docker_stats": {"total": 0, "running": 0, "stopped": 0},
            "docker_service_status": docker_service_status,
            "docker_connection_error": "Module Python docker introuvable. Installe python3-docker ou docker dans le venv.",
            "docker_connection_notice": "",
        }

    client = None
    try:
        client = get_docker_client()
        sorted_docker_stacks = list_stacks(client)
        docker_stats = get_docker_stats(sorted_docker_stacks)
        return {
            "docker_stacks": sorted_docker_stacks,
            "docker_stats": docker_stats,
            "docker_service_status": docker_service_status,
            "docker_connection_error": "",
            "docker_connection_notice": "",
        }
    except DockerException as exc:
        clean_msg = clean_docker_error(exc)
        if clean_msg == "Service Docker arrêté.":
            return {
                "docker_stacks": {},
                "docker_stats": {"total": 0, "running": 0, "stopped": 0},
                "docker_service_status": get_docker_service_status(),
                "docker_connection_error": "",
                "docker_connection_notice": clean_msg,
            }
        msg = f"Impossible de se connecter à Docker : {clean_msg}"
        print(msg)
        return {
            "docker_stacks": {},
            "docker_stats": {"total": 0, "running": 0, "stopped": 0},
            "docker_service_status": docker_service_status,
            "docker_connection_error": msg,
            "docker_connection_notice": "",
        }
    except Exception as exc:
        clean_msg = clean_docker_error(exc)
        if clean_msg == "Service Docker arrêté.":
            return {
                "docker_stacks": {},
                "docker_stats": {"total": 0, "running": 0, "stopped": 0},
                "docker_service_status": get_docker_service_status(),
                "docker_connection_error": "",
                "docker_connection_notice": clean_msg,
            }
        msg = f"Erreur dockers : {clean_msg}"
        print(msg)
        return {
            "docker_stacks": {},
            "docker_stats": {"total": 0, "running": 0, "stopped": 0},
            "docker_service_status": docker_service_status,
            "docker_connection_error": msg,
            "docker_connection_notice": "",
        }
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass



# ---------------------------------------------------------------------------
# Onglet YML intégré depuis l’ancien module compose.py
# ---------------------------------------------------------------------------
def yml_safe_filename(filename: str) -> str:
    filename = (filename or "").strip().replace("\\", "/")
    filename = os.path.basename(filename)

    if not filename:
        raise ValueError("Nom de fichier manquant")
    if filename in (".", "..") or "/" in filename or "\x00" in filename:
        raise ValueError("Nom de fichier invalide")
    if not filename.lower().endswith((".yml", ".yaml")):
        filename += ".yml"

    return filename


def yml_resolve_file_path(base_dir: str, filename: str) -> Tuple[str, str]:
    safe_name = yml_safe_filename(filename)
    base_real = os.path.realpath(base_dir)
    final_path = os.path.realpath(os.path.join(base_real, safe_name))

    if os.path.commonpath([base_real, final_path]) != base_real:
        raise ValueError("Chemin refusé")

    return final_path, safe_name


def yml_list_files(base_dir: str) -> List[Dict[str, int]]:
    files: List[Dict[str, int]] = []
    seen = set()

    patterns = [
        os.path.join(base_dir, "*.yml"),
        os.path.join(base_dir, "*.yaml"),
    ]

    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            name = os.path.basename(path)
            if name in seen:
                continue
            seen.add(name)
            try:
                stat_result = os.stat(path)
                files.append({
                    "name": name,
                    "size": int(stat_result.st_size),
                    "mtime": int(stat_result.st_mtime),
                })
            except Exception:
                files.append({"name": name, "size": 0, "mtime": 0})

    files.sort(key=lambda item: item["name"].lower())
    return files


def yml_validate_content(content: str) -> Dict[str, object]:
    """Validation YAML optionnelle : utilise PyYAML s'il est présent."""
    if "\t" in content:
        return {
            "ok": False,
            "message": "Erreur YAML : tabulation détectée. En YAML, utilise des espaces, pas des TAB.",
        }

    try:
        import yaml  # type: ignore
    except Exception:
        return {
            "ok": True,
            "message": "Contrôle léger OK : aucune tabulation détectée. Validation complète indisponible car PyYAML n’est pas installé dans ce venv.",
        }

    try:
        parsed = yaml.safe_load(content) if content.strip() else None
    except Exception as exc:
        return {
            "ok": False,
            "message": f"Erreur YAML : {exc}",
        }

    if parsed is None:
        return {
            "ok": True,
            "message": "YAML valide, mais fichier vide.",
        }

    if not isinstance(parsed, dict):
        return {
            "ok": True,
            "message": "YAML valide. Attention : la racine n’est pas un objet/dictionnaire, ce qui est inhabituel pour un compose.",
        }

    services = parsed.get("services")
    if services is None:
        return {
            "ok": True,
            "message": "YAML valide. Attention : aucune clé services: trouvée.",
        }

    if not isinstance(services, dict):
        return {
            "ok": False,
            "message": "YAML lisible, mais services: doit être un dictionnaire.",
        }

    return {
        "ok": True,
        "message": f"YAML valide · {len(services)} service(s) détecté(s).",
    }


def yml_payload_from_conf(conf: Dict[str, str]) -> Dict[str, Any]:
    yml_folder = conf.get("YML_FOLDER", "/dockers/yml")
    try:
        os.makedirs(yml_folder, exist_ok=True)
    except Exception:
        pass

    try:
        yml_files = yml_list_files(yml_folder)
        error = ""
    except Exception as exc:
        yml_files = []
        error = str(exc)

    return {
        "yml_files": yml_files,
        "yml_error": error,
    }
