REGISTRY_ICON_CACHE_TTL = 60
REGISTRY_ARCH_CACHE_TTL = 300
_REGISTRY_ICON_CACHE: Dict[str, object] = {"expires": 0.0, "yml_dir": "", "data": {}}
_REGISTRY_ARCH_CACHE: Dict[str, Tuple[float, str]] = {}


def registry_browser_url(conf: Dict[str, str]) -> str:
    return strip_quotes(conf.get("REGISTRY_URL", "")).strip().rstrip("/")


def registry_browser_timeout(conf: Dict[str, str]) -> int:
    try:
        return max(1, int(str(conf.get("REGISTRY_REQUEST_TIMEOUT", "10")).strip() or "10"))
    except ValueError:
        return 10


def registry_browser_session(conf: Dict[str, str]):
    if requests is None:
        return None
    session = requests.Session()
    user = strip_quotes(conf.get("REGISTRY_USER", "")).strip()
    password = strip_quotes(conf.get("REGISTRY_PASSWORD", "")).strip()
    if user and password:
        session.auth = (user, password)
    return session


def registry_catalog_request(conf: Dict[str, str], endpoint: str, method: str = "GET", headers: Optional[dict] = None):
    base_url = registry_browser_url(conf)
    session = registry_browser_session(conf)
    if not base_url or session is None:
        return None

    endpoint = endpoint.lstrip("/")
    url = f"{base_url}/v2/{endpoint}"
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)

    try:
        return session.request(method=method, url=url, headers=req_headers, timeout=registry_browser_timeout(conf))
    except Exception as exc:
        print(f"ERREUR CONNEXION REGISTRY: {exc}")
        return None


def registry_parse_json(response) -> dict:
    if response is None:
        return {}
    try:
        return response.json()
    except ValueError:
        return {}


def registry_split_image_reference(image_ref: str) -> str:
    image_ref = (image_ref or "").split("@", 1)[0].strip()
    last_slash = image_ref.rfind("/")
    last_colon = image_ref.rfind(":")
    if last_colon > last_slash:
        return image_ref[:last_colon]
    return image_ref


def registry_get_repo_icons_from_yml(conf: Dict[str, str], force_refresh: bool = False) -> Dict[str, str]:
    now = time.time()
    yml_dir = strip_quotes(conf.get("YML_DIR", "")).strip()
    cached_data = _REGISTRY_ICON_CACHE.get("data")
    cached_dir = _REGISTRY_ICON_CACHE.get("yml_dir")
    cached_expires = float(_REGISTRY_ICON_CACHE.get("expires") or 0.0)
    if not force_refresh and cached_data and cached_dir == yml_dir and cached_expires > now:
        return cached_data  # type: ignore[return-value]

    mapping: Dict[str, str] = {}
    if not yml_dir or not os.path.exists(yml_dir):
        _REGISTRY_ICON_CACHE["data"] = mapping
        _REGISTRY_ICON_CACHE["yml_dir"] = yml_dir
        _REGISTRY_ICON_CACHE["expires"] = now + REGISTRY_ICON_CACHE_TTL
        return mapping

    pattern_files = glob.glob(os.path.join(yml_dir, "*.yml")) + glob.glob(os.path.join(yml_dir, "*.yaml"))
    icon_pattern = re.compile(r"net\.unraid\.docker\.icon[\"']?\s*[:=]\s*[\"']?([^\"\s']+)")
    image_pattern = re.compile(r"image\s*:\s*([^\s]+)")

    for filepath in pattern_files:
        try:
            with open(filepath, "r", encoding="utf-8") as handle:
                content = handle.read()

            icon_match = icon_pattern.search(content)
            image_match = image_pattern.search(content)
            if not icon_match or not image_match:
                continue

            icon_url = icon_match.group(1).strip().strip('"\'')
            full_image = image_match.group(1).strip().strip('"\'')
            image_without_tag = registry_split_image_reference(full_image)
            parts = [part for part in image_without_tag.split("/") if part]

            candidates = {full_image, image_without_tag}
            if parts:
                candidates.add(parts[-1])
            if len(parts) >= 2:
                candidates.add(f"{parts[-2]}/{parts[-1]}")

            for candidate in candidates:
                mapping[candidate] = icon_url
        except Exception as exc:
            print(f"ERREUR LECTURE YML {filepath}: {exc}")

    _REGISTRY_ICON_CACHE["data"] = mapping
    _REGISTRY_ICON_CACHE["yml_dir"] = yml_dir
    _REGISTRY_ICON_CACHE["expires"] = now + REGISTRY_ICON_CACHE_TTL
    return mapping


def registry_get_digest(conf: Dict[str, str], repo: str, tag: str) -> Optional[str]:
    headers = {
        "Accept": ", ".join([
            "application/vnd.docker.distribution.manifest.v2+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.oci.image.index.v1+json",
        ])
    }
    response = registry_catalog_request(conf, f"{repo}/manifests/{tag}", method="HEAD", headers=headers)
    if response is not None and response.status_code == 200:
        return response.headers.get("Docker-Content-Digest")
    return None


def registry_get_architecture(conf: Dict[str, str], repo: str, tag: str) -> str:
    base_url = registry_browser_url(conf)
    cache_key = f"{base_url}|{repo}:{tag}"
    now = time.time()
    cached = _REGISTRY_ARCH_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]

    headers = {
        "Accept": ", ".join([
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.docker.distribution.manifest.v2+json",
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.oci.image.manifest.v1+json",
        ])
    }
    response = registry_catalog_request(conf, f"{repo}/manifests/{tag}", method="GET", headers=headers)
    architectures = set()

    if response is not None and response.status_code == 200:
        try:
            data = response.json()
            architectures = arches_from_manifest_payload(data)
        except ValueError:
            pass

    arch_string = ", ".join(sorted(architectures))
    _REGISTRY_ARCH_CACHE[cache_key] = (now + REGISTRY_ARCH_CACHE_TTL, arch_string)
    return arch_string


def registry_get_repo_list(conf: Dict[str, str]) -> List[str]:
    response = registry_catalog_request(conf, "_catalog")
    if response is None or response.status_code != 200:
        return []
    repositories = registry_parse_json(response).get("repositories", [])
    if not isinstance(repositories, list):
        return []
    return sorted(repo for repo in repositories if isinstance(repo, str))


def registry_get_repo_tags(conf: Dict[str, str], repo: str) -> List[str]:
    response = registry_catalog_request(conf, f"{repo}/tags/list")
    if response is None or response.status_code != 200:
        return []

    tags = registry_parse_json(response).get("tags", [])
    if not tags:
        return []

    clean_tags = [tag for tag in tags if isinstance(tag, str)]
    return sorted(clean_tags, reverse=True)


def registry_build_repo_payload(conf: Dict[str, str]) -> Tuple[List[dict], int]:
    icon_map = registry_get_repo_icons_from_yml(conf)
    images_data = []
    total_tags_count = 0

    for repo in registry_get_repo_list(conf):
        tags_list = registry_get_repo_tags(conf, repo)
        total_tags_count += len(tags_list)

        tags_info = []
        for tag in tags_list:
            tags_info.append({
                "name": tag,
                "arch": registry_get_architecture(conf, repo, tag),
            })

        icon_url = icon_map.get(repo) or icon_map.get(repo.split("/")[-1]) or conf.get("DEFAULT_ICON", "/static/logo.png")
        search_blob = " ".join([repo] + tags_list).lower()

        images_data.append({
            "name": repo,
            "tags": tags_info,
            "icon": icon_url,
            "search": search_blob,
        })

    return images_data, total_tags_count


def registry_browser_json_payload(conf: Dict[str, str], message: str = "", message_type: str = "success") -> Dict:
    images, total_tags = registry_build_repo_payload(conf)
    return {
        "ok": message_type != "error",
        "message": message,
        "message_type": message_type,
        "images": images,
        "total_repos": len(images),
        "total_tags": total_tags,
        "registry_url": registry_browser_url(conf),
        "registry_storage": registry_storage_payload(conf),
    }


def registry_path_size(path: str) -> int:
    total = 0
    if not path or not os.path.exists(path):
        return 0
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(root, name))]
        for name in files:
            item = os.path.join(root, name)
            try:
                if not os.path.islink(item):
                    total += os.path.getsize(item)
            except OSError:
                pass
    return total


def registry_storage_payload(conf: Dict[str, str]) -> Dict[str, object]:
    try:
        settings = registry_host_settings(conf)
        values = registry_host_load_conf(settings)
        data_dir = values.get("DATA_DIR", "")
        size_bytes = registry_path_size(data_dir)
        return {
            "ok": True,
            "data_dir": data_dir,
            "exists": bool(data_dir and os.path.isdir(data_dir)),
            "size_bytes": size_bytes,
            "size_h": human_size(size_bytes),
            "error": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "data_dir": "",
            "exists": False,
            "size_bytes": 0,
            "size_h": human_size(0),
            "error": str(exc),
        }


# ============================================================
# Onglet Système Registre host - Python natif, sans dépendance au script SH.
# ============================================================

