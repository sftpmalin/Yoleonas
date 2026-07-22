"""Client minimal Docker Registry HTTP API V2 pour les TAR OCI de YoLeo.

Le module Build produit exclusivement des archives OCI avec buildx. Il n'a
donc pas besoin d'un outil generaliste tel que regctl pour les envoyer : il
suffit de verifier les digests, d'uploader les blobs absents et de publier les
manifests dans l'ordre.
"""

REGISTRY_V2_MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/json",
])

REGISTRY_V2_INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}

REGISTRY_V2_MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}


class RegistryV2Error(RuntimeError):
    pass


def registry_v2_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def registry_v2_target_parts(target: str, mode: str) -> Dict[str, str]:
    """Decompose host:port/repo:tag sans imposer aucune adresse fixe."""
    value = strip_quotes(str(target or "")).strip().rstrip("/")
    value = value.removeprefix("http://").removeprefix("https://")
    if "/" not in value:
        raise RegistryV2Error(f"cible registre incomplete : {target}")

    host, image = value.split("/", 1)
    host = host.strip()
    image = image.strip().strip("/")
    if not host or not image or any(char.isspace() for char in value):
        raise RegistryV2Error(f"cible registre invalide : {target}")

    if "@" in image:
        repo, reference = image.rsplit("@", 1)
    else:
        last_slash = image.rfind("/")
        last_colon = image.rfind(":")
        if last_colon > last_slash:
            repo, reference = image[:last_colon], image[last_colon + 1:]
        else:
            repo, reference = image, "latest"

    repo = repo.strip().strip("/")
    reference = reference.strip() or "latest"
    if not repo or not reference or ".." in repo.split("/"):
        raise RegistryV2Error(f"depot ou tag registre invalide : {target}")

    scheme = "http" if normalize_registry_mode(mode) == "http" else "https"
    return {
        "host": host,
        "repo": repo,
        "reference": reference,
        "scheme": scheme,
        "base_url": f"{scheme}://{host}",
    }


def registry_v2_login_values(conf: Dict[str, str], target_host: str, mode: str) -> Tuple[str, str]:
    """Lit les identifiants deja geres par l'onglet Options.

    Comme avant, le mode HTTP local ne force aucun login. En HTTPS, le fichier
    registre_login.conf reste la source prioritaire, avec builds.conf en
    secours. Les identifiants ne sont jamais envoyes a un autre host que celui
    declare dans REGISTRY_HOST.
    """
    if normalize_registry_mode(mode) == "http":
        return "", ""

    login_file = conf.get("DOCKER_REGISTRY_LOGIN_FILE") or os.path.join(
        conf.get("DOCKER_CONF_DIR", NAS_CONF_DIR), "registre_login.conf"
    )
    values = read_env_login_file(login_file)
    configured_host = registry_host_from_target(values.get("REGISTRY_HOST", ""))
    if configured_host and configured_host != target_host:
        return "", ""

    user = strip_quotes(values.get("REGISTRY_USER", conf.get("REGISTRY_USER", ""))).strip()
    password = strip_quotes(values.get("REGISTRY_PASS", conf.get("REGISTRY_PASSWORD", ""))).strip()
    pass_file = strip_quotes(values.get("REGISTRY_PASS_FILE", "")).strip()
    if not password and pass_file:
        if not os.path.isabs(pass_file):
            pass_file = os.path.join(os.path.dirname(login_file), pass_file)
        if os.path.isfile(pass_file):
            password = local_read_text(pass_file).strip()
    return user, password


class RegistryV2Client:
    def __init__(self, conf: Dict[str, str], name: str, target: str):
        if requests is None:
            raise RegistryV2Error("module Python requests introuvable")
        self.conf = conf
        self.name = normalize_item_name(name)
        self.mode = get_registry_mode_for(conf, self.name)
        self.target = registry_v2_target_parts(target, self.mode)
        self.repo = self.target["repo"]
        self.reference = self.target["reference"]
        self.base_url = self.target["base_url"].rstrip("/")
        self.timeout = registry_browser_timeout(conf)
        self.session = requests.Session()
        user, password = registry_v2_login_values(conf, self.target["host"], self.mode)
        self.basic_auth = (user, password) if user and password else None
        self.bearer_tokens: Dict[str, str] = {}
        self.scope_tokens: Dict[str, str] = {}

    def _url(self, endpoint: str) -> str:
        return f"{self.base_url}/v2/{endpoint.lstrip('/')}"

    def _bearer_token(self, challenge: str, scope: str) -> str:
        raw = challenge.strip()
        if not raw.lower().startswith("bearer "):
            return ""
        params = requests.utils.parse_dict_header(raw[7:])
        realm = str(params.get("realm") or "").strip()
        if not realm:
            return ""
        token_scope = str(params.get("scope") or scope or "").strip()
        cache_key = f"{realm}\n{token_scope}"
        if cache_key in self.bearer_tokens:
            return self.bearer_tokens[cache_key]
        query = {}
        if params.get("service"):
            query["service"] = params["service"]
        if token_scope:
            query["scope"] = token_scope
        try:
            response = self.session.get(
                realm,
                params=query,
                auth=self.basic_auth,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise RegistryV2Error(f"authentification registre impossible : {exc}") from exc
        if response.status_code != 200:
            raise RegistryV2Error(f"authentification registre refusee (HTTP {response.status_code})")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RegistryV2Error("reponse d'authentification registre illisible") from exc
        token = str(payload.get("token") or payload.get("access_token") or "").strip()
        if not token:
            raise RegistryV2Error("jeton d'authentification registre absent")
        self.bearer_tokens[cache_key] = token
        if token_scope:
            self.scope_tokens[token_scope] = token
        return token

    def request(self, method: str, url: str, *, scope: str = "", **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        cached_token = self.scope_tokens.get(scope, "")
        if cached_token:
            headers["Authorization"] = f"Bearer {cached_token}"
        auth = None if cached_token else self.basic_auth
        body = kwargs.get("data")
        body_position = None
        if hasattr(body, "tell"):
            try:
                body_position = body.tell()
            except Exception:
                body_position = None
        try:
            response = self.session.request(
                method=method,
                url=url,
                headers=headers,
                auth=auth,
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise RegistryV2Error(f"registre injoignable : {exc}") from exc

        if response.status_code != 401:
            return response

        challenge = response.headers.get("WWW-Authenticate", "")
        if not challenge.lower().startswith("bearer "):
            return response
        token = self._bearer_token(challenge, scope)
        retry_headers = dict(headers)
        retry_headers["Authorization"] = f"Bearer {token}"
        if body_position is not None and hasattr(body, "seek"):
            try:
                body.seek(body_position)
            except Exception as exc:
                raise RegistryV2Error("flux d'upload impossible a rejouer apres authentification") from exc
        try:
            return self.session.request(
                method=method,
                url=url,
                headers=retry_headers,
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise RegistryV2Error(f"registre injoignable apres authentification : {exc}") from exc

    def repo_request(self, method: str, endpoint: str, **kwargs):
        scope = f"repository:{self.repo}:pull,push"
        return self.request(method, self._url(f"{quote(self.repo, safe='/')}/{endpoint}"), scope=scope, **kwargs)

    def ping(self) -> None:
        response = self.request("GET", self._url(""))
        if response.status_code != 200:
            raise RegistryV2Error(f"API Registry V2 indisponible (HTTP {response.status_code})")

    def get_manifest(self, reference: Optional[str] = None) -> Tuple[bool, str, bytes, str]:
        ref = reference or self.reference
        headers = {"Accept": REGISTRY_V2_MANIFEST_ACCEPT}
        response = self.repo_request("GET", f"manifests/{quote(ref, safe=':._-')}", headers=headers)
        if response.status_code == 404:
            return False, "", b"", ""
        if response.status_code != 200:
            raise RegistryV2Error(f"lecture manifest refusee (HTTP {response.status_code})")
        raw = response.content
        digest = response.headers.get("Docker-Content-Digest", "").strip() or registry_v2_digest(raw)
        media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
        return True, digest, raw, media_type

    def blob_exists(self, digest: str) -> bool:
        response = self.repo_request("HEAD", f"blobs/{quote(digest, safe=':')}")
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise RegistryV2Error(f"verification blob {digest[:23]} refusee (HTTP {response.status_code})")

    def manifest_exists(self, digest: str) -> bool:
        exists, remote_digest, _raw, _media_type = self.get_manifest(digest)
        return bool(exists and remote_digest == digest)

    def upload_blob(self, tar: tarfile.TarFile, descriptor: dict, stats: Dict[str, int]) -> None:
        digest = str((descriptor or {}).get("digest") or "").strip()
        if not digest.startswith("sha256:"):
            raise RegistryV2Error(f"digest OCI non pris en charge : {digest or 'vide'}")
        member_name = _oci_blob_member_name(digest)
        try:
            member = tar.getmember(member_name)
        except KeyError as exc:
            raise RegistryV2Error(f"blob absent du TAR : {digest}") from exc
        expected_size = int((descriptor or {}).get("size") or member.size)
        if member.size != expected_size:
            raise RegistryV2Error(f"taille blob incorrecte : {digest}")

        stats["blobs_total"] += 1
        if self.blob_exists(digest):
            stats["blobs_reused"] += 1
            return

        extracted = tar.extractfile(member)
        if extracted is None:
            raise RegistryV2Error(f"blob illisible dans le TAR : {digest}")
        local_hash = hashlib.sha256()
        for chunk in iter(lambda: extracted.read(1024 * 1024), b""):
            local_hash.update(chunk)
        if "sha256:" + local_hash.hexdigest() != digest:
            raise RegistryV2Error(f"SHA-256 blob incorrect : {digest}")

        start = self.repo_request("POST", "blobs/uploads/")
        if start.status_code != 202:
            raise RegistryV2Error(f"ouverture upload blob refusee (HTTP {start.status_code})")
        location = start.headers.get("Location", "").strip()
        if not location:
            raise RegistryV2Error("URL d'upload blob absente")
        upload_url = urljoin(self.base_url + "/", location)

        extracted = tar.extractfile(member)
        if extracted is None:
            raise RegistryV2Error(f"blob illisible dans le TAR : {digest}")
        response = self.request(
            "PUT",
            upload_url,
            scope=f"repository:{self.repo}:pull,push",
            params={"digest": digest},
            data=extracted,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(member.size),
            },
        )
        if response.status_code not in {201, 202}:
            raise RegistryV2Error(f"envoi blob refuse (HTTP {response.status_code}) : {digest}")
        returned_digest = response.headers.get("Docker-Content-Digest", "").strip()
        if returned_digest and returned_digest != digest:
            raise RegistryV2Error(f"digest retourne incorrect pour le blob : {digest}")
        stats["blobs_uploaded"] += 1
        stats["bytes_uploaded"] += member.size

    def put_manifest(self, reference: str, raw: bytes, media_type: str, expected_digest: str = "") -> str:
        digest = registry_v2_digest(raw)
        if expected_digest and digest != expected_digest:
            raise RegistryV2Error(f"SHA-256 manifest incorrect : attendu {expected_digest}, obtenu {digest}")
        response = self.repo_request(
            "PUT",
            f"manifests/{quote(reference, safe=':._-')}",
            data=raw,
            headers={"Content-Type": media_type, "Content-Length": str(len(raw))},
        )
        if response.status_code not in {201, 202}:
            detail = ""
            try:
                detail = str((response.json() or {}).get("errors") or "")
            except ValueError:
                detail = ""
            suffix = f" : {detail[:300]}" if detail else ""
            raise RegistryV2Error(f"publication manifest refusee (HTTP {response.status_code}){suffix}")
        returned_digest = response.headers.get("Docker-Content-Digest", "").strip()
        if returned_digest and returned_digest != digest:
            raise RegistryV2Error(f"digest retourne incorrect pour le manifest : {digest}")
        return digest


def registry_v2_read_oci_blob(tar: tarfile.TarFile, descriptor: dict) -> Tuple[bytes, dict, str]:
    digest = str((descriptor or {}).get("digest") or "").strip()
    if not digest.startswith("sha256:"):
        raise RegistryV2Error(f"digest manifest OCI invalide : {digest or 'vide'}")
    member_name = _oci_blob_member_name(digest)
    try:
        member = tar.getmember(member_name)
    except KeyError as exc:
        raise RegistryV2Error(f"manifest absent du TAR : {digest}") from exc
    extracted = tar.extractfile(member)
    if extracted is None:
        raise RegistryV2Error(f"manifest illisible dans le TAR : {digest}")
    raw = extracted.read()
    expected_size = int((descriptor or {}).get("size") or len(raw))
    if len(raw) != expected_size:
        raise RegistryV2Error(f"taille manifest incorrecte : {digest}")
    if registry_v2_digest(raw) != digest:
        raise RegistryV2Error(f"SHA-256 manifest incorrect : {digest}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RegistryV2Error(f"JSON manifest OCI illisible : {digest}") from exc
    if not isinstance(payload, dict):
        raise RegistryV2Error(f"JSON manifest OCI invalide : {digest}")
    media_type = str((descriptor or {}).get("mediaType") or payload.get("mediaType") or "").strip()
    if not media_type:
        media_type = "application/vnd.oci.image.manifest.v1+json"
    return raw, payload, media_type


def registry_v2_publish_descriptor(
    client: RegistryV2Client,
    tar: tarfile.TarFile,
    descriptor: dict,
    stats: Dict[str, int],
    published: set,
) -> None:
    digest = str((descriptor or {}).get("digest") or "").strip()
    if digest in published:
        return
    raw, payload, media_type = registry_v2_read_oci_blob(tar, descriptor)

    registry_v2_publish_dependencies(client, tar, payload, media_type, stats, published)

    if client.manifest_exists(digest):
        stats["manifests_reused"] += 1
    else:
        client.put_manifest(digest, raw, media_type, digest)
        stats["manifests_uploaded"] += 1
    published.add(digest)


def registry_v2_publish_dependencies(
    client: RegistryV2Client,
    tar: tarfile.TarFile,
    payload: dict,
    media_type: str,
    stats: Dict[str, int],
    published: set,
) -> None:
    """Publie ce qu'un manifest reference, sans publier le manifest lui-meme."""

    nested = payload.get("manifests", []) if isinstance(payload.get("manifests"), list) else []
    if nested or media_type in REGISTRY_V2_INDEX_MEDIA_TYPES:
        for child in nested:
            if isinstance(child, dict):
                registry_v2_publish_descriptor(client, tar, child, stats, published)
    else:
        config = payload.get("config")
        if isinstance(config, dict) and config.get("digest"):
            client.upload_blob(tar, config, stats)
        for key in ("layers", "blobs"):
            for blob in payload.get(key, []) or []:
                if isinstance(blob, dict) and blob.get("digest"):
                    client.upload_blob(tar, blob, stats)


def registry_v2_import_oci(conf: Dict[str, str], name: str, target: str, tar_path: str) -> Dict[str, object]:
    client = RegistryV2Client(conf, name, target)
    client.ping()
    stats: Dict[str, object] = {
        "target": target,
        "mode": client.mode,
        "host": client.target["host"],
        "repo": client.repo,
        "reference": client.reference,
        "blobs_total": 0,
        "blobs_uploaded": 0,
        "blobs_reused": 0,
        "manifests_uploaded": 0,
        "manifests_reused": 0,
        "bytes_uploaded": 0,
        "already_current": False,
    }

    with tarfile.open(tar_path, "r") as tar:
        try:
            member = tar.getmember("index.json")
            extracted = tar.extractfile(member)
        except KeyError as exc:
            raise RegistryV2Error("index.json absent : le TAR n'est pas une archive OCI") from exc
        if extracted is None:
            raise RegistryV2Error("index.json OCI illisible")
        index_raw = extracted.read()
        try:
            index = json.loads(index_raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise RegistryV2Error("index.json OCI invalide") from exc
        if not isinstance(index, dict) or int(index.get("schemaVersion") or 0) != 2:
            raise RegistryV2Error("index.json OCI non pris en charge")

        layout_digest = registry_v2_digest(index_raw)
        descriptors = index.get("manifests", []) or []
        if not descriptors or not all(isinstance(item, dict) for item in descriptors):
            raise RegistryV2Error("index.json OCI sans manifest valide")

        # index.json est l'index du layout OCI. Quand il contient une seule
        # reference image (cas normal de buildx --output type=oci), le tag du
        # registre doit pointer vers cette reference, pas vers une enveloppe
        # d'index supplémentaire. En multi-arch, cette reference peut elle-meme
        # etre un index : la recursion ci-dessous le gere.
        named = [
            item for item in descriptors
            if str(((item.get("annotations") or {}).get("org.opencontainers.image.ref.name")) or "").strip()
        ]
        selected = named[0] if len(named) == 1 else (descriptors[0] if len(descriptors) == 1 else None)
        if selected is not None:
            root_raw, root_payload, root_media_type = registry_v2_read_oci_blob(tar, selected)
            root_digest = str(selected.get("digest") or "").strip()
        else:
            root_raw = index_raw
            root_payload = index
            root_media_type = str(index.get("mediaType") or "application/vnd.oci.image.index.v1+json")
            root_digest = layout_digest

        stats["layout_digest"] = layout_digest
        stats["local_digest"] = root_digest
        exists, remote_digest, _raw, _media_type = client.get_manifest()
        stats["remote_digest_before"] = remote_digest
        if exists and remote_digest == root_digest:
            stats["already_current"] = True
            stats["remote_digest_after"] = remote_digest
            return stats

        published: set = set()
        registry_v2_publish_dependencies(
            client, tar, root_payload, root_media_type, stats, published
        )
        client.put_manifest(client.reference, root_raw, root_media_type, root_digest)
        stats["manifests_uploaded"] += 1

    exists, remote_digest, _raw, _media_type = client.get_manifest()
    if not exists or remote_digest != stats["local_digest"]:
        raise RegistryV2Error("verification finale du digest registre echouee")
    stats["remote_digest_after"] = remote_digest
    return stats


def registry_v2_manifest_status(conf: Dict[str, str], name: str, target: str) -> Tuple[bool, str, dict, str]:
    """Retourne l'etat reel du tag configure, sans cache local."""
    try:
        client = RegistryV2Client(conf, name, target)
        client.ping()
        exists, digest, raw, _media_type = client.get_manifest()
        if not exists:
            return False, "", {}, "tag absent du registre"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            payload = {}
        return True, digest, payload if isinstance(payload, dict) else {}, ""
    except Exception as exc:
        return False, "", {}, str(exc)
